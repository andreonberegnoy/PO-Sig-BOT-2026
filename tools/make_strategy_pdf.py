"""Generate a presentation-style PDF describing how the bot strategy works.

Run: python3 tools/make_strategy_pdf.py
Output: STRATEGY.pdf in the project root.
"""

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.colors import HexColor, white, black
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, PageBreak,
    Table, TableStyle, KeepTogether
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import os

# ─── Fonts: register a TTF that has Cyrillic glyphs ───────────────────────────
# Try to find a system font with Cyrillic support
def _register_font():
    candidates = [
        # macOS
        ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", "Cyr"),
        ("/System/Library/Fonts/Helvetica.ttc", "Cyr"),
        ("/Library/Fonts/Arial.ttf", "Cyr"),
        # Linux fallback
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "Cyr"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "CyrB"),
    ]
    registered = False
    for path, name in candidates:
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont(name, path))
                registered = True
            except Exception:
                pass
    return registered

_has_cyr_font = _register_font()
BASE_FONT = "Cyr" if _has_cyr_font else "Helvetica"
BOLD_FONT = "CyrB" if _has_cyr_font and "CyrB" in pdfmetrics.getRegisteredFontNames() else BASE_FONT

# ─── Color palette (matches Mini App dark-tech vibe but light bg for print) ──
NAVY = HexColor("#0f1115")
ACCENT = HexColor("#6db4ff")
GREEN = HexColor("#22c55e")
YELLOW = HexColor("#fdf647")
RED = HexColor("#ef4444")
GRAY = HexColor("#5a6473")
LIGHT_BG = HexColor("#f3f5f8")
CARD_BG = HexColor("#1a1d23")

# ─── Styles ───────────────────────────────────────────────────────────────────
styles = getSampleStyleSheet()

title_style = ParagraphStyle(
    "TitleBig", parent=styles["Title"],
    fontName=BOLD_FONT, fontSize=28, leading=34,
    textColor=NAVY, alignment=TA_CENTER, spaceAfter=10,
)

subtitle_style = ParagraphStyle(
    "Subtitle", parent=styles["Normal"],
    fontName=BASE_FONT, fontSize=14, leading=18,
    textColor=GRAY, alignment=TA_CENTER, spaceAfter=24,
)

h1_style = ParagraphStyle(
    "H1", parent=styles["Heading1"],
    fontName=BOLD_FONT, fontSize=22, leading=28,
    textColor=ACCENT, spaceAfter=14, spaceBefore=20,
)

h2_style = ParagraphStyle(
    "H2", parent=styles["Heading2"],
    fontName=BOLD_FONT, fontSize=15, leading=20,
    textColor=NAVY, spaceAfter=8, spaceBefore=14,
)

body_style = ParagraphStyle(
    "Body", parent=styles["Normal"],
    fontName=BASE_FONT, fontSize=11, leading=16,
    textColor=NAVY, alignment=TA_LEFT, spaceAfter=8,
)

body_indent = ParagraphStyle(
    "BodyIndent", parent=body_style,
    leftIndent=18, bulletIndent=6,
)

note_style = ParagraphStyle(
    "Note", parent=body_style,
    fontSize=10, textColor=GRAY, leftIndent=12, spaceAfter=4,
)

example_style = ParagraphStyle(
    "Example", parent=body_style,
    fontSize=10, leading=14, textColor=NAVY,
    backColor=LIGHT_BG, borderColor=ACCENT, borderWidth=0.5,
    borderPadding=8, leftIndent=0, rightIndent=0, spaceAfter=12, spaceBefore=4,
)


def page_number(canvas, doc):
    """Footer with page number on every page."""
    canvas.saveState()
    canvas.setFont(BASE_FONT, 9)
    canvas.setFillColor(GRAY)
    canvas.drawRightString(
        A4[0] - 1.5 * cm, 1 * cm,
        f"стр. {doc.page}",
    )
    canvas.drawString(1.5 * cm, 1 * cm, "PO-Sig Bot — Стратегия")
    canvas.restoreState()


