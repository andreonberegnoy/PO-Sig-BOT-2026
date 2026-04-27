"""Generate a deploy cheat-sheet PDF with all common commands.

Run: python3 tools/make_deploy_pdf.py
Output: DEPLOY_CHEATSHEET.pdf in the project root.
"""

import os
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.colors import HexColor, white
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, PageBreak,
    Table, TableStyle,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


# ─── Fonts: register a TTF that has Cyrillic glyphs ───────────────────────
def _register_font():
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont("Cyr", path))
                return True
            except Exception:
                pass
    return False


_has_cyr = _register_font()
BASE_FONT = "Cyr" if _has_cyr else "Helvetica"
MONO_FONT = "Courier"   # ASCII-only, always available

# ─── Colors ─────────────────────────────────────────────────────────────
NAVY = HexColor("#0f1115")
ACCENT = HexColor("#6db4ff")
GREEN = HexColor("#22c55e")
RED = HexColor("#ef4444")
GRAY = HexColor("#5a6473")
LIGHT_BG = HexColor("#f3f5f8")
CODE_BG = HexColor("#1a1d23")
CODE_FG = HexColor("#e8eaed")

# ─── Styles ─────────────────────────────────────────────────────────────
styles = getSampleStyleSheet()

title_style = ParagraphStyle(
    "TitleBig", parent=styles["Title"],
    fontName=BASE_FONT, fontSize=26, leading=32,
    textColor=NAVY, alignment=TA_CENTER, spaceAfter=8,
)

subtitle_style = ParagraphStyle(
    "Subtitle", parent=styles["Normal"],
    fontName=BASE_FONT, fontSize=13, leading=17,
    textColor=GRAY, alignment=TA_CENTER, spaceAfter=20,
)

h1_style = ParagraphStyle(
    "H1", parent=styles["Heading1"],
    fontName=BASE_FONT, fontSize=18, leading=22,
    textColor=ACCENT, spaceAfter=10, spaceBefore=14,
)

h2_style = ParagraphStyle(
    "H2", parent=styles["Heading2"],
    fontName=BASE_FONT, fontSize=13, leading=16,
    textColor=NAVY, spaceAfter=4, spaceBefore=10,
)

body_style = ParagraphStyle(
    "Body", parent=styles["Normal"],
    fontName=BASE_FONT, fontSize=10, leading=14,
    textColor=NAVY, alignment=TA_LEFT, spaceAfter=6,
)

note_style = ParagraphStyle(
    "Note", parent=body_style,
    fontSize=9, textColor=GRAY, leftIndent=10, spaceAfter=4,
)

code_style = ParagraphStyle(
    "Code", parent=styles["Normal"],
    fontName=MONO_FONT, fontSize=9, leading=12,
    textColor=CODE_FG, backColor=CODE_BG,
    borderColor=GRAY, borderWidth=0.5, borderPadding=8,
    leftIndent=0, rightIndent=0, spaceAfter=10, spaceBefore=4,
)

warning_style = ParagraphStyle(
    "Warning", parent=body_style,
    fontSize=10, textColor=RED, leftIndent=10,
    backColor=HexColor("#fff4f4"), borderColor=RED, borderWidth=0.3,
    borderPadding=6, spaceAfter=8,
)


def page_number(canvas, doc):
    canvas.saveState()
    canvas.setFont(BASE_FONT, 9)
    canvas.setFillColor(GRAY)
    canvas.drawRightString(A4[0] - 1.5 * cm, 1 * cm, f"стр. {doc.page}")
    canvas.drawString(1.5 * cm, 1 * cm, "PO-Sig Bot — Deploy Cheat Sheet")
    canvas.restoreState()


def code_block(text: str):
    """Render a multi-line shell command as a styled code block."""
    # Escape HTML chars for reportlab Paragraph
    text = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    # Replace newlines with <br/> for Paragraph
    text = text.replace("\n", "<br/>\n")
    return Paragraph(text, code_style)


