"""Telegram control bot — commands + daily report.

Commands:
  /status    — текущее состояние (пара, МГ-шаг, потери, баланс)
  /balance   — запросить баланс
  /pause     — приостановить торговлю
  /resume    — возобновить (или перезапустить после стоп-суммы)
  /stop      — остановить бота полностью
  /bans      — показать активные баны пар
  /ping      — проверить живость бота + кнопка принудительного реконнекта
  /help
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Callable, Optional
import pytz

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)

logger = logging.getLogger(__name__)


class TelegramBot:
    def __init__(self, cfg: dict, state_machine=None, journal=None, feed=None):
        self.cfg = cfg
        self.token = cfg["telegram"]["token"]
        self.chat_id = int(cfg["telegram"]["chat_id"])
        self.sm = state_machine
        self.journal = journal
        self.feed = feed

        self.bot = Bot(token=self.token)
        self.dp = Dispatcher()
        self._register_handlers()

        self._stop_cb: Optional[Callable] = None

    def attach(self, state_machine, journal, feed, stop_cb):
        self.sm = state_machine
        self.journal = journal
        self.feed = feed
        self._stop_cb = stop_cb

    async def notify(self, text: str, parse_mode: Optional[str] = None):
        try:
            await self.bot.send_message(self.chat_id, text,
                                        disable_web_page_preview=True,
                                        parse_mode=parse_mode)
        except Exception as e:
            logger.warning("tg send failed: %s", e)

    async def send_chart(self, png_path: str, caption: str = ""):
        try:
            from aiogram.types import FSInputFile
            await self.bot.send_photo(self.chat_id, FSInputFile(png_path), caption=caption)
        except Exception as e:
            logger.warning("tg send_chart failed: %s", e)

    # ---------- handlers ----------

    def _register_handlers(self):
        dp = self.dp

        @dp.message(Command("help"))
        async def _help(m: Message):
            if m.chat.id != self.chat_id: return
            await m.answer(
                "Команды:\n"
                "/status — состояние + кнопки управления циклом\n"
                "/balance — баланс\n"
                "/ping — диагностика бота 🩺\n"
                "/pause — пауза\n"
                "/resume — продолжить\n"
                "/stop — остановить бота\n"
                "/bans — активные баны\n"
                "/test SYMBOL call|put [amount] — тестовая сделка\n\n"
                "В /status доступны кнопки:\n"
                "🔀 Сменить пару — взять лучшую, МГ-шаг сохранится\n"
                "🔄 Сбросить цикл — вернуться в FREE (MG→0)\n"
            )

        def _build_status_text() -> str:
            if not self.sm:
                return "Бот ещё не запущен."
            s = self.sm.state
            tracked = len(getattr(self.sm, "_tracked", set()) or set())
            ticks = sum((getattr(self.sm, "_tick_counts", {}) or {}).values())
            active = sum(1 for v in (getattr(self.sm, "_tick_counts", {}) or {}).values() if v > 0)
            day_off = ""
            if s.day_off_until and s.day_off_until > int(time.time()):
                mins = (s.day_off_until - int(time.time())) // 60
                day_off = f"\n😴 Day-off: ещё {mins} мин"
            # In active MG cycle but no pair locked → searching across all
            if s.mg_step > 0 and not s.current_pair:
                pair_display = "🔍 поиск сигнала на всех допустимых"
            else:
                pair_display = s.current_pair or "—"
            return (
                f"Режим: {self.cfg['mode']}\n"
                f"Пара: {pair_display}\n"
                f"МГ-шаг: {s.mg_step}  (сумма ${self.sm._amount_for_step(s.mg_step):.2f})\n"
                f"Сделок на паре: {s.trades_on_pair}\n"
                f"Смен пары в цикле: {s.cycle_switches}\n"
                f"Потери за сессию: ${s.session_loss:.2f}\n"
                f"Пауза: {'ДА' if s.paused else 'нет'}   Стоп-сумма: {'ЖДУ /resume' if s.waiting_resume else 'нет'}\n"
                f"Баланс: {self.feed.balance() if self.feed else '?'}\n"
                f"Tracked пар: {tracked}  |  live-тиков: {ticks} (активных пар {active})"
                f"{day_off}"
            )

        def _build_status_keyboard() -> InlineKeyboardMarkup:
            s = self.sm.state if self.sm else None
            in_cycle = s and s.mg_step > 0
            in_search = in_cycle and not s.current_pair
            rows = []
            if in_cycle:
                # Reset cycle works in both locked and search mode.
                # Switch pair only makes sense when locked (need a pair to switch FROM).
                cycle_buttons = []
                if not in_search:
                    cycle_buttons.append(
                        InlineKeyboardButton(text="🔀 Сменить пару (МГ сохранить)", callback_data="sm:switch_pair")
                    )
                cycle_buttons.append(
                    InlineKeyboardButton(text="🔄 Сбросить цикл (FREE)", callback_data="sm:reset_cycle")
                )
                rows.append(cycle_buttons)
            rows.append([
                InlineKeyboardButton(text="⏸ Пауза" if (s and not s.paused) else "▶️ Продолжить",
                                     callback_data="sm:pause" if (s and not s.paused) else "sm:resume"),
                InlineKeyboardButton(text="🔃 Обновить", callback_data="sm:refresh_status"),
            ])
            return InlineKeyboardMarkup(inline_keyboard=rows)

        @dp.message(Command("status"))
        async def _status(m: Message):
            if m.chat.id != self.chat_id: return
            await m.answer(_build_status_text(), reply_markup=_build_status_keyboard())

        @dp.callback_query(F.data.startswith("sm:"))
        async def _sm_callback(cb: CallbackQuery):
            if cb.message.chat.id != self.chat_id:
                return await cb.answer("Нет доступа", show_alert=True)
            action = cb.data.split(":", 1)[1]

            if action == "refresh_status":
                await cb.message.edit_text(_build_status_text(),
                                           reply_markup=_build_status_keyboard())
                await cb.answer("Обновлено ✅")

            elif action == "pause":
                if self.sm: self.sm.pause()
                await cb.answer("⏸ Пауза включена")
                await cb.message.edit_text(_build_status_text(),
                                           reply_markup=_build_status_keyboard())

            elif action == "resume":
                if self.sm:
                    if self.sm.state.waiting_resume:
                        self.sm.resume_after_stop_sum()
                        await cb.answer("▶️ Перезапуск после стоп-суммы")
                    else:
                        self.sm.resume()
                        await cb.answer("▶️ Торговля возобновлена")
                await cb.message.edit_text(_build_status_text(),
                                           reply_markup=_build_status_keyboard())

            elif action == "reset_cycle":
                if not self.sm:
                    return await cb.answer("SM не запущен", show_alert=True)
                s = self.sm.state
                old = f"{s.current_pair} MG{s.mg_step}"
                self.sm.force_reset_cycle()
                await cb.answer(f"🔄 Цикл сброшен ({old} → FREE)")
                await cb.message.edit_text(
                    _build_status_text() + f"\n\n♻️ Сброшен цикл {old} → FREE. Ищу новый сигнал…",
                    reply_markup=_build_status_keyboard()
                )

            elif action == "switch_pair":
                if not self.sm:
                    return await cb.answer("SM не запущен", show_alert=True)
                if not (self.sm.state.mg_step > 0 and self.sm.state.current_pair):
                    return await cb.answer("Нет активного цикла для смены пары", show_alert=True)
                await cb.answer("🔀 Ищу лучшую пару…")
                old_pair = self.sm.state.current_pair
                new_pair = await self.sm.force_switch_pair()
                if new_pair:
                    await cb.message.edit_text(
                        _build_status_text() + f"\n\n🔀 Вручную сменена пара: {old_pair} → {new_pair}",
                        reply_markup=_build_status_keyboard()
                    )
                else:
                    await cb.message.edit_text(
                        _build_status_text() + "\n\n⚠️ Нет доступных пар для смены. Жду сигнал на текущей.",
                        reply_markup=_build_status_keyboard()
                    )

        @dp.message(Command("balance"))
        async def _balance(m: Message):
            if m.chat.id != self.chat_id: return
            await m.answer(f"Баланс: ${self.feed.balance() if self.feed else '?'} (mode={self.cfg['mode']})")

        @dp.message(Command("pause"))
        async def _pause(m: Message):
            if m.chat.id != self.chat_id: return
            if self.sm: self.sm.pause()
            await m.answer("⏸ Пауза включена. /resume чтобы снять.")

        @dp.message(Command("resume"))
        async def _resume(m: Message):
            if m.chat.id != self.chat_id: return
            if not self.sm:
                return await m.answer("Бот не запущен.")
            if self.sm.state.waiting_resume:
                self.sm.resume_after_stop_sum()
                await m.answer("▶️ Перезапуск после стоп-суммы. Начинаю с базовой суммы.")
            else:
                self.sm.resume()
                await m.answer("▶️ Торговля возобновлена.")

        @dp.message(Command("stop"))
        async def _stop(m: Message):
            if m.chat.id != self.chat_id: return
            await m.answer("🛑 Останавливаю бота…")
            if self._stop_cb:
                await self._stop_cb()

        @dp.message(Command("chart"))
        async def _chart(m: Message):
            if m.chat.id != self.chat_id: return
            parts = (m.text or "").split(maxsplit=1)
            if len(parts) < 2:
                return await m.answer("Использование: /chart EURUSD_otc")
            sym = parts[1].strip()
            buf = self.sm._candles.get(sym) if self.sm else None
            if not buf:
                # Try to fetch on-demand
                try:
                    from feed.history import fetch_candles
                    buf = await fetch_candles(self.feed, sym, period=60, limit=1060)
                except Exception as e:
                    return await m.answer(f"Ошибка загрузки {sym}: {e}")
            if not buf:
                return await m.answer(f"Нет данных по {sym}")
            try:
                from tg.chart import render_chart
                params = {**self.cfg["indicator"]}
                png = render_chart(buf, params, sym)
                await self.send_chart(png, caption=f"📊 {sym}")
            except Exception as e:
                logger.exception("chart error")
                await m.answer(f"Ошибка рисования: {e}")

        @dp.message(Command("test"))
        async def _test(m: Message):
            if m.chat.id != self.chat_id: return
            if not self.sm:
                return await m.answer("Бот не запущен.")
            parts = (m.text or "").split()
            if len(parts) < 3:
                return await m.answer("Использование: /test EURUSD_otc call|put [amount]")
            sym = parts[1].strip()
            action = parts[2].strip().lower()
            if action not in ("call", "put"):
                return await m.answer("action должен быть call или put")
            amount = float(parts[3]) if len(parts) >= 4 else float(self.cfg["trading"]["base_amount"])
            exp = int(self.cfg["trading"]["expiry_seconds"])
            await m.answer(f"🧪 Тест: {sym} {action.upper()} ${amount} exp={exp}s … отправляю open_trade")
            try:
                trade = await self.sm.tc.open_trade(asset=sym, amount=amount, action=action, time_sec=exp)
            except Exception as e:
                logger.exception("test open_trade failed")
                return await m.answer(f"❌ Ошибка: {e}")
            if not trade:
                return await m.answer("❌ open_trade не подтверждён (timeout/login)")
            await m.answer(f"✅ Открыто: id={trade.trade_id}  entry={getattr(trade,'entry_price','?')}")

        @dp.message(Command("bans"))
        async def _bans(m: Message):
            if m.chat.id != self.chat_id: return
            if not self.journal: return await m.answer("Журнал не инициализирован.")
            bans = self.journal.active_bans()
            if not bans:
                return await m.answer("Активных банов нет.")
            now = int(time.time())
            lines = [f"{sym} — до {datetime.fromtimestamp(exp).strftime('%Y-%m-%d %H:%M')} ({(exp-now)//60} мин)"
                     for sym, exp in bans]
            await m.answer("Активные баны:\n" + "\n".join(lines))

        # ── /ping — health check + кнопки действий ──────────────────────────

        def _build_ping_text() -> str:
            """Собирает диагностику состояния бота."""
            lines = ["🩺 <b>Диагностика бота</b>"]
            # Feed / WS status
            feed = self.feed
            if feed is None:
                lines.append("❌ Feed не подключён")
            else:
                last_frame = getattr(feed, "_last_frame_ts", None)
                if last_frame:
                    age = int(time.time() - last_frame)
                    icon = "✅" if age < 60 else ("⚠️" if age < 120 else "❌")
                    lines.append(f"{icon} WS: последний фрейм {age}с назад")
                else:
                    lines.append("⚠️ WS: нет данных о фреймах")
                ws = getattr(feed, "_ws", None)
                # websockets 13+ removed .closed; use .state enum.
                # Fallback to .closed for older versions.
                ws_state = "?"
                if ws is not None:
                    state_attr = getattr(ws, "state", None)
                    if state_attr is not None:
                        ws_state = getattr(state_attr, "name", str(state_attr)).lower()
                    elif hasattr(ws, "closed"):
                        ws_state = "closed" if ws.closed else "open"
                lines.append(f"🔌 WebSocket: <code>{ws_state}</code>")
                relogin_in = getattr(feed, "_relogin_in_progress", False)
                if relogin_in:
                    lines.append("🔄 Relogin: в процессе…")
                bal = feed.balance()
                lines.append(f"💳 Баланс: <b>${bal}</b>")
            # State machine
            sm = self.sm
            if sm:
                s = sm.state
                if s.mg_step > 0 and not s.current_pair:
                    pair = "🔍 search-mode"
                else:
                    pair = s.current_pair or "—"
                mg = s.mg_step
                paused = "⏸ ПАУЗА" if s.paused else ("⏳ ОЖИДАНИЕ /resume" if s.waiting_resume else "▶️ активен")
                lines.append(f"🤖 SM: {paused}  |  пара: {pair}  |  МГ-шаг: {mg}")
                tracked = len(getattr(sm, "_tracked", set()) or set())
                tick_counts = getattr(sm, "_tick_counts", {}) or {}
                active_ticks = sum(1 for v in tick_counts.values() if v > 0)
                total_ticks = sum(tick_counts.values())
                lines.append(f"📡 Пар в трекере: {tracked}  |  активных тик-потоков: {active_ticks}  |  тиков всего: {total_ticks}")
            # Asyncio tasks. If recv-task is missing by name BUT WS frames are
            # arriving fresh (<60s), treat it as healthy — task may have been
            # renamed during a reconnect and we don't want to false-alarm.
            task_names = {t.get_name() for t in asyncio.all_tasks() if not t.done()}
            critical = {"state_machine", "tg_polling", "po_direct_recv", "po_heartbeat"}
            missing = critical - task_names
            # Behavioural override: live frames mean the recv loop is alive
            # regardless of what its task is named.
            if "po_direct_recv" in missing and feed is not None:
                lf = getattr(feed, "_last_frame_ts", None)
                if lf and (time.time() - lf) < 60:
                    missing.discard("po_direct_recv")
            if missing:
                lines.append(f"❌ Отсутствуют задачи: {', '.join(sorted(missing))}")
            else:
                lines.append(f"✅ Все критичные задачи живы ({len(task_names)} всего)")
            lines.append(f"\n🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            return "\n".join(lines)

        def _build_ping_keyboard() -> InlineKeyboardMarkup:
            return InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="🔄 Реконнект WS", callback_data="ping:reconnect"),
                    InlineKeyboardButton(text="🔑 Relogin (новый SSID)", callback_data="ping:relogin"),
                ],
                [
                    InlineKeyboardButton(text="🔃 Обновить статус", callback_data="ping:refresh"),
                ],
            ])

        @dp.message(Command("ping"))
        async def _ping(m: Message):
            if m.chat.id != self.chat_id: return
            await m.answer(_build_ping_text(), parse_mode="HTML",
                           reply_markup=_build_ping_keyboard())

        @dp.callback_query(F.data.startswith("ping:"))
        async def _ping_callback(cb: CallbackQuery):
            if cb.message.chat.id != self.chat_id:
                return await cb.answer("Нет доступа", show_alert=True)
            action = cb.data.split(":", 1)[1]

            if action == "refresh":
                await cb.message.edit_text(_build_ping_text(), parse_mode="HTML",
                                           reply_markup=_build_ping_keyboard())
                await cb.answer("Обновлено ✅")

            elif action == "reconnect":
                feed = self.feed
                if feed is None:
                    return await cb.answer("Feed не подключён", show_alert=True)
                await cb.answer("Запускаю реконнект…")
                await cb.message.edit_text("🔄 Принудительный реконнект WS…\n"
                                           "Жди ~10-15 секунд, потом нажми 🔃 Обновить статус.",
                                           parse_mode="HTML", reply_markup=_build_ping_keyboard())
                async def _do_reconnect():
                    try:
                        ws = getattr(feed, "_ws", None)
                        if ws:
                            await ws.close()
                        # Сброс watchdog-таймера чтоб не зациклился
                        feed._last_frame_ts = time.time()
                    except Exception as e:
                        logger.warning("ping reconnect failed: %s", e)
                asyncio.create_task(_do_reconnect(), name="ping_reconnect")

            elif action == "relogin":
                feed = self.feed
                if feed is None:
                    return await cb.answer("Feed не подключён", show_alert=True)
                if not getattr(feed, "_relogin_callback", None):
                    return await cb.answer("Relogin callback не настроен (нет PO_STORAGE_STATE_B64?)",
                                           show_alert=True)
                if getattr(feed, "_relogin_in_progress", False):
                    return await cb.answer("Relogin уже выполняется…", show_alert=True)
                await cb.answer("Запускаю relogin…")
                await cb.message.edit_text("🔑 Принудительный relogin…\n"
                                           "Playwright открывает Chromium (30–60с). "
                                           "Нажми 🔃 Обновить статус через минуту.",
                                           parse_mode="HTML", reply_markup=_build_ping_keyboard())
                asyncio.create_task(feed._do_relogin(reason="manual_tg"),
                                    name="ping_relogin")

    # ---------- polling ----------

    async def run_polling(self):
        """Start Telegram polling with automatic restart on any exception.
        Aiogram polling can die on network blips or Telegram API errors;
        without a restart the bot goes silent (no commands work)."""
        import asyncio as _asyncio
        retry_delay = 5
        while True:
            try:
                logger.info("telegram polling started")
                await self.dp.start_polling(self.bot, handle_signals=False)
                # start_polling returned normally — bot was stopped intentionally
                logger.info("telegram polling stopped normally")
                return
            except _asyncio.CancelledError:
                logger.info("telegram polling cancelled")
                return
            except Exception as e:
                logger.warning("telegram polling crashed (%s) — restart in %ds", e, retry_delay)
                await _asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)   # exponential backoff up to 60s

    async def close(self):
        try:
            await self.bot.session.close()
        except Exception:
            pass

    # ---------- daily report ----------

    async def daily_report_loop(self):
        """Sends a summary once per day at daily_report_hour local time."""
        tz = pytz.timezone(self.cfg["telegram"]["daily_report_timezone"])
        target_hour = int(self.cfg["telegram"]["daily_report_hour"])
        while True:
            now = datetime.now(tz)
            next_run = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=1)
            sleep_s = (next_run - now).total_seconds()
            logger.info("daily report in %.0fs", sleep_s)
            await asyncio.sleep(sleep_s)

            # If bot is in the middle of a trade cycle, delay until WIN
            while self.sm and self.sm.state.mg_step > 0:
                await asyncio.sleep(30)

            await self._send_daily_report()

    async def _send_daily_report(self):
        if not self.journal: return
        since = int(time.time()) - 86400
        d = self.journal.daily_summary(since, self.cfg["mode"])
        bal = self.feed.balance() if self.feed else "?"
        text = (
            f"📊 Отчёт за 24 часа ({self.cfg['mode']})\n"
            f"\n"
            f"💰 Чистая прибыль: *${d['net_profit']:+.2f}*\n"
            f"🎯 WR: {d['win_rate']}%   (✅{d['wins']} / ❌{d['losses']} / =${d['draws']})\n"
            f"🔄 Смен пар: {d['pair_switches']}\n"
            f"📉 Макс. минусов подряд: {d['max_loss_streak']}\n"
            f"🚫 Банов за сутки: {d['bans_24h']}\n"
            f"💳 Баланс: ${bal}"
        )
        await self.notify(text)

    # ---------- proactive health watchdog ----------

    async def health_watchdog_loop(self):
        """Quietly checks bot health every CHECK_INTERVAL_SEC. Sends a
        Telegram alert ONLY when something is wrong — no "I'm alive"
        notifications during normal operation.

        Catches the failure modes that simpler watchdogs (WS freeze, stall)
        miss: critical asyncio task missing, balance unreachable, scan
        machinery silently broken.

        Cooldowns per category prevent spam during long outages.
        """
        CHECK_INTERVAL_SEC = 30 * 60   # 30 min
        FRAME_AGE_BAD = 5 * 60         # WS frame older than 5 min → alert
        TASK_NAMES_CRITICAL = {"state_machine", "tg_polling", "po_direct_recv"}
        cooldowns: dict[str, float] = {}

        def _alert_once(category: str, text: str, cooldown_sec: int = 60 * 60) -> None:
            now = time.time()
            if now - cooldowns.get(category, 0) < cooldown_sec:
                return
            cooldowns[category] = now
            asyncio.create_task(
                self.notify(text, parse_mode="HTML"), name=f"health_alert_{category}"
            )

        # Wait one full interval before first check — gives the bot time to
        # finish initial scan, history-loading, etc., so we don't false-alarm.
        await asyncio.sleep(60)

        while True:
            try:
                await asyncio.sleep(CHECK_INTERVAL_SEC)
                problems: list[str] = []

                # 1. Critical asyncio tasks alive? Behavioural override: if
                # frames are arriving fresh, treat recv as alive even if its
                # task name is missing (renamed across reconnects, etc.).
                live = {t.get_name() for t in asyncio.all_tasks() if not t.done()}
                missing = TASK_NAMES_CRITICAL - live
                if "po_direct_recv" in missing and self.feed is not None:
                    lf = getattr(self.feed, "_last_frame_ts", None)
                    if lf and (time.time() - lf) < 60:
                        missing.discard("po_direct_recv")
                if missing:
                    problems.append(
                        f"❌ Отсутствуют критичные задачи: <code>{', '.join(sorted(missing))}</code>"
                    )

                # 2. Last WS frame fresh?
                if self.feed is not None:
                    last_frame = getattr(self.feed, "_last_frame_ts", None)
                    if last_frame:
                        age = int(time.time() - last_frame)
                        if age > FRAME_AGE_BAD:
                            problems.append(
                                f"❌ Последний WS-фрейм {age}с назад "
                                f"(порог {FRAME_AGE_BAD}с)"
                            )

                # 3. Balance reachable? (None usually means feed lost auth)
                if self.feed is not None:
                    try:
                        bal = self.feed.balance()
                        if bal is None:
                            problems.append("⚠️ Баланс недоступен — возможно сессия протухла")
                    except Exception:
                        problems.append("⚠️ feed.balance() бросает исключение")

                # 4. State machine healthy? (paused without user action is suspect)
                # We don't alert on paused — user might have done it via /pause.
                # But waiting_resume is a stop-loss state user already knows about.

                if problems:
                    msg = (
                        "🩺 <b>Watchdog: нашёл проблемы</b>\n\n"
                        + "\n".join(problems)
                        + "\n\n👉 Нажми /ping для подробной диагностики, "
                        + "посмотри логи Railway если не очевидно."
                    )
                    _alert_once("health_problems", msg)
                else:
                    # Reset cooldown so next problem fires immediately
                    cooldowns.pop("health_problems", None)

            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("health_watchdog_loop tick failed (will continue)")

    # ---------- periodic report (schedule-aware) ----------

    async def periodic_report_loop(self):
        """Отчёт по таймеру:
          schedule.enabled=true  → каждый день при наступлении schedule.end_hour
          schedule.enabled=false → каждый день в periodic_report.hour_when_24_7

        Проверяет каждую минуту, отправляет один раз за календарный день.
        Если идёт мартингейл — ждёт закрытия цикла перед отправкой."""
        tz = pytz.timezone(self.cfg["telegram"]["daily_report_timezone"])
        last_sent_date = None
        while True:
            await asyncio.sleep(60)
            try:
                pr = self.cfg.get("periodic_report") or {}
                if not pr.get("enabled"):
                    continue
                sched = self.cfg.get("schedule") or {}
                if sched.get("enabled"):
                    target_hour = int(sched.get("end_hour", 22)) % 24
                else:
                    target_hour = int(pr.get("hour_when_24_7", 9)) % 24
                now = datetime.now(tz)
                if now.hour != target_hour or now.minute >= 5:
                    continue
                today = now.date()
                if last_sent_date == today:
                    continue
                # Если активный мартингейл — даём ему довестись до WIN.
                while self.sm and self.sm.state.mg_step > 0:
                    await asyncio.sleep(30)
                await self._send_periodic_report()
                last_sent_date = today
            except Exception:
                logger.exception("periodic report loop tick failed")

    async def _send_periodic_report(self):
        if not self.journal: return
        since = int(time.time()) - 86400
        d = self.journal.daily_summary(since, self.cfg["mode"])
        bal = self.feed.balance() if self.feed else "?"
        signals = int(d.get("wins", 0)) + int(d.get("losses", 0)) + int(d.get("draws", 0))
        # Минимальный payout среди WIN-сделок что закрыли цикл после ≥1 минуса.
        # Показывает «худшую выплату на которой я всё-таки вытащил мартингейл».
        min_win_p = None
        recovered_n = 0
        last_win_p = None
        try:
            wp_rows = self.journal.win_payout_aggregate(
                since, mode=self.cfg["mode"], after_loss_only=True,
            )
            if wp_rows:
                # overall min across all pairs in the period
                vals = [int(r["min_win_payout"]) for r in wp_rows if r.get("min_win_payout")]
                if vals:
                    min_win_p = min(vals)
                recovered_n = sum(int(r.get("n_recovered_wins", 0)) for r in wp_rows)
                # most recent overall
                last_pair = max(wp_rows, key=lambda r: r.get("last_win_payout", 0) and 1, default=None)
                last_win_p = last_pair.get("last_win_payout") if last_pair else None
        except Exception:
            logger.exception("win_payout_aggregate failed")
        recovered_line = ""
        if recovered_n:
            recovered_line = (
                f"\n🎯 Min выплата при +: {min_win_p}%   "
                f"(вытащено циклов: {recovered_n})"
            )
        text = (
            f"📋 Сводка ({self.cfg['mode']})\n"
            f"\n"
            f"💳 Текущий баланс: ${bal}\n"
            f"💰 Заработано за сутки: *${d['net_profit']:+.2f}*\n"
            f"📉 Макс. минусов подряд: {d['max_loss_streak']}\n"
            f"📡 Сигналов: {signals}"
            f"{recovered_line}"
        )
        await self.notify(text)