def make_table(rows, col_widths=None, header=True):
    """Helper to build a styled table."""
    t = Table(rows, colWidths=col_widths, hAlign="LEFT")
    style = [
        ("FONT", (0, 0), (-1, -1), BASE_FONT, 10),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("ROWBACKGROUNDS", (0, 1 if header else 0), (-1, -1), [white, LIGHT_BG]),
        ("LINEBELOW", (0, 0), (-1, 0), 1.2, ACCENT) if header else
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, GRAY),
        ("BOX", (0, 0), (-1, -1), 0.5, GRAY),
    ]
    if header:
        style.extend([
            ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
            ("TEXTCOLOR", (0, 0), (-1, 0), white),
            ("FONT", (0, 0), (-1, 0), BOLD_FONT, 10),
        ])
    t.setStyle(TableStyle(style))
    return t


# ────────────────────────────────────────────────────────────────────────────
# CONTENT
# ────────────────────────────────────────────────────────────────────────────

story = []

# ═══ COVER ═════════════════════════════════════════════════════════════════
story.append(Spacer(1, 4 * cm))
story.append(Paragraph("Стратегия бота PO-Sig", title_style))
story.append(Paragraph("Как бот выбирает пары и принимает решения", subtitle_style))
story.append(Spacer(1, 1.5 * cm))

cover_meta = [
    ["Платформа", "Pocket Option (бинарные опционы)"],
    ["Стратегия", "CONSENSUS 4/5 индикаторов"],
    ["Таймфрейм", "M1 (свечи по 1 минуте)"],
    ["Тип входа", "На открытии следующей свечи (nextBarOpen)"],
    ["Экспирация", "120 секунд (2 минуты)"],
]
story.append(make_table(cover_meta, col_widths=[5 * cm, 11 * cm], header=False))
story.append(PageBreak())

# ═══ 1. СУТЬ СТРАТЕГИИ ════════════════════════════════════════════════════
story.append(Paragraph("1. Суть стратегии", h1_style))
story.append(Paragraph(
    "Бот ищет момент когда <b>4 из 5 индикаторов</b> сходятся в одном направлении на закрытии минутной свечи. "
    "Это и есть «сигнал». На сигнал бот заходит в сделку на 2 минуты в ту же сторону.",
    body_style
))
story.append(Spacer(1, 8))
story.append(Paragraph("5 индикаторов CONSENSUS:", h2_style))
indicators = [
    ["1. RSI с QQE", "Сглаженный RSI с QQE-фильтром перепроданности/перекупленности"],
    ["2. HTF EMA", "Тренд по старшему таймфрейму (EMA на M5)"],
    ["3. ATR-фильтр", "Волатильность не слишком низкая и не слишком высокая"],
    ["4. Bollinger Bands", "Цена на краю канала (зона возврата)"],
    ["5. Свеча", "Тело текущей свечи направлено в сторону сигнала"],
]
story.append(make_table(indicators, col_widths=[4 * cm, 12 * cm]))
story.append(Spacer(1, 8))
story.append(Paragraph(
    "<b>Минимум 4/5 совпадений</b> → сигнал. Меньше — пропуск. После сигнала бот ждёт открытия следующей минуты "
    "и заходит в сделку (CALL вверх или PUT вниз) на 120 секунд.",
    body_style
))

# ═══ 2. КАК ВЫБИРАЮТСЯ ПАРЫ ════════════════════════════════════════════════
story.append(PageBreak())
story.append(Paragraph("2. Как выбираются пары для торговли", h1_style))
story.append(Paragraph(
    "Раз в час бот делает «скан» всех доступных OTC пар PO. Для каждой пары загружает 1000 минутных свечей "
    "и виртуально прогоняет CONSENSUS — считает сколько было бы сделок и какой % выиграл бы.",
    body_style
))
story.append(Paragraph("Затем применяет 5 фильтров последовательно:", h2_style))

