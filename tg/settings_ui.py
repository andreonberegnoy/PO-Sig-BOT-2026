"""Telegram inline-keyboard UI for editing all bot settings live.

Shows /settings command. Browse by category → click a setting → edit value
(toggle for booleans, +/− buttons for bounded ints, free input for floats,
preset choices for enums). Changes apply immediately to the running cfg dict
and are saved to config.yaml so they survive restart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

import yaml
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

logger = logging.getLogger(__name__)


# ─────────────────────────  schema  ─────────────────────────

@dataclass
class Setting:
    key: str            # dotted path: "indicator.minConsensus"
    label: str          # displayed name
    typ: str            # "int" | "float" | "bool" | "choice"
    minv: Optional[float] = None
    maxv: Optional[float] = None
    step: Optional[float] = None
    options: Optional[list] = None     # for choice type


CATEGORIES: dict[str, list[Setting]] = {
    "📊 Индикатор: правила": [
        Setting("indicator.minConsensus", "Минимум голосов (4 или 5)", "int", 3, 5, 1),
        Setting("indicator.requireAll5OnWeekend", "На выходных требовать 5/5", "bool"),
        Setting("indicator.cooldownBars", "Cooldown между сигналами (бары)", "int", 0, 30, 1),
        Setting("indicator.expiryBars", "Экспирация в барах (бэктест)", "int", 1, 10, 1),
        Setting("indicator.entryMode", "Режим входа", "choice",
                options=["nextBarOpen", "signalBarClose"]),
    ],
    "📈 Индикатор: RSI-QQE": [
        Setting("indicator.rsiPeriod", "RSI period", "int", 2, 50, 1),
        Setting("indicator.rsiSmoothing", "RSI smoothing", "int", 1, 30, 1),
        Setting("indicator.qqeFactor", "QQE factor", "float", 1.0, 10.0, 0.1),
    ],
    "🌐 Индикатор: HTF": [
        Setting("indicator.htfMultiplier", "HTF multiplier (M1×N)", "int", 2, 60, 1),
        Setting("indicator.htfMaPeriod", "HTF MA period", "int", 5, 200, 1),
        Setting("indicator.htfMaType", "HTF MA type", "choice",
                options=["EMA", "SMA", "WMA", "RMA"]),
    ],
    "📉 Индикатор: ATR": [
        Setting("indicator.atrPeriod", "ATR period", "int", 5, 50, 1),
        Setting("indicator.atrAvgWindow", "ATR avg window", "int", 20, 500, 10),
        Setting("indicator.atrMinRatio", "ATR min ratio", "float", 0.1, 5.0, 0.05),
        Setting("indicator.atrMaxRatio", "ATR max ratio", "float", 0.5, 10.0, 0.1),
    ],
    "📊 Индикатор: Bollinger": [
        Setting("indicator.bbPeriod", "BB period", "int", 5, 100, 1),
        Setting("indicator.bbStdDev", "BB std dev", "float", 0.5, 5.0, 0.1),
        Setting("indicator.bbZoneDepth", "BB zone depth (0-1)", "float", 0.05, 0.95, 0.05),
    ],
    "🕯 Индикатор: свеча": [
        Setting("indicator.candleMaxAtrMult", "Max body / ATR", "float", 0.5, 10.0, 0.1),
        Setting("indicator.candleReqAlign", "Свеча в направлении сигнала", "bool"),
    ],
    # Note: filter.asset_categories (категорії активів — forex/crypto/stocks/etc)
    # доступне тільки через Mini App (multi-checkbox UI). У TG-боті немає
    # зручного способу показати multi-select через inline-кнопки.
    "🔍 Фильтр пар": [
        Setting("filter.min_payout", "Минимум payout (%)", "int", 50, 95, 1),
        Setting("filter.payout_floor", "Порог смены пары (%)", "int", 50, 90, 1),
        Setting("filter.max_losses_in_row", "Макс. минусов до бана", "int", 1, 10, 1),
        Setting("filter.min_wr1", "Мин. общая проходимость (%)", "int", 0, 100, 5),
        Setting("filter.min_wr1_recent", "Мин. проходимость последних свечей (%)", "int", 0, 100, 5),
        Setting("filter.recent_lookback_bars", "Окно recent (свечей)", "int", 50, 500, 50),
        Setting("filter.history_candles", "Размер истории (бары)", "int", 200, 2000, 50),
        Setting("filter.ban_hours", "Бан пары (часов)", "int", 1, 72, 1),
        Setting("filter.pause_minutes", "Пауза за низкий recent WR1 (мин.)", "int", 5, 1440, 5),
        Setting("filter.temp_pause_hours", "Временная пауза (часов)", "int", 1, 48, 1),
    ],
    "💰 Торговля": [
        Setting("trading.base_amount", "Базовая ставка ($)", "float", 0.5, 100.0, 0.5),
        Setting("trading.expiry_seconds", "Экспирация сделки (сек)", "int", 30, 600, 30),
        Setting("trading.max_pair_switch_per_cycle", "Смен пары за цикл", "int", 0, 5, 1),
    ],
    "🎰 Мартингейл": [
        Setting("martingale.enabled", "Включить мартингейл", "bool"),
        Setting("martingale.coefficient", "Множитель", "float", 1.5, 5.0, 0.1),
        Setting("martingale.cycle_total_limit", "Общий лимит шагов в цикле", "int", 1, 20, 1),
        Setting("martingale.stop_sum", "Стоп-сумма ($)", "float", 10.0, 10000.0, 50.0),
        Setting("martingale.consecutive_losses_switch", "Минусов подряд → switch", "int", 0, 10, 1),
        Setting("martingale.carry_unused", "Переносить неиспользованные перекрытия", "bool"),
        Setting("martingale.last_pair_until_stop_sum", "На последней паре до stop_sum", "bool"),
    ],
    "⏰ Расписание": [
        Setting("schedule.enabled", "Расписание включено", "bool"),
        Setting("schedule.start_hour", "Час начала (0-23)", "int", 0, 23, 1),
        Setting("schedule.end_hour", "Час конца (0-24)", "int", 0, 24, 1),
        Setting("schedule.no_weekends", "Пропускать выходные", "bool"),
    ],
    "🗄 Хранение аналитики": [
        Setting("retention.signals_days", "Хранить signals (дней)", "int", 30, 365, 30),
    ],
}

# Flat lookup
ALL_SETTINGS: dict[str, Setting] = {
    s.key: s for cat in CATEGORIES.values() for s in cat
}


# ─────────────────────────  helpers  ─────────────────────────

def _get(cfg: dict, key: str) -> Any:
    parts = key.split(".")
    cur = cfg
    for p in parts:
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


def _set(cfg: dict, key: str, value: Any) -> None:
    parts = key.split(".")
    cur = cfg
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


def _format_value(v: Any) -> str:
    if isinstance(v, bool):
        return "ON" if v else "OFF"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _save_yaml(cfg: dict, path: str) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)
    except Exception:
        logger.exception("save config.yaml failed")


# ─────────────────────────  router  ─────────────────────────

def make_router(cfg: dict, config_path: str, allowed_chat_id: int) -> Router:
    router = Router()
    pending_edit: dict[int, str] = {}   # chat_id → setting key awaiting text input

    def _check(chat_id: int) -> bool:
        return chat_id == allowed_chat_id

    # ─── /settings — main menu (categories) ───
    @router.message(Command("settings"))
    async def cmd_settings(m: Message):
        if not _check(m.chat.id):
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=name, callback_data=f"st_cat:{i}")]
            for i, name in enumerate(CATEGORIES.keys())
        ])
        await m.answer("⚙️ <b>Настройки бота</b>\nВыбери категорию:",
                       parse_mode="HTML", reply_markup=kb)

    # ─── category list ───
    @router.callback_query(F.data.startswith("st_cat:"))
    async def cb_cat(cq: CallbackQuery):
        if not _check(cq.message.chat.id):
            return
        idx = int(cq.data.split(":", 1)[1])
        cat_name = list(CATEGORIES.keys())[idx]
        items = CATEGORIES[cat_name]
        rows = []
        for s in items:
            v = _get(cfg, s.key)
            rows.append([InlineKeyboardButton(
                text=f"{s.label}: {_format_value(v)}",
                callback_data=f"st_edit:{s.key}",
            )])
        rows.append([InlineKeyboardButton(text="« Назад", callback_data="st_back")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        try:
            await cq.message.edit_text(f"<b>{cat_name}</b>", parse_mode="HTML",
                                       reply_markup=kb)
        except Exception:
            await cq.message.answer(f"<b>{cat_name}</b>", parse_mode="HTML",
                                    reply_markup=kb)
        await cq.answer()

    @router.callback_query(F.data == "st_back")
    async def cb_back(cq: CallbackQuery):
        if not _check(cq.message.chat.id):
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=name, callback_data=f"st_cat:{i}")]
            for i, name in enumerate(CATEGORIES.keys())
        ])
        await cq.message.edit_text("⚙️ <b>Настройки бота</b>\nВыбери категорию:",
                                   parse_mode="HTML", reply_markup=kb)
        await cq.answer()

    # ─── edit specific setting ───
    def _edit_kb(s: Setting) -> InlineKeyboardMarkup:
        v = _get(cfg, s.key)
        rows = []
        if s.typ == "bool":
            rows.append([
                InlineKeyboardButton(
                    text=("✅ ON" if v else "ON"),
                    callback_data=f"st_val:{s.key}:1",
                ),
                InlineKeyboardButton(
                    text=("OFF" if v else "✅ OFF"),
                    callback_data=f"st_val:{s.key}:0",
                ),
            ])
        elif s.typ in ("int", "float"):
            step = s.step if s.step else (1 if s.typ == "int" else 0.1)
            rows.append([
                InlineKeyboardButton(text=f"− {step:g}", callback_data=f"st_dec:{s.key}"),
                InlineKeyboardButton(text=f"{_format_value(v)}", callback_data="noop"),
                InlineKeyboardButton(text=f"+ {step:g}", callback_data=f"st_inc:{s.key}"),
            ])
            rows.append([
                InlineKeyboardButton(text="✏️ Ввести вручную",
                                     callback_data=f"st_input:{s.key}"),
            ])
        elif s.typ == "choice" and s.options:
            for opt in s.options:
                tag = "✅ " if opt == v else ""
                rows.append([InlineKeyboardButton(
                    text=f"{tag}{opt}",
                    callback_data=f"st_val:{s.key}:{opt}",
                )])
        rows.append([InlineKeyboardButton(text="« К категориям",
                                          callback_data="st_back")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @router.callback_query(F.data.startswith("st_edit:"))
    async def cb_edit(cq: CallbackQuery):
        if not _check(cq.message.chat.id):
            return
        key = cq.data.split(":", 1)[1]
        s = ALL_SETTINGS.get(key)
        if not s:
            return await cq.answer("Unknown setting")
        v = _get(cfg, key)
        text = (f"<b>{s.label}</b>\n"
                f"Текущее: <code>{_format_value(v)}</code>\n")
        if s.minv is not None and s.maxv is not None:
            text += f"Диапазон: {s.minv:g} … {s.maxv:g}"
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=_edit_kb(s))
        await cq.answer()

    # ─── apply value (bool / choice / +/-) ───
    async def _apply_and_refresh(cq: CallbackQuery, s: Setting, new_value: Any):
        # clamp
        if s.typ in ("int", "float") and s.minv is not None and s.maxv is not None:
            new_value = max(s.minv, min(s.maxv, new_value))
            new_value = int(new_value) if s.typ == "int" else float(new_value)
        _set(cfg, s.key, new_value)
        _save_yaml(cfg, config_path)
        text = (f"<b>{s.label}</b>\n"
                f"Текущее: <code>{_format_value(new_value)}</code>")
        if s.minv is not None and s.maxv is not None:
            text += f"\nДиапазон: {s.minv:g} … {s.maxv:g}"
        try:
            await cq.message.edit_text(text, parse_mode="HTML", reply_markup=_edit_kb(s))
        except Exception:
            pass
        await cq.answer(f"Установлено: {_format_value(new_value)}")

    @router.callback_query(F.data.startswith("st_val:"))
    async def cb_val(cq: CallbackQuery):
        if not _check(cq.message.chat.id):
            return
        _, key, raw = cq.data.split(":", 2)
        s = ALL_SETTINGS.get(key)
        if not s:
            return await cq.answer("Unknown")
        if s.typ == "bool":
            new = raw == "1"
        elif s.typ == "choice":
            new = raw
        else:
            return await cq.answer("Bad type")
        await _apply_and_refresh(cq, s, new)

    @router.callback_query(F.data.startswith("st_inc:"))
    async def cb_inc(cq: CallbackQuery):
        if not _check(cq.message.chat.id):
            return
        key = cq.data.split(":", 1)[1]
        s = ALL_SETTINGS.get(key)
        if not s: return
        v = _get(cfg, key) or 0
        step = s.step if s.step else (1 if s.typ == "int" else 0.1)
        await _apply_and_refresh(cq, s, v + step)

    @router.callback_query(F.data.startswith("st_dec:"))
    async def cb_dec(cq: CallbackQuery):
        if not _check(cq.message.chat.id):
            return
        key = cq.data.split(":", 1)[1]
        s = ALL_SETTINGS.get(key)
        if not s: return
        v = _get(cfg, key) or 0
        step = s.step if s.step else (1 if s.typ == "int" else 0.1)
        await _apply_and_refresh(cq, s, v - step)

    @router.callback_query(F.data.startswith("st_input:"))
    async def cb_input(cq: CallbackQuery):
        if not _check(cq.message.chat.id):
            return
        key = cq.data.split(":", 1)[1]
        s = ALL_SETTINGS.get(key)
        if not s: return
        pending_edit[cq.message.chat.id] = key
        await cq.message.answer(
            f"Пришли новое значение для <b>{s.label}</b>\n"
            f"(текущее: <code>{_format_value(_get(cfg, key))}</code>)",
            parse_mode="HTML",
        )
        await cq.answer()

    @router.callback_query(F.data == "noop")
    async def cb_noop(cq: CallbackQuery):
        await cq.answer()

    # ─── plain text input handler — only when user has pending edit ───
    @router.message(F.text & ~F.text.startswith("/"))
    async def text_input(m: Message):
        if not _check(m.chat.id):
            return
        key = pending_edit.pop(m.chat.id, None)
        if not key:
            return  # not editing — let other handlers process
        s = ALL_SETTINGS.get(key)
        if not s:
            return await m.answer("Setting не найден")
        raw = (m.text or "").strip()
        try:
            if s.typ == "int":
                value = int(raw)
            elif s.typ == "float":
                value = float(raw.replace(",", "."))
            elif s.typ == "bool":
                value = raw.lower() in ("1", "true", "on", "yes", "да")
            else:
                value = raw
        except ValueError:
            pending_edit[m.chat.id] = key   # stay in edit mode
            return await m.answer(f"❌ Не удалось распарсить '{raw}' как {s.typ}. Попробуй ещё.")
        if s.typ in ("int", "float") and s.minv is not None and s.maxv is not None:
            value = max(s.minv, min(s.maxv, value))
        _set(cfg, s.key, value)
        _save_yaml(cfg, config_path)
        await m.answer(
            f"✅ <b>{s.label}</b> = <code>{_format_value(value)}</code>",
            parse_mode="HTML",
        )

    return router