def make_table(rows, col_widths=None, header=True):
    t = Table(rows, colWidths=col_widths, hAlign="LEFT")
    style = [
        ("FONT", (0, 0), (-1, -1), BASE_FONT, 9),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("ROWBACKGROUNDS", (0, 1 if header else 0), (-1, -1), [white, LIGHT_BG]),
        ("BOX", (0, 0), (-1, -1), 0.4, GRAY),
    ]
    if header:
        style.extend([
            ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
            ("TEXTCOLOR", (0, 0), (-1, 0), white),
            ("FONT", (0, 0), (-1, 0), BASE_FONT, 9),
        ])
    t.setStyle(TableStyle(style))
    return t


# ────────────────────────────────────────────────────────────────────────
story = []

# ═══ COVER ════════════════════════════════════════════════════════════
story.append(Spacer(1, 4 * cm))
story.append(Paragraph("Deploy Cheat Sheet", title_style))
story.append(Paragraph(
    "Все команды для управления ботом на VPS",
    subtitle_style,
))
story.append(Spacer(1, 1.2 * cm))

cover_meta = [
    ["VPS-провайдер", "Hetzner Cloud (Falkenstein)"],
    ["IP-адрес", "178.105.36.60"],
    ["Логин", "root (по SSH-ключу)"],
    ["Папка проекта", "/opt/po-bot"],
    ["Папка деплоя", "/opt/po-bot/deploy"],
    ["Контейнер", "po-bot"],
]
story.append(make_table(cover_meta, col_widths=[5 * cm, 11 * cm], header=False))
story.append(PageBreak())

# ═══ 1. ПОДКЛЮЧЕНИЕ ══════════════════════════════════════════════════
story.append(Paragraph("1. Подключение к серверу", h1_style))

story.append(Paragraph("На Mac открой Terminal и выполни:", body_style))
story.append(code_block("ssh root@178.105.36.60"))

story.append(Paragraph(
    "Должен зайти БЕЗ пароля (по SSH-ключу). Приглашение станет:",
    body_style,
))
story.append(code_block("root@po-bot:~#"))

story.append(Paragraph("Если просит пароль — SSH-ключ не подцепился", h2_style))
story.append(code_block(
    "# На Mac сначала проверь:\n"
    "ls -la ~/.ssh/id_ed25519\n\n"
    "# Если ключ есть, добавь в агент:\n"
    "ssh-add ~/.ssh/id_ed25519\n\n"
    "# Или явно укажи ключ:\n"
    "ssh -i ~/.ssh/id_ed25519 root@178.105.36.60"
))

story.append(Paragraph("Если warning о смене ключа сервера", h2_style))
story.append(Paragraph(
    "Это нормально если сервер пересоздавался. Очисти старый отпечаток:",
    note_style,
))
story.append(code_block(
    "ssh-keygen -R 178.105.36.60\n"
    "ssh root@178.105.36.60   # снова — пиши yes когда спросит fingerprint"
))

# ═══ 2. СТАНДАРТНЫЙ ДЕПЛОЙ ═══════════════════════════════════════════
story.append(PageBreak())
story.append(Paragraph("2. Стандартный деплой обновлений", h1_style))

story.append(Paragraph(
    "Когда в репо появились новые коммиты — подтянуть и пересобрать:",
    body_style,
))

story.append(code_block(
    "cd /opt/po-bot\n"
    "git pull\n"
    "cd deploy\n"
    "docker compose down\n"
    "docker compose up -d --build\n"
    "docker compose logs -f po-bot"
))

story.append(Paragraph(
    "Сборка ~30-60 секунд. Жди в логах строку:",
    body_style,
))
story.append(code_block(
    "feed.po_direct: feed ready (assets=183, balance_real=...)\n"
    "main: 🚀 starting — mode=real"
))

