#!/usr/bin/env python3
"""Local control panel for the PO-Sig bot on VPS.

Run on Mac:
    python3 tools/po_control.py
    # → browser opens to http://localhost:5555/

Click buttons → SSH commands run on VPS → output streamed back.

No dependencies beyond stdlib.
"""

import http.server
import json
import os
import shlex
import socketserver
import subprocess
import sys
import threading
import webbrowser
from urllib.parse import urlparse, parse_qs

# ─── CONFIG ───────────────────────────────────────────────────────────
VPS_HOST = "178.105.36.60"
VPS_USER = "root"
PORT = 5555


# ─── ACTION DEFINITIONS ───────────────────────────────────────────────
# Each action has:
#   label    — button text
#   icon     — emoji
#   color    — button color class
#   confirm  — show JS confirm dialog before running
#   command  — shell command to execute via SSH (single string)
ACTIONS = [
    {
        "id": "deploy",
        "label": "Deploy (git pull + rebuild)",
        "icon": "🚀",
        "color": "primary",
        "confirm": False,
        "command": (
            "cd /opt/po-bot && git pull && cd deploy && "
            "docker compose down && docker compose up -d --build && "
            "docker compose logs --tail=20 po-bot"
        ),
    },
    {
        "id": "restart",
        "label": "Restart container",
        "icon": "🔄",
        "color": "primary",
        "confirm": False,
        "command": (
            "cd /opt/po-bot/deploy && docker compose restart po-bot && "
            "sleep 3 && docker compose logs --tail=15 po-bot"
        ),
    },
    {
        "id": "logs",
        "label": "Last 50 logs",
        "icon": "📜",
        "color": "ghost",
        "confirm": False,
        "command": (
            "cd /opt/po-bot/deploy && docker compose logs --tail=50 po-bot "
            "| grep -v 'GET /api/status' | grep -v 'GET /health'"
        ),
    },
    {
        "id": "status",
        "label": "Status check",
        "icon": "🩺",
        "color": "ghost",
        "confirm": False,
        "command": (
            "cd /opt/po-bot/deploy && "
            "echo '=== docker compose ps ===' && docker compose ps && "
            "echo '' && echo '=== /health ===' && "
            "curl -sI http://localhost:8080/health && "
            "echo '' && echo '=== git ===' && "
            "cd /opt/po-bot && git log -1 --oneline && "
            "echo '' && echo '=== disk ===' && df -h / && "
            "echo '' && echo '=== ram ===' && free -h"
        ),
    },
    {
        "id": "miniapp_url",
        "label": "Mini App URL",
        "icon": "🌐",
        "color": "ghost",
        "confirm": False,
        "command": (
            "echo '=== Current tunnel URL ===' && "
            "grep 'trycloudflare.com' /var/log/cloudflared.log | tail -1 && "
            "echo '' && echo '=== cloudflared process ===' && "
            "ps aux | grep cloudflared | grep -v grep || echo 'NOT RUNNING'"
        ),
    },
    {
        "id": "tunnel_restart",
        "label": "Restart Mini App tunnel",
        "icon": "🔁",
        "color": "warning",
        "confirm": True,
        "command": (
            "pkill cloudflared 2>/dev/null; sleep 2; "
            "nohup cloudflared tunnel --url http://127.0.0.1:8080 "
            "> /var/log/cloudflared.log 2>&1 & "
            "sleep 6; "
            "echo '=== New tunnel URL ===' && "
            "grep 'trycloudflare.com' /var/log/cloudflared.log | tail -1 && "
            "echo '' && echo 'PASTE THIS URL INTO BotFather (with ?v=N)'"
        ),
    },
    {
        "id": "backup",
        "label": "Backup data/",
        "icon": "💾",
        "color": "ghost",
        "confirm": False,
        "command": (
            "cd /opt/po-bot/deploy && "
            "tar -czf /root/po-bot-backup-$(date +%F).tar.gz data/ && "
            "ls -lh /root/po-bot-backup-*.tar.gz"
        ),
    },
    {
        "id": "force_rebuild",
        "label": "Force rebuild --no-cache",
        "icon": "🔥",
        "color": "warning",
        "confirm": True,
        "command": (
            "cd /opt/po-bot/deploy && docker compose down && "
            "docker compose build --no-cache po-bot && "
            "docker compose up -d --force-recreate && "
            "sleep 5 && docker compose logs --tail=20 po-bot"
        ),
    },
    {
        "id": "watch_logs",
        "label": "Live logs (10 sec)",
        "icon": "👁",
        "color": "ghost",
        "confirm": False,
        "command": (
            "cd /opt/po-bot/deploy && "
            "timeout 10 docker compose logs -f --tail=5 po-bot 2>&1 || "
            "echo '--- 10 seconds elapsed ---'"
        ),
    },
    {
        "id": "git_log",
        "label": "Last 5 commits on VPS",
        "icon": "📖",
        "color": "ghost",
        "confirm": False,
        "command": "cd /opt/po-bot && git log -5 --oneline",
    },
]


