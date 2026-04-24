#!/usr/bin/env bash
# Launch headless Chromium with CDP, wait for it, then start the bot.
# Designed for Railway/Docker deploys.

set -euo pipefail

CHROME_BIN="${CHROME_BIN:-/ms-playwright/chromium-*/chrome-linux/chrome}"
# Resolve glob
CHROME_BIN_RESOLVED=$(ls $CHROME_BIN 2>/dev/null | head -n 1 || true)
if [ -z "$CHROME_BIN_RESOLVED" ]; then
  # Fallbacks
  for c in google-chrome chromium-browser chromium; do
    if command -v $c >/dev/null 2>&1; then CHROME_BIN_RESOLVED=$(command -v $c); break; fi
  done
fi
if [ -z "$CHROME_BIN_RESOLVED" ]; then
  echo "ERROR: no chromium binary found"; exit 1
fi
echo "Using Chrome: $CHROME_BIN_RESOLVED"

USER_DATA="${CHROME_USER_DATA_DIR:-/chrome-data}"
mkdir -p "$USER_DATA"

# Launch Chrome in background with CDP exposed locally.
# Flags chosen to minimize headless fingerprinting and keep the multi-chart
# layout viewport big enough for the 15-window grid (5 rows × 3 cols).
"$CHROME_BIN_RESOLVED" \
  --headless=new \
  --no-sandbox \
  --disable-dev-shm-usage \
  --disable-gpu \
  --disable-blink-features=AutomationControlled \
  --user-data-dir="$USER_DATA" \
  --remote-debugging-port=9222 \
  --remote-debugging-address=0.0.0.0 \
  --window-size=1800,1400 \
  "https://po-signals.com/en/app/charts" \
  > /tmp/chrome.log 2>&1 &

CHROME_PID=$!
echo "Chrome started (pid $CHROME_PID), waiting for CDP…"

# Wait for CDP to accept connections
for i in $(seq 1 40); do
  if curl -s -o /dev/null http://localhost:9222/json/version; then
    echo "CDP is up."
    break
  fi
  sleep 0.5
done

if ! curl -s -o /dev/null http://localhost:9222/json/version; then
  echo "ERROR: CDP did not come up within 20s"
  tail -n 50 /tmp/chrome.log || true
  exit 1
fi

# Trap: on exit, kill Chrome so the container restarts cleanly.
trap "kill $CHROME_PID 2>/dev/null || true" EXIT TERM INT

# Run the bot (foreground so Railway tracks it)
exec python3 main.py