story.append(Paragraph(
    "Когда увидишь — нажми Ctrl+C. Контейнер продолжит работать в фоне (флаг -d).",
    note_style,
))

story.append(Paragraph("Одной строкой (когда лень):", h2_style))
story.append(code_block(
    "ssh root@178.105.36.60 'cd /opt/po-bot && git pull && "
    "cd deploy && docker compose down && docker compose up -d --build'"
))

# ═══ 3. КОНФЛИКТЫ GIT ════════════════════════════════════════════════
story.append(PageBreak())
story.append(Paragraph("3. Если git pull ругается", h1_style))

story.append(Paragraph(
    "Типичная ошибка: Your branch is behind / Changes not staged for commit.",
    body_style,
))
story.append(Paragraph(
    "Локальные правки в config.yaml блокируют pull. Решение — сбросить и применить заново:",
    body_style,
))

story.append(code_block(
    "cd /opt/po-bot\n"
    "git checkout -- config.yaml      # откатить локальные правки\n"
    "git pull                          # должно пройти\n"
    "sed -i 's/^mode: paper/mode: real/' config.yaml   # вернуть mode=real\n"
    "git log -1 --oneline              # подтвердить свежий коммит"
))

story.append(Paragraph("Hard reset (если ничего не помогает)", h2_style))
story.append(Paragraph(
    "Удалит ВСЕ локальные правки и подтянет версию с GitHub:",
    note_style,
))

story.append(code_block(
    "cd /opt/po-bot\n"
    "git fetch origin\n"
    "git reset --hard origin/main\n"
    "sed -i 's/^mode: paper/mode: real/' config.yaml\n"
    "git log -1 --oneline"
))

# ═══ 4. ПРОВЕРКА СОСТОЯНИЯ ═══════════════════════════════════════════
story.append(PageBreak())
story.append(Paragraph("4. Проверка состояния бота", h1_style))

checks = [
    ["Команда", "Что показывает"],
    ["docker compose ps",
     "Запущен ли контейнер (статус Up / Restarting / Down)"],
    ["curl -sI http://localhost:8080/health",
     "Отвечает ли API (HTTP 200 = живой)"],
    ["docker compose logs --tail=30 po-bot",
     "Последние 30 строк логов"],
    ["docker compose logs -f po-bot",
     "Логи в реальном времени (Ctrl+C для выхода)"],
    ["docker compose logs --tail=100 po-bot \\| grep ERROR",
     "Найти все ошибки в последних 100 строках"],
    ["docker stats --no-stream",
     "RAM/CPU контейнера"],
    ["df -h",
     "Сколько места на диске"],
    ["free -h",
     "Свободная RAM"],
]
story.append(make_table(checks, col_widths=[7 * cm, 9 * cm]))

# ═══ 5. ПЕРЕЗАПУСК ══════════════════════════════════════════════════════
story.append(PageBreak())
story.append(Paragraph("5. Перезапустить бота", h1_style))

story.append(Paragraph("Простой рестарт (без обновления кода)", h2_style))
story.append(code_block(
    "cd /opt/po-bot/deploy\n"
    "docker compose restart po-bot\n"
    "docker compose logs -f po-bot"
))

story.append(Paragraph("Force-rebuild без кеша (если что-то сломано)", h2_style))
story.append(Paragraph(
    "Когда docker не подхватывает новый код несмотря на git pull:",
    note_style,
))
story.append(code_block(
    "cd /opt/po-bot/deploy\n"
    "docker compose down\n"
    "docker compose build --no-cache po-bot   # 3-5 мин, всё с нуля\n"
    "docker compose up -d --force-recreate\n"
    "docker compose logs -f po-bot"
))

# ═══ 6. CLOUDFLARED TUNNEL ════════════════════════════════════════════
story.append(PageBreak())
story.append(Paragraph("6. Mini App URL (Cloudflare Tunnel)", h1_style))