# ─── HTML PAGE ────────────────────────────────────────────────────────
def render_html() -> str:
    button_rows = []
    for a in ACTIONS:
        confirm_attr = ' data-confirm="1"' if a["confirm"] else ""
        button_rows.append(
            f'<button class="btn {a["color"]}" data-action="{a["id"]}"{confirm_attr}>'
            f'<span class="icon">{a["icon"]}</span>'
            f'<span class="label">{a["label"]}</span>'
            f'</button>'
        )
    buttons_html = "\n".join(button_rows)

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PO-Sig Bot Control</title>
<style>
  :root {{
    --bg: #0f1115;
    --fg: #e8eaed;
    --hint: #8a93a0;
    --primary: #6db4ff;
    --primary-bg: rgba(109,180,255,.15);
    --warn: #fdbe45;
    --warn-bg: rgba(253,190,69,.12);
    --ok: #22c55e;
    --err: #ef4444;
    --card: #1a1d23;
    --border: rgba(255,255,255,.08);
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg); color: var(--fg);
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro", sans-serif;
    font-size: 14px; min-height: 100vh; padding: 24px;
  }}
  header {{
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid var(--border);
  }}
  h1 {{ font-size: 20px; font-weight: 600; }}
  .vps-info {{ color: var(--hint); font-size: 12px; font-family: monospace; }}
  .grid {{
    display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 10px; margin-bottom: 20px;
  }}
  .btn {{
    display: flex; align-items: center; gap: 10px;
    padding: 14px 16px; border-radius: 10px;
    border: 1px solid var(--border);
    background: var(--card); color: var(--fg);
    cursor: pointer; font-family: inherit; font-size: 14px;
    text-align: left; transition: all .15s;
  }}
  .btn:hover {{ border-color: var(--primary); background: var(--primary-bg); }}
  .btn:disabled {{ opacity: .4; cursor: not-allowed; }}
  .btn .icon {{ font-size: 22px; }}
  .btn.primary {{ border-color: var(--primary); background: var(--primary-bg); }}
  .btn.warning {{ border-color: var(--warn); }}
  .btn.warning:hover {{ background: var(--warn-bg); }}
  .output {{
    background: #000; color: #d4d4d4;
    border: 1px solid var(--border); border-radius: 8px;
    padding: 14px; min-height: 280px; max-height: 60vh; overflow: auto;
    font-family: "SF Mono", Menlo, Consolas, monospace;
    font-size: 12px; line-height: 1.5; white-space: pre-wrap; word-break: break-all;
  }}
  .output .meta {{ color: var(--hint); }}
  .output .ok {{ color: var(--ok); }}
  .output .err {{ color: var(--err); }}
  .output .running::after {{
    content: "▋"; color: var(--primary); animation: blink 1s infinite;
  }}
  @keyframes blink {{ 0%, 50% {{ opacity: 1; }} 50.01%, 100% {{ opacity: 0; }} }}
  .clear-btn {{
    margin-bottom: 8px; background: transparent; color: var(--hint);
    border: 1px solid var(--border); padding: 4px 10px; border-radius: 5px;
    font-size: 12px; cursor: pointer;
  }}
  .clear-btn:hover {{ color: var(--fg); }}
</style>
</head>
<body>

<header>
  <h1>🤖 PO-Sig Bot Control</h1>
  <div class="vps-info">{VPS_USER}@{VPS_HOST}</div>
</header>

<div class="grid">
{buttons_html}
</div>

<button class="clear-btn" id="clear-btn">Очистить вывод</button>