filter_rows = [
    ["№", "Фильтр", "Что проверяет", "Если не прошёл"],
    ["1", "Payout ≥ min_payout", "Текущий процент выплаты PO ≥ 92% (по умолч.)", "SKIP"],
    ["2", "OTC = да", "Только пары категории OTC (24/7 синтетика)", "SKIP"],
    ["3", "completed ≥ 5", "В последних 1000 свечах было хотя бы 5 виртуальных сделок", "SKIP"],
    ["4", "max_loss_streak\n≤ max_losses_in_row", "Не было серии больше 3 минусов подряд за 1000 свечей", "BAN"],
    ["5", "WR1 ≥ min_wr1", "% первой плюсовой сделки за 1000 свечей ≥ 60%", "SKIP"],
    ["6", "WR1 recent\n≥ min_wr1_recent", "% первой плюсовой за последние 200 свечей ≥ 70%", "PAUSE"],
]
story.append(make_table(filter_rows, col_widths=[1 * cm, 4 * cm, 7 * cm, 4 * cm]))
story.append(Spacer(1, 6))
story.append(Paragraph(
    "Прошедшие все 6 фильтров пары попадают в список <b>tracked</b> — бот живёт на их тиках и ждёт сигнал.",
    body_style
))

# Priority
story.append(Paragraph("Приоритет среди ALLOWED пар:", h2_style))
story.append(Paragraph(
    "Среди всех допущенных бот сортирует так: чем меньше <b>самая длинная серия минусов до плюса</b> в истории — "
    "тем выше приоритет. При равенстве — побеждает пара с большим payout. Сигнал берётся на первой подходящей паре.",
    body_style
))

# ═══ 3. ТРИ УРОВНЯ ОТСТРАНЕНИЯ ═════════════════════════════════════════════
story.append(PageBreak())
story.append(Paragraph("3. Три уровня «не торговать»", h1_style))
story.append(Paragraph(
    "Не каждое отстранение пары — это «бан навсегда». Есть 3 разных уровня:",
    body_style
))

levels_rows = [
    ["Уровень", "Триггер", "Срок", "Что дальше"],
    ["SKIP",
     "Мало сделок (<5)\nWR1 long < min_wr1\nPayout упал",
     "1 скан\n(до часа)",
     "Переоценка на следующем сканировании"],
    ["PAUSE",
     "WR1 recent < min_wr1_recent\n(плохая текущая форма)",
     "pause_hours\n(дефолт 1ч)",
     "Авто-переоценка через 1ч.\nЕсли опять плохо — снова пауза"],
    ["BAN",
     "max_loss_streak > 3\n(системно плохая пара)",
     "ban_hours\n(дефолт 12ч)",
     "Длительный бан, переоценка\nчерез 12 часов"],
]
story.append(make_table(levels_rows, col_widths=[2.5 * cm, 5 * cm, 3 * cm, 5.5 * cm]))
story.append(Spacer(1, 6))
story.append(Paragraph(
    "<b>Логика:</b> SKIP — это «сейчас не подходит, посмотрим через час». PAUSE — «временный спад, дадим отдохнуть». "
    "BAN — «системно проблемная пара, не подходит». Все три статуса автоматически снимаются через таймер.",
    body_style
))

# ═══ 4. КОГДА ПАРЫ ОБНОВЛЯЮТСЯ ═════════════════════════════════════════════
story.append(PageBreak())
story.append(Paragraph("4. Когда обновляется список пар", h1_style))

refresh_rows = [
    ["Событие", "Что происходит"],
    ["Старт бота",
     "Полный _rescan_pairs: загрузка 1000 свечей по каждой OTC-паре, фильтры, сортировка"],
    ["Каждый час",
     "Повторный _rescan_pairs (логирование статистики + переоценка bans/pauses)"],
    ["Закрытие минутной свечи",
     "Проверка свежего бара на каждой tracked паре (если новый — оценка CONSENSUS)"],
    ["Истечение PAUSE/BAN",
     "Пара автоматически снова попадает в кандидаты на следующем _rescan_pairs"],
    ["Изменение настроек",
     "Через TG /settings или Mini App — изменения применяются мгновенно"],
    ["Выпала пара (нет сигналов)",
     "Если с пары нет тиков 10+ минут — стрес-watchdog триггерит реконнект WS"],
]
story.append(make_table(refresh_rows, col_widths=[5 * cm, 11 * cm]))