story.append(Paragraph(
    "Mini App требует HTTPS. Используем Cloudflare Tunnel — бесплатно, "
    "но URL временный (меняется при перезапуске cloudflared).",
    body_style,
))

story.append(Paragraph("Узнать текущий URL", h2_style))
story.append(code_block(
    'grep "trycloudflare.com" /var/log/cloudflared.log | tail -1'
))

story.append(Paragraph("Перезапустить туннель (если умер)", h2_style))
story.append(code_block(
    "pkill cloudflared 2>/dev/null\n"
    "sleep 2\n"
    "nohup cloudflared tunnel --url http://127.0.0.1:8080 \\\n"
    "    > /var/log/cloudflared.log 2>&1 &\n"
    "sleep 5\n"
    'grep "trycloudflare.com" /var/log/cloudflared.log | tail -1'
))

story.append(Paragraph("Получишь НОВЫЙ URL — обнови в BotFather", h2_style))

bf_steps = [
    ["1.", "В Telegram → @BotFather"],
    ["2.", "/mybots → выбери бота"],
    ["3.", "Bot Settings → Menu Button"],
    ["4.", "Edit menu button URL"],
    ["5.", "Вставь новый URL с ?v=N (увеличивай N каждый раз)"],
    ["6.", "Save → подтверждение 'Menu button has been changed'"],
]
story.append(make_table(bf_steps, col_widths=[1 * cm, 15 * cm], header=False))

story.append(Paragraph("Закрой Telegram полностью (свайп вверх → закрыть карточку) и открой заново.", note_style))

# ═══ 7. WORKFLOW ОБНОВЛЕНИЯ MINI APP ═════════════════════════════════
story.append(PageBreak())
story.append(Paragraph("7. Когда не видно изменений в Mini App", h1_style))

story.append(Paragraph(
    "Telegram кеширует Mini App. После git pull + rebuild новый код в "
    "контейнере, но Telegram грузит старый JS из кеша.",
    body_style,
))

story.append(Paragraph("Шаг 1 — проверь что код в контейнере свежий", h2_style))
story.append(code_block(
    'docker compose exec po-bot grep -c "По часам" /app/miniapp/index.html\n'
    'docker compose exec po-bot grep -c "asset_categories" /app/miniapp/app.js'
))
story.append(Paragraph(
    "Должно вернуть >0 для обеих. Если 0 — контейнер старый, делай force-rebuild.",
    note_style,
))

story.append(Paragraph("Шаг 2 — cache-bust через BotFather", h2_style))
story.append(Paragraph(
    "Если код в контейнере свежий, но в Telegram старый — обнови URL с увеличением "
    "?v=N. Telegram считает другой URL = другое приложение и тянет всё свежее.",
    body_style,
))

story.append(Paragraph("Шаг 3 — полный перезапуск Telegram", h2_style))
restart_steps = [
    ["1.", "Свайп вверх по экрану → откроется список приложений"],
    ["2.", "Найди карточку Telegram → свайп её вверх (закрыть)"],
    ["3.", "Открой Telegram заново через иконку"],
    ["4.", "Открой бот → нажми Menu Button"],
]
story.append(make_table(restart_steps, col_widths=[1 * cm, 15 * cm], header=False))

# ═══ 8. БЭКАПЫ ════════════════════════════════════════════════════════
story.append(PageBreak())
story.append(Paragraph("8. Бэкап данных", h1_style))

story.append(Paragraph(
    "Папка data/ содержит SQLite с историей сделок, настройками, "
    "кешем свечей. Бэкапь раз в неделю чтобы не потерять.",
    body_style,
))

story.append(code_block(
    "# Создать архив\n"
    "cd /opt/po-bot/deploy\n"
    "tar -czf /root/po-bot-backup-$(date +%F).tar.gz data/\n\n"
    "# Посмотреть какие бэкапы есть\n"
    "ls -lh /root/po-bot-backup-*.tar.gz"
))

