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
import math
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
    # Гибкий MG (этап 2/3): резерв неиспользованных перекрытий с предыдущих пар
    # цикла. На последней паре отдаётся целиком (pair_limits[-1] + cycle_unused_carry).
    cycle_unused_carry: int = 0
    # Серия минусов подряд на ТЕКУЩЕЙ паре. Сейчас не используется как
    # триггер switch (consec убран по запросу юзера) — оставлено как
    # внутренний счётчик для возможной аналитики и логирования.
    losses_streak_on_pair: int = 0
    # ── Stage 3: parallel trading ─────────────────────────────────────────
    # Когда trading.parallel_pairs=True, бот ведёт НЕЗАВИСИМЫЕ MG-циклы на
    # нескольких парах одновременно (до trading.max_parallel_pairs штук).
    # Каждый ключ = symbol, значение = PairCycle.to_dict() форма:
    #   {direction, mg_step, cycle_loss, pending_trade, losses_streak,
    #    started_at, trades_count}
    # current_pair / mg_step / pending_trade выше — НЕ используются в parallel
    # mode (резервируются под одиночный legacy режим для backwards-compat).
    pair_cycles: dict = field(default_factory=dict)

    def to_dict(self): return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RuntimeState":
        """Preserve all fields across restart — bot resumes the cycle exactly
        where it left off (mg_step, current_pair, direction). pending_trade
        is recovered separately by _resume_pending_trade()."""
        if not d: return cls()
        s = cls()
        for k, v in d.items():
            if hasattr(s, k):
                setattr(s, k, v)
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
        self._tracked: set[str] = set()      # NARROW — пары на которых торгуем (live WS subs)
        self._scan_pool: set[str] = set()    # BROAD — все пары прошедшие базовый scan
                                              # (payout + asset_categories), независимо от
                                              # того allowed/banned/pause. Используется
                                              # для аналитики через REST-loop (signals
                                              # пишутся по этому пулу, не только tracked).
                                              # Юзер: «аналитика должна писаться ВСЕГДА
                                              # независимо от фильтра».
        # Этап 2: signals collector — сколько сигналов уже записано на символ
        # (по close-time бара). Используется для дедупа `_record_signals_phase`,
        # чтобы не пересчитывать CONSENSUS на одном баре дважды.
        self._last_signal_record_bar: dict[str, int] = {}
        # WR-based blacklist кэш: (sym -> (wr_pct_or_None, computed_at_ts)).
        # Пере-расчёт раз в 10 минут — SQL по 1500+ signals не моментальный.
        self._pair_wr_cache: dict[str, tuple] = {}
        # Stage 3: defensive flag — один лог-warning про orphan pair_cycles
        # при переключении parallel→legacy mode, чтобы не спамить.
        self._warned_orphan_cycles: bool = False

        self._running = False
        self._tick_event = asyncio.Event()
        self._force_rescan = False   # set True to trigger _rescan_pairs next tick
        self._was_feed_ready = False  # edge-detect for WS-ready transition

        # Live loss-streak monitor (доп. защита поверх BAN/TEMP_PAUSE).
        # Считает подряд идущие LOSS на паре через все циклы; на WIN — сброс.
        # Если live_streak ≥ historical_max_loss_streak_to_win × multiplier
        # и собрана достаточная история — пара ставится на live-pause.
        # Не путать с self.state.losses_streak_on_pair: тот сбрасывается
        # _reset_cycle() на WIN, этот — только на WIN текущей пары.
        self._live_loss_streak: dict[str, int] = {}

        # Авто-пересчёт base_amount от живого баланса (см. config.trading.auto_base_amount).
        # Счётчик циклов с момента последнего успешного пересчёта (инкрементится на WIN).
        self._cycles_since_recalc: int = 0
        # YYYY-MM-DD (в локальной TZ) последнего daily-пересчёта, чтобы не дёргать
        # больше раза в сутки.
        self._last_daily_recalc_date: Optional[str] = None
        # Если триггер сработал во время МГ — сюда складываем причину;
        # как только цикл закроется (WIN → mg_step=0) — выполнится.
        self._pending_recalc_reason: Optional[str] = None

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
            # weekends: если no_weekends включён — sat/sun считаем не-рабочими.
            if sched.get("no_weekends"):
                if now.weekday() >= 5:   # 5=Sat, 6=Sun
                    return False
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

    # NOTE: _pair_stats_logger_loop был удалён в этапе 1 рефакторинга.
    # В этапе 2 будет переработан как часть нового signals collector.

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
        self.state.cycle_unused_carry = 0
        self.state.losses_streak_on_pair = 0
        self._persist()
        logger.info("RESUMED after stop-sum — fresh cycle")

    def force_reset_cycle(self):
        """Принудительный сброс цикла в FREE-режим.
        МГ-шаг → 0, пара → None, потери сессии сохраняются (деньги потрачены).
        Бот сразу начинает искать новый сигнал на любой паре.

        Stage 3: также сбрасывает ВСЕ parallel-циклы (pair_cycles → {}).
        Pending parallel trades тоже сбрасываются — их результаты придут
        через PO close-event но не будут обработаны (cycle нет). Это
        приемлемо т.к. /reset_cycle — emergency action, юзер явно знает что
        теряет track активных сделок."""
        old_pair = self.state.current_pair
        old_step = self.state.mg_step
        # Stage 3: cleanup parallel cycles
        old_parallel = list((self.state.pair_cycles or {}).keys())
        if old_parallel:
            logger.info("FORCE RESET CYCLE: also clearing %d parallel cycles: %s",
                        len(old_parallel), old_parallel)
            self.state.pair_cycles = {}
        self._reset_cycle()
        self.state.day_off_until = 0   # снять day-off если он был
        self._persist()
        self._force_rescan = True
        self._tick_event.set()
        logger.info("FORCE RESET CYCLE: was %s MG%d → FREE (parallel cleared: %d)",
                    old_pair, old_step, len(old_parallel))

    async def force_switch_pair(self) -> str | None:
        """Принудительная смена пары — переход в SEARCH режим.
        Бот не выбирает конкретную пару, а **сканирует все tracked-пары** и
        войдёт на ту, где CONSENSUS первой даст сигнал. МГ-шаг сохраняется.
        Старая пара уходит в switched_pairs (исключение от повторного входа
        в этом цикле).

        Возвращает 'SEARCH' если режим активирован, или None если нет
        активного цикла / нет tracked-пар.

        Stage 3: в parallel mode этот концепт не применим — каждый цикл
        ведётся на СВОЕЙ паре, нет глобальной 'current_pair' которую можно
        свитчить. Если юзер хочет принудительно закрыть конкретный цикл —
        используй /reset_cycle (закроет ВСЕ parallel-циклы)."""
        if bool((self.cfg.get("trading") or {}).get("parallel_pairs", False)):
            logger.warning("force_switch_pair: not applicable in parallel mode "
                            "(use force_reset_cycle to clear all parallel cycles)")
            return None
        if self.state.mg_step == 0:
            logger.warning("force_switch_pair: no active MG cycle to switch")
            return None
        if not self._tracked:
            logger.warning("force_switch_pair: no tracked pairs to search")
            return None
        old_pair = self.state.current_pair
        if old_pair and old_pair not in (self.state.switched_pairs or []):
            self.state.switched_pairs.append(old_pair)
        # Этап 2: гибкий MG — учёт ручной смены и перенос неиспользованных
        # перекрытий. cfg.martingale.manual_switch_counts (default true)
        # контролирует засчитывается ли это в cycle_switches.
        mg_cfg = self.cfg.get("martingale") or {}
        if bool(mg_cfg.get("manual_switch_counts", True)):
            self.state.cycle_switches += 1
        if bool(mg_cfg.get("carry_unused", True)):
            limits = self._mg_pair_limits()
            pos = max(0, min(self.state.cycle_switches - 1, len(limits) - 1))
            unused = max(0, limits[pos] - self.state.trades_on_pair)
            self.state.cycle_unused_carry += unused
        self.state.current_pair = None   # ← SEARCH режим
        self.state.trades_on_pair = 0
        self.state.losses_streak_on_pair = 0
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

    async def _notify(self, msg: str, parse_mode: str | None = None):
        try:
            # Если notify callback поддерживает parse_mode (как TG-обёртка) —
            # пробуем передать, иначе вызываем со старой сигнатурой.
            try:
                res = self.notify(msg, parse_mode=parse_mode) if parse_mode \
                    else self.notify(msg)
            except TypeError:
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
        action = "call" if last.side == "buy" else "put"
        # Этап 3 — per-strategy user filter (multi-dim). Применяется ТОЛЬКО к
        # торговому решению, не к записи в `signals` (collector использует
        # `_check_signal_with_meta` напрямую и пишет всё). Если фильтр активен
        # и snapshot не проходит — игнорируем сигнал на торговой стороне.
        try:
            strat_for_filter = "consensus"
            if self.registry:
                try:
                    strat_for_filter = self.registry.get_active().name
                except Exception:
                    pass
            f = self.journal.signal_filter_get(strat_for_filter)
            if f and f.get("enabled"):
                # Snapshot для filter check — берём params активной стратегии
                # из registry (если доступна), иначе global cfg.indicator.
                # _market_snapshot использует только CONSENSUS-специфичные
                # ключи; для кастомных стратегий он мирится с пропуском.
                snap_params = self.cfg.get("indicator") or {}
                if self.registry:
                    try:
                        snap_params = self.registry.get_active().merged_params()
                    except Exception:
                        pass
                snap = self._market_snapshot(symbol, last,
                                             {**DEFAULT_PARAMS, **snap_params},
                                             closed, strat_for_filter)
                if not self._signal_passes_user_filter(snap, f):
                    logger.info("user_filter: %s %s rejected by active filter",
                                symbol, action)
                    return None
        except Exception:
            logger.exception("user_filter check failed for %s — propagating signal", symbol)
        return action

    def _pair_matches_trade_mode(self, sym: str) -> bool:
        """True если пара разрешена для ТОРГОВЛИ в текущем filter.trade_mode.
        Analytics-collection (_record_signals_phase) этот метод НЕ дёргает —
        она пишет сигналы по всему self._tracked независимо от trade_mode.

        Режимы:
          • "otc"     (default) — открываем только на _otc парах
          • "regular" — только на обычных (без суффикса _otc)
          • "mixed"   — обе категории
        Неизвестный mode → пропускаем (failsafe — не блокировать торговлю при
        опечатке в конфиге)."""
        mode = (self.cfg.get("filter") or {}).get("trade_mode", "otc")
        if mode == "mixed":
            return True
        info = self.feed.assets.get(sym) or {}
        is_otc = bool(info.get("is_otc"))
        if mode == "otc":
            return is_otc
        if mode == "regular":
            return not is_otc
        return True

    def _reclassify_current_pair_now(self):
        """Этап 3: real-time бан/пере-фильтрация. Берёт cached candles
        текущей пары, прогоняет filter_1000.classify(), обновляет
        _pair_scores[sym]. Если max_loss_streak ≥ max_losses_in_row →
        кладёт в journal.ban() немедленно (без ожидания 5-мин rescan).
        Дёшево по CPU (~10ms на пару) — выполняется на каждой close.
        """
        from strategy.filter_1000 import classify
        sym = self.state.current_pair
        if not sym:
            return
        candles = self._candles.get(sym) or []
        if len(candles) < 100:
            return
        params = {**DEFAULT_PARAMS, **(self.cfg.get("indicator") or {})}
        if self.registry:
            try:
                params = self.registry.get_active().merged_params()
            except Exception:
                pass
        f_cfg = self.cfg.get("filter") or {}
        max_losses = int(f_cfg.get("max_losses_in_row", 3))
        min_wr1 = float(f_cfg.get("min_wr1", 0) or 0)
        min_wr1_recent = float(f_cfg.get("min_wr1_recent", 0) or 0)
        payout_now = int((self.feed.assets.get(sym) or {}).get("payout", 0))
        try:
            score = classify(sym, payout_now, candles, params,
                             max_losses, min_wr1, min_wr1_recent)
        except Exception:
            logger.exception("classify failed for %s", sym)
            return
        # Обновляем кэш сейчас же — _verify_current_pair_still_passes сразу
        # увидит свежие данные.
        self._pair_scores[sym] = score
        # Real-time ban — если паттерн стал явно деструктивным
        if score.ban and not self.journal.is_banned(sym):
            ban_hours = int(f_cfg.get("ban_hours", 12))
            self.journal.ban(sym, ban_hours, score.reason or "real-time reclassify")
            logger.warning("real-time BAN %s (%dh) — %s", sym, ban_hours, score.reason)

    def _verify_current_pair_still_passes(self):
        """Постоянная пере-фильтрация (этап 3, архитектурный принцип №6).
        Берёт последний score из _pair_scores и проверяет что текущая пара
        по-прежнему `allowed=True` И не упала в payout. Если нет —
        помечает switched + переходит в SEARCH (МГ-шаг сохранён). Ban/pause
        выставит следующий _rescan_pairs (через час)."""
        sym = self.state.current_pair
        if not sym:
            return
        score = self._pair_scores.get(sym)
        if score is None:
            return
        payout = int((self.feed.assets.get(sym) or {}).get("payout", 0))
        floor = int(self.cfg["filter"].get("payout_floor", 0))
        reason = None
        if not score.allowed:
            reason = score.reason or "не проходит фильтр"
        elif payout < floor:
            reason = f"payout {payout}% < {floor}%"
        if not reason:
            return
        # carry unused в резерв (по limit'у текущей позиции)
        mg_cfg = self.cfg.get("martingale") or {}
        if bool(mg_cfg.get("carry_unused", True)):
            limits = self._mg_pair_limits()
            pos = self._mg_position()
            unused = max(0, limits[pos] - self.state.trades_on_pair)
            self.state.cycle_unused_carry += unused
        # Лимит смен = len(pair_limits) - 1
        max_switches = max(0, len(self._mg_pair_limits()) - 1)
        if self.state.cycle_switches < max_switches:
            self.state.switched_pairs.append(sym)
            self.state.cycle_switches += 1
            self.state.current_pair = None
            self.state.trades_on_pair = 0
            self.state.losses_streak_on_pair = 0
            self._persist()
            asyncio.create_task(self._notify(
                f"🔁 Перефильтровано: {sym} больше не проходит ({reason}). "
                f"Перехожу в SEARCH. МГ-шаг {self.state.mg_step} сохранён, "
                f"резерв перекрытий: {self.state.cycle_unused_carry}."
            ), name=f"refilter_{sym}")
            self._tick_event.set()

    def _signal_passes_user_filter(self, snap: dict, f: dict) -> bool:
        """True если snapshot укладывается во все границы активного фильтра.
        Отсутствующие в snapshot поля считаются проходящими (custom-стратегии
        могут не заполнять часть индикаторов CONSENSUS)."""
        def _between(val, lo, hi):
            if val is None or (lo is None and hi is None):
                return True
            if lo is not None and val < lo: return False
            if hi is not None and val > hi: return False
            return True
        if not _between(snap.get("atr_ratio"),       f.get("atr_ratio_min"),  f.get("atr_ratio_max")):     return False
        if not _between(snap.get("bb_position"),     f.get("bb_position_min"), f.get("bb_position_max")):  return False
        if not _between(snap.get("rsi_ma"),          f.get("rsi_ma_min"),     f.get("rsi_ma_max")):        return False
        cmax = f.get("candle_atr_ratio_max")
        cval = snap.get("candle_atr_ratio")
        if cmax is not None and cval is not None and cval > cmax: return False
        pmin = f.get("payout_min"); pval = snap.get("payout_at_signal")
        if pmin is not None and pval is not None and pval < pmin: return False
        vmin = f.get("votes_total_min"); vval = snap.get("votes_total")
        if vmin is not None and vval is not None and vval < vmin: return False
        ha = f.get("hours_allowed") or []
        if ha and snap.get("hour_local") is not None and snap["hour_local"] not in ha: return False
        da = f.get("dow_allowed") or []
        if da and snap.get("day_of_week") is not None and snap["day_of_week"] not in da: return False
        return True

    # ---------- TG notifications (Stage 2 — enriched) ----------

    def _compute_expiry_preview(self, sym: str) -> tuple[int, str]:
        """Возвращает (expiry_sec, source) для текущей сделки на sym.

        Используется только для отображения в TG-нотификации (не для реального
        входа — там та же логика выполняется ниже по стеку в _open_and_track /
        _open_parallel_trade). source ∈ {"hour","pair","default"}.
        """
        default_expiry = int(self.cfg["trading"]["expiry_seconds"])
        tf_sec = int(self.cfg["filter"].get("tf", 60))
        if not (self.cfg.get("filter") or {}).get("auto_expiry_enabled", True):
            return default_expiry, "default"
        try:
            from strategy.expiry_optimizer import resolve_expiry_bars
            tz_name = (self.cfg.get("telegram") or {}).get(
                "daily_report_timezone") or "Europe/Kyiv"
            try:
                tz = pytz.timezone(tz_name)
                current_hour = datetime.fromtimestamp(int(time.time()), tz=tz).hour
            except Exception:
                current_hour = datetime.utcfromtimestamp(int(time.time())).hour
            opt = resolve_expiry_bars(self.journal, sym, current_hour)
            if opt:
                return int(opt["bars"]) * tf_sec, opt.get("source", "hour")
        except Exception:
            pass
        return default_expiry, "default"

    def _notify_open_async(self, sym: str, action: str, amt: float,
                            mg_step: int, payout: int, pre_balance: float):
        """Формирует подробное TG-уведомление об открытии + шлёт график PNG.
        Вызывается fire-and-forget (asyncio.create_task), чтобы PNG render
        не задерживал реальный вход в сделку.

        ВАЖНО (визуальная фиксация сигнала): снапшотим буфер свечей СИНХРОННО
        прямо сейчас, до спавна таски. Если этого не делать — пока _bg() ждёт
        в очереди event loop, может прилететь новый тик и `generate_signals`
        внутри render_chart пересчитает стрелки → на скриншоте может оказаться
        либо отсутствующая стрелка, либо в противоположном направлении.
        Юзеру важно: то что бот увидел в момент входа — то и на картинке.
        Реальную торговлю это не ограничивает (на следующем баре после LOSS
        бот всё равно перечитает свежий сигнал — см. _in_cycle_step)."""
        candles_snapshot = list(self._candles.get(sym) or [])
        # Экспирация для этой сделки (двухуровневый fallback hour→pair→default).
        exp_sec, exp_src = self._compute_expiry_preview(sym)
        tf_sec = int(self.cfg["filter"].get("tf", 60)) or 60
        exp_bars = max(1, exp_sec // tf_sec)
        src_label = {"hour": "ч×пара", "pair": "по паре", "default": "дефолт"}.get(exp_src, exp_src)

        async def _bg():
            stage = "первая сделка" if mg_step == 0 else f"МГ{mg_step}"
            msg = (
                f"📡 <b>{sym} → {action.upper()}</b> ({stage})\n"
                f"💰 Ставка: ${amt:.2f}   📊 Payout: {payout}%\n"
                f"💼 Баланс до: ${pre_balance:.2f}\n"
                f"⏱ Экспирация: {exp_bars} бара ({exp_sec}с, {src_label})"
            )
            try:
                # parse_mode=HTML чтобы <b>...</b> рендерился жирным, а не
                # текстом (бага со скриншота 2026-05-16).
                await self._notify(msg, parse_mode="HTML")
            except Exception:
                logger.exception("notify_open failed")
            # Chart на каждой ступени (этап 2 — раньше слался только на 1-й)
            if self.send_chart:
                try:
                    from tg.chart import render_chart
                    params = {**self.cfg["indicator"]}
                    png = render_chart(candles_snapshot, params, sym)
                    cap = f"📊 {sym} — {action.upper()} ({stage})"
                    await self.send_chart(png, caption=cap)
                except Exception as e:
                    logger.exception("send_chart failed for %s", sym)
                    try:
                        await self._notify(
                            f"⚠️ График {sym} не построился: {type(e).__name__}: {e}"
                        )
                    except Exception:
                        pass
        asyncio.create_task(_bg(), name=f"notify_open_{sym}_{mg_step}")

    # ---------- signals collector (Stage 2) ----------

    def _check_signal_with_meta(self, symbol: str):
        """Same gate as `_check_signal`, но возвращает (action, sig, params,
        strategy_name) если сигнал есть на последнем закрытом баре. Иначе None.
        Используется collectorom + free-scan'ом."""
        buf = self._candles.get(symbol)
        if not buf or len(buf) < 200:
            return None
        closed = buf[:-1]
        period = int(self.cfg.get("filter", {}).get("tf", 60))
        recent = closed[-100:]
        if len(recent) >= 2:
            first_t = int(recent[0]["time"])
            last_t = int(recent[-1]["time"])
            expected = max(1, (last_t - first_t) // period + 1)
            density = len(recent) / expected
            if density < 0.95:
                return None
        strategy_name = "consensus"
        if self.registry:
            try:
                strat = self.registry.get_active()
                params = strat.merged_params()
                sigs, _ = strat.generate_signals(closed, params)
                strategy_name = strat.name
            except Exception:
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
        action = "call" if last.side == "buy" else "put"
        return (action, last, params, strategy_name)

    def _market_snapshot(self, symbol: str, sig, params: dict,
                         closed: list[dict], strategy_name: str) -> dict:
        """Снимает срез индикаторов на момент signal bar. Безопасен к падениям —
        не-CONSENSUS стратегии могут не выдавать `sig.votes` или индикаторы;
        отсутствующие поля попадают в БД как NULL."""
        from strategy.indicators import qqe, htf_trend, atr, sma, bollinger
        p = {**DEFAULT_PARAMS, **(params or {})}
        n = len(closed)
        i = getattr(sig, "i", n - 1)
        if i < 0 or i >= n:
            return {}
        times  = [c["time"]  for c in closed]
        opens  = [c["open"]  for c in closed]
        highs  = [c["high"]  for c in closed]
        lows   = [c["low"]   for c in closed]
        closes = [c["close"] for c in closed]

        snap: dict = {}
        try:
            rsi_ma_arr, trail_arr = qqe(closes, p["rsiPeriod"], p["rsiSmoothing"], p["qqeFactor"])
            snap["rsi_ma"] = rsi_ma_arr[i] if i < len(rsi_ma_arr) else None
            snap["qqe_trailing"] = trail_arr[i] if i < len(trail_arr) else None
        except Exception:
            snap["rsi_ma"] = snap["qqe_trailing"] = None
        try:
            tf_sec = 60
            if len(times) >= 3:
                deltas = sorted(times[k+1] - times[k] for k in range(len(times) - 1))
                tf_sec = max(1, int(deltas[len(deltas) // 2]))
            htf = htf_trend(opens, highs, lows, closes, p["htfMultiplier"],
                            p["htfMaPeriod"], p["htfMaType"],
                            times=times, tf_seconds=tf_sec)
            snap["htf_value"] = htf[i] if i < len(htf) else None
        except Exception:
            snap["htf_value"] = None
        try:
            atr_arr = atr(highs, lows, closes, p["atrPeriod"])
            atr_avg_arr = sma(atr_arr, p["atrAvgWindow"])
            atr_v = atr_arr[i] if i < len(atr_arr) else None
            atr_avg_v = atr_avg_arr[i] if i < len(atr_avg_arr) else None
            snap["atr14_1m"] = atr_v
            snap["atr_avg"] = atr_avg_v
            snap["atr_ratio"] = (atr_v / atr_avg_v) if (atr_v and atr_avg_v) else None
        except Exception:
            snap["atr14_1m"] = snap["atr_avg"] = snap["atr_ratio"] = None
        try:
            bb = bollinger(closes, p["bbPeriod"], p["bbStdDev"])
            up_i = bb["upper"][i] if i < len(bb["upper"]) else None
            lo_i = bb["lower"][i] if i < len(bb["lower"]) else None
            snap["bb_upper"] = up_i
            snap["bb_lower"] = lo_i
            if up_i is not None and lo_i is not None and (up_i - lo_i) != 0:
                snap["bb_position"] = (closes[i] - lo_i) / (up_i - lo_i)
            else:
                snap["bb_position"] = None
        except Exception:
            snap["bb_upper"] = snap["bb_lower"] = snap["bb_position"] = None

        body = abs(closes[i] - opens[i])
        snap["candle_body"] = body
        atr_v = snap.get("atr14_1m")
        snap["candle_atr_ratio"] = (body / atr_v) if atr_v else None
        snap["candle_direction"] = 1 if closes[i] > opens[i] else (-1 if closes[i] < opens[i] else 0)

        # голоса (есть только у CONSENSUS-совместимых)
        votes = getattr(sig, "votes", None) or {}
        snap["votes_rsi"]    = votes.get("rsi")
        snap["votes_htf"]    = votes.get("htf")
        snap["votes_vol"]    = votes.get("vol")
        snap["votes_bb"]     = votes.get("bb")
        snap["votes_candle"] = votes.get("candle")
        snap["votes_total"]  = getattr(sig, "total", None)

        # контекст: hour_local и day_of_week берём из РЕАЛЬНОГО wall-clock
        # времени когда бот зафиксировал сигнал — НЕ из candle.time. Причина:
        # PO в WS-стриме шлёт timestamp с локальным офсетом (+2-3ч от UTC),
        # который мы трактовали как UTC и потом ещё накладывали Kyiv/Helsinki —
        # получалось двойное смещение, "час" сигнала уезжал на 2-3 часа вперёд.
        # signal_ts в БД остаётся как PO даёт (для уникальности/сортировки),
        # а hour_local/day_of_week отражают РЕАЛЬНЫЙ локальный час когда сигнал
        # был обработан — это критично для фильтрации по времени торговли.
        ts = int(times[i])  # для уникальности signal_ts (без изменений)
        real_now = int(time.time())
        try:
            tz_name = (self.cfg.get("telegram") or {}).get("daily_report_timezone") or "Europe/Kyiv"
            tz = pytz.timezone(tz_name)
            local = datetime.fromtimestamp(real_now, tz=tz)
        except Exception:
            local = datetime.utcfromtimestamp(real_now)
        snap["hour_local"] = local.hour
        snap["day_of_week"] = local.weekday()

        try:
            snap["payout_at_signal"] = int((self.feed.assets.get(symbol) or {}).get("payout") or 0) or None
        except Exception:
            snap["payout_at_signal"] = None
        score = self._pair_scores.get(symbol)
        snap["wr1_long_at_signal"] = getattr(score, "wr1", None) if score else None
        snap["wr1_recent_at_signal"] = getattr(score, "wr1_recent", None) if score else None

        side = "call" if sig.side == "buy" else "put"
        snap.update({
            "strategy_name": strategy_name,
            "symbol": symbol,
            "side": side,
            "signal_ts": ts,
            "entry_close": closes[i],
            "entered": 0,
            "trade_id": None,
        })
        return snap

    async def _record_signals_phase(self):
        """Iterate ALL tracked pairs, persist any new CONSENSUS signal that
        fired on the latest closed bar. Idempotent (UNIQUE на symbol+ts+strat
        в БД). Не блокирует торговлю — отдельная фаза до scan/cycle веток."""
        if not self._tracked:
            return
        for sym in list(self._tracked):
            buf = self._candles.get(sym)
            if not buf or len(buf) < 200:
                continue
            last_closed_t = int(buf[-2]["time"])
            if last_closed_t <= self._last_signal_record_bar.get(sym, 0):
                continue
            self._last_signal_record_bar[sym] = last_closed_t
            try:
                meta = self._check_signal_with_meta(sym)
            except Exception:
                logger.exception("record_signals_phase: signal check failed for %s", sym)
                continue
            if not meta:
                continue
            action, sig, params, strat_name = meta
            try:
                snap = self._market_snapshot(sym, sig, params, buf[:-1], strat_name)
            except Exception:
                logger.exception("market_snapshot failed for %s", sym)
                continue
            if not snap:
                continue
            try:
                inserted = self.journal.insert_signal(snap)
                if inserted:
                    logger.debug("signal recorded: %s %s @%d total=%s",
                                 sym, snap["side"], snap["signal_ts"], snap.get("votes_total"))
            except Exception:
                logger.exception("insert_signal failed for %s", sym)

    async def _record_signals_broad_loop(self):
        """BROAD-аналитика: раз в 60 сек проходит по парам из _scan_pool которые
        НЕ в _tracked (т.е. на которых бот не торгует / нет live WS-кэша),
        REST-fetch свечей и записывает signals в БД если CONSENSUS сработал на
        последнем закрытом баре.

        Цель — аналитика идёт по ВСЕМ парам прошедшим scan, независимо от того
        проходит ли пара торговый фильтр. Это даёт broad pool данных для
        будущих переанализов («что было бы если торговать пары которые сейчас
        в blacklist»). Решает фундаментальную проблему предыдущей архитектуры
        где аналитика собиралась только по _tracked.

        Дёшево по нагрузке: ~30-40 REST fetch раз в 60с = 0.5-0.7 запросов/сек
        к PO. Гораздо меньше чем подписка на ticks для всех этих пар.

        Live tracked пары обрабатываются в _record_signals_phase (через live
        candle cache, real-time). Этот метод — fallback для broad pool.
        """
        from feed.history import fetch_candles as _fetch_candles
        while self._running:
            try:
                await asyncio.sleep(60)
                # BUG-FIX (re-review #2): tf и broad_history читаем ВНУТРИ loop —
                # если юзер изменит filter.tf или filter.history_candles через
                # Mini App, broad-loop подхватит на следующей итерации.
                tf = int(self.cfg.get("filter", {}).get("tf", 60))
                history_n = int(self.cfg.get("filter", {}).get("history_candles", 1000))
                # 250 свечей хватает для CONSENSUS (нужно ~200 для прогрева
                # индикаторов). Меньше нагрузки на REST чем 1000.
                broad_history = min(history_n, 250)
                # BUG-FIX (re-review #2): guard feed_ready — если WS down,
                # все fetch_candles будут падать тихо, бессмысленно тратить
                # CPU/сеть на 30+ failed REST-вызовов. Лучше пропустить итерацию.
                feed_ready = getattr(self.feed, "_ready", None)
                if feed_ready is not None and not feed_ready.is_set():
                    continue
                # Скан pool - tracked = пары для broad-record (не дублируем _record_signals_phase)
                broad_pool = list(self._scan_pool - self._tracked)
                if not broad_pool:
                    continue
                # NOTE: params для _check_signal_with_meta не передаются — он
                # сам подтянет их через self.registry. Поэтому здесь params
                # вычислять не нужно (раньше был dead code).

                # Ограничиваем параллельность чтобы не утопить PO
                sem = asyncio.Semaphore(5)
                async def _record_one(sym: str):
                    async with sem:
                        try:
                            candles = await _fetch_candles(self.feed, sym, period=tf, limit=broad_history)
                        except Exception as e:
                            # BUG-FIX (re-review #2): был silent return — мешало
                            # дебагу. Логируем на DEBUG чтобы можно было увидеть
                            # при необходимости (--log-level=DEBUG), но не спамим
                            # на INFO/WARN при штатных тайм-аутах PO.
                            logger.debug("broad: fetch_candles failed for %s: %s", sym, e)
                            return
                        if not candles or len(candles) < 200:
                            return
                        last_closed_t = int(candles[-2]["time"])
                        # Дедупликация — не пишем тот же бар дважды
                        if last_closed_t <= self._last_signal_record_bar.get(sym, 0):
                            return
                        self._last_signal_record_bar[sym] = last_closed_t
                        try:
                            # Запускаем стратегию (CONSENSUS или активная user strategy)
                            tmp_buf = self._candles.get(sym)
                            try:
                                # Подсовываем broad-pool candles в _candles чтобы
                                # _check_signal_with_meta мог работать (он берёт оттуда)
                                self._candles[sym] = candles
                                meta = self._check_signal_with_meta(sym)
                            finally:
                                # Восстанавливаем оригинальный кэш если был
                                if tmp_buf is not None:
                                    self._candles[sym] = tmp_buf
                                else:
                                    self._candles.pop(sym, None)
                        except Exception:
                            logger.exception("broad: signal check failed for %s", sym)
                            return
                        if not meta: return
                        action, sig, params_used, strat_name = meta
                        try:
                            snap = self._market_snapshot(sym, sig, params_used,
                                                          candles[:-1], strat_name)
                        except Exception:
                            return
                        if not snap: return
                        try:
                            self.journal.insert_signal(snap)
                        except Exception:
                            logger.exception("broad: insert_signal failed for %s", sym)
                await asyncio.gather(*[_record_one(s) for s in broad_pool])
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("record_signals_broad_loop crashed")

    async def _signals_retention_loop(self):
        """Раз в 24ч удаляет signals старше cfg.retention.signals_days."""
        while self._running:
            try:
                await asyncio.sleep(24 * 3600)
                days = int((self.cfg.get("retention") or {}).get("signals_days", 180))
                if days <= 0:
                    continue
                deleted = self.journal.signals_retention_cleanup(days)
                if deleted:
                    logger.info("retention: deleted %d signals older than %d days",
                                deleted, days)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("retention loop crashed")

    async def _signals_settle_loop(self):
        """Background task: каждые 30 сек ищет неосёдланные signals у которых
        signal_ts + 5*tf уже прошло, и считает exp_wins[w1..w5] по next 5 close.

        Источники свечей (по приоритету):
          1) self._candles[sym] — live cache, покрывает только последние ~N
             баров; работает для свежих сигналов (~до часа назад).
          2) feed.history.fetch_candles — REST fallback для старых сигналов
             (например после restart кэш пуст для давно прошедших сигналов).
          3) Если signal_ts > 24ч и оба источника дали < 5 баров — settle с
             пустым exp_wins=[null]*5, чтобы перестать пытаться (иначе они
             вечно висят в очереди и тормозят аналитику).
        """
        tf = int(self.cfg.get("filter", {}).get("tf", 60))
        from feed.history import fetch_candles as _fetch_candles
        while self._running:
            try:
                await asyncio.sleep(30)
                now = int(time.time())
                cutoff = now - 6 * tf
                rows = self.journal.pending_signals_to_settle(cutoff, limit=200)
                for r in rows:
                    sym = r["symbol"]
                    side = r["side"]
                    sig_ts = int(r["signal_ts"])
                    entry_close = float(r["entry_close"])
                    candles = self._candles.get(sym) or []
                    after = [c for c in candles if int(c["time"]) > sig_ts][:5]
                    if len(after) < 5:
                        # Fallback REST — пробуем для сигналов старше 5 минут
                        # (live cache обычно покрывает последние минут 20-30).
                        if sig_ts < now - 300:
                            try:
                                rest = await _fetch_candles(self.feed, sym, tf, limit=20)
                                if rest:
                                    after = [c for c in rest if int(c["time"]) > sig_ts][:5]
                            except Exception:
                                pass
                    if len(after) < 5:
                        # Старше 24ч — закрываем с пустым результатом, чтобы
                        # очередь pending не росла бесконечно.
                        if sig_ts < now - 86400:
                            self.journal.settle_signal(int(r["id"]), [None]*5)
                        continue
                    exp_wins = []
                    for c in after:
                        cl = float(c["close"])
                        if cl == entry_close:
                            exp_wins.append(None)
                        elif side == "call":
                            exp_wins.append(1 if cl > entry_close else 0)
                        else:
                            exp_wins.append(1 if cl < entry_close else 0)
                    self.journal.settle_signal(int(r["id"]), exp_wins)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("signals_settle_loop tick crashed")

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
        # BROAD pool — все пары что прошли базовый payout+categories фильтр.
        # Используется отдельным _record_signals_broad_loop для записи signals
        # независимо от того что пара allowed/banned/pause для торговли.
        self._scan_pool = set(scores.keys())

        # Detect total scan failure (WS dropped mid-iteration → all 0 candles).
        # Don't overwrite previous _pair_scores; retry sooner than 5 min default.
        if not scores:
            logger.warning("rescan: 0 pairs returned (WS hiccup?), retry in 60s")
            self._force_rescan = True
            await asyncio.sleep(60)
            return

        self._pair_scores = scores
        # Apply bans + temp_pauses + pauses. Все используют ту же bans таблицу,
        # отличаются только сроком истечения:
        #   BAN  — score.ban,         filter.ban_hours        (макс. минусов > N)
        #   TEMP — score.temp_pause,  filter.temp_pause_hours (обе проходимости провалены)
        #   PAUSE— score.pause,       filter.pause_minutes    (только проходимость последних)
        new_bans = 0
        new_temp = 0
        new_pauses = 0
        ban_hours = int(self.cfg["filter"].get("ban_hours", 12))
        # Backwards-compat: day_off_hours (старый ключ) → temp_pause_hours
        temp_pause_hours = int(self.cfg["filter"].get("temp_pause_hours",
                               self.cfg["filter"].get("day_off_hours", 6)))
        pause_minutes = int(self.cfg["filter"].get("pause_minutes",
                            int(self.cfg["filter"].get("pause_hours", 1)) * 60))
        for sym, s in scores.items():
            if s.ban and not self.journal.is_banned(sym):
                self.journal.ban(sym, hours=ban_hours, reason=s.reason)
                logger.info("BAN %s (%dh) — %s", sym, ban_hours, s.reason)
                new_bans += 1
            elif getattr(s, "temp_pause", False) and not self.journal.is_banned(sym):
                self.journal.ban(sym, hours=temp_pause_hours, reason=s.reason)
                logger.info("TEMP-PAUSE %s (%dh) — %s", sym, temp_pause_hours, s.reason)
                new_temp += 1
            elif s.pause and not self.journal.is_banned(sym):
                self.journal.ban(sym, minutes=pause_minutes, reason=s.reason)
                logger.info("PAUSE %s (%dmin) — %s", sym, pause_minutes, s.reason)
                new_pauses += 1
        if new_bans or new_temp or new_pauses:
            logger.info("applied %d bans + %d temp_pauses + %d pauses this scan",
                        new_bans, new_temp, new_pauses)

        # Build tracked set = currently allowed + not banned + payout>=min_payout
        min_payout = self.cfg["filter"]["min_payout"]
        wanted = {sym for sym, s in scores.items()
                  if s.allowed and not self.journal.is_banned(sym)
                  and int(self.feed.assets.get(sym, {}).get("payout", 0)) >= min_payout}

        # ВАЖНО (юзер): пара которая сейчас торгуется (current_pair в активном
        # цикле) НЕ ДОЛЖНА вылетать из tracked, даже если её оценка ухудшилась
        # на этом скане. Цикл должен довестись до WIN/stop_sum на этой паре.
        if self.state.current_pair and self.state.mg_step > 0:
            wanted.add(self.state.current_pair)
        # Stage 3: то же правило для parallel mode — пары с активными
        # циклами должны оставаться в _tracked чтобы live candles обновлялись
        # и _parallel_step мог искать сигнал для следующего MG-шага.
        if self.state.pair_cycles:
            for cycle_sym in self.state.pair_cycles.keys():
                wanted.add(cycle_sym)

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
        info = self.feed.assets.get(symbol) or {}
        payout = int(info.get("payout", 0))
        # Per-pair payout-порог: OTC → min_payout, обычные → min_payout_regular
        f_cfg = self.cfg.get("filter") or {}
        threshold = (int(f_cfg.get("min_payout", 0)) if info.get("is_otc")
                     else int(f_cfg.get("min_payout_regular", f_cfg.get("min_payout", 0)) or
                              f_cfg.get("min_payout", 0)))
        if payout < threshold: return False
        # Asset-category whitelist (e.g. only forex+crypto, no stocks/indices)
        allowed_cats = set((self.cfg.get("filter") or {}).get("asset_categories") or [])
        if allowed_cats:
            if categorize_symbol(symbol, self.feed.assets.get(symbol)) not in allowed_cats:
                return False
        # WR-based blacklist: пары с историческим WR ниже порога не торгуем.
        # Использует counterfactual WR из таблицы signals (exp_wins 2-бар).
        # Аналитика по этим парам ПРОДОЛЖАЕТ писаться (через _record_signals_broad_loop),
        # просто реальная торговля блокируется. Защита от ловушек DOTUSD-типа
        # где CONSENSUS даёт ложные сигналы на трендовых движениях.
        min_pair_wr = float(f_cfg.get("min_pair_wr_actual", 0) or 0)
        if min_pair_wr > 0:
            # Кэшируем расчёт WR на 10 минут чтобы не дёргать SQL на каждый тик
            cached = self._pair_wr_cache.get(symbol)
            now = time.time()
            if cached is None or now - cached[1] > 600:
                wr_val = self.journal.pair_wr_from_signals(
                    symbol, min_count=30, exp_bar_index=1, days_lookback=30,
                )
                self._pair_wr_cache[symbol] = (wr_val, now)
            else:
                wr_val = cached[0]
            # wr_val=None → данных мало, не блокируем (даём шанс)
            if wr_val is not None and wr_val < min_pair_wr:
                return False
        # NOTE: hour-whitelist filter был удалён в этапе 1 рефакторинга.
        # В этапе 2 будет переработан как часть новой Аналитики per-strategy.
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
        Then update mg_step / cycle as normal.

        Stage 3: при рестарте в parallel mode могут быть orphan pending_trade
        в pair_cycles. Реcurency resolution для них пока не реализован — но
        мы должны ОЧИСТИТЬ их при старте, иначе phase 1 будет вечно ждать
        (cycle.get('pending_trade') → True → skip). Очищаем + логируем.
        """
        # Stage 3 cleanup: clear orphaned parallel pending_trades
        if self.state.pair_cycles:
            orphaned = []
            for sym, cycle in list(self.state.pair_cycles.items()):
                if cycle.get("pending_trade"):
                    orphaned.append(sym)
                    cycle["pending_trade"] = None
            if orphaned:
                logger.warning(
                    "parallel: cleared %d orphaned pending_trades from pair_cycles "
                    "on resume (cannot recover after restart): %s",
                    len(orphaned), orphaned,
                )
                self._persist()

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
        # Этап 2: settlement loop для exp_wins по записанным signals.
        asyncio.create_task(self._signals_settle_loop(), name="signals_settle")
        asyncio.create_task(self._signals_retention_loop(), name="signals_retention")
        # Broad analytics — signals по парам в scan_pool но не торгуемым
        # (REST-based, раз в 60с). Гарантирует что аналитика идёт по всем
        # парам прошедшим базовый scan, не только активно торгуемым.
        asyncio.create_task(self._record_signals_broad_loop(), name="signals_broad")
        # Stage 2: per-pair × hour авто-оптимизация экспирации.
        # Раз в 4ч пересчитывает оптимальные 1-5 баров для каждой ячейки
        # (sym, hour) на основе counterfactual exp_wins. Бот в _open_and_track
        # читает таблицу и использует оптимум вместо дефолтной expiry_seconds.
        from strategy.expiry_optimizer import expiry_optimizer_loop
        asyncio.create_task(expiry_optimizer_loop(self.journal), name="expiry_optimizer")
        # Resume any in-flight trade interrupted by the previous restart
        await self._resume_pending_trade()

        await self._rescan_pairs()
        if not self._tracked:
            # Глобальный day_off убран в этапе 3+. Per-pair temp_pause выставляет
            # сама `_rescan_pairs` для пар провалявших обе проходимости. Если
            # tracked пуст — просто крутим main loop, рескан раз в минуту
            # автоматически добавит пары которые освободились из паузы.
            if self.feed.assets:
                logger.warning(
                    "initial scan: 0 tracked. Per-pair temp_pause/ban применены, "
                    "loop продолжается, рескан раз в минуту."
                )
                await self._notify(
                    "ℹ️ Стартовый скан: 0 подходящих пар. Часть в bans/pause/temp_pause "
                    "по правилам фильтра. Бот будет авто-переоценивать каждую минуту."
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

            # ── АНАЛИТИКА 24/7 ──
            # Запись signals + market snapshots должна идти НЕЗАВИСИМО от
            # paused/auto-pause/day-off — даже когда бот не торгует (выходные,
            # вне рабочих часов, ручная пауза), мы продолжаем собирать данные
            # для аналитики. Иначе пропадает критичная история на которой
            # юзер потом строит фильтры и принимает решения.
            try:
                await self._record_signals_phase()
            except Exception:
                logger.exception("record_signals_phase failed (always-on)")

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
                # ── Rescan даже в paused/waiting_resume ──
                # Иначе _pair_scores замерзает на старых score'ах (включая старые
                # значения max_losses_in_row, min_wr1 и т.д.), и UI показывает
                # «tracked»-пары которые уже должны быть забанены по новым
                # настройкам. Скан безопасен — не открывает сделок, только
                # обновляет _pair_scores + tracked-set + кладёт новые баны.
                now = time.time()
                if self._force_rescan or now - last_scan > 60:
                    self._force_rescan = False
                    last_scan = now
                    try:
                        await self._rescan_pairs()
                        self.journal.prune_bans()
                    except Exception:
                        logger.exception("rescan in paused/waiting failed")
                continue

            # ── daily-триггер авто-пересчёта base_amount ──
            # Проверяем КАЖДЫЙ тик (~1с), но `_check_daily_recalc_due()`
            # сам гарантирует не более 1 срабатывания в сутки. Если МГ идёт —
            # _recalc_base_from_balance отложит до WIN.
            if self._check_daily_recalc_due():
                try:
                    await self._recalc_base_from_balance(
                        f"ежедневный пересчёт ({self._last_daily_recalc_date})"
                    )
                except Exception:
                    logger.exception("daily auto-recalc failed")

            now = time.time()

            # NOTE: глобальный day_off механизм удалён в этапе 3+. Per-pair
            # temp_pause кладёт пары в bans с длительным сроком когда обе
            # проходимости провалены. Если есть legacy day_off_until в state —
            # снимаем его одноразово (миграция).
            if self.state.day_off_until:
                self.state.day_off_until = 0
                self._persist()
                logger.info("legacy day_off_until cleared (mechanism removed in stage 3+)")

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

                # Periodic rescan (every 60s) OR forced (e.g. assets arrived late,
                # /api/control/rescan, etc). Снижено с 300с → 60с для живого
                # отображения tracked-пар: payout у пар меняется в реальном
                # времени (через updateAssets WS-фрейм), но tracked-set обновлялся
                # только при rescan. С 60с задержка не превышает минуту, что
                # юзер уже не замечает в UI.
                if self._force_rescan or now - last_scan > 60:
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

                # NOTE: _record_signals_phase вызывается выше до skip-чеков,
                # чтобы аналитика работала 24/7 даже когда бот не торгует.

                # Branch: three modes
                #  • FREE: no active cycle → scan all pairs for first signal
                #  • IN-CYCLE LOCKED: cycle active + locked on a pair → wait
                #    for next bar on THAT pair
                #  • IN-CYCLE SEARCHING: cycle active but pair switched out
                #    (payout drop, max_trades hit) → scan ALL eligible pairs,
                #    first signal becomes new locked pair (preserves mg_step)
                #
                # Stage 3 (parallel mode): если trading.parallel_pairs=True →
                # независимые MG-циклы на нескольких парах одновременно.
                # Полностью отдельный путь _parallel_step (использует
                # state.pair_cycles вместо current_pair/mg_step).
                if bool((self.cfg.get("trading") or {}).get("parallel_pairs", False)):
                    await self._parallel_step()
                else:
                    # Legacy single-pair mode (backwards-compat default).
                    # WARN если есть orphan parallel cycles (юзер отключил
                    # parallel_pairs при активных циклах) — они не будут
                    # обрабатываться. Не удаляем автоматически (потенциальная
                    # потеря денег) — юзер должен явно /reset_cycle.
                    if self.state.pair_cycles and not self._warned_orphan_cycles:
                        logger.warning(
                            "parallel_pairs=False, но в state остались %d "
                            "активных pair_cycles: %s. Они НЕ обрабатываются "
                            "в legacy режиме. Включи parallel_pairs обратно "
                            "или сделай /reset_cycle.",
                            len(self.state.pair_cycles),
                            list(self.state.pair_cycles.keys()),
                        )
                        self._warned_orphan_cycles = True
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
            if not self._pair_matches_trade_mode(sym):
                continue  # отфильтровано по filter.trade_mode (OTC/regular/mixed)
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
        self.state.cycle_unused_carry = 0
        self.state.losses_streak_on_pair = 0
        self._persist()

        pre_balance_now = float(self.feed.balance() or 0.0)
        self._notify_open_async(sym, action, amt, 0, payout, pre_balance_now)

        await self._open_and_track(sym, action, amt)

    # ---------- helpers: pair_limits / position ----------
    def _mg_pair_limits(self) -> list[int]:
        mg = self.cfg.get("martingale") or {}
        raw = mg.get("pair_limits", [3, 3, 2])
        if isinstance(raw, str):
            try:
                raw = [int(x.strip()) for x in raw.split(",") if x.strip()]
            except Exception:
                raw = [3, 3, 2]
        if not isinstance(raw, list) or not raw:
            raw = [3, 3, 2]
        return [max(1, int(x)) for x in raw]

    def _mg_position(self) -> int:
        """0-based индекс текущей пары в ротации (= cycle_switches, но
        clamped к len(pair_limits)-1)."""
        limits = self._mg_pair_limits()
        return min(self.state.cycle_switches, len(limits) - 1)

    def _mg_is_last_pair(self) -> bool:
        limits = self._mg_pair_limits()
        return self.state.cycle_switches >= len(limits) - 1

    def _mg_current_pair_allowed(self) -> int:
        """Сколько перекрытий допустимо на текущей паре с учётом carry."""
        limits = self._mg_pair_limits()
        pos = self._mg_position()
        own = limits[pos]
        if self._mg_is_last_pair() and bool((self.cfg.get("martingale") or {}).get("carry_unused", True)):
            return own + int(self.state.cycle_unused_carry)
        return own

    # ---------- in-cycle step ----------
    async def _in_cycle_step(self):
        sym = self.state.current_pair

        # ── Гибкий MG (этап 2/3) — per-position limits + serie of losses ──
        # Архитектура: pair_limits = [3, 3, 2] = три пары в цикле (две смены).
        # На текущей паре (по позиции cycle_switches):
        #   - allowed = pair_limits[pos]; для последней — + cycle_unused_carry
        #   - триггеры switch (НЕ-последняя пара): pair_limit / consecutive_losses / payout_drop
        #   - на последней + last_pair_until_stop_sum: только stop_sum / cycle_total_limit
        mg_cfg = self.cfg.get("martingale") or {}
        is_last_pair = self._mg_is_last_pair()
        last_until_stop = bool(mg_cfg.get("last_pair_until_stop_sum", True))
        carry_unused = bool(mg_cfg.get("carry_unused", True))
        skip_pair_limits = is_last_pair and last_until_stop

        # 1. Лимит перекрытий на текущей паре (если не освобождена последней парой)
        if not skip_pair_limits:
            allowed = self._mg_current_pair_allowed()
            if self.state.trades_on_pair >= allowed:
                # used == limit, unused = 0, carry не растёт
                self.state.switched_pairs.append(sym)
                self.state.cycle_switches += 1
                self.state.current_pair = None
                self.state.trades_on_pair = 0
                self.state.losses_streak_on_pair = 0
                self._persist()
                await self._notify(
                    f"🔀 Лимит {allowed} перекрытий на {sym} исчерпан — "
                    f"переход на следующую пару. МГ-шаг {self.state.mg_step} сохранён."
                )
                return

        # NOTE: триггер «N минусов подряд» убран по запросу юзера.
        # Логика switch: только pair_limits (исчерпан лимит) + payout_drop.
        # Раньше тут была проверка consecutive_losses_switch.

        # 3. Payout drop (только на не-последней)
        if not skip_pair_limits:
            payout = int(self.feed.assets.get(sym, {}).get("payout", 0))
            floor = self.cfg["filter"]["payout_floor"]
            if payout < floor:
                if carry_unused:
                    pos_limit = self._mg_pair_limits()[self._mg_position()]
                    unused = max(0, pos_limit - self.state.trades_on_pair)
                    self.state.cycle_unused_carry += unused
                self.state.switched_pairs.append(sym)
                self.state.cycle_switches += 1
                self.state.current_pair = None
                self.state.trades_on_pair = 0
                self.state.losses_streak_on_pair = 0
                self._persist()
                await self._notify(
                    f"🔄 Payout {payout}% < {floor}% на {sym} → переход "
                    f"(резерв: {self.state.cycle_unused_carry} перекрытий). "
                    f"МГ-шаг {self.state.mg_step} сохранён."
                )
                return

        # 4. Stop-sum guardrail (всегда)
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
        # 5. Общий лимит цикла (cycle_total_limit = РОВНО N сделок).
        # mg_step считает сделки начиная с 0 (первая сделка = mg_step=0).
        # После N-й LOSS → mg_step=N. Проверка `>=` останавливает (N+1)-ю.
        total_limit = int(mg_cfg.get("cycle_total_limit",
                                      mg_cfg.get("max_steps", 10)))
        if self.state.mg_step >= total_limit:
            self.state.waiting_resume = True
            self._persist()
            await self._notify(
                f"🛑 Достигнут общий лимит цикла ({total_limit} сделок). Жду /resume."
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

        # Signal fixation (юзерская фича 2026-05-16):
        # • Пока сделка pending (в полёте) — другие сигналы игнорируются
        #   (это происходит ВЫШЕ через `if self.state.pending_trade`).
        # • После закрытия trade (LOSS) — следующий MG-шаг может быть в
        #   ЛЮБОМ направлении какое появится на новом баре. То есть direction
        #   цикла НЕ фиксируется глобально, только текущая сделка фиксирована
        #   на момент входа.
        # Если новый сигнал противоположный — это валидный новый вход,
        # обновляем direction и открываем сделку.
        if action != self.state.direction:
            logger.info("in-cycle: signal direction %s → %s on %s "
                        "(new bar after LOSS — следующий MG-шаг в новом направлении)",
                        self.state.direction, action, sym)
            self.state.direction = action
            self._persist()

        payout_in = int(self.feed.assets.get(sym, {}).get("payout", 0))
        pre_balance_now = float(self.feed.balance() or 0.0)
        self._notify_open_async(sym, action, next_amt, self.state.mg_step,
                                payout_in, pre_balance_now)
        await self._open_and_track(sym, action, next_amt)

    # ---------- in-cycle SEARCH (no pair locked, scan all eligible) ----------
    async def _in_cycle_search_step(self):
        """Active MG cycle but no pair locked (just switched away from previous).
        Scan ALL КАНДИДАТЫ для добивания цикла. First match wins → that pair
        becomes new current_pair, MG step preserved.

        В отличие от FREE-режима, в SEARCH мы УЖЕ потеряли деньги на предыдущей
        паре и должны их отбить. Поэтому фильтр **ослаблен**:
          • IGNORE: short pause (60-min, recent WR1 fail) и temp_pause (6h, обе
            проходимости провалены) — юзер: «проходимость последних свечей не
            должна влиять на поиск новых пар если уже сделка в работе»
          • KEEP: payout (чтобы вообще была выгода), score.ban (max_loss_streak —
            эти пары деструктивны системно, не отбивают), switched_pairs (уже
            использовались в этом цикле — не bounce'аем)
        """
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
        _mg = self.cfg.get("martingale") or {}
        total_limit = int(_mg.get("cycle_total_limit", _mg.get("max_steps", 10)))
        if self.state.mg_step >= total_limit:
            self.state.waiting_resume = True
            self._persist()
            await self._notify(
                f"🛑 Достигнут общий лимит цикла ({total_limit} сделок). Жду /resume."
            )
            return

        tf = self.cfg["filter"]["tf"]
        MAX_STALENESS = 25
        now_ts = int(time.time())
        f_cfg = self.cfg.get("filter") or {}
        min_payout = int(f_cfg.get("min_payout", 0))
        min_payout_regular = int(f_cfg.get("min_payout_regular", min_payout) or min_payout)

        # Расширенный candidate-set: ВСЕ пары из _pair_scores (не только tracked).
        # Включает pairs которые сейчас в pause/temp_pause — их recent WR1
        # провалена, но мы СОГЛАСНЫ это игнорить ради добивания цикла.
        candidates = list((self._pair_scores or {}).keys())
        if not candidates:
            return

        sorted_cands = sorted(
            candidates,
            key=lambda s: (
                (self._pair_scores.get(s).priority if self._pair_scores.get(s) else 999),
                -int((self.feed.assets.get(s) or {}).get("payout", 0)),
            ),
        )

        evaluated = 0
        new_bars = 0
        fired = None
        for sym in sorted_cands:
            # Skip pairs already used in this cycle (anti-bounce)
            if sym in self.state.switched_pairs:
                continue
            # Skip pairs не подходящие текущему режиму торговли (OTC/regular/mixed)
            if not self._pair_matches_trade_mode(sym):
                continue
            # Skip ТОЛЬКО жёсткий бан (max_loss_streak > N) — деструктивные пары
            score = self._pair_scores.get(sym)
            if score is None or score.ban:
                continue
            # NB: НЕ пропускаем score.pause / score.temp_pause / score.allowed=False —
            # в режиме SEARCH цикла нам важно добить, форма пары вторична.
            # Skip low payout — без выгоды нет смысла торговать.
            # Per-pair payout-порог: OTC vs regular.
            info = self.feed.assets.get(sym) or {}
            payout = int(info.get("payout", 0))
            threshold = min_payout if info.get("is_otc") else min_payout_regular
            if payout < threshold:
                continue
            # NOTE: hour-whitelist gate был удалён в этапе 1 рефакторинга.
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
                # Signal fixation: после LOSS бот может зайти в ЛЮБОМ направлении
                # на следующем сигнале (включая противоположное). direction цикла
                # не фиксируется глобально — фиксируется только сделка на момент
                # входа. Так что любой свежий сигнал принимаем.
                fired = (sym, action)
                break

        # Heartbeat log so we know the search is alive (mirrors _free_scan_step)
        logger.info("in-cycle SEARCH: tracked=%d eligible=%d new_bars=%d fired=%s mg_step=%d",
                    len(self._tracked), evaluated, new_bars, fired, self.state.mg_step)

        if not fired:
            return  # no signal anywhere — keep waiting

        sym, action = fired
        payout = int(self.feed.assets.get(sym, {}).get("payout", 0))

        # Lock onto this pair. Direction обновляем под текущий сигнал
        # (signal fixation = direction конкретной сделки, не цикла).
        self.state.current_pair = sym
        self.state.direction = action
        self.state.trades_on_pair = 0
        self._persist()

        pre_balance_now = float(self.feed.balance() or 0.0)
        self._notify_open_async(sym, action, next_amt, self.state.mg_step,
                                payout, pre_balance_now)
        await self._open_and_track(sym, action, next_amt)

    # ════════════════════════════════════════════════════════════════════
    # ─────────── STAGE 3: PARALLEL TRADING (max N pairs) ────────────────
    # ════════════════════════════════════════════════════════════════════
    # Параллельный режим: бот ведёт независимые MG-циклы на нескольких парах
    # одновременно, до trading.max_parallel_pairs штук. Каждая пара = отдельный
    # PairCycle в self.state.pair_cycles. Активируется через trading.parallel_pairs=True.
    # Legacy single-pair код (current_pair/mg_step) НЕ используется в этом режиме.

    def _parallel_amount_for_step(self, step: int) -> float:
        """Аналог _amount_for_step но для parallel mode. Идентично — base*coef^step."""
        return self._amount_for_step(step)

    async def _parallel_step(self):
        """Main loop tick для parallel mode. Выполняется при parallel_pairs=True
        вместо free_scan/in_cycle. Делает 2 вещи:
          1. Обрабатывает существующие циклы — открывает следующий шаг MG
             для каждой активной пары если на ней появился свежий сигнал.
          2. Если ёмкость есть (active < max_par) — ищет новые сигналы на
             tracked парах и открывает новые циклы.
        """
        # Migration guard: если перешли в parallel mode при активном legacy
        # цикле (current_pair + mg_step>0) — переносим его в pair_cycles
        # чтобы цикл не «тленел» без обработки. После миграции сбрасываем
        # legacy поля.
        if (self.state.current_pair and self.state.mg_step > 0
                and self.state.current_pair not in self.state.pair_cycles):
            logger.warning(
                "parallel migration: legacy cycle %s @ MG%d → pair_cycles",
                self.state.current_pair, self.state.mg_step,
            )
            # last_eval_bar: берём из _last_closed_bar_time legacy чтобы phase 1
                # сразу не открывал ещё одну сделку на том же баре где legacy
                # уже сделал свою попытку.
            cur_sym = self.state.current_pair
            self.state.pair_cycles[cur_sym] = {
                "direction": self.state.direction or "call",
                "mg_step": self.state.mg_step,
                "cycle_loss": 0.0,  # ← начинаем считать с 0; реальные потери уже в session_loss
                "pending_trade": self.state.pending_trade,
                "losses_streak": self.state.losses_streak_on_pair,
                "started_at": int(time.time()),
                "trades_count": self.state.trades_on_pair,
                "active": True,
                "last_eval_bar": int(self._last_closed_bar_time.get(cur_sym, 0)),
            }
            # Сбрасываем legacy поля чтобы они не мешали
            self.state.current_pair = None
            self.state.original_pair = None
            self.state.mg_step = 0
            self.state.pending_trade = None
            self.state.losses_streak_on_pair = 0
            self.state.trades_on_pair = 0
            self._persist()

        # Stop conditions
        if self.state.paused or self.state.waiting_resume:
            return
        # Global stop_sum guard (общий для всех parallel cycles).
        # stop_sum=0 → отключено (как и в legacy).
        stop_sum = float((self.cfg.get("martingale") or {}).get("stop_sum", 0) or 0)
        if stop_sum > 0 and self.state.session_loss >= stop_sum:
            self.state.waiting_resume = True
            self._persist()
            await self._notify(
                f"🛑 Достигнут stop_sum (${stop_sum:.2f}). Все parallel-циклы остановлены. "
                f"Жду /resume."
            )
            return

        # Defensive: clamp max_par >= 1 (юзер мог через API поставить 0)
        max_par = max(1, int((self.cfg.get("trading") or {})
                              .get("max_parallel_pairs", 3) or 3))
        cycle_limit = int((self.cfg.get("martingale") or {})
                            .get("cycle_total_limit", 5))

        # ── 1. Обрабатываем активные циклы (ищем сигнал для следующего MG-шага) ──
        # snapshot keys, потому что pair_cycles может меняться внутри (close)
        for sym in list(self.state.pair_cycles.keys()):
            cycle = self.state.pair_cycles.get(sym)
            if not cycle:
                continue
            # Если на этой паре сделка в полёте — ждём её результата
            if cycle.get("pending_trade"):
                continue
            # Если mg_step==0 — цикл только что закрылся WIN-ом (или новый, ещё
            # ничего не открывали). Удаляем закрытые (signal_at_open=False) — они уже
            # ушли через _on_trade_closed_parallel. Здесь не должны быть, но safety.
            if cycle.get("mg_step", 0) == 0 and not cycle.get("active"):
                # Свежесозданный цикл с mg_step=0 — будет открыт в фазе #2
                continue
            # mg_step > 0 — цикл активен, ждём сигнал на этой же паре чтобы
            # сделать следующее перекрытие.
            buf = self._candles.get(sym)
            if not buf or len(buf) < 200:
                continue
            last_closed_t = int(buf[-2]["time"])
            last_eval_t = cycle.get("last_eval_bar", 0)
            if last_closed_t <= last_eval_t:
                continue
            cycle["last_eval_bar"] = last_closed_t

            try:
                meta = self._check_signal_with_meta(sym)
            except Exception:
                logger.exception("parallel_step: signal check failed for %s", sym)
                continue
            if not meta:
                continue
            action, _sig, _params, _strat = meta

            # Signal fixation (юзерская фича 2026-05-16):
            # • Pending trade на этой паре уже отсеян выше (cycle.get("pending_trade"))
            # • После LOSS direction цикла НЕ фиксируется глобально — следующий
            #   MG-шаг может быть в ЛЮБОМ направлении какое появится на новом баре.
            # Обновляем direction цикла под текущий сигнал и продолжаем.
            cycle["direction"] = action

            # Проверяем что не превысили cycle_total_limit
            if cycle["mg_step"] >= cycle_limit:
                # Bust — этот цикл лопнул, баним пару на ban_hours
                ban_hours = int((self.cfg.get("filter") or {}).get("ban_hours", 12))
                try:
                    self.journal.ban(sym, hours=ban_hours,
                                      reason=f"parallel cycle_total_limit MG{cycle_limit}")
                except Exception:
                    logger.exception("parallel: ban after limit failed for %s", sym)
                logger.warning("parallel %s: cycle_total_limit (%d) reached → bust, removing",
                                sym, cycle_limit)
                # session_loss уже включал потери — НЕ удваиваем
                self._cleanup_pair_cycle(sym)
                continue

            next_amt = self._parallel_amount_for_step(cycle["mg_step"])
            payout = int((self.feed.assets.get(sym) or {}).get("payout", 0))
            pre_balance = float(self.feed.balance() or 0.0)
            self._notify_open_async(sym, action, next_amt, cycle["mg_step"],
                                    payout, pre_balance)
            await self._open_parallel_trade(sym, action, next_amt)

        # ── 2. Открываем новые циклы если есть ёмкость ──
        # ВНЕ рабочих часов НЕ открываем новые циклы — это согласуется с legacy
        # _free_scan_step (там тот же check). Существующие циклы в фазе 1
        # продолжают работать (как legacy _in_cycle_step) чтобы доработать до
        # WIN/bust. Auto-pause после закрытия ПОСЛЕДНЕГО цикла происходит в
        # _on_trade_closed_parallel WIN handler.
        if not self._within_working_hours():
            return
        active_count = len(self.state.pair_cycles)
        if active_count >= max_par:
            return
        if not self._tracked:
            return

        # Pre-cooldown — после WIN не сразу же открывать на той же паре
        # (избегаем «двойного входа» когда сигнал на той же свече).
        # Используем _last_signal_record_bar для дедупа.

        sorted_tracked = sorted(
            self._tracked,
            key=lambda s: (
                (self._pair_scores.get(s).priority if self._pair_scores.get(s) else 999),
                -int((self.feed.assets.get(s) or {}).get("payout", 0)),
            ),
        )
        for sym in sorted_tracked:
            if active_count >= max_par:
                break
            # Пара уже в активном цикле — пропускаем
            if sym in self.state.pair_cycles:
                continue
            if not self._pair_matches_trade_mode(sym):
                continue
            if not self._eligible_for_new_cycle(sym):
                continue
            buf = self._candles.get(sym)
            if not buf or len(buf) < 200:
                continue
            last_closed_t = int(buf[-2]["time"])
            # Использует общий _last_closed_bar_time чтобы не открывать дважды на одной свече
            if last_closed_t <= self._last_closed_bar_time.get(sym, 0):
                continue
            self._last_closed_bar_time[sym] = last_closed_t

            try:
                meta = self._check_signal_with_meta(sym)
            except Exception:
                logger.exception("parallel_step: free-scan signal check failed for %s", sym)
                continue
            if not meta:
                continue
            action, _sig, _params, _strat = meta

            # Открываем новый цикл
            self.state.pair_cycles[sym] = {
                "direction": action,
                "mg_step": 0,
                "cycle_loss": 0.0,
                "pending_trade": None,
                "losses_streak": 0,
                "started_at": int(time.time()),
                "trades_count": 0,
                "active": True,
                "last_eval_bar": last_closed_t,
            }
            self._persist()
            amount = self._parallel_amount_for_step(0)
            payout = int((self.feed.assets.get(sym) or {}).get("payout", 0))
            pre_balance = float(self.feed.balance() or 0.0)
            logger.info("parallel: NEW cycle started on %s (action=%s, base=$%.2f) "
                        "[%d/%d active]",
                        sym, action, amount, active_count + 1, max_par)
            self._notify_open_async(sym, action, amount, 0, payout, pre_balance)
            await self._open_parallel_trade(sym, action, amount)
            active_count += 1

    async def _open_parallel_trade(self, sym: str, action: str, amount: float):
        """Аналог _open_and_track но для parallel mode. Записывает pending_trade
        в pair_cycles[sym]. Возвращается сразу после успешного открытия — close
        отслеживается фоновой задачей _watch_parallel_close (fire-and-forget),
        чтобы _parallel_step мог продолжать работу с другими циклами.
        """
        if not self._pair_matches_trade_mode(sym):
            logger.warning("BLOCKED parallel open: %s не соответствует trade_mode", sym)
            return
        cycle = self.state.pair_cycles.get(sym)
        if not cycle:
            logger.error("parallel: pair_cycles[%s] missing на открытии trade — abort", sym)
            return
        # Double-check paused/waiting (бот мог быть приостановлен между phase 2
        # detection и непосредственным open call).
        if self.state.paused or self.state.waiting_resume:
            logger.info("parallel: skip open %s — paused/waiting_resume", sym)
            return

        # Auto-expiry (как в _open_and_track)
        expiry = int(self.cfg["trading"]["expiry_seconds"])
        tf_sec = int(self.cfg["filter"].get("tf", 60))
        if (self.cfg.get("filter") or {}).get("auto_expiry_enabled", True):
            try:
                from strategy.expiry_optimizer import resolve_expiry_bars
                try:
                    tz_name = (self.cfg.get("telegram") or {}).get(
                        "daily_report_timezone") or "Europe/Kyiv"
                    tz = pytz.timezone(tz_name)
                    current_hour = datetime.fromtimestamp(int(time.time()), tz=tz).hour
                except Exception:
                    current_hour = datetime.utcfromtimestamp(int(time.time())).hour
                opt = resolve_expiry_bars(self.journal, sym, current_hour)
                if opt:
                    optimum_bars = int(opt["bars"])
                    new_expiry = optimum_bars * tf_sec
                    if new_expiry != expiry:
                        logger.info("parallel auto-expiry: %s @%dh → %d bars "
                                    "(was %ds → %ds, src=%s)",
                                    sym, current_hour, optimum_bars,
                                    expiry, new_expiry, opt["source"])
                        expiry = new_expiry
            except Exception:
                logger.debug("parallel auto-expiry lookup failed for %s", sym)

        # Subscribe (idempotent) — КРИТИЧНО: должна работать, иначе live ticks
        # не пойдут → close event не дойдёт до _closed_deals_index → trade
        # никогда не закроется. Abort если subscribe упал.
        try:
            if hasattr(self.feed, "subscribe"):
                await self.feed.subscribe(sym, int(self.cfg["filter"]["tf"]))
        except Exception:
            logger.exception("parallel: subscribe failed for %s — abort open", sym)
            return  # не открываем trade без подписки на live ticks

        # CRITICAL: capture balance BEFORE tc.open_trade (как в legacy)
        pre_balance = float(self.feed.balance() or 0.0)
        try:
            opened = await self.tc.open_trade(sym, amount, action, expiry)
        except Exception:
            logger.exception("parallel: open_trade exception %s", sym)
            return
        if not opened:
            logger.warning("parallel: open_trade returned None/False for %s", sym)
            return

        # Записываем pending_trade в cycle (с trade_id для recovery)
        cycle["pending_trade"] = {
            "asset": sym,
            "action": action,
            "amount": float(amount),
            "pre_balance": pre_balance,
            "open_ts": int(time.time()),
            "expiry_sec": int(expiry),
            "trade_id": opened.trade_id,
        }
        self._persist()
        logger.info("parallel OPEN %s %s $%s exp=%ss trade_id=%s",
                    sym, action, amount, expiry, opened.trade_id)

        # Mark signal entered (для аналитики связи signal→trade)
        try:
            buf = self._candles.get(sym) or []
            if len(buf) >= 2:
                signal_ts = int(buf[-2]["time"])
                strat_name = (self.registry.get_active().name
                               if self.registry else "consensus")
                self.journal.mark_signal_entered(
                    sym, signal_ts, str(strat_name), str(opened.trade_id),
                )
        except Exception:
            logger.exception("parallel: mark_signal_entered failed for %s", sym)

        # CRITICAL: spawn fire-and-forget task to watch for close event.
        # Без этого trade откроется но НИКОГДА не закроется (callbacks PO только
        # пишут в _closed_deals_index, не вызывают _on_trade_closed напрямую).
        asyncio.create_task(
            self._watch_parallel_close(sym, opened, amount, pre_balance, expiry),
            name=f"parallel_close_{sym}_{int(time.time())}",
        )

    async def _watch_parallel_close(self, sym: str, opened, amount: float,
                                     pre_balance: float, expiry: int):
        """Фоновая задача: ждёт expiry+2с, потом polls _closed_deals_index за
        результатом, конструирует ClosedTrade и вызывает _on_trade_closed.
        Логика идентична last части legacy _open_and_track."""
        try:
            await asyncio.sleep(expiry + 2)
            # Defensive: opened.open_time может быть None если tc.open_trade
            # вернул OpenedTrade с timestamp=None (PO иногда так делает).
            # Fallback на время открытия trade (now - expiry - 2).
            open_ts = opened.open_time or (int(time.time()) - expiry - 2)
            deal = None
            for _ in range(15):
                for dt in range(-2, 3):
                    deal = self._closed_deals_index.get(f"{sym}:{open_ts + dt}")
                    if deal:
                        break
                if deal:
                    break
                await asyncio.sleep(1.0)

            if deal and "profit" in deal:
                profit_raw = float(deal.get("profit") or 0)
                if profit_raw > 0.01:
                    result = "WIN"
                    profit = profit_raw + amount
                elif profit_raw < -0.01:
                    result = "LOSS"
                    profit = 0.0
                else:
                    result = "DRAW"
                    profit = amount
                post_balance = float(self.feed.balance() or 0.0)
                delta = round(post_balance - pre_balance, 2)
                logger.info("parallel CLOSE event %s: profit_raw=%.2f → %s",
                            sym, profit_raw, result)
            else:
                # Fallback на balance-delta (как legacy)
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
                logger.warning("parallel CLOSE fallback %s: delta=%.2f → %s",
                                sym, delta, result)

            closed = ClosedTrade(
                trade_id=opened.trade_id,
                asset=opened.asset,
                action=opened.action,
                amount=opened.amount,
                profit=profit,
                result=result,
                close_time=int(time.time()),
                raw={"pre_balance": pre_balance, "post_balance": post_balance,
                      "delta": delta, "parallel": True},
            )
            await self._on_trade_closed(opened, closed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("parallel: _watch_parallel_close crashed for %s", sym)

    def _cleanup_pair_cycle(self, sym: str):
        """Удаляет cycle из pair_cycles после WIN/bust."""
        if sym in self.state.pair_cycles:
            del self.state.pair_cycles[sym]
            self._persist()

    async def _on_trade_closed_parallel(self, opened, closed):
        """Parallel-mode обработка закрытия сделки. Обновляет ТОЛЬКО state
        своего pair_cycle, остальные циклы не трогает.

        Глобальный state (session_loss, base_amount recalc) обновляется как
        обычно — это нормально, депо одно на всех.
        """
        sym = opened.asset
        cycle = self.state.pair_cycles.get(sym)
        if not cycle:
            logger.warning("parallel close: no cycle for %s — falling back to legacy", sym)
            return False  # caller fallback to legacy

        # Trade resolved
        cycle["pending_trade"] = None
        cycle["trades_count"] = cycle.get("trades_count", 0) + 1
        open_ts_actual = opened.open_time or int(time.time())
        self.journal.log_trade({
            "trade_id": closed.trade_id,
            "symbol": opened.asset,
            "action": opened.action,
            "amount": opened.amount,
            "profit": closed.profit,
            "result": closed.result,
            "payout": opened.payout,
            "mg_step": cycle.get("mg_step", 0),
            "open_ts": open_ts_actual,
            "close_ts": closed.close_time or int(time.time()),
            "balance_after": self.feed.balance(),
            "mode": self.cfg["mode"],
        })

        bal_after = float(self.feed.balance() or 0.0)
        prev_step = cycle.get("mg_step", 0)

        # Live loss-streak (общий с legacy — используем _live_loss_streak dict)
        if closed.result == "WIN":
            self._live_loss_streak.pop(sym, None)
        elif closed.result == "LOSS":
            self._live_loss_streak[sym] = self._live_loss_streak.get(sym, 0) + 1

        if closed.result == "WIN":
            gained = closed.profit - opened.amount
            self.state.session_loss = max(0.0, self.state.session_loss - gained)
            # Закрываем цикл — удаляем pair_cycle
            self._cleanup_pair_cycle(sym)
            self._persist()
            # Auto-recalc base_amount (если включено)
            self._cycles_since_recalc += 1
            ab_cfg = (self.cfg.get("trading", {}).get("auto_base_amount") or {})
            try:
                if ab_cfg.get("enabled", True) and self._pending_recalc_reason:
                    await self._recalc_base_from_balance(self._pending_recalc_reason)
                elif ab_cfg.get("enabled", True):
                    every_n = int(ab_cfg.get("every_n_cycles", 0) or 0)
                    if every_n > 0 and self._cycles_since_recalc >= every_n:
                        await self._recalc_base_from_balance(
                            f"каждые {every_n} циклов (parallel WIN на {sym})"
                        )
            except Exception:
                logger.exception("parallel: auto-recalc on WIN failed")
            await self._notify(
                f"✅ <b>WIN {sym}</b> (parallel) +${gained:.2f}\n"
                f"💼 Баланс: ${bal_after:.2f}   📉 Сессия: ${self.state.session_loss:.2f}\n"
                f"🔄 Цикл на {sym} закрыт (был MG{prev_step}). "
                f"Активных циклов: {len(self.state.pair_cycles)}"
            )
            # Auto-pause при закрытии последнего цикла в нерабочие часы.
            # Согласуется с legacy: после WIN-цикла если рабочее окно закрылось →
            # paused=True до утра. В parallel mode проверяем когда последний
            # cycle закрылся (pair_cycles пуст).
            if not self.state.pair_cycles and not self._within_working_hours():
                self.state.paused = True
                self.state.auto_paused_schedule = True
                self._persist()
                await self._notify(
                    "🌙 Все parallel-циклы закрыты, рабочее окно закончилось. "
                    "Жду рабочее окно (или /resume)."
                )
        elif closed.result == "LOSS":
            self.state.session_loss += opened.amount
            cycle["cycle_loss"] = cycle.get("cycle_loss", 0.0) + opened.amount
            cycle["losses_streak"] = cycle.get("losses_streak", 0) + 1
            # Stage 3: live-streak защита (как в legacy). Если на этой паре
            # серия LOSS превысила исторический max_loss_streak_to_win × multiplier
            # → temp-pause через bans (отдельно от cycle_total_limit).
            try:
                await self._maybe_pause_on_live_streak(sym)
            except Exception:
                logger.exception("parallel: live-streak check failed for %s", sym)
            mg_enabled = bool(self.cfg["martingale"].get("enabled", True))
            cycle_limit = int((self.cfg.get("martingale") or {})
                                .get("cycle_total_limit", 5))
            if mg_enabled:
                cycle["mg_step"] = cycle.get("mg_step", 0) + 1
                self._persist()
                if cycle["mg_step"] >= cycle_limit:
                    # Bust — удаляем cycle И баним пару на ban_hours чтобы не
                    # открыть на ней сразу следующий цикл (та же причина что
                    # legacy при стоп-сумме — пара показала себя плохо).
                    ban_hours = int((self.cfg.get("filter") or {}).get("ban_hours", 12))
                    try:
                        self.journal.ban(sym, hours=ban_hours,
                                          reason=f"parallel bust MG{cycle_limit}, "
                                                 f"cycle_loss=${cycle['cycle_loss']:.2f}")
                    except Exception:
                        logger.exception("parallel: ban after bust failed for %s", sym)
                    await self._notify(
                        f"💥 <b>BUST {sym}</b> (parallel) — достигнут лимит MG{cycle_limit}\n"
                        f"💼 Баланс: ${bal_after:.2f}   📉 Cycle loss: ${cycle['cycle_loss']:.2f}\n"
                        f"🚫 Пара забанена на {ban_hours}ч. Активных циклов: "
                        f"{len(self.state.pair_cycles)-1}"
                    )
                    self._cleanup_pair_cycle(sym)
                else:
                    await self._notify(
                        f"❌ <b>LOSS {sym}</b> (parallel) -${opened.amount:.2f}\n"
                        f"💼 Баланс: ${bal_after:.2f}   📉 Сессия: ${self.state.session_loss:.2f}\n"
                        f"📈 MG-шаг → MG{cycle['mg_step']} на {sym}"
                    )
            else:
                # MG отключён — закрываем цикл
                self._cleanup_pair_cycle(sym)
                self._persist()
                await self._notify(
                    f"❌ <b>LOSS {sym}</b> (parallel, no MG) -${opened.amount:.2f}\n"
                    f"💼 Баланс: ${bal_after:.2f}"
                )
        else:  # DRAW
            # DRAW в parallel — refund, mg_step не меняется, цикл не закрывается
            self._persist()
            await self._notify(
                f"➖ <b>DRAW {sym}</b> (parallel)\n"
                f"💼 Баланс: ${bal_after:.2f}   ↻ MG{cycle.get('mg_step', 0)} остаётся."
            )
        return True

    # ════════════════════════════════════════════════════════════════════
    # END Stage 3 parallel methods
    # ════════════════════════════════════════════════════════════════════

    # ---------- trade open / close flow ----------
    async def _open_and_track(self, sym: str, action: str, amount: float):
        # Defense-in-depth: на случай если вход прошёл через нестандартный путь
        # (manual switch, кастомная стратегия и т.д.) ещё раз проверим что пара
        # подходит trade_mode. Основная фильтрация уже в _free_scan_step /
        # _in_cycle_search_step — этот guard страхует от регрессий.
        if not self._pair_matches_trade_mode(sym):
            logger.warning(
                "BLOCKED open: %s не соответствует trade_mode=%s — abort",
                sym, (self.cfg.get("filter") or {}).get("trade_mode"),
            )
            return
        expiry = int(self.cfg["trading"]["expiry_seconds"])
        tf_sec = int(self.cfg["filter"].get("tf", 60))
        # Per-pair × hour экспирация (Stage 2 авто-оптимизация).
        # Бот раз в 4ч пересчитывает оптимум 1-5 баров для каждой ячейки
        # (sym, hour). Если есть данные — используем оптимум, иначе fallback
        # на дефолтную expiry_seconds. Управляется через
        # filter.auto_expiry_enabled (default true).
        if (self.cfg.get("filter") or {}).get("auto_expiry_enabled", True):
            try:
                from strategy.expiry_optimizer import resolve_expiry_bars
                # КРИТИЧНО: hour_local в БД signals хранится в LOCAL TZ (Kyiv/Helsinki),
                # не в UTC. Должны использовать ту же TZ что _market_snapshot:
                #   tz_name = telegram.daily_report_timezone (default Europe/Kyiv).
                # Иначе lookup всегда промахивается (UTC=18 vs Kyiv=20).
                try:
                    tz_name = (self.cfg.get("telegram") or {}).get(
                        "daily_report_timezone") or "Europe/Kyiv"
                    tz = pytz.timezone(tz_name)
                    current_hour = datetime.fromtimestamp(int(time.time()), tz=tz).hour
                except Exception:
                    current_hour = datetime.utcfromtimestamp(int(time.time())).hour
                # Двухуровневый fallback: сначала (sym, hour) ≥8 сигналов,
                # потом per-pair overall ≥30, иначе дефолт из config.
                opt = resolve_expiry_bars(self.journal, sym, current_hour)
                if opt:
                    optimum_bars = int(opt["bars"])
                    new_expiry = optimum_bars * tf_sec
                    if new_expiry != expiry:
                        logger.info(
                            "auto-expiry: %s @%dh → %d bars (was %ds → %ds, WR=%s%%, N=%d, src=%s)",
                            sym, current_hour, optimum_bars,
                            expiry, new_expiry, opt["wr"], opt["n"], opt["source"],
                        )
                        expiry = new_expiry
            except Exception:
                logger.exception("auto-expiry lookup failed for %s (using default)", sym)
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

        # Этап 2: связать только что открытую сделку с записью в `signals`.
        # `_record_signals_phase` уже сохранил сигнал на этом баре. Берём
        # signal_ts напрямую из cached candles (а не из `_last_signal_info`),
        # чтобы не зависеть от состояния промежуточных кэшей и любых
        # исключений в `_record_signals_phase`. Strategy name — текущая
        # активная (т.е. та же что использовалась в _check_signal).
        try:
            buf = self._candles.get(sym) or []
            if len(buf) >= 2:
                signal_ts = int(buf[-2]["time"])
                strat_name = (self.registry.get_active().name
                               if self.registry else "consensus")
                self.journal.mark_signal_entered(
                    sym, signal_ts, str(strat_name), str(opened.trade_id),
                )
        except Exception:
            logger.exception("mark_signal_entered failed for %s", sym)

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
        # Stage 3: если parallel_pairs включён И эта пара в pair_cycles —
        # обрабатываем как parallel close, не трогаем legacy state.
        if bool((self.cfg.get("trading") or {}).get("parallel_pairs", False)):
            try:
                handled = await self._on_trade_closed_parallel(opened, closed)
                if handled:
                    return
            except Exception:
                logger.exception("_on_trade_closed_parallel failed — falling back to legacy")
        # ── Legacy single-pair handling ──
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
        # Этап 3: постоянная пере-фильтрация в реальном времени. На КАЖДОЙ
        # закрытой сделке (не только LOSS, не раз в час!) — заново классифицируем
        # текущую пару на cached candles и обновляем _pair_scores. Если пара
        # перестала проходить фильтр — баним (если max_losses_in_row превышен)
        # или переходим в SEARCH.
        if self.state.current_pair:
            try:
                self._reclassify_current_pair_now()
            except Exception:
                logger.exception("real-time reclassify failed")
            try:
                self._verify_current_pair_still_passes()
            except Exception:
                logger.exception("re-filter on close failed")

        bal_after = float(self.feed.balance() or 0.0)
        prev_step = self.state.mg_step
        # ── live loss-streak (см. self._live_loss_streak в __init__) ──
        sym_for_streak = opened.asset
        if closed.result == "WIN":
            self._live_loss_streak.pop(sym_for_streak, None)
        elif closed.result == "LOSS":
            self._live_loss_streak[sym_for_streak] = self._live_loss_streak.get(sym_for_streak, 0) + 1
        # DRAW — не трогаем.

        if closed.result == "WIN":
            gained = closed.profit - opened.amount
            self.state.session_loss = max(0.0, self.state.session_loss - gained)
            self.state.losses_streak_on_pair = 0
            self._reset_cycle()
            self._persist()
            # ── авто-пересчёт base_amount (если включён) ──
            self._cycles_since_recalc += 1
            ab_cfg = (self.cfg.get("trading", {}).get("auto_base_amount") or {})
            try:
                # 1) Отложенный пересчёт (например, daily-триггер сработал во время МГ)
                if ab_cfg.get("enabled", True) and self._pending_recalc_reason:
                    await self._recalc_base_from_balance(self._pending_recalc_reason)
                # 2) Триггер «каждые N циклов»
                elif ab_cfg.get("enabled", True):
                    every_n = int(ab_cfg.get("every_n_cycles", 0) or 0)
                    if every_n > 0 and self._cycles_since_recalc >= every_n:
                        await self._recalc_base_from_balance(
                            f"каждые {every_n} циклов (счётчик={self._cycles_since_recalc})"
                        )
            except Exception:
                logger.exception("auto-recalc on WIN failed")
            base_msg = (
                f"✅ <b>WIN {opened.asset}</b>  +${gained:.2f}\n"
                f"💼 Баланс: ${bal_after:.2f}   📉 Потери сессии: ${self.state.session_loss:.2f}\n"
                f"🔄 МГ сброшен (был шаг {prev_step})"
            )
            if not self._within_working_hours():
                self.state.paused = True
                self.state.auto_paused_schedule = True
                self._persist()
                await self._notify(
                    base_msg + "\n\n🌙 Закрыл цикл — пора отдыхать. "
                    "Жду рабочее окно (или /resume)."
                )
            else:
                await self._notify(base_msg + "\n🔍 Возвращаюсь в поиск.")
        elif closed.result == "LOSS":
            self.state.session_loss += opened.amount
            self.state.losses_streak_on_pair += 1
            mg_enabled = bool(self.cfg["martingale"].get("enabled", True))
            if mg_enabled:
                self.state.mg_step += 1
                self._persist()
                await self._notify(
                    f"❌ <b>LOSS {opened.asset}</b>  -${opened.amount:.2f}\n"
                    f"💼 Баланс: ${bal_after:.2f}   📉 Потери сессии: ${self.state.session_loss:.2f}\n"
                    f"📈 МГ-шаг → MG{self.state.mg_step}"
                )
            else:
                self._reset_cycle()
                self._persist()
                await self._notify(
                    f"❌ <b>LOSS {opened.asset}</b>  -${opened.amount:.2f}\n"
                    f"💼 Баланс: ${bal_after:.2f}   📉 Потери сессии: ${self.state.session_loss:.2f}\n"
                    f"🚫 МГ выключен — ищу новый сигнал."
                )
            # Доп. защита: если live-серия LOSS на этой паре превысила
            # исторический max_loss_streak_to_win × multiplier — ставим
            # пару на временную паузу (через bans). Не падаем при ошибке.
            try:
                await self._maybe_pause_on_live_streak(sym_for_streak)
            except Exception:
                logger.exception("live loss-streak check failed for %s", sym_for_streak)
            self._tick_event.set()
        else:  # DRAW
            # DRAW не обнуляет streak (это «нейтральный» исход) — но и не
            # увеличивает. Логика консистентна с экспирацией: refund != loss.
            self._persist()
            await self._notify(
                f"➖ <b>DRAW {opened.asset}</b>\n"
                f"💼 Баланс: ${bal_after:.2f}   ↻ Повторяю тот же шаг (MG{self.state.mg_step})."
            )
            self._tick_event.set()

    # ─── Авто-пересчёт base_amount от живого баланса ───────────────────
    def _persist_setting_override(self, key: str, value):
        """Записать setting в journal.settings_overrides (как делает
        PUT /api/settings). Гарантирует что значение переживёт деплой
        (config.yaml перезаписывается при git pull, а journal на volume)."""
        try:
            overrides = self.journal.get("settings_overrides") or {}
            overrides[key] = value
            self.journal.set("settings_overrides", overrides)
        except Exception:
            logger.exception("persist setting override %s failed", key)

    def _is_safe_for_recalc(self) -> bool:
        """Безопасно ли применить пересчёт ставки прямо сейчас?
        Можно только когда цикл закрыт И нет открытой/pending-сделки.

        Stage 3: также проверяем что нет активных parallel-циклов. Recalc
        меняет base_amount, и если в этот момент в parallel-цикле уже
        прошли LOSS-ы, следующие MG-шаги будут считаться от НОВОЙ базы —
        recovery математика ломается."""
        if self.state.mg_step != 0:
            return False
        if self.state.pending_trade:
            return False
        if self.state.waiting_resume:
            return False
        # Stage 3: блокируем recalc если есть активные parallel-циклы
        # (mg_step>0 ИЛИ есть pending_trade в любом цикле)
        for cycle in (self.state.pair_cycles or {}).values():
            if cycle.get("mg_step", 0) > 0 or cycle.get("pending_trade"):
                return False
        return True

    async def _recalc_base_from_balance(self, reason: str) -> bool:
        """Пересчитать trading.base_amount от живого баланса.
        Если небезопасно сейчас — отложить (будет применено после WIN).
        Возвращает True если применили, False иначе.
        """
        ab_cfg = (self.cfg.get("trading", {}).get("auto_base_amount") or {})
        if not bool(ab_cfg.get("enabled", True)):
            return False

        # Защита: только в чистом состоянии. Если МГ активен — отложим.
        if not self._is_safe_for_recalc():
            self._pending_recalc_reason = reason
            logger.info("recalc deferred (mg_step=%d, pending=%s): %s",
                        self.state.mg_step, bool(self.state.pending_trade), reason)
            return False

        mg_cfg = self.cfg.get("martingale") or {}
        N = int(mg_cfg.get("cycle_total_limit", mg_cfg.get("max_steps", 7)))
        q = float(mg_cfg.get("coefficient", 2.1))
        min_amount = float(ab_cfg.get("min_amount", 1.0))
        if N < 1 or q <= 1:
            logger.warning("recalc skipped: invalid N=%s or q=%s", N, q)
            return False

        balance = float(self.feed.balance() or 0.0)
        # NEW: процент от баланса который выделяется НА ОДИН ЦИКЛ.
        # default 100 = используется весь баланс (старое поведение, backwards-compat).
        # Юзер может выставить 10-14% чтобы каждый цикл рисковал только частью депо.
        # Полезно особенно в parallel mode где N циклов одновременно: 3 цикла × 14% = 42% риска.
        balance_pct = float(ab_cfg.get("balance_pct", 100) or 100)
        balance_pct = max(1.0, min(100.0, balance_pct))   # clamp 1..100
        effective_budget = balance * balance_pct / 100.0
        sum_factor = (q ** N - 1.0) / (q - 1.0)
        raw_base = effective_budget / sum_factor if sum_factor > 0 else 0.0
        # floor до десятых: 3.347 → 3.3
        new_base = math.floor(raw_base * 10.0) / 10.0
        old_base = float(self.cfg.get("trading", {}).get("base_amount", 1.0))

        if new_base < min_amount:
            # Ставка слишком мала — НЕ применяем, шлём алерт.
            # min_balance_needed считается с учётом процента: нужен такой полный
            # баланс, чтобы (balance × pct/100) ≥ min_amount × sum_factor.
            min_balance_needed = (min_amount * sum_factor * 100.0) / balance_pct
            logger.warning(
                "recalc rejected: new_base=$%.2f < min=$%.2f (balance=$%.2f, N=%d, q=%.2f)",
                new_base, min_amount, balance, N, q,
            )
            try:
                pct_note = (f" (бюджет {balance_pct:.0f}% = ${effective_budget:.2f})"
                              if balance_pct < 100 else "")
                await self._notify(
                    f"⛔ <b>Авто-пересчёт ставки отклонён</b>\n"
                    f"Расчёт: ${new_base:.2f} &lt; мин ${min_amount:.2f}\n"
                    f"Баланс ${balance:.2f}{pct_note}, цикл N={N}, q={q}\n"
                    f"Базовая осталась <b>${old_base:.2f}</b>.\n"
                    f"Для запуска нужен баланс ≥ <b>${min_balance_needed:.2f}</b> "
                    f"либо уменьшить N/q/процент бюджета.\n"
                    f"<i>Триггер: {reason}</i>"
                )
            except Exception:
                pass
            # Сбрасываем счётчик циклов всё равно — иначе на каждый WIN будет повтор алерта.
            self._cycles_since_recalc = 0
            self._pending_recalc_reason = None
            return False

        if abs(new_base - old_base) < 1e-6:
            # Без изменений — тихо сбрасываем триггеры, без TG-спама.
            self._cycles_since_recalc = 0
            self._pending_recalc_reason = None
            return False

        # Применяем
        self.cfg.setdefault("trading", {})["base_amount"] = new_base
        self._persist_setting_override("trading.base_amount", new_base)
        self._cycles_since_recalc = 0
        self._pending_recalc_reason = None

        delta = new_base - old_base
        sign = "📈" if delta > 0 else "📉"
        try:
            pct_note = (f" → бюджет {balance_pct:.0f}% = ${effective_budget:.2f}"
                         if balance_pct < 100 else "")
            await self._notify(
                f"🧮 <b>Авто-пересчёт ставки</b> {sign}\n"
                f"${old_base:.2f} → <b>${new_base:.2f}</b>\n"
                f"Баланс ${balance:.2f}{pct_note}, цикл N={N}, q={q}\n"
                f"<i>Триггер: {reason}</i>"
            )
        except Exception:
            pass
        logger.info("recalc applied: %.2f → %.2f (balance=%.2f, reason=%s)",
                    old_base, new_base, balance, reason)
        return True

    def _check_daily_recalc_due(self) -> bool:
        """True если нужно сейчас сработать daily-триггер (час совпал, ещё
        не делали сегодня)."""
        ab = (self.cfg.get("trading", {}).get("auto_base_amount") or {})
        if not bool(ab.get("enabled", True)):
            return False
        if not bool(ab.get("daily_recalc", False)):
            return False
        try:
            tz_name = self.cfg.get("telegram", {}).get("daily_report_timezone", "UTC")
            target_hour = int(self.cfg.get("telegram", {}).get("daily_report_hour", 7))
        except Exception:
            return False
        try:
            import pytz
            tz = pytz.timezone(tz_name)
        except Exception:
            tz = None
        if tz is None:
            now_local = datetime.utcnow()
        else:
            now_local = datetime.now(tz)
        if now_local.hour != target_hour:
            return False
        date_str = now_local.strftime("%Y-%m-%d")
        if self._last_daily_recalc_date == date_str:
            return False  # уже делали сегодня
        # Помечаем дату ДО фактического выполнения, чтобы не дёргать
        # повторно если запуск отложен (МГ идёт).
        self._last_daily_recalc_date = date_str
        return True

    async def _maybe_pause_on_live_streak(self, symbol: str):
        """Сравнивает live-серию LOSS-ов на паре с историческим
        max_loss_streak_to_win и ставит пару на live-pause если превышен порог.

        Защита от перекоса исторических данных vs текущая форма пары:
        если модель говорит «макс серия 2», а в live уже 4 — модель
        не работает на этой паре сегодня, лучше остыть.

        Параметры (config.yaml → protection):
          live_streak_multiplier   (default 1.5) — порог = hist_max × этот
          live_streak_pause_minutes (default 60)
          live_streak_min_abs      (default 4)   — не срабатывать на streak<этого
          live_streak_min_history  (default 30)  — нужно ≥ N settled сигналов
                                                  для надёжной hist_max
        Учитывается active strategy_name.
        """
        prot = self.cfg.get("protection") or {}
        if not bool(prot.get("live_streak_pause_enabled", True)):
            return
        live = int(self._live_loss_streak.get(symbol, 0))
        abs_floor = int(prot.get("live_streak_min_abs", 4) or 4)
        if live < abs_floor:
            return

        multiplier = float(prot.get("live_streak_multiplier", 1.5) or 1.5)
        pause_min = int(prot.get("live_streak_pause_minutes", 60) or 60)
        min_hist = int(prot.get("live_streak_min_history", 30) or 30)

        # Не плодим новый ban, если он уже стоит.
        if self.journal.is_banned(symbol):
            return

        strat_name = None
        if self.registry:
            try:
                strat_name = self.registry.get_active().name
            except Exception:
                strat_name = None
        # окно последних 30 дней — достаточно репрезентативно и не сканит всё
        since = int(time.time()) - 30 * 86400
        rows = self.journal.analytics_aggregate(
            since_ts=since, strategy_name=strat_name, only_symbol=symbol,
        )
        if not rows:
            return
        r = rows[0]
        hist_max = int(r.get("max_loss_streak_to_win") or 0)
        n_settled = int(r.get("settled") or 0)
        if hist_max <= 0 or n_settled < min_hist:
            return  # данных мало — не судим

        threshold = max(abs_floor, int(round(hist_max * multiplier)))
        if live < threshold:
            return

        reason = (
            f"live_loss_streak={live} ≥ hist_max({hist_max})×{multiplier}={threshold} "
            f"(n_settled={n_settled})"
        )
        self.journal.ban(symbol, minutes=pause_min, reason=reason)
        logger.warning("LIVE-STREAK-PAUSE %s for %dmin — %s", symbol, pause_min, reason)
        # сбрасываем live-streak — пара ушла в pause, отсчёт начнётся заново
        # после возврата в торговлю.
        self._live_loss_streak.pop(symbol, None)
        try:
            await self._notify(
                f"⏸ <b>LIVE-PAUSE {symbol}</b> на {pause_min} мин\n"
                f"Серия минусов {live} ≥ исторический max {hist_max} × {multiplier} = {threshold}.\n"
                f"Модель пары устарела — даём пересобраться."
            )
        except Exception:
            pass

    def _reset_cycle(self):
        self.state.current_pair = None
        self.state.original_pair = None
        self.state.direction = None
        self.state.trades_on_pair = 0
        self.state.mg_step = 0
        self.state.cycle_switches = 0
        self.state.switched_pairs = []
        self.state.cycle_unused_carry = 0
        self.state.losses_streak_on_pair = 0