<div class="output" id="output"><span class="meta">Нажми кнопку чтобы выполнить команду на VPS.</span></div>

<script>
const out = document.getElementById("output");
const clearBtn = document.getElementById("clear-btn");

function append(text, cls = "") {{
  const span = document.createElement("span");
  if (cls) span.className = cls;
  span.textContent = text;
  out.appendChild(span);
  out.scrollTop = out.scrollHeight;
}}

clearBtn.addEventListener("click", () => {{
  out.innerHTML = '<span class="meta">Очищено.</span>';
}});

document.querySelectorAll(".btn[data-action]").forEach(btn => {{
  btn.addEventListener("click", async () => {{
    const action = btn.dataset.action;
    const label = btn.querySelector(".label").textContent;

    if (btn.dataset.confirm === "1") {{
      if (!confirm(`Выполнить "${{label}}"?\\n\\nЭто действие может занять время или изменить состояние.`)) return;
    }}

    // Disable all buttons during execution
    document.querySelectorAll(".btn").forEach(b => b.disabled = true);

    out.innerHTML = "";
    append(`▸ ${{label}}\\n`, "meta");
    append("─".repeat(60) + "\\n\\n", "meta");

    const runningSpan = document.createElement("span");
    runningSpan.className = "running";
    runningSpan.textContent = "запущено... ";
    out.appendChild(runningSpan);

    try {{
      const res = await fetch(`/run/${{action}}`, {{ method: "POST" }});
      const text = await res.text();

      runningSpan.remove();

      if (res.ok) {{
        append(text || "(no output)", "");
        append("\\n\\n─ done ─", "ok");
      }} else {{
        append(text, "err");
        append("\\n\\n─ failed ─", "err");
      }}
    }} catch (e) {{
      runningSpan.remove();
      append(`Network error: ${{e.message}}`, "err");
    }} finally {{
      document.querySelectorAll(".btn").forEach(b => b.disabled = false);
    }}
  }});
}});
</script>
</body>
</html>"""


# ─── HTTP HANDLER ─────────────────────────────────────────────────────
class ControlHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Quieter logs
        sys.stderr.write(f"  {self.address_string()} - {format % args}\n")

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            html = render_html()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html.encode("utf-8"))))
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
            return
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path.startswith("/run/"):
            action_id = path[len("/run/"):]
            action = next((a for a in ACTIONS if a["id"] == action_id), None)
            if not action:
                self.send_error(404, f"Unknown action: {action_id}")
                return
            self._run_command(action["command"])
            return
        self.send_error(404)

    def _run_command(self, remote_cmd: str):
        ssh_cmd = [
            "ssh",
            "-o", "BatchMode=yes",          # no password prompt
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            f"{VPS_USER}@{VPS_HOST}",
            remote_cmd,
        ]
        try:
            result = subprocess.run(
                ssh_cmd,
                capture_output=True, text=True,
                timeout=300,   # 5 min max per command
            )
            output = result.stdout
            if result.stderr:
                output += "\n--- stderr ---\n" + result.stderr
            status = 200 if result.returncode == 0 else 500
            self._send_text(status, output or "(no output)")
        except subprocess.TimeoutExpired:
            self._send_text(500, "Command timed out after 5 minutes")
        except FileNotFoundError:
            self._send_text(500, "ssh command not found on Mac. "
                                  "Install: xcode-select --install")
        except Exception as e:
            self._send_text(500, f"Error: {type(e).__name__}: {e}")

    def _send_text(self, status: int, body: str):
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        body_bytes = body.encode("utf-8")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)


# ─── ENTRY POINT ──────────────────────────────────────────────────────
def main():
    print(f"╭─ PO-Sig Bot Control Panel ─" + "─" * 30)
    print(f"│  VPS:    {VPS_USER}@{VPS_HOST}")
    print(f"│  URL:    http://localhost:{PORT}/")
    print(f"│  Stop:   Ctrl+C")
    print(f"╰─" + "─" * 56)
    print()

    # Start server
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", PORT), ControlHandler) as httpd:
        # Open browser shortly after server starts
        threading.Timer(0.5, lambda: webbrowser.open(f"http://localhost:{PORT}/")).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  Bye!")


if __name__ == "__main__":
    main()