# ═══ 5. МАРТИНГЕЙЛ ════════════════════════════════════════════════════════
story.append(PageBreak())
story.append(Paragraph("5. Мартингейл — как работает догон", h1_style))
story.append(Paragraph(
    "Мартингейл — это удвоение ставки после минуса, чтобы один плюс перекрыл всю серию минусов.",
    body_style
))
story.append(Paragraph("Поведение по шагам:", h2_style))

mg_rows = [
    ["Шаг", "Что происходит", "Ставка (база $1)"],
    ["1 (вход)", "Сигнал → заходим в сделку base_amount", "$1.00"],
    ["WIN", "Цикл сброшен, возврат в FREE-режим, ищем новый сигнал", "—"],
    ["LOSS → MG=1", "Удваиваем (× коэффициент 2.1), ждём новый сигнал на той же паре", "$2.10"],
    ["LOSS → MG=2", "Снова удваиваем", "$4.41"],
    ["LOSS → MG=3", "Снова", "$9.26"],
    ["…", "До max_steps (дефолт 10)", "…"],
    ["Стоп", "Если потери + следующая ставка > stop_sum ($1000) → пауза, ждём /resume", "—"],
]
story.append(make_table(mg_rows, col_widths=[3 * cm, 8.5 * cm, 4.5 * cm]))
story.append(Spacer(1, 6))
story.append(Paragraph(
    "<b>Тоггл martingale.enabled:</b> можно выключить. Тогда каждая сделка $1 (base_amount), "
    "после LOSS — поиск нового сигнала на ЛЮБОЙ паре, без догонов. Чистая стратегия без удвоений.",
    body_style
))

# ═══ 6. SEARCH-MODE ═══════════════════════════════════════════════════════
story.append(PageBreak())
story.append(Paragraph("6. Search-mode — поиск на всех парах", h1_style))
story.append(Paragraph(
    "Когда автоматически меняется пара (payout упал или достигнут лимит сделок) — бот не «прыгает» на одну "
    "конкретную альтернативу. Вместо этого он входит в режим <b>SEARCH</b>: сканирует ВСЕ допустимые пары "
    "и берёт первый же сигнал.",
    body_style
))

states_rows = [
    ["Состояние", "Условие", "Что делает"],
    ["FREE",
     "mg_step = 0",
     "Скан всех пар, первый сигнал → базовая сделка"],
    ["LOCKED",
     "mg_step > 0,\ncurrent_pair задана",
     "Ждёт сигнал на закреплённой паре\n(продолжение МГ-цикла)"],
    ["SEARCH",
     "mg_step > 0,\ncurrent_pair = None",
     "Сканирует все пары (исключая switched_pairs),\nпервый сигнал = новая закрепл. пара,\nМГ-шаг сохраняется"],
]
story.append(make_table(states_rows, col_widths=[2.5 * cm, 4.5 * cm, 9 * cm]))
story.append(Spacer(1, 6))
story.append(Paragraph(
    "Триггеры перехода в SEARCH:",
    h2_style
))
story.append(Paragraph(
    "• <b>Payout упал</b> ниже payout_floor (85%) на текущей паре<br/>"
    "• <b>Лимит сделок</b> на одной паре (max_trades_on_pair) достигнут<br/>"
    "• <b>Принудительный бан</b> после N минусов подряд (если включено)",
    body_style
))

# ═══ 7. ДОПОЛНИТЕЛЬНЫЕ ФИЛЬТРЫ ═════════════════════════════════════════════
story.append(PageBreak())
story.append(Paragraph("7. Дополнительные фильтры", h1_style))

