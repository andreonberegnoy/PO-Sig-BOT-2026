"""Telegram control bot — commands + daily report.

Commands:
  /status    — текущее состояние (пара, МГ-шаг, потери, баланс)
  /balance   — запросить баланс
  /pause     — приостановить торговлю
  /resume    — возобновить (или перезапустить после стоп-суммы)
  /stop      — остановить бота полностью
  /bans      — показать активные баны пар
  /help
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Callable, Optional
import pytz

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message

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
                "/status — состояние\n"
                "/balance — баланс\n"
                "/pause — пауза\n"
                "/resume — продолжить\n"
                "/stop — остановить бота\n"
                "/bans — активные баны\n"
                "/test SYMBOL call|put [amount] — тестовая сделка\n"
            )

        @dp.message(Command("status"))
        async def _status(m: Message):
            if m.chat.id != self.chat_id: return
            if not self.sm:
                return await m.answer("Бот ещё не запущен.")
            s = self.sm.state
            tracked = len(getattr(self.sm, "_tracked", set()) or set())
            ticks = sum((getattr(self.sm, "_tick_counts", {}) or {}).values())
            active = sum(1 for v in (getattr(self.sm, "_tick_counts", {}) or {}).values() if v > 0)
            text = (
                f"Режим: {self.cfg['mode']}\n"
                f"Пара: {s.current_pair or '—'}\n"
                f"МГ-шаг: {s.mg_step}  (сумма ${self.sm._amount_for_step(s.mg_step):.2f})\n"
                f"Сделок на паре: {s.trades_on_pair}\n"
                f"Смен пары в цикле: {s.cycle_switches}\n"
                f"Потери за сессию: ${s.session_loss:.2f}\n"
                f"Пауза: {'ДА' if s.paused else 'нет'}   Стоп-сумма: {'ЖДУ /resume' if s.waiting_resume else 'нет'}\n"
                f"Баланс: {self.feed.balance() if self.feed else '?'}\n"
                f"Tracked пар: {tracked}  |  live-тиков: {ticks} (активных пар {active})"
            )
            await m.answer(text)   # plain text — underscores in symbols break Markdown

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

    # ---------- polling ----------

    async def run_polling(self):
        logger.info("telegram polling started")
        await self.dp.start_polling(self.bot, handle_signals=False)

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
