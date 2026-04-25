"""Trading state machine — continuously scans ALL eligible pairs for signals,
enters a martingale cycle on the first pair to fire, stays locked to it until WIN.

Two operating modes:
  FREE  — no active cycle. On every new closed bar, run CONSENSUS on every
          tracked pair. Enter on first valid signal with payout >= min_payout.
  CYCLE — locked to a pair until WIN. Apply martingale 2.1× after each LOSS,
          same direction as the originating signal. Payout drop below 85%
          allows one-time switch to another eligible pair (keep MG step).

Recovery semantics (per spec): mid-cycle crash → restart from base amount,
fresh cycle, preserve journal.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from strategy.consensus import generate_signals as _consensus_generate_signals, DEFAULT_PARAMS
from strategy.filter_1000 import scan_all_pairs, pick_best, PairScore
from feed.history import fetch_candles
from trading.ws_client import TradeClient, ClosedTrade, OpenedTrade
# Legacy Chrome-based helpers; only used when feed has _page (po-signals path)
try:
    from trading.window_manager import autoset_windows, ensure_pair_in_window
except Exception:
    autoset_windows = None
    ensure_pair_in_window = None
from journal.db import Journal

logger = logging.getLogger(__name__)


@dataclass
class RuntimeState:
    current_pair: Optional[str] = None       # pair locked during MG cycle (None in FREE mode)
    original_pair: Optional[str] = None      # pair where current cycle started
    direction: Optional[str] = None          # "call" | "put" — frozen for the whole cycle
    trades_on_pair: int = 0
    cycle_switches: int = 0
    switched_pairs: list[str] = field(default_factory=list)
    session_loss: float = 0.0
    paused: bool = False
    waiting_resume: bool = False
    day_off_until: int = 0
    mg_step: int = 0
    # Set to True when bot auto-pauses at end of working hours after closing
    # a cycle. On reaching start_hour next day, auto-resume (manual /pause
    # leaves this False so manual pause stays until manual /resume).
    auto_paused_schedule: bool = False
    # Trade in-flight at the moment of crash/restart. Resolved on startup.
    pending_trade: Optional[dict] = None
    # ↑ {asset, action, amount, pre_balance, open_ts, expiry_sec}

    def to_dict(self): return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RuntimeState":
        """Preserve all fields across restart — bot resumes the cycle exactly
        where it left off (mg_step, current_pair, direction). pending_trade
        is recovered separately by _resume_pending_trade()."""
        if not d: return cls()
        s = cls()
        for k, v in d.items():
            if hasattr(s, k): setattr(s, k, v)
        return s


class StateMachine:
    def __init__(self, cfg: dict, feed, trade_client: TradeClient, journal: Journal,
                 notify=None, send_chart=None):
        self.cfg = cfg
        self.feed = feed
        self.tc = trade_client
        self.journal = journal
        self.notify = notify or (lambda msg: None)
        self.send_chart = send_chart  # async(png_path, caption) → sends photo to Telegram

        saved = journal.get("runtime_state")
        self.state = RuntimeState.from_dict(saved) if saved else RuntimeState()
        logger.info("restored state: %s", self.state)

        # Candle caches & bar-close tracking
        self._candles: dict[str, list[dict]] = {}
        self._last_closed_bar_time: dict[str, int] = {}
        self._last_refresh: dict[str, float] = {}   # ts of last REST fetch per sym
        self._pair_scores: dict[str, PairScore] = {}
        self._tracked: set[str] = set()

        self._running = False
        self._tick_event = asyncio.Event()
        self._force_rescan = False   # set True to trigger _rescan_pairs next tick

        # Strategy registry — replaceable at runtime via Mini App
        self.registry = None   # set externally by main.py if available

        feed.on_tick = self._on_tick
        feed.on_assets_update = self._on_assets_update
        feed.on_trade_close = self._on_trade_close_event
        # Latest closed deals from PO keyed by "asset:open_ts" → {profit, ...}
        self._closed_deals_index: dict[str, dict] = {}

    # ---------- persist ----------
    def _persist(self):
        self.journal.set("runtime_state", self.state.to_dict())

    # ---------- working hours ----------
    def _within_working_hours(self) -> bool:
        """Returns True if NOW is within the configured trading window. If
        schedule.enabled is False, always returns True."""
        sched = self.cfg.get("schedule") or {}
        if not sched.get("enabled"):
            return True
        try:
            import datetime as _dt
            tz_name = (self.cfg.get("telegram") or {}).get("daily_report_timezone") or "Europe/Kyiv"
            try:
                import pytz
                tz = pytz.timezone(tz_name)
                now = _dt.datetime.now(tz)
            except Exception:
                now = _dt.datetime.now()
            start = int(sched.get("start_hour", 0))
            end = int(sched.get("end_hour", 24))
            h = now.hour
            if start <= end:
                return start <= h < end
            else:
                # window crosses midnight (e.g. 22..6)
                return h >= start or h < end
        except Exception:
            logger.exception("working-hours check failed, defaulting to True")
            return True

    # ---------- analytics: hourly backtest snapshot per pair ----------
    async def _pair_stats_logger_loop(self):
        """Run consensus.analyze on every tracked pair's buffer once per hour
        and persist the result to journal.pair_stats_log. Two months of these
        accumulate into a useful per-pair performance history (~720 hourly
        snapshots × ~30 pairs = manageable size)."""
        # First snapshot ~10 min after start so buffers have time to fill.
        await asyncio.sleep(600)
        from strategy.consensus import analyze as _analyze, DEFAULT_PARAMS as _DP
        params = {**_DP, **(self.cfg.get("indicator") or {})}
        while self._running:
            try:
                pairs = list(self._tracked) or list(self._candles.keys())
                wrote = 0
                for sym in pairs:
                    buf = self._candles.get(sym) or []
                    if len(buf) < 300:
                        continue
                    try:
                        a = _analyze(buf[:-1], params)
                    except Exception:
                        logger.exception("analyze failed for %s", sym)
                        continue
                    snap = {
                        "bars": len(buf) - 1,
                        "signals_total": int(a.completed),
                        "wins": int(a.wins),
                        "losses": int(a.losses),
                        "wr": float(a.wr),
                        "wr1": float(a.wr1),
                        "max_streak": int(a.max_loss_streak_before_win),
                        "max_streak_overall": int(a.max_loss_streak_overall),
                    }
                    try:
                        self.journal.log_pair_stats(sym, snap)
                        wrote += 1
                    except Exception:
                        logger.exception("log_pair_stats failed for %s", sym)
                if wrote:
                    logger.info("pair_stats logger: wrote %d snapshots", wrote)
            except Exception:
                logger.exception("pair_stats logger crashed (will retry next hour)")
            await asyncio.sleep(3600)

    # ---------- control ----------
    async def stop(self):
        self._running = False
        logger.info("stopping state machine")

    def pause(self):
        self.state.paused = True; self._persist()
        logger.info("PAUSED")

    def resume(self):
        self.state.paused = False; self._persist()
        logger.info("RESUMED")

    def resume_after_stop_sum(self):
        self.state.waiting_resume = False
        self.state.paused = False
        self.state.session_loss = 0.0
        self.state.mg_step = 0
        self.state.current_pair = None
        self.state.original_pair = None
        self.state.direction = None
        self.state.trades_on_pair = 0
        self.state.cycle_switches = 0
        self.state.switched_pairs = []
        self._persist()
        logger.info("RESUMED after stop-sum — fresh cycle")

    def _amount_for_step(self, step: int) -> float:
        base = float(self.cfg["trading"]["base_amount"])
        coef = float(self.cfg["martingale"]["coefficient"])
        return round(base * (coef ** step), 2)

    # ---------- feed callbacks ----------
    def _on_trade_close_event(self, payload):
        """PO sends `updateClosedDeals` (list) or `successcloseOrder` (dict).
        Index by asset + openTimestamp so _open_and_track can look up profit."""
        deals = payload if isinstance(payload, list) else [payload] if isinstance(payload, dict) else []
        for d in deals:
            if not isinstance(d, dict):
                continue
            asset = d.get("asset") or d.get("symbol")
            open_ts = d.get("openTimestamp") or d.get("open_timestamp") or 0
            if not asset or not open_ts:
                continue
            self._closed_deals_index[f"{asset}:{int(open_ts)}"] = d

    def _on_assets_update(self, assets: dict):
        """When assets arrive late (after startup scan ran on empty list),
        wake the main loop so it can rescan and start tracking pairs."""
        if not self._tracked and assets:
            logger.info("assets arrived late (%d) — forcing rescan", len(assets))
            self._force_rescan = True
            # Also clear any day-off that was set because the initial scan found 0 pairs.
            if self.state.day_off_until:
                self.state.day_off_until = 0
                self._persist()
                logger.info("cleared day-off (was set due to empty initial assets)")
            self._tick_event.set()

    def _on_tick(self, symbol: str, tf: int, candle: dict):
        # Wake main loop; closed-bar detection happens in loop.
        self._tick_event.set()
        # Cheap heartbeat: count ticks per symbol for debug
        c = getattr(self, "_tick_counts", None)
        if c is None:
            c = {}
            self._tick_counts = c
        c[symbol] = c.get(symbol, 0) + 1

        # Mirror live tick into state_machine._candles so scan sees fresh data
        # without waiting for REST. Only for tracked pairs (avoid bloat for pairs
        # we don't care about).
        if symbol not in self._tracked:
            return
        if int(tf) != int(self.cfg["filter"]["tf"]):
            return
        buf = self._candles.get(symbol)
        if not buf:
            return  # wait until REST primes history first
        t = int(candle["time"])
        last_t = int(buf[-1]["time"])
        if t == last_t:
            # update forming candle
            buf[-1]["high"] = max(buf[-1]["high"], candle["high"])
            buf[-1]["low"]  = min(buf[-1]["low"],  candle["low"])
            buf[-1]["close"] = candle["close"]
            buf[-1]["volume"] = candle.get("volume", buf[-1].get("volume", 0))
        elif t > last_t:
            # new bar opened → the previous bar just closed
            buf.append({
                "time": t,
                "open": candle["open"], "high": candle["high"],
                "low": candle["low"],  "close": candle["close"],
                "volume": candle.get("volume", 0),
            })
            # Trim to configured history size to bound memory
            limit = self.cfg["filter"]["history_candles"]
            if len(buf) > limit:
                del buf[:len(buf) - limit]
            # Mark refresh so REST backoff can skip this pair
            self._last_refresh[symbol] = time.time()

    async def _notify(self, msg: str):
        try:
            res = self.notify(msg)
            if asyncio.iscoroutine(res):
                await res
        except Exception:
            logger.exception("notify failed")

    # ---------- tracking / candles ----------
    async def _load_history(self, symbol: str):
        """Initial REST fetch. No WS subscription here — ticker2 supports only 1 pair at a time."""
        tf = self.cfg["filter"]["tf"]
        limit = self.cfg["filter"]["history_candles"]
        hist = await fetch_candles(self.feed, symbol, period=tf, limit=limit)
        if len(hist) >= 200:
            self._candles[symbol] = hist[-limit:]
            # Mark LAST closed bar as already-evaluated so the first scan does
            # NOT fire a stale signal on a bar that closed before bot startup.
            # Entry will happen on the NEXT close (within 1-2 sec of bar close).
            self._last_closed_bar_time[symbol] = hist[-2]["time"] if len(hist) >= 2 else 0
            self._last_refresh[symbol] = time.time()
            logger.info("cached %s (%d candles via REST)", symbol, len(hist))

    async def _refresh_one(self, symbol: str):
        tf = self.cfg["filter"]["tf"]
        limit = self.cfg["filter"]["history_candles"]
        try:
            hist = await fetch_candles(self.feed, symbol, period=tf, limit=limit)
            if len(hist) >= 200:
                self._candles[symbol] = hist[-limit:]
                self._last_refresh[symbol] = time.time()
        except Exception as e:
            logger.debug("refresh %s failed: %s", symbol, e)

    async def _maybe_refresh_all(self, min_interval_sec: int = 60):
        now = time.time()
        stale = [s for s in self._tracked
                 if now - self._last_refresh.get(s, 0) >= min_interval_sec]
        if not stale:
            return
        sem = asyncio.Semaphore(5)
        async def job(sym):
            async with sem:
                await self._refresh_one(sym)
        await asyncio.gather(*[job(s) for s in stale], return_exceptions=True)
        logger.info("REST refresh: %d pairs updated", len(stale))

    def _just_closed_new_bar(self, symbol: str) -> bool:
        """Returns True if a new bar closed since last check."""
        buf = self._candles.get(symbol)
        if not buf or len(buf) < 2:
            return False
        last_closed = buf[-2]["time"]
        prev = self._last_closed_bar_time.get(symbol, 0)
        if last_closed > prev:
            self._last_closed_bar_time[symbol] = last_closed
            return True
        return False

    def _check_signal(self, symbol: str) -> Optional[str]:
        buf = self._candles.get(symbol)
        if not buf or len(buf) < 200:
            return None
        closed = buf[:-1]
        # Buffer-density guard: indicators are meaningless if the recent window
        # has gaps (RSI/QQE/HTF EMA stitch unrelated bars together → fantom
        # signals). Background buffer-keeper in feed normally fills these
        # within ~60s; skip this scan only.
        period = int(self.cfg.get("filter", {}).get("tf", 60))
        recent = closed[-100:]
        if len(recent) >= 2:
            first_t = int(recent[0]["time"])
            last_t = int(recent[-1]["time"])
            expected = max(1, (last_t - first_t) // period + 1)
            density = len(recent) / expected
            if density < 0.95:
                logger.warning("skip %s — buffer density %.0f%% over last %d bars (gap detected)",
                               symbol, density * 100, len(recent))
                return None
        # Use active strategy's own params (per-strategy storage), falling back
        # to global cfg["indicator"] if registry unavailable. Time-budget the
        # strategy call so a buggy user plugin can't hang the whole loop.
        if self.registry:
            try:
                strat = self.registry.get_active()
                params = strat.merged_params()
                if strat.source == "user":
                    # Hard cap user code at 2 sec per pair scan
                    import signal as _sig
                    # Note: signal.alarm only works in main thread on Unix.
                    # In asyncio we rely on it being short-running synchronous code.
                    # The plugin runs synchronously; we time the wallclock and warn.
                    t0 = time.time()
                    sigs, _ = strat.generate_signals(closed, params)
                    dt = time.time() - t0
                    if dt > 2.0:
                        logger.warning("strategy %s slow: %.2fs on %s — consider optimizing",
                                       strat.name, dt, symbol)
                else:
                    sigs, _ = strat.generate_signals(closed, params)
            except Exception:
                logger.exception("active strategy failed, fallback to consensus")
                params = {**DEFAULT_PARAMS, **self.cfg["indicator"]}
                sigs, _ = _consensus_generate_signals(closed, params)
        else:
            params = {**DEFAULT_PARAMS, **self.cfg["indicator"]}
            sigs, _ = _consensus_generate_signals(closed, params)
        if not sigs:
            return None
        last = sigs[-1]
        if last.i != len(closed) - 1:
            return None
        return "call" if last.side == "buy" else "put"

    # ---------- scan & subscribe ----------
    async def _rescan_pairs(self):
        scores = await scan_all_pairs(self.feed, self.cfg)
        self._pair_scores = scores
        # Apply bans silently (only log locally, no Telegram spam; summary in daily report)
        new_bans = 0
        for sym, s in scores.items():
            if s.ban and not self.journal.is_banned(sym):
                self.journal.ban(sym, self.cfg["filter"]["ban_hours"], s.reason)
                logger.info("BAN %s — %s", sym, s.reason)
                new_bans += 1
        if new_bans:
            logger.info("applied %d new bans this scan", new_bans)

        # Build tracked set = currently allowed + not banned + payout>=min_payout
        min_payout = self.cfg["filter"]["min_payout"]
        wanted = {sym for sym, s in scores.items()
                  if s.allowed and not self.journal.is_banned(sym)
                  and int(self.feed.assets.get(sym, {}).get("payout", 0)) >= min_payout}

        # Drop pairs we no longer track — unsubscribe from WS if supported.
        to_drop = self._tracked - wanted
        if hasattr(self.feed, "unsubscribe"):
            tf = int(self.cfg["filter"]["tf"])
            for sym in to_drop:
                try: await self.feed.unsubscribe(sym, tf)
                except Exception: pass
        self._tracked -= to_drop

        # Subscribe new pairs (bounded concurrency)
        to_add = wanted - self._tracked
        sem = asyncio.Semaphore(5)
        async def add(sym):
            async with sem:
                await self._load_history(sym)
                self._tracked.add(sym)
        await asyncio.gather(*[add(s) for s in to_add])
        logger.info("tracked pairs: %d", len(self._tracked))

    def _eligible_for_new_cycle(self, symbol: str) -> bool:
        if self.journal.is_banned(symbol): return False
        sc = self._pair_scores.get(symbol)
        if not sc or not sc.allowed: return False
        payout = int(self.feed.assets.get(symbol, {}).get("payout", 0))
        if payout < self.cfg["filter"]["min_payout"]: return False
        return True

    def _pick_switch_pair(self, exclude: set[str]) -> Optional[PairScore]:
        pool = {sym: sc for sym, sc in self._pair_scores.items()
                if sc.allowed and sym not in exclude and not self.journal.is_banned(sym)
                and int(self.feed.assets.get(sym, {}).get("payout", 0)) >= self.cfg["filter"]["min_payout"]}
        return pick_best(pool, exclude)

    # ---------- main loop ----------
    async def _resume_pending_trade(self):
        """If state has a pending_trade (bot was restarted mid-trade), wait
        for the trade to expire and classify the result via balance delta.
        Then update mg_step / cycle as normal."""
        pt = self.state.pending_trade
        if not pt:
            return
        try:
            sym = pt["asset"]; action = pt["action"]; amount = float(pt["amount"])
            pre_balance = float(pt["pre_balance"])
            open_ts = int(pt["open_ts"]); expiry = int(pt["expiry_sec"])
            close_ts = open_ts + expiry
            now = int(time.time())
            elapsed = now - open_ts
            # Staleness gate: if trade is way too old (more than 2× expiry past
            # close), the pre_balance is meaningless because dozens of other
            # events may have shifted the balance. Discard rather than guess.
            staleness_limit = expiry * 3
            if now - close_ts > staleness_limit:
                logger.warning("resume: pending_trade %s too stale (%ds since close) — discarding",
                               sym, now - close_ts)
                await self._notify(
                    f"⚠️ Незакрытая сделка {sym} слишком старая ({(now-close_ts)//60} мин). "
                    f"Пропускаю восстановление, начинаю с FREE."
                )
                self.state.pending_trade = None
                self._reset_cycle()
                self._persist()
                return
            await self._notify(
                f"🔄 Восстановление сделки {sym} {action.upper()} ${amount} "
                f"(прошло {elapsed}s из {expiry}s). Жду результат…"
            )
            # If trade not yet expired, wait the rest
            if now < close_ts + 5:
                wait = (close_ts + 5) - now
                logger.info("resume: waiting %ds for trade %s to close", wait, sym)
                await asyncio.sleep(max(1, wait))
            # Now check balance delta
            post_balance = float(self.feed.balance() or 0.0)
            for _ in range(15):
                if abs(post_balance - pre_balance) >= 0.01:
                    break
                await asyncio.sleep(1.0)
                post_balance = float(self.feed.balance() or 0.0)
            delta = round(post_balance - pre_balance, 2)
            if delta > 0.005:
                result = "WIN"; profit = delta + amount
            elif delta < -0.005:
                result = "LOSS"; profit = 0.0
            else:
                result = "DRAW"; profit = amount
            logger.info("resume CLOSE %s: delta=%s → %s", sym, delta, result)
            # Synthesize closed trade and apply it
            opened = OpenedTrade(
                trade_id=pt.get("trade_id", "resumed"),
                asset=sym, action=action, amount=amount,
                payout=int((self.feed.assets.get(sym) or {}).get("payout", 0)),
                open_time=open_ts, expiry_sec=expiry,
            )
            closed = ClosedTrade(
                trade_id=opened.trade_id, asset=sym, action=action,
                amount=amount, profit=profit, result=result,
                close_time=close_ts,
                raw={"resumed": True, "pre_balance": pre_balance,
                     "post_balance": post_balance, "delta": delta},
            )
            await self._on_trade_closed(opened, closed)
        except Exception:
            logger.exception("_resume_pending_trade failed")
            # Clear pending so we don't loop on a broken record
            self.state.pending_trade = None
            self._persist()

    async def run(self):
        self._running = True
        await self._notify(f"🤖 Бот запущен ({self.cfg['mode']})")
        # Background analytics — pair backtest snapshots once an hour.
        asyncio.create_task(self._pair_stats_logger_loop(), name="pair_stats_logger")
        # Resume any in-flight trade interrupted by the previous restart
        await self._resume_pending_trade()

        await self._rescan_pairs()
        if not self._tracked:
            # Only trigger day-off if assets were actually available — otherwise
            # it's a startup race (assets haven't arrived yet) and _on_assets_update
            # will force a rescan once they do.
            if self.feed.assets:
                hours = self.cfg["filter"]["day_off_hours"]
                self.state.day_off_until = int(time.time()) + hours * 3600
                self._persist()
                await self._notify(f"😴 Ни одна пара не прошла фильтр. Пауза {hours}ч.")
            else:
                logger.info("initial scan empty — waiting for assets_list from WS")

        last_scan = time.time()
        last_bar_minute = -1   # track minute boundary for bar-aligned refresh
        last_autoset = 0.0     # track auto window assignment

        # Signal check cadence: poll every 1–2s to catch bar close promptly.
        check_interval = max(1, int(self.cfg.get("misc", {}).get("poll_interval_sec", 1)))
        while self._running:
            try:
                await asyncio.wait_for(self._tick_event.wait(), timeout=check_interval)
            except asyncio.TimeoutError:
                pass
            self._tick_event.clear()

            if self.state.paused or self.state.waiting_resume:
                # Auto-resume if pause was triggered by schedule and working
                # hours have started again.
                if (self.state.paused and self.state.auto_paused_schedule
                        and not self.state.waiting_resume
                        and self._within_working_hours()):
                    self.state.paused = False
                    self.state.auto_paused_schedule = False
                    self._persist()
                    await self._notify("☀️ Доброе утро. Рабочее окно открылось — возвращаюсь к торговле.")
                    continue
                continue

            now = time.time()

            # Day-off handling
            if self.state.day_off_until and now < self.state.day_off_until:
                continue
            if self.state.day_off_until and now >= self.state.day_off_until:
                self.state.day_off_until = 0
                self._persist()
                await self._rescan_pairs()
                await self._notify("⏰ Пауза истекла, возобновляю поиск.")
                if not self._tracked:
                    # still no pairs → another day_off
                    self.state.day_off_until = int(now) + self.cfg["filter"]["day_off_hours"] * 3600
                    self._persist()
                    continue

            # Periodic rescan (every 5 min) OR forced (e.g., assets arrived late)
            if self._force_rescan or now - last_scan > 300:
                self._force_rescan = False
                last_scan = now
                await self._rescan_pairs()
                self.journal.prune_bans()

            # Periodic auto-assignment of multi-chart windows — ONLY for legacy
            # po-signals browser path. Direct-PO feed doesn't need any clicks
            # (live ticks stream for every subscribed pair automatically).
            if (autoset_windows is not None
                    and self.state.mg_step == 0
                    and now - last_autoset > 90
                    and getattr(self.feed, "_page", None)):
                last_autoset = now
                try:
                    summary = await autoset_windows(
                        self.feed._page,
                        min_payout=self.cfg["filter"]["min_payout"],
                        max_payout=92,
                    )
                    if summary.get("windows"):
                        logger.info("autoset windows: %s", summary)
                except Exception:
                    logger.exception("autoset_windows failed")

            # Bar-aligned force refresh: right after each minute boundary, pull all pairs
            # so that a freshly closed bar is available within ~1–2s of close.
            tf = self.cfg["filter"]["tf"]
            bar_key = int(now) // tf
            if bar_key != last_bar_minute:
                last_bar_minute = bar_key
                await self._maybe_refresh_all(min_interval_sec=0)
            else:
                # Between boundaries keep a loose refresh (fallback if tick stream lagged)
                await self._maybe_refresh_all(min_interval_sec=15)

            # Branch: in-cycle vs free
            if self.state.mg_step > 0 and self.state.current_pair:
                await self._in_cycle_step()
            else:
                await self._free_scan_step()

    # ---------- free scan (no cycle active) ----------
    async def _free_scan_step(self):
        # Outside working hours: don't ENTER new cycles. (An active MG cycle
        # goes through _in_cycle_step path, not here, so it keeps running.)
        if not self._within_working_hours():
            return
        fired = None
        evaluated = 0
        new_bars = 0
        now_ts = int(time.time())
        tf = self.cfg["filter"]["tf"]
        # Signal is entry-worthy only if we're within ~25s of bar close (entry
        # mode = nextBarOpen → entry price is that bar's open; later = decayed edge).
        MAX_STALENESS = 25
        # Iterate tracked pairs in priority order (lower priority = fewer
        # losses-before-win = better). Tie-break by higher payout.
        sorted_tracked = sorted(
            self._tracked,
            key=lambda s: (
                (self._pair_scores.get(s).priority if self._pair_scores.get(s) else 999),
                -int((self.feed.assets.get(s) or {}).get("payout", 0)),
            ),
        )
        for sym in sorted_tracked:
            if not self._eligible_for_new_cycle(sym):
                continue
            buf = self._candles.get(sym)
            if not buf or len(buf) < 200:
                continue
            evaluated += 1
            last_closed_t = buf[-2]["time"]
            last_eval_t = self._last_closed_bar_time.get(sym, 0)
            if last_closed_t <= last_eval_t:
                continue
            self._last_closed_bar_time[sym] = last_closed_t
            new_bars += 1
            # Freshness: bar must have closed within MAX_STALENESS seconds ago
            age = now_ts - (last_closed_t + tf)   # tf = closed_ts
            if age > MAX_STALENESS:
                logger.info("skip stale bar %s: age=%ds (bar closed %ds ago)", sym, age, age)
                continue
            action = self._check_signal(sym)
            if action:
                fired = (sym, action)
                break
        # Always log scan state so we know the loop is alive
        tick_counts = getattr(self, "_tick_counts", {}) or {}
        total_ticks = sum(tick_counts.values())
        active_syms = sum(1 for v in tick_counts.values() if v > 0)
        logger.info("scan: tracked=%d evaluated=%d new_bars=%d fired=%s | ticks total=%d active_syms=%d",
                    len(self._tracked), evaluated, new_bars, fired, total_ticks, active_syms)

        if not fired:
            return

        sym, action = fired
        payout = int(self.feed.assets.get(sym, {}).get("payout", 0))
        amt = self._amount_for_step(0)

        # CRITICAL: open trade FIRST. TG notifications/chart go in background —
        # PNG render + upload can take 5-30s, must not delay entry past the
        # current bar (would shift the actual entry vs the signal bar).
        self.state.current_pair = sym
        self.state.original_pair = sym
        self.state.direction = action
        self.state.trades_on_pair = 0
        self.state.mg_step = 0
        self.state.cycle_switches = 0
        self.state.switched_pairs = []
        self._persist()

        # Fire-and-forget notification + chart (don't await, don't block trade)
        async def _notify_bg():
            try:
                await self._notify(
                    f"📡 Сигнал {sym} → {action.upper()} (payout {payout}%). Захожу в сделку ${amt}."
                )
            except Exception:
                logger.exception("notify failed")
            if self.send_chart:
                try:
                    from tg.chart import render_chart
                    params = {**self.cfg["indicator"]}
                    png = render_chart(self._candles[sym], params, sym)
                    await self.send_chart(png, caption=f"📊 {sym} — сигнал {action.upper()}")
                except Exception:
                    logger.exception("send_chart failed")
        asyncio.create_task(_notify_bg(), name=f"notify_{sym}")

        await self._open_and_track(sym, action, amt)

    # ---------- in-cycle step (after a loss) ----------
    async def _in_cycle_step(self):
        sym = self.state.current_pair
        # Check payout drop → possibly switch
        payout = int(self.feed.assets.get(sym, {}).get("payout", 0))
        floor = self.cfg["filter"]["payout_floor"]
        if payout < floor and self.state.cycle_switches < self.cfg["trading"]["max_pair_switch_per_cycle"]:
            exclude = {sym, *self.state.switched_pairs}
            pick = self._pick_switch_pair(exclude)
            if pick:
                self.state.switched_pairs.append(sym)
                self.state.cycle_switches += 1
                self.state.current_pair = pick.symbol
                self._persist()
                await self._notify(
                    f"🔄 Смена пары: → {pick.symbol} (payout {pick.payout}%). "
                    f"Причина: payout {payout}% < {floor}%. МГ-шаг {self.state.mg_step} сохранён."
                )
                sym = pick.symbol

        # Stop-sum guardrail
        next_amt = self._amount_for_step(self.state.mg_step)
        stop_sum = float(self.cfg["martingale"]["stop_sum"])
        if self.state.session_loss + next_amt > stop_sum:
            self.state.waiting_resume = True
            self._persist()
            await self._notify(
                f"🛑 СТОП-СУММА: потери ${self.state.session_loss:.2f} + ставка ${next_amt} > ${stop_sum}.\n"
                f"Жду /resume."
            )
            return
        if self.state.mg_step > int(self.cfg["martingale"]["max_steps"]):
            self.state.waiting_resume = True
            self._persist()
            await self._notify(
                f"🛑 Достигнут максимум догонов ({self.cfg['martingale']['max_steps']}). Жду /resume."
            )
            return

        # WAIT for a fresh CONSENSUS signal on this pair before opening the
        # MG trade. We don't blindly double-down anymore — a new edge must form.
        tf = self.cfg["filter"]["tf"]
        MAX_STALENESS = 25
        buf = self._candles.get(sym)
        if not buf or len(buf) < 200:
            return
        last_closed_t = buf[-2]["time"]
        last_eval_t = self._last_closed_bar_time.get(sym, 0)
        if last_closed_t <= last_eval_t:
            return  # no new bar yet — main loop will tick again soon
        self._last_closed_bar_time[sym] = last_closed_t

        age = int(time.time()) - (last_closed_t + tf)
        if age > MAX_STALENESS:
            logger.info("in-cycle: skip stale bar on %s (age=%ds)", sym, age)
            return

        action = self._check_signal(sym)
        if not action:
            return  # no consensus on this bar — keep waiting

        # Accept signal. If direction flipped vs original, update state.direction
        # so the MG ladder follows the new edge instead of fighting it.
        if action != self.state.direction:
            logger.info("in-cycle: signal direction flipped %s → %s on %s",
                        self.state.direction, action, sym)
            self.state.direction = action
            self._persist()

        # Fire-and-forget notify; never block trade entry on TG latency
        msg = f"📡 Новый сигнал {sym} → {action.upper()}. МГ-шаг {self.state.mg_step}, ставка ${next_amt}."
        asyncio.create_task(self._notify(msg), name=f"notify_mg_{sym}")
        await self._open_and_track(sym, action, next_amt)

    # ---------- trade open / close flow ----------
    async def _open_and_track(self, sym: str, action: str, amount: float):
        expiry = int(self.cfg["trading"]["expiry_seconds"])
        # Clear any stale pending_trade from a prior aborted attempt (otherwise
        # a restart could resume the wrong trade).
        if self.state.pending_trade:
            logger.warning("clearing stale pending_trade before new trade: %s",
                           self.state.pending_trade.get("asset"))
            self.state.pending_trade = None
            self._persist()
        # Direct-PO feed: subscribe to pair so live ticks flow (idempotent).
        # Legacy po-signals path: click into one of 15 windows (via window_manager).
        try:
            if hasattr(self.feed, "subscribe"):
                await self.feed.subscribe(sym, int(self.cfg["filter"]["tf"]))
            elif ensure_pair_in_window and getattr(self.feed, "_page", None):
                await ensure_pair_in_window(self.feed._page, sym)
        except Exception:
            logger.exception("ensure live-tick subscription failed")

        # CRITICAL: capture balance BEFORE sending the frame. The site debits
        # the amount within milliseconds of receiving open_trade, so reading
        # balance after tc.open_trade gives us the already-debited value and
        # turns LOSS (delta = -amount) into DRAW (delta ≈ 0).
        pre_balance = float(self.feed.balance() or 0.0)
        opened = await self.tc.open_trade(sym, amount, action, expiry)
        # Persist pending trade so we can recover its result if the bot is
        # restarted before the close-detection logic finishes.
        if opened:
            self.state.pending_trade = {
                "asset": sym, "action": action, "amount": float(amount),
                "pre_balance": pre_balance,
                "open_ts": int(time.time()),
                "expiry_sec": int(expiry),
                "trade_id": opened.trade_id,
            }
            self._persist()
        if not opened:
            await self._notify(f"⚠️ Не удалось открыть сделку {sym}. Пробую снова на следующем сигнале.")
            # Reset cycle if free mode
            if self.state.mg_step == 0:
                self._reset_cycle()
                self._persist()
            return

        logger.info("OPEN %s %s $%s exp=%ss trade_id=%s", sym, action, amount, expiry, opened.trade_id)

        # Wait for expiry, then check for explicit close event from PO first.
        # Direct-PO feed gets `updateClosedDeals` with a `profit` field — much
        # more reliable than balance delta (which can see partial debits).
        await asyncio.sleep(expiry + 2)

        open_ts = opened.open_time
        deal = None
        # Poll up to 15s for the close event to arrive
        for _ in range(15):
            # Try exact match by open_ts (+/- 2s tolerance)
            for dt in range(-2, 3):
                deal = self._closed_deals_index.get(f"{sym}:{open_ts + dt}")
                if deal:
                    break
            if deal:
                break
            await asyncio.sleep(1.0)

        if deal and "profit" in deal:
            profit_raw = float(deal.get("profit") or 0)
            # PO's `profit` field semantics vary by region:
            #   > 0  → WIN, value is net profit (e.g. +$0.92 on $1 trade)
            #   == amount with percentProfit=0 → DRAW (refund)
            #   < 0 or == -amount → LOSS
            if profit_raw > 0.01:
                result = "WIN"
                profit = profit_raw + amount   # gross return (what user gets back)
            elif profit_raw < -0.01:
                result = "LOSS"
                profit = 0.0
            else:
                result = "DRAW"
                profit = amount
            post_balance = float(self.feed.balance() or 0.0)
            delta = round(post_balance - pre_balance, 2)
            logger.info("CLOSE event %s: profit_raw=%.2f → result=%s (balance delta=%.2f)",
                        sym, profit_raw, result, delta)
        else:
            # Fallback: balance-delta classification (old method)
            post_balance = float(self.feed.balance() or 0.0)
            for _ in range(10):
                if abs(post_balance - pre_balance) >= 0.01:
                    break
                await asyncio.sleep(1.0)
                post_balance = float(self.feed.balance() or 0.0)
            delta = round(post_balance - pre_balance, 2)
            if delta > 0.005:
                result = "WIN"
                profit = delta + amount
            elif delta < -0.005:
                result = "LOSS"
                profit = 0.0
            else:
                result = "DRAW"
                profit = amount
            logger.warning("CLOSE fallback %s: no event — balance delta=%.2f → %s",
                           sym, delta, result)

        closed = ClosedTrade(
            trade_id=opened.trade_id,
            asset=opened.asset,
            action=opened.action,
            amount=opened.amount,
            profit=profit,
            result=result,
            close_time=int(time.time()),
            raw={"pre_balance": pre_balance, "post_balance": post_balance, "delta": delta},
        )
        logger.info("CLOSE %s %s delta=%s result=%s bal=%s",
                    sym, opened.trade_id, delta, result, post_balance)
        await self._on_trade_closed(opened, closed)

    async def _on_trade_closed(self, opened: OpenedTrade, closed: ClosedTrade):
        # Trade resolved — clear the pending-trade marker so a future restart
        # doesn't try to recover this trade again.
        self.state.pending_trade = None
        self.state.trades_on_pair += 1
        self.journal.log_trade({
            "trade_id": closed.trade_id,
            "symbol": opened.asset,
            "action": opened.action,
            "amount": opened.amount,
            "profit": closed.profit,
            "result": closed.result,
            "payout": opened.payout,
            "mg_step": self.state.mg_step,
            "open_ts": opened.open_time or int(time.time()),
            "close_ts": closed.close_time or int(time.time()),
            "balance_after": self.feed.balance(),
            "mode": self.cfg["mode"],
        })

        if closed.result == "WIN":
            gained = closed.profit - opened.amount
            self.state.session_loss = max(0.0, self.state.session_loss - gained)
            self._reset_cycle()
            self._persist()
            # If we just finished a cycle outside working hours — pause and say
            # goodnight. We let the cycle finish even after hours (mandatory),
            # then rest until next start_hour.
            if not self._within_working_hours():
                self.state.paused = True
                self.state.auto_paused_schedule = True
                self._persist()
                await self._notify(
                    f"✅ WIN {opened.asset}  +${gained:.2f}  (баланс ${self.feed.balance()}).\n\n"
                    f"🌙 Закрыл цикл — пора отдыхать. Хороший день был. "
                    f"Жду пока наступит рабочее окно (или /resume чтобы возобновить раньше)."
                )
            else:
                await self._notify(
                    f"✅ WIN {opened.asset}  +${gained:.2f}  (баланс ${self.feed.balance()}). "
                    f"Сбрасываю мартингейл, возвращаюсь в поиск."
                )
        elif closed.result == "LOSS":
            self.state.session_loss += opened.amount
            self.state.mg_step += 1
            self._persist()
            await self._notify(
                f"❌ LOSS {opened.asset}  -${opened.amount:.2f}  "
                f"(шаг → MG{self.state.mg_step}, потери ${self.state.session_loss:.2f})"
            )
            # Loop will immediately trigger _in_cycle_step on next iteration
            self._tick_event.set()
        else:  # DRAW
            self._persist()
            await self._notify(f"➖ DRAW {opened.asset} — повторяю тот же шаг.")
            self._tick_event.set()

    def _reset_cycle(self):
        self.state.current_pair = None
        self.state.original_pair = None
        self.state.direction = None
        self.state.trades_on_pair = 0
        self.state.mg_step = 0
        self.state.cycle_switches = 0
        self.state.switched_pairs = []
