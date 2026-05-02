#!/usr/bin/env bash
# Заливает свежий state.json (от tools/make_storage_state.py) на VPS,
# обновляет PO_STORAGE_STATE_B64 в /opt/po-bot/deploy/.env и
# перезапускает контейнер po-bot.
#
# Использование (с Mac):
#     ./tools/update_po_session.sh             # ищет ./state.json
#     ./tools/update_po_session.sh path/to/state.json
#
# Переменные окружения (опционально):
#     VPS=root@37.27.13.173                    # SSH-таргет (default)
#
set -euo pipefail

VPS="${VPS:-root@37.27.13.173}"
STATE="${1:-state.json}"

if [[ ! -f "$STATE" ]]; then
    echo "❌ Не найден $STATE." >&2
    echo "   Сначала сгенерируй cookies:  python3 tools/make_storage_state.py" >&2
    exit 1
fi

echo "📦 Кодирую $STATE в base64…"
B64=$(base64 -i "$STATE" | tr -d '\n')
echo "   длина: ${#B64} символов"

echo "🚀 Заливаю на $VPS и перезапускаю po-bot…"
ssh "$VPS" "B64='$B64' bash -s" <<'REMOTE'
set -euo pipefail
ENV=/opt/po-bot/deploy/.env
[[ -f "$ENV" ]] || { echo "❌ $ENV не найден на VPS"; exit 1; }
cp "$ENV" "${ENV}.bak.$(date +%s)"
if grep -q '^PO_STORAGE_STATE_B64=' "$ENV"; then
    sed -i "s|^PO_STORAGE_STATE_B64=.*|PO_STORAGE_STATE_B64=${B64}|" "$ENV"
    echo "   ✓ заменил существующую строку"
else
    echo "PO_STORAGE_STATE_B64=${B64}" >> "$ENV"
    echo "   ✓ добавил новую строку"
fi
cd /opt/po-bot/deploy
docker compose restart po-bot
echo "   ✓ контейнер перезапущен"
sleep 4
docker compose ps po-bot --format "{{.Status}}"
REMOTE

echo
echo "✅ Готово. Через ~30с в TG должно прийти подтверждение что бот ожил."