story.append(Paragraph("Скачать на Mac (выполни на Mac)", h2_style))
story.append(code_block(
    "scp root@178.105.36.60:/root/po-bot-backup-*.tar.gz ~/Downloads/"
))

story.append(Paragraph("Восстановить из бэкапа на VPS", h2_style))
story.append(code_block(
    "cd /opt/po-bot/deploy\n"
    "docker compose down\n"
    "rm -rf data/\n"
    "tar -xzf /root/po-bot-backup-2026-04-27.tar.gz\n"
    "docker compose up -d"
))

# ═══ 9. АВАРИЙНЫЙ СБРОС ════════════════════════════════════════════════
story.append(PageBreak())
story.append(Paragraph("9. Аварийный сброс (крайний случай)", h1_style))

story.append(Paragraph(
    "Если совсем ничего не работает — полная перезаливка с нуля. "
    "Удалит все Docker-кеши, пересоберёт с нуля, сбросит код к удалённому. "
    "Volume data/ сохранится (история не теряется).",
    warning_style,
))

story.append(code_block(
    "cd /opt/po-bot/deploy\n"
    "docker compose down\n"
    "docker system prune -af              # удалит все Docker-образы (5-10 мин пересборка)\n\n"
    "cd /opt/po-bot\n"
    "git fetch origin\n"
    "git reset --hard origin/main         # код из GitHub\n"
    "sed -i 's/^mode: paper/mode: real/' config.yaml\n\n"
    "cd deploy\n"
    "docker compose up -d --build\n"
    "docker compose logs -f po-bot"
))

# ═══ 10. БЫСТРАЯ СПРАВКА ═══════════════════════════════════════════════
story.append(PageBreak())
story.append(Paragraph("10. Быстрая справка — самое нужное", h1_style))

story.append(Paragraph("Подключиться", h2_style))
story.append(code_block("ssh root@178.105.36.60"))

story.append(Paragraph("Стандартный деплой (95% случаев)", h2_style))
story.append(code_block(
    "cd /opt/po-bot && git pull && cd deploy && \\\n"
    "  docker compose down && docker compose up -d --build\n"
    "docker compose logs -f po-bot"
))

story.append(Paragraph("Посмотреть состояние", h2_style))
story.append(code_block(
    "docker compose ps\n"
    "docker compose logs --tail=30 po-bot"
))

story.append(Paragraph("Перезапустить (без обновления)", h2_style))
story.append(code_block("docker compose restart po-bot"))

story.append(Paragraph("URL Mini App", h2_style))
story.append(code_block(
    'grep "trycloudflare.com" /var/log/cloudflared.log | tail -1'
))

story.append(Paragraph("Бэкап", h2_style))
story.append(code_block(
    "cd /opt/po-bot/deploy && \\\n"
    "  tar -czf /root/po-bot-backup-$(date +%F).tar.gz data/"
))

# ═══ EOD ═══
story.append(PageBreak())
story.append(Spacer(1, 4 * cm))
story.append(Paragraph("Конец документа", title_style))
story.append(Spacer(1, 1 * cm))
story.append(Paragraph(
    "Распечатай или сохрани на iPad / в Notes. Команды одинаковые независимо "
    "от того сколько времени прошло — workflow стабильный.",
    subtitle_style,
))


# ────────────────────────────────────────────────────────────────────────
def main():
    output_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "DEPLOY_CHEATSHEET.pdf",
    )
    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        topMargin=2 * cm, bottomMargin=2 * cm,
        leftMargin=1.8 * cm, rightMargin=1.8 * cm,
        title="PO-Sig Bot — Deploy Cheat Sheet",
        author="Andrii (PO-Sig Bot)",
    )
    doc.build(story, onFirstPage=page_number, onLaterPages=page_number)
    print(f"Wrote: {output_path}")
    size = os.path.getsize(output_path)
    print(f"Size: {size} bytes ({size/1024:.1f} KB)")


if __name__ == "__main__":
    main()
