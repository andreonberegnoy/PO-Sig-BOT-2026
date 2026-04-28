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
from datetime import datetime
from typing import Optional

import pytz

from strategy.consensus import generate_signals as _consensus_generate_signals, DEFAULT_PARAMS
from strategy.filter_1000 import scan_all_pairs, pick_best, PairScore, categorize_symbol
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
        self._was_feed_ready = False  # edge-detect for WS-ready transition

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

    # ---------- virtual signals collector (всегда пишет, даже когда LOCKED) ----------
    async def _virtual_signals_loop(self):
        """Independent loop that records every CONSENSUS-fired signal on each
        tracked pair to journal.virtual_signals — regardless of whether the
        bot itself entered a trade. Runs even in LOCKED / paused states.

        Settles signals (computes exp_wins) once 5+ bars have elapsed since
        the entry, using cached candles. Combined with /api/expiry_stats and
        /api/hourly_stats virtual sources, this gives full strategy coverage
        for analytics — not just the few trades the bot actually took.
        """
        import json as _json
        from strategy.consensus import generate_signals as _gen_v, DEFAULT_PARAMS as _DEFV
        last_eval: dict[str, int] = {}
        backfilled: set[str] = set()   # пары, уже прогнанные по истории один раз
        await asyncio.sleep(15)   # let initial scan + tracked-set populate
        while self._running:
            try:
                # Scan for new signals
                tf = int(self.cfg["filter"].get("tf", 60))
                now_ts = int(time.time())
                MAX_STALENESS = 25
                for sym in list(self._tracked):
                    buf = self._candles.get(sym)
                    if not buf or len(buf) < 200:
                        continue
                    # ── ПЕРВАЯ встреча tracked-пары: авто-backfill всей
                    # cached истории. Прогоняем CONSENSUS на ~1060 свечах,
                    # вставляем все исторические сигналы + сразу считаем
                    # exp_wins по следующим 5 барам в том же буфере. Делается
                    # один раз на пару. INSERT OR IGNORE предотвращает дубли
                    # если бот уже видел эти сигналы в предыдущем сеансе.
                    if sym not in backfilled:
                        backfilled.add(sym)
                        try:
                            params = {**_DEFV, **(self.cfg.get("indicator") or {})}
                            f_cfg2 = self.cfg.get("filter") or {}
                            if "stats_lookback_bars" in f_cfg2:
                                params["statsLookbackBars"] = f_cfg2["stats_lookback_bars"]
                            if "recent_lookback_bars" in f_cfg2:
                                params["recentLookbackBars"] = f_cfg2["recent_lookback_bars"]
                            closed_hist = buf[:-1]
                            sigs, _ = _gen_v(closed_hist, params)
                            inserted = 0
                            for s in sigs:
                                idx = int(s.i)
                                if idx < 0 or idx >= len(closed_hist):
                                    continue
                                bar = closed_hist[idx]
                                ent_ts = int(bar["time"]) + tf
                                ent_close = float(bar.get("close") or 0)
                                if ent_close == 0:
                                    continue
                                side = "call" if s.side == "buy" else "put"
                                self.journal.insert_virtual_signal(sym, side, ent_ts, ent_close)
                                inserted += 1
                                # Compute exp_wins from following 5 bars
                                results = []
                                for n in (1, 2, 3, 4, 5):
                                    ei = idx + n
                                    if ei >= len(closed_hist):
                                        results.append(None); continue
                                    ex_close = float(closed_hist[ei].get("close") or 0)
                                    if ex_close == 0:
                                        results.append(None); continue
                                    if side == "call":
                                        results.append(1 if ex_close > ent_close else 0)
                                    else:
                                        results.append(1 if ex_close < ent_close else 0)
                                if not all(r is None for r in results):
                                    cur = self.journal.conn.execute(
                                        "SELECT id, settled_at FROM virtual_signals "
                                        "WHERE symbol=? AND signal_ts=?",
                                        (sym, ent_ts),
                                    ).fetchone()
                                    if cur and cur[1] is None:
                                        self.journal.settle_virtual_signal(
                                            int(cur[0]), _json.dumps(results))
                            if inserted:
                                logger.info(
                                    "virtual backfill %s: %d signals from %d cached candles",
                                    sym, inserted, len(closed_hist),
                                )
                            # Mark last_eval to skip current bar in normal loop
                            last_eval[sym] = int(buf[-2]["time"])
                            continue   # не обрабатывать в этой же итерации
                        except Exception:
                            logger.exception("auto-backfill failed for %s", sym)
                    # Normal incremental processing
                    last_closed_t = int(buf[-2]["time"])
                    if last_closed_t <= last_eval.get(sym, 0):
                        continue
                    last_eval[sym] = last_closed_t
                    age = now_ts - (last_closed_t + tf)
                    if age > MAX_STALENESS:
                        continue
                    # _check_signal is pure (no side-effects on other state)
                    action = self._check_signal(sym)
                    if action:
                        entry_ts = last_closed_t + tf
                        entry_close = float(buf[-2].get("close") or 0)
                        if entry_close > 0:
                            self.journal.insert_virtual_signal(
                                sym, action, entry_ts, entry_close,
                            )
                # Settle older virtual signals (those with 5+ bars after entry)
                older_than = now_ts - 5 * tf - 10
                pending = self.journal.pending_virtual_signals(older_than, limit=50)
                for vs in pending:
                    sym = vs["symbol"]
                    candles = self._candles.get(sym) or []
                    entry_ts = int(vs["signal_ts"])
                    entry_close = float(vs["entry_close"])
                    side = vs["side"]
                    # Find entry candle by time
                    entry_idx = None
                    for i, c in enumerate(candles):
                        if int(c["time"]) == entry_ts:
                            entry_idx = i
                            break
                    if entry_idx is None:
                        # Candle may have been pruned. Mark settled with empty data
                        # so we stop reprocessing forever.
                        self.journal.settle_virtual_signal(int(vs["id"]), _json.dumps([None]*5))
                        continue
                    results = []
                    for n in (1, 2, 3, 4, 5):
                        ei = entry_idx + n
                        if ei >= len(candles):
                            results.append(None)
                            continue
                        exit_close = float(candles[ei].get("close") or 0)
                        if exit_close == 0 or entry_close == 0:
                            results.append(None)
                            continue
                        if side == "call":
                            results.append(1 if exit_close > entry_close else 0)
                        elif side == "put":
                            results.append(1 if exit_close < entry_close else 0)
                        else:
                            results.append(None)
                    self.journal.settle_virtual_signal(int(vs["id"]), _json.dumps(results))
            except Exception:
                logger.exception("virtual_signals_loop iteration failed")
            await asyncio.sleep(2)

    # ---------- exp_wins backtest ----------
    def _persist_exp_wins(self, symbol: str, action: str, open_ts: int, trade_id: str):
        """Compute & save which expiries (1..5 M1 bars) would have closed the
        trade in profit. Persists as JSON `[1,0,1,1,0]` in trades.exp_wins.

        Approximation: entry price = open of bar containing open_ts, exit
        price = close of bar at open_ts + N*60. For call: WIN if exit > entry.
        For put: WIN if exit < entry. None if candle missing.

        Aggregated over time by /api/hourly_stats to show avg min winning
        expiry per (sym, hour) — helps tune trading.expiry_seconds.
        """
        import json as _json
        candles = (self._candles or {}).get(symbol) or []
        if not candles:
            return
        entry_minute = (open_ts // 60) * 60
        # Find entry candle by time (binary search would be nicer; linear is fine)
        entry_idx = None
        for i, c in enumerate(candles):
            if c["time"] == entry_minute:
                entry_idx = i
                break
        if entry_idx is None:
            return
        entry_price = float(candles[entry_idx].get("open", 0) or 0)
        if entry_price == 0:
            return
        results: list = []
        for n in (1, 2, 3, 4, 5):
            exit_idx = entry_idx + n
            if exit_idx >= len(candles):
                results.append(None)
                continue
            exit_price = float(candles[exit_idx].get("close", 0) or 0)
            if exit_price == 0:
                results.append(None)
                continue
            if action == "call":
                results.append(1 if exit_price > entry_price else 0)
            elif action == "put":
                results.append(1 if exit_price < entry_price else 0)
            else:
                results.append(None)
        # Skip persisting if all None (no data) — avoids polluting query agg
        if all(r is None for r in results):
            return
        self.journal.update_exp_wins(trade_id, _json.dumps(results))

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

    def force_reset_cycle(self):
        """Принудительный сброс цикла в FREE-режим.
        МГ-шаг → 0, пара → None, потери сессии сохраняются (деньги потрачены).
        Бот сразу начинает искать новый сигнал на любой паре."""
        old_pair = self.state.current_pair
        old_step = self.state.mg_step
        self._reset_cycle()
        self.state.day_off_until = 0   # снять day-off если он был
        self._persist()
        self._force_rescan = True
        self._tick_event.set()
        logger.info("FORCE RESET CYCLE: was %s MG%d → FREE", old_pair, old_step)

    async def force_switch_pair(self) -> str | None:
        """Принудительная смена пары — переход в SEARCH режим.
        Бот не выбирает конкретную пару, а **сканирует все tracked-пары** и
        войдёт на ту, где CONSENSUS первой даст сигнал. МГ-шаг сохраняется.
        Старая пара уходит в switched_pairs (исключение от повторного входа
        в этом цикле).

        Возвращает 'SEARCH' если режим активирован, или None если нет
        активного цикла / нет tracked-пар."""
        if self.state.mg_step == 0:
            logger.warning("force_switch_pair: no active MG cycle to switch")
            return None
        if not self._tracked:
            logger.warning("force_switch_pair: no tracked pairs to search")
            return None
        old_pair = self.state.current_pair
        if old_pair and old_pair not in (self.state.switched_pairs or []):
            self.state.switched_pairs.append(old_pair)
        self.state.cycle_switches += 1
        self.state.current_pair = None   # ← SEARCH режим
        self.state.trades_on_pair = 0
        self._persist()
        # Сбросить last_closed_bar_time для всех tracked, чтобы _in_cycle_search_step
        # видел все пары как "свежие" и не пропустил недавно закрытые бары
        for sym in list(self._tracked):
            self._last_closed_bar_time.pop(sym, None)
        self._tick_event.set()
        logger.info(
            "FORCE SWITCH PAIR: %s → SEARCH (MG%d saved, sym blacklisted from cycle)",
            old_pair, self.state.mg_step,
        )
        return "SEARCH"
        return pick.symbol

    def _amount_for_step(self, step: int) -> float:
        base = float(self.cfg["trading"]["base_amount"])
        # Martingale toggle: when disabled, every trade uses base_amount
        # regardless of step. Lets user run "flat" strategy without doubling
        # after losses.
        if not self.cfg["martingale"].get("enabled", True):
            return base
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
        # Guard: if WS feed isn't ready (mid-reconnect / mid-relogin), skip the
        # scan instead of wasting it on 0-candle results. Without this, the
        # hourly tick can fire during a relogin window and produce
        # "0 allowed, 0 banned, 0 total" because every fetch_candles returns
        # empty — leaving tracked={} for the next 5 minutes.
        feed_ready = getattr(self.feed, "_ready", None)
        if feed_ready is not None and not feed_ready.is_set():
            logger.info("rescan: skipped — feed not ready, will retry in 60s")
            self._force_rescan = True
            await asyncio.sleep(60)
            return

        scores = await scan_all_pairs(self.feed, self.cfg)

        # Detect total scan failure (WS dropped mid-iteration → all 0 candles).
        # Don't overwrite previous _pair_scores; retry sooner than 5 min default.
        if not scores:
            logger.warning("rescan: 0 pairs returned (WS hiccup?), retry in 60s")
            self._force_rescan = True
            await asyncio.sleep(60)
            return

        self._pair_scores = scores
        # Apply bans + pauses silently. Pause uses same bans table but with
        # shorter expiry (filter.pause_hours) — reused journal.is_banned() check
        # naturally hides paused pairs from trading until they expire. After
        # expiry the pair is auto-rechecked on next _rescan_pairs (hourly).
        new_bans = 0
        new_pauses = 0
        ban_hours = int(self.cfg["filter"].get("ban_hours", 12))
        pause_hours = int(self.cfg["filter"].get("pause_hours", 1))
        for sym, s in scores.items():
            if s.ban and not self.journal.is_banned(sym):
                self.journal.ban(sym, ban_hours, s.reason)
                logger.info("BAN %s (%dh) — %s", sym, ban_hours, s.reason)
                new_bans += 1
            elif s.pause and not self.journal.is_banned(sym):
                self.journal.ban(sym, pause_hours, s.reason)
                logger.info("PAUSE %s (%dh) — %s", sym, pause_hours, s.reason)
                new_pauses += 1
        if new_bans or new_pauses:
            logger.info("applied %d bans + %d pauses this scan", new_bans, new_pauses)

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
        # Asset-category whitelist (e.g. only forex+crypto, no stocks/indices)
        allowed_cats = set((self.cfg.get("filter") or {}).get("asset_categories") or [])
        if allowed_cats:
            if categorize_symbol(symbol, self.feed.assets.get(symbol)) not in allowed_cats:
                return False
        # User-applied hour-of-day whitelist (filter.hour_whitelist)
        if not self._hour_allowed(symbol): return False
        return True

    def _pick_switch_pair(self, exclude: set[str]) -> Optional[PairScore]:
        pool = {sym: sc for sym, sc in self._pair_scores.items()
                if sc.allowed and sym not in exclude and not self.journal.is_banned(sym)
                and int(self.feed.assets.get(sym, {}).get("payout", 0)) >= self.cfg["filter"]["min_payout"]}
        return pick_best(pool, exclude)

    def _hour_allowed(self, sym: str) -> bool:
        """User-applied hour-whitelist gate. When `filter.hour_whitelist` is
        non-empty, only trade (sym, current_hour_local) combos that are in
        the whitelist. Empty whitelist = filter disabled = all hours allowed.

        Built via Mini App "По часам" → "Применить как фильтр" button which
        scans historical trades, picks pair×hour cells with high WR, and
        saves the list. Bot then refuses to enter trades outside these
        windows — implements time-of-day strategy refinement.
        """
        wl = (self.cfg.get("filter") or {}).get("hour_whitelist") or {}
        if not wl:
            return True   # filter inactive
        hours = wl.get(sym)
        if not hours:
            return False  # pair not whitelisted
        try:
            tz_name = (self.cfg.get("telegram") or {}).get("daily_report_timezone") or "Europe/Kyiv"
            tz = pytz.timezone(tz_name)
            current_hour = datetime.now(tz).hour
            return int(current_hour) in [int(h) for h in hours]
        except Exception:
            logger.exception("hour_allowed check failed for %s — defaulting to allow", sym)
            return True

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
                end_h = time.strftime("%H:%M UTC", time.gmtime(self.state.day_off_until))
                logger.warning(
                    "🛌 ENTERING DAY-OFF (startup): no pairs passed filter, "
                    "pausing %dh until %s. Trading loop will sit idle. "
                    "Clear via: python3 -c '... state_kv day_off_until=0' or wait.",
                    hours, end_h,
                )
                await self._notify(
                    f"😴 Ни одна пара не прошла фильтр на старте. "
                    f"Пауза {hours}ч до <code>{end_h}</code>. Бот будет молчать "
                    f"всё это время — открой /control → 🔄 Reset cycle если "
                    f"хочешь снять раньше."
                )
            else:
                logger.info("initial scan empty — waiting for assets_list from WS")

        last_scan = time.time()
        last_bar_minute = -1   # track minute boundary for bar-aligned refresh
        last_autoset = 0.0     # track auto window assignment
        last_tick_ts = time.time()   # stall watchdog: last time any tick arrived
        stall_notified = False       # don't spam TG on every check

        # Signal check cadence: poll every 1–2s to catch bar close promptly.
        check_interval = max(1, int(self.cfg.get("misc", {}).get("poll_interval_sec", 1)))
        while self._running:
            try:
                await asyncio.wait_for(self._tick_event.wait(), timeout=check_interval)
                last_tick_ts = time.time()   # tick received — reset stall timer
                stall_notified = False
            except asyncio.TimeoutError:
                pass
            self._tick_event.clear()

            # ── Stall watchdog: if we have tracked pairs but no tick for
            # STALL_LIMIT seconds, subscriptions were likely lost after a
            # reconnect. Force a resubscription + feed reconnect.
            STALL_LIMIT = 600   # 10 minutes
            if (self._tracked and not self.state.paused
                    and not self.state.waiting_resume
                    and time.time() - last_tick_ts > STALL_LIMIT):
                logger.warning("stall watchdog: no ticks for %.0fs on %d tracked pairs — forcing reconnect",
                               time.time() - last_tick_ts, len(self._tracked))
                if not stall_notified:
                    stall_notified = True
                    await self._notify(
                        f"⚠️ Нет тиков {STALL_LIMIT//60} мин на {len(self._tracked)} парах. "
                        f"Принудительный реконнект WS…"
                    )
                last_tick_ts = time.time()   # reset so we don't spam
                # Force feed WebSocket close → auto_reconnect_loop takes over
                try:
                    ws = getattr(self.feed, "_ws", None)
                    if ws:
                        await ws.close()
                except Exception:
                    logger.exception("stall watchdog: ws.close() failed")
                continue

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
                # Periodic reminder so user sees bot is in day-off, not crashed.
                # Every ~5 min: one heartbeat log line with remaining time.
                if int(now) - getattr(self, "_last_dayoff_log", 0) > 300:
                    self._last_dayoff_log = int(now)
                    remain_min = int((self.state.day_off_until - now) / 60)
                    end_h = time.strftime("%H:%M UTC", time.gmtime(self.state.day_off_until))
                    logger.info(
                        "🛌 day-off active: %d min remaining (until %s). "
                        "No scan/trade until then.",
                        remain_min, end_h,
                    )
                continue
            if self.state.day_off_until and now >= self.state.day_off_until:
                self.state.day_off_until = 0
                self._persist()
                logger.info("⏰ day-off expired — running rescan")
                await self._rescan_pairs()
                await self._notify("⏰ Пауза истекла, возобновляю поиск.")
                if not self._tracked:
                    # still no pairs → another day_off
                    hours = self.cfg["filter"]["day_off_hours"]
                    self.state.day_off_until = int(now) + hours * 3600
                    self._persist()
                    end_h = time.strftime("%H:%M UTC", time.gmtime(self.state.day_off_until))
                    logger.warning(
                        "🛌 RE-ENTERING DAY-OFF: rescan still found 0 pairs. "
                        "Pausing %dh until %s.", hours, end_h,
                    )
                    await self._notify(
                        f"😴 После переоценки фильтр опять пуст. "
                        f"Ещё пауза {hours}ч до <code>{end_h}</code>."
                    )
                    continue

            # ── Main loop body wrapped in broad try/except so a single bad tick
            # or unexpected exception never kills the entire trading loop.
            try:
                # Edge-detect: feed just became ready (e.g. after relogin) →
                # force an immediate rescan instead of waiting up to 5 min.
                # Without this, a mid-cycle relogin leaves tracked={} until the
                # next periodic tick fires — with no trading in between.
                feed_ready_obj = getattr(self.feed, "_ready", None)
                feed_ready_now = feed_ready_obj.is_set() if feed_ready_obj is not None else True
                if feed_ready_now and not self._was_feed_ready:
                    logger.info("feed became ready — forcing immediate rescan")
                    self._force_rescan = True
                self._was_feed_ready = feed_ready_now

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

                # Branch: three modes
                #  • FREE: no active cycle → scan all pairs for first signal
                #  • IN-CYCLE LOCKED: cycle active + locked on a pair → wait
                #    for next bar on THAT pair
                #  • IN-CYCLE SEARCHING: cycle active but pair switched out
                #    (payout drop, max_trades hit) → scan ALL eligible pairs,
                #    first signal becomes new locked pair (preserves mg_step)
                if self.state.mg_step > 0:
                    if self.state.current_pair:
                        await self._in_cycle_step()
                    else:
                        await self._in_cycle_search_step()
                else:
                    await self._free_scan_step()

            except asyncio.CancelledError:
                raise   # propagate cancellation — bot is shutting down
            except Exception:
                logger.exception("state_machine loop tick crashed — continuing on next tick")

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
                    candles = self._candles.get(sym) or []
                    png = render_chart(candles, params, sym)
                    await self.send_chart(png, caption=f"📊 {sym} — сигнал {action.upper()}")
                except Exception as e:
                    logger.exception("send_chart failed for %s", sym)
                    # Best-effort: tell user via TG so they know charts are
                    # broken (silent failures are the worst).
                    try:
                        await self._notify(
                            f"⚠️ Не удалось построить график для {sym}: {type(e).__name__}: {e}"
                        )
                    except Exception:
                        pass
        asyncio.create_task(_notify_bg(), name=f"notify_{sym}")

        await self._open_and_track(sym, action, amt)

    # ---------- in-cycle step (after a loss) ----------
    async def _in_cycle_step(self):
        sym = self.state.current_pair

        # ── Hard cap: max trades per pair within this cycle ──
        # User-configured via two settings:
        #   trading.limit_trades_per_pair_enabled (bool toggle, default false)
        #   trading.max_trades_on_pair (int count, used when toggle is true)
        # Setting count to 0 also disables it (legacy compat).
        limit_enabled = bool(self.cfg["trading"].get("limit_trades_per_pair_enabled", False))
        max_trades = int(self.cfg["trading"].get("max_trades_on_pair", 0) or 0)
        if limit_enabled and max_trades > 0 and self.state.trades_on_pair >= max_trades:
            self.state.switched_pairs.append(sym)
            self.state.cycle_switches += 1
            self.state.current_pair = None   # enter SEARCHING mode
            self.state.trades_on_pair = 0
            self._persist()
            await self._notify(
                f"🔀 Лимит {max_trades} сделок на паре {sym} достигнут — "
                f"ищу сигнал на других допустимых парах. МГ-шаг {self.state.mg_step} сохранён."
            )
            return  # next loop tick → _in_cycle_search_step

        # Check payout drop → switch to SEARCHING mode (don't pick one specific pair)
        payout = int(self.feed.assets.get(sym, {}).get("payout", 0))
        floor = self.cfg["filter"]["payout_floor"]
        if payout < floor and self.state.cycle_switches < self.cfg["trading"]["max_pair_switch_per_cycle"]:
            self.state.switched_pairs.append(sym)
            self.state.cycle_switches += 1
            self.state.current_pair = None   # enter SEARCHING mode
            self._persist()
            await self._notify(
                f"🔄 Payout {payout}% < {floor}% на {sym} — "
                f"ищу сигнал на других допустимых парах. МГ-шаг {self.state.mg_step} сохранён."
            )
            return  # next loop tick → _in_cycle_search_step

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

    # ---------- in-cycle SEARCH (no pair locked, scan all eligible) ----------
    async def _in_cycle_search_step(self):
        """Active MG cycle but no pair locked (just switched away from previous).
        Scan ALL eligible tracked pairs for a fresh CONSENSUS signal. First
        match wins → that pair becomes new current_pair, MG step preserved.

        Honours the same stop-sum / max-steps guardrails as _in_cycle_step.
        Pairs already used in this cycle (in switched_pairs) are excluded so
        we don't bounce back and forth.
        """
        if not self._tracked:
            return

        # Stop-sum guardrail (same as _in_cycle_step)
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

        tf = self.cfg["filter"]["tf"]
        MAX_STALENESS = 25
        now_ts = int(time.time())
        min_payout = self.cfg["filter"]["min_payout"]

        # Iterate by priority + payout (same ordering as _free_scan_step)
        sorted_tracked = sorted(
            self._tracked,
            key=lambda s: (
                (self._pair_scores.get(s).priority if self._pair_scores.get(s) else 999),
                -int((self.feed.assets.get(s) or {}).get("payout", 0)),
            ),
        )

        evaluated = 0
        new_bars = 0
        fired = None
        for sym in sorted_tracked:
            # Skip pairs already used in this cycle
            if sym in self.state.switched_pairs:
                continue
            # Skip banned pairs
            if self.journal.is_banned(sym):
                continue
            # Skip low payout
            payout = int(self.feed.assets.get(sym, {}).get("payout", 0))
            if payout < min_payout:
                continue
            # Skip if user's hour-whitelist excludes this pair at this hour
            if not self._hour_allowed(sym):
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
            age = now_ts - (last_closed_t + tf)
            if age > MAX_STALENESS:
                continue
            action = self._check_signal(sym)
            if action:
                fired = (sym, action)
                break

        # Heartbeat log so we know the search is alive (mirrors _free_scan_step)
        logger.info("in-cycle SEARCH: tracked=%d eligible=%d new_bars=%d fired=%s mg_step=%d",
                    len(self._tracked), evaluated, new_bars, fired, self.state.mg_step)

        if not fired:
            return  # no signal anywhere — keep waiting

        sym, action = fired
        payout = int(self.feed.assets.get(sym, {}).get("payout", 0))

        # Lock onto this pair, adapt direction if needed
        self.state.current_pair = sym
        if action != self.state.direction:
            logger.info("in-cycle search: direction flipped %s → %s on %s",
                        self.state.direction, action, sym)
            self.state.direction = action
        self.state.trades_on_pair = 0
        self._persist()

        msg = (
            f"🎯 Найден сигнал на {sym} → {action.upper()} "
            f"(payout {payout}%). МГ-шаг {self.state.mg_step}, ставка ${next_amt:.2f}."
        )
        asyncio.create_task(self._notify(msg), name=f"notify_search_{sym}")
        await self._open_and_track(sym, action, next_amt)

    # ---------- trade open / close flow ----------
    async def _open_and_track(self, sym: str, action: str, amount: float):
        expiry = int(self.cfg["trading"]["expiry_seconds"])
        # Per-(symbol, current_hour) expiry override — set by Mini App
        # «По часам» → ⭐ Применить как фильтр. Picks expiry that virtual
        # signals showed best WR for this exact pair-hour combo.
        try:
            overrides = (self.cfg.get("filter") or {}).get("hour_expiry_overrides") or {}
            if overrides and sym in overrides:
                # Convert UTC time → user's TZ (same logic as _hour_allowed)
                import datetime as _dt
                tz_name = (self.cfg.get("telegram") or {}).get("daily_report_timezone") or "Europe/Kyiv"
                try:
                    import pytz
                    tz = pytz.timezone(tz_name)
                    cur_hour = _dt.datetime.now(tz).hour
                except Exception:
                    cur_hour = _dt.datetime.utcnow().hour
                pref = overrides[sym].get(str(cur_hour))
                if pref:
                    pref = int(pref)
                    if pref != expiry:
                        logger.info(
                            "expiry override applied for %s @ %02d:00 — using %ds "
                            "(default was %ds) per virtual stats",
                            sym, cur_hour, pref, expiry,
                        )
                        expiry = pref
        except Exception:
            logger.exception("hour_expiry_overrides lookup failed — using default")
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
        open_ts_actual = opened.open_time or int(time.time())
        self.journal.log_trade({
            "trade_id": closed.trade_id,
            "symbol": opened.asset,
            "action": opened.action,
            "amount": opened.amount,
            "profit": closed.profit,
            "result": closed.result,
            "payout": opened.payout,
            "mg_step": self.state.mg_step,
            "open_ts": open_ts_actual,
            "close_ts": closed.close_time or int(time.time()),
            "balance_after": self.feed.balance(),
            "mode": self.cfg["mode"],
        })
        # Backtest 1..5 bar expiries against cached candles around entry —
        # persist as JSON in trades.exp_wins so /api/hourly_stats can show
        # accumulated "min winning expiry per (sym, hour)" over time.
        try:
            self._persist_exp_wins(opened.asset, opened.action,
                                   open_ts_actual, closed.trade_id)
        except Exception:
            logger.exception("exp_wins backtest failed for %s", closed.trade_id)

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
            mg_enabled = bool(self.cfg["martingale"].get("enabled", True))
            if mg_enabled:
                self.state.mg_step += 1
                self._persist()
                await self._notify(
                    f"❌ LOSS {opened.asset}  -${opened.amount:.2f}  "
                    f"(шаг → MG{self.state.mg_step}, потери ${self.state.session_loss:.2f})"
                )
            else:
                # No-MG mode: don't double down. Reset cycle and look for next
                # signal on best available pair.
                self._reset_cycle()
                self._persist()
                await self._notify(
                    f"❌ LOSS {opened.asset}  -${opened.amount:.2f}  "
                    f"(МГ выкл — ищу новый сигнал, потери ${self.state.session_loss:.2f})"
                )
            # Loop will immediately trigger next iteration
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
