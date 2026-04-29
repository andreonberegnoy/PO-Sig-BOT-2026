# Claude Code context for PO-Sig Bot

This file is auto-loaded by Claude Code at the start of every session.
It gives Claude immediate context about the project so we don't waste tokens
on rediscovery.

## What this project is

Trading bot for Pocket Option (binary options) using CONSENSUS 4/5 indicator.
Direct WebSocket connection to PO (no browser), Telegram Mini App for control,
runs 24/7 on Hetzner VPS in Docker.

> 🚧 **АКТИВНЫЙ РЕФАКТОРИНГ** (с 2026-04-29). Перед любыми изменениями кода
> прочитай [REFACTOR_PLAN.md](REFACTOR_PLAN.md) — там source of truth по
> структуре. Сейчас Mini App перестроен на 3 топ-таба (Главная / Настройки бота
> / Стратегия с подвкладками). Backend старых analytics endpoints ещё жив но
> UI их не вызывает (этап 1.2 удалит их). Этап 2 — новая аналитика с market
> snapshots на каждый CONSENSUS-сигнал.
> Стабильная точка для отката: tag `stable-pre-strategy-removal`.

## Where things live

- **Active worktree**: this directory (`.claude/worktrees/youthful-wiles-992d65/`)
- **Main repo**: parent of `.claude/` — same files, different branch maybe
- **VPS**: `root@37.27.13.173` (Hetzner Helsinki, Ubuntu 24.04, CX33)
  - Migrated from Nuremberg `178.105.36.60` (offline) on 2026-04-28
  - **PO_PREFERRED_WS_URL=wss://api-eu.po.market/...** обязательно — без пина PO роутит на api-spb, который CF блокирует из Helsinki Hetzner-IP
  - api-eu и api-msk из Helsinki проходят (HTTP 400 на тест), api-spb — HTTP 403
  - Bot runs at `/opt/po-bot/`
  - Docker compose at `/opt/po-bot/deploy/`
  - Container name: `po-bot`
- **Local secrets**: `~/.po-bot/secrets.env` (chmod 600, NOT in git)
  - Contains: PO_SSID, PO_UID, PO_IS_DEMO, PO_STORAGE_STATE_B64, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

## How to deploy (the proper way)

```bash
ssh root@37.27.13.173 'cd /opt/po-bot && git checkout -- config.yaml && git pull && \
  sed -i "s/^mode: paper/mode: real/" config.yaml && \
  cd deploy && docker compose down && docker compose up -d --build'
```

The `git checkout -- config.yaml` is critical — mode=real is applied via sed
after every pull (config.yaml in repo has mode=paper as the safe default).
Without checkout, git pull conflicts with local mode=real edit.

## How to debug when user reports problem

1. SSH to VPS first: `ssh root@37.27.13.173`
2. Check container status: `cd /opt/po-bot/deploy && docker compose ps`
3. Last logs: `docker compose logs --tail=80 po-bot | grep -v "GET /api"`
4. Check git: `cd /opt/po-bot && git log -1 --oneline`
5. Health: `curl -sI http://localhost:8080/health`
6. Tunnel: `grep trycloudflare.com /var/log/cloudflared.log | tail -1`

## Common issues we've already solved (don't re-debug)

- **WS дроп каждые ~30 мин на api-msk/api-spb** — это PO server-side recycle, не баг.
  Auto-reconnect handles it. Не паникуй на каждый "WebSocket замолчал" алерт.
- **Cloudflare 403 на api-spb из Hetzner Helsinki/Nuremberg/Falkenstein** —
  CF блокирует датацентр-IP именно для api-spb endpoint. api-eu и api-msk
  обычно проходят. Решение — `PO_PREFERRED_WS_URL=wss://api-eu.po.market/...`
  в `/opt/po-bot/deploy/.env`. Бот использует circuit breaker если auth
  падает 3 раза подряд — снимает пин на 1 цикл, потом возвращает.
- **WARP на этом VPS НЕЛЬЗЯ ставить** — даже proxy mode ломает SSH inbound,
  recovery только через Hetzner Rescue Mode + chroot. Trapped дважды.
  Если нужен другой egress IP — используй WireGuard к WireGuard-серверу,
  не WARP.
- **TelegramConflictError** = два бота с одним токеном делают getUpdates.
  Признак: `terminated by other getUpdates request`. Лечение: revoke токен
  через @BotFather → новый в `/opt/po-bot/deploy/.env` → restart.
- **git pull блокируется config.yaml** — всегда делать `git checkout -- config.yaml`
  ПЕРЕД pull. Уже встроено в Deploy кнопку HTML панели.
- **Mini App не видит изменения** — Telegram cache. Решение: BotFather → Edit
  menu URL с `?v=N+1` в конце для cache-bust.

## Architecture you should already know

- **3 SM states**: FREE / LOCKED / SEARCH (current_pair=None when searching)
- **3 levels of "no trade"**: SKIP / PAUSE (1h) / BAN (12h)
- **Double WR1 filter**: long 1000 свечей (≥60%) + recent 200 свечей (≥70%)
- **Search-mode**: после payout drop / max_trades — НЕ выбирать одну пару,
  а сканировать все допустимые
- **Asset categories**: forex / crypto / stocks / indices / commodities — multi-checkbox в Mini App

## Permissions you have (auto-allow in `.claude/settings.json` если настроено)

If user has set up auto-allow:
- SSH to 37.27.13.173 — yes (read-only commands like docker logs, ps, git log)
- git pull/push — yes (with confirmation for push)
- docker compose restart — yes
- docker compose down/up --build — needs confirmation (destructive)
- Anything with `rm -rf`, `git reset --hard`, `kill` — ALWAYS ask user

## Tools the user has

- **HTML панель на Mac**: http://localhost:5555/ (launchd-сервис, авто-старт)
  - 10 buttons with tooltips: Deploy, Restart, Logs, Status, etc.
  - 📋 Copy button to grab terminal output
- **Telegram /control**: 10 inline buttons in TG with descriptions
- **Telegram /panel**: link to HTML panel (если CONTROL_PANEL_URL задан)
- **PDFs**: STRATEGY.pdf (стратегия) и DEPLOY_CHEATSHEET.pdf (deploy commands)
  - Both regenerated from `tools/make_*_pdf.py`

## Conventions when working with this codebase

- **Russian/Ukrainian comments are fine** — user is Ukrainian, mixes them.
- **Telegram messages — Russian**. User reads Russian primarily.
- **No emojis in comments** unless they're meaningful (✓ for done, etc.)
- **Don't write new doc files** unless user asks — README/STRATEGY.pdf cover most things.
- **Always commit + push** after substantial changes — user expects to see deploy ready immediately.
- **Use `git pull` before any commit** to avoid conflicts since user might pull on VPS in parallel.

## What to NEVER do without explicit ask

- Run `docker system prune -af` (deletes all images)
- Run `git reset --hard` (loses uncommitted work)
- Delete files in `/data` directories (history is there)
- Push to main without commit message
- Hardcode secrets anywhere — use ~/.po-bot/secrets.env
- Cat the contents of secrets.env in chat output — they'd appear in transcript