story.append(Paragraph("Hour Whitelist — фильтр по часам", h2_style))
story.append(Paragraph(
    "После накопления статистики (1-2 недели) можно в Mini App → «По часам» нажать <b>«Применить как фильтр»</b>. "
    "Бот возьмёт только пары/часы с WR ≥ 70% и ≥ 5 сделок и будет торговать ИСКЛЮЧИТЕЛЬНО в этих окнах.",
    body_style
))
story.append(Paragraph(
    "<b>Пример:</b> EURUSD_otc показал 78% WR в 09:00-10:00 → фильтр пропустит торговлю по EURUSD_otc только в это окно. "
    "В 22:00 эту же пару бот пропустит даже если есть сигнал.",
    example_style
))

story.append(Paragraph("Двойная проверка WR1 (1000 свечей + 200 свечей)", h2_style))
story.append(Paragraph(
    "Пара должна пройти ОБЕ проверки одновременно:",
    body_style
))
story.append(Paragraph(
    "• <b>WR1 long</b> ≥ <b>min_wr1</b> (60%) — за 1000 свечей<br/>"
    "• <b>WR1 recent</b> ≥ <b>min_wr1_recent</b> (70%) — за 200 свечей<br/>"
    "Если хотя бы одна не проходит — пара отстраняется (SKIP или PAUSE).",
    body_style
))

story.append(Paragraph("Лимит сделок на одной паре", h2_style))
story.append(Paragraph(
    "<b>trading.max_trades_on_pair</b> (0 = без лимита). Если ставишь например 2 — после 2 сделок подряд "
    "на паре бот принудительно входит в search-mode и ищет другую пару. Защита от «застревания» на одной паре.",
    body_style
))

# ═══ 8. УПРАВЛЕНИЕ ═══════════════════════════════════════════════════════
story.append(PageBreak())
story.append(Paragraph("8. Управление ботом", h1_style))

story.append(Paragraph("Кнопки в Mini App", h2_style))
miniapp_rows = [
    ["Где", "Кнопка", "Что делает"],
    ["Статус", "⏸ Pause / ▶ Resume", "Поставить на паузу / снять"],
    ["Статус", "🔀 Сменить пару", "В активном цикле — выбрать другую пару (МГ сохр.)"],
    ["Статус", "🔄 Сбросить цикл", "Прервать МГ, вернуться в FREE-режим"],
    ["Настройки", "Слайдеры", "Все параметры стратегии (мгновенное применение)"],
    ["По часам", "📥 Экспорт CSV", "Скачать статистику для анализа"],
    ["По часам", "⭐ Применить фильтр", "Использовать лучшие часы как whitelist"],
    ["По часам", "🔓 Снять фильтр", "Отключить hour-whitelist"],
    ["По часам", "⤺ reset stats", "Сброс baseline (старые сделки скрыть)"],
]
story.append(make_table(miniapp_rows, col_widths=[2.5 * cm, 4 * cm, 9.5 * cm]))

story.append(Paragraph("Команды в Telegram", h2_style))
tg_rows = [
    ["Команда", "Что делает"],
    ["/status", "Статус бота + inline-кнопки управления"],
    ["/ping", "Диагностика (WS, задачи, баланс)"],
    ["/pause", "Пауза"],
    ["/resume", "Продолжить торговлю"],
    ["/balance", "Текущий баланс"],
    ["/settings", "Меню всех настроек"],
]
story.append(make_table(tg_rows, col_widths=[3 * cm, 13 * cm]))

# ═══ 9. АВАРИЙНЫЕ СИТУАЦИИ ═════════════════════════════════════════════════
story.append(PageBreak())
story.append(Paragraph("9. Что происходит при сбоях", h1_style))

