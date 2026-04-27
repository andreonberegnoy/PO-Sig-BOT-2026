#!/usr/bin/env bash
# Install po_control.py as a macOS launchd service.
# Runs at login, auto-restarts if crashed, accessible at http://localhost:5555/
#
# Usage:
#   ./tools/install_control_service.sh          → install (start now + auto-start on login)
#   ./tools/install_control_service.sh status   → show status
#   ./tools/install_control_service.sh logs     → tail service logs
#   ./tools/install_control_service.sh stop     → stop without uninstalling
#   ./tools/install_control_service.sh start    → start (after stop)
#   ./tools/install_control_service.sh uninstall → remove completely

set -euo pipefail

LABEL="com.po-sig-bot.control"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"

# Resolve absolute paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONTROL_PY="$PROJECT_DIR/tools/po_control.py"
PYTHON_BIN="$(which python3)"
LOG_DIR="$HOME/Library/Logs/po-sig-bot-control"

cmd="${1:-install}"

case "$cmd" in
    tunnel)
        # Start cloudflared tunnel for the control panel (port 5555).
        # Gives a public HTTPS URL → can be opened from phone via Telegram button.
        echo "→ Starting cloudflared tunnel for control panel..."
        if ! command -v cloudflared >/dev/null 2>&1; then
            echo "❌ cloudflared not installed on Mac. Install:"
            echo "    brew install cloudflared"
            exit 1
        fi
        # Kill old panel tunnel
        pkill -f "cloudflared tunnel.*5555" 2>/dev/null || true
        sleep 1
        # Start fresh
        mkdir -p "$LOG_DIR"
        nohup cloudflared tunnel --url http://localhost:5555 \
            > "$LOG_DIR/tunnel.log" 2>&1 &
        sleep 6
        URL=$(grep "trycloudflare.com" "$LOG_DIR/tunnel.log" | head -1 | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com')
        if [ -z "$URL" ]; then
            echo "❌ Failed to get tunnel URL. See $LOG_DIR/tunnel.log"
            exit 1
        fi
        echo "$URL" > "$LOG_DIR/panel-url.txt"
        echo ""
        echo "✅ Tunnel started!"
        echo ""
        echo "   Public URL: $URL"
        echo ""
        echo "   Saved to:   $LOG_DIR/panel-url.txt"
        echo ""
        echo "Next: copy this URL → BotFather → ваш бот → Bot Settings →"
        echo "      Menu Button → можно сделать второй кастомной командой"
        echo "      или просто использовать /panel в боте."
        echo ""
        echo "Чтобы остановить tunnel:"
        echo "    pkill -f 'cloudflared tunnel.*5555'"
        ;;

    install)
        echo "→ Installing po_control launchd service..."

        # Verify Python and script exist
        [ -f "$CONTROL_PY" ] || { echo "ERROR: $CONTROL_PY not found"; exit 1; }
        [ -x "$PYTHON_BIN" ] || { echo "ERROR: python3 not found"; exit 1; }

        mkdir -p "$LOG_DIR"
        mkdir -p "$(dirname "$PLIST_PATH")"

        # Generate plist
        cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON_BIN}</string>
        <string>${CONTROL_PY}</string>
    </array>

    <key>WorkingDirectory</key>
    <string>${PROJECT_DIR}</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <key>StandardOutPath</key>
    <string>${LOG_DIR}/control.log</string>

    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/control.err</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
EOF

        # Unload if already loaded (so we pick up changes)
        launchctl unload "$PLIST_PATH" 2>/dev/null || true
        launchctl load "$PLIST_PATH"

        sleep 2
        echo ""
        echo "✅ Service installed and started."
        echo ""
        echo "  Open in browser: http://localhost:5555/"
        echo "  Logs:            tail -f $LOG_DIR/control.log"
        echo "  Stop:            $0 stop"
        echo "  Uninstall:       $0 uninstall"
        echo ""
        echo "Service will auto-start at every login."
        ;;

    status)
        if [ ! -f "$PLIST_PATH" ]; then
            echo "Not installed. Run: $0 install"
            exit 1
        fi
        echo "→ Service status:"
        launchctl list | grep -E "PID|$LABEL" || echo "Not running."
        echo ""
        echo "→ Last 20 log lines:"
        if [ -f "$LOG_DIR/control.log" ]; then
            tail -20 "$LOG_DIR/control.log"
        else
            echo "(no logs yet)"
        fi
        ;;

    logs)
        echo "→ Tailing logs (Ctrl+C to exit):"
        echo "  $LOG_DIR/control.log"
        echo "---"
        tail -f "$LOG_DIR/control.log" "$LOG_DIR/control.err" 2>/dev/null
        ;;

    stop)
        if [ ! -f "$PLIST_PATH" ]; then
            echo "Not installed."
            exit 0
        fi
        launchctl unload "$PLIST_PATH" 2>/dev/null || true
        echo "✅ Stopped (will not auto-start until you run: $0 start)"
        ;;

    start)
        if [ ! -f "$PLIST_PATH" ]; then
            echo "Not installed. Run: $0 install"
            exit 1
        fi
        launchctl load "$PLIST_PATH"
        sleep 1
        echo "✅ Started. http://localhost:5555/"
        ;;

    restart)
        $0 stop
        sleep 1
        $0 start
        ;;

    uninstall)
        if [ ! -f "$PLIST_PATH" ]; then
            echo "Already uninstalled."
            exit 0
        fi
        launchctl unload "$PLIST_PATH" 2>/dev/null || true
        rm -f "$PLIST_PATH"
        echo "✅ Service uninstalled."
        echo "  (Logs preserved at $LOG_DIR — delete manually if needed)"
        ;;

    *)
        echo "Usage: $0 [install|status|logs|stop|start|restart|uninstall|tunnel]"
        echo ""
        echo "  install   — register service + start (default if no arg)"
        echo "  status    — show running state + recent logs"
        echo "  logs      — tail logs in real time"
        echo "  stop      — stop without uninstalling"
        echo "  start     — start (after stop)"
        echo "  restart   — stop + start"
        echo "  uninstall — remove service completely"
        echo "  tunnel    — start cloudflared tunnel for phone access"
        exit 1
        ;;
esac