incidents_rows = [
    ["Сбой", "Что делает бот", "Уведомление в TG"],
    ["WS не отвечает 90с",
     "Force-close + auto-reconnect",
     "⚠️ WebSocket замолчал на 90с"],
    ["WS не восстановился\nпосле 10 попыток",
     "Phase-2: retry каждые 2 мин,\nrelogin каждые 3 раунда",
     "❌ WebSocket не восстанавливается\n— возможно бан VPS IP"],
    ["WS восстановился",
     "Re-subscribe всех пар,\nпродолжение работы",
     "✅ WebSocket восстановлен"],
    ["NotAuthorized\n(ssid протух)",
     "Playwright-relogin →\nновый ssid → переподключение",
     "(тихо, без алерта если успех)"],
    ["Top-level краш",
     "Supervisor перезапускает run()\nс backoff 2-60с",
     "🔴 Бот падал и перезапустился"],
    ["Health watchdog (30 мин)",
     "Если recv-task мёртв ИЛИ\nфрейм >5мин ИЛИ балансa нет",
     "🩺 Watchdog: нашёл проблемы"],
]
story.append(make_table(incidents_rows, col_widths=[3.5 * cm, 5.5 * cm, 7 * cm]))

# ═══ 10. ШПАРГАЛКА ════════════════════════════════════════════════════════
story.append(PageBreak())
story.append(Paragraph("10. Шпаргалка по настройкам", h1_style))

settings_rows = [
    ["Параметр", "Дефолт", "Что регулирует"],
    ["filter.min_payout", "92", "Минимальный % выплаты для входа в сделку"],
    ["filter.payout_floor", "85", "При падении ниже — пара уходит в search-mode"],
    ["filter.max_losses_in_row", "3", ">N минусов подряд в истории → BAN"],
    ["filter.min_wr1", "60", "Минимум WR за 1000 свечей"],
    ["filter.min_wr1_recent", "70", "Минимум WR за 200 свечей (свежая форма)"],
    ["filter.recent_lookback_bars", "200", "Размер «recent» окна"],
    ["filter.ban_hours", "12", "Срок длительного BAN"],
    ["filter.pause_hours", "1", "Срок короткой PAUSE (за низкий recent WR1)"],
    ["filter.day_off_hours", "6", "Если вообще нет пар — пауза N часов"],
    ["filter.history_candles", "1060", "Сколько свечей грузим для анализа"],
    ["filter.stats_lookback_bars", "1000", "Окно статистики (для priority)"],
    ["trading.base_amount", "1", "Базовая ставка $"],
    ["trading.expiry_seconds", "120", "Длительность сделки (сек)"],
    ["trading.max_trades_on_pair", "0", "Лимит сделок на паре (0=выкл)"],
    ["trading.max_pair_switch_per_cycle", "1", "Сколько раз менять пару за цикл"],
    ["martingale.enabled", "true", "Вкл/выкл мартингейл"],
    ["martingale.coefficient", "2.1", "Множитель ставки после LOSS"],
    ["martingale.max_steps", "10", "Максимум шагов догона"],
    ["martingale.stop_sum", "1000", "Стоп-сумма потерь $"],
    ["schedule.enabled", "false", "Работать по расписанию или 24/7"],
    ["schedule.start_hour", "6", "Начало рабочего окна"],
    ["schedule.end_hour", "22", "Конец рабочего окна"],
]
story.append(make_table(settings_rows, col_widths=[6 * cm, 1.8 * cm, 8.2 * cm]))

# ═══ EOD ═══
story.append(PageBreak())
story.append(Spacer(1, 6 * cm))
story.append(Paragraph("Конец документа", title_style))
story.append(Paragraph(
    "Все настройки можно менять в реальном времени через Mini App или Telegram /settings — "
    "перезапуск бота не нужен. Изменения сохраняются на volume и переживают рестарты.",
    subtitle_style
))


# ────────────────────────────────────────────────────────────────────────────
def main():
    output_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "STRATEGY.pdf",
    )
    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        topMargin=2 * cm, bottomMargin=2 * cm,
        leftMargin=1.8 * cm, rightMargin=1.8 * cm,
        title="PO-Sig Bot — Стратегия",
        author="Andrii (PO-Sig Bot)",
    )
    doc.build(story, onFirstPage=page_number, onLaterPages=page_number)
    print(f"Wrote: {output_path}")
    print(f"Cyrillic font registered: {_has_cyr_font} (using {BASE_FONT}/{BOLD_FONT})")


if __name__ == "__main__":
    main()
