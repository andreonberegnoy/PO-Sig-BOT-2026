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

## How to deploy

**Автоматически через GitHub Actions** (по умолчанию):
```bash
git push origin main   # → workflow .github/workflows/deploy.yml
                       # → SSH в VPS, git pull, docker compose down/up --build
                       # → healthcheck + verify auth (≈2-3 минуты)
```

История запусков: https://github.com/andreonberegnoy/PO-Sig-BOT-2026/actions

Ручной триггер без коммита:
```bash
gh workflow run deploy.yml -R andreonberegnoy/PO-Sig-BOT-2026
```

Workflow не триггерится при push изменений только в `*.md`, `.github/**`,
`tools/make_*_pdf.py` (экономия минут GitHub Actions).

**Manual fallback** (если GitHub Actions недоступны / нужно отлаживать):
```bash
ssh root@37.27.13.173 'cd /opt/po-bot && git checkout -- config.yaml && git pull && \
  sed -i "s/^mode: paper/mode: real/" config.yaml && \
  cd deploy && docker compose down && docker compose up -d --build'
```

The `git checkout -- config.yaml` is critical — mode=real is applied via sed
after every pull (config.yaml in repo has mode=paper as the safe default).
Без checkout pull конфликтует с локальной правкой mode=real.

Подробная документация по автодеплою: [.github/DEPLOY_SETUP.md](.github/DEPLOY_SETUP.md)
(SSH-ключи, GitHub Secrets, откат, отзыв ключа).

## How to debug when user reports problem

1. SSH to VPS first: `ssh root@37.27.13.173`
2. Check container status: `cd /opt/po-bot/deploy && docker compose ps`
3. Last logs: `docker compose logs --tail=80 po-bot | grep -v "GET /api"`
4. Check git: `cd /opt/po-bot && git log -1 --oneline`
5. Health: `curl -sI http://localhost:8080/health` (или публично `curl -sI https://po-bot.duckdns.org/health`)
6. HTTPS reverse-proxy: контейнер `flycycle_caddy` (Caddyfile `/root/bots/flycycle/Caddyfile`). Mini App URL: `https://po-bot.duckdns.org/miniapp/`. Cert auto-renew Let's Encrypt/ZeroSSL, без вмешательства.
7. DuckDNS auto-update: cron `/etc/cron.d/duckdns-po-bot` каждое воскресенье 04:00 UTC. Log: `/var/log/duckdns.log`.

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
- **Supervisor спамит «🚨 Задача X упала»** — task supervisor (main.py, каждые
  30с) считает любую завершившуюся задачу dead и рестартит с TG-алертом.
  Если задаче нечего делать (например `daily_report_loop` при включённом
  `periodic_report.enabled=true`) — НЕ делать `return`, а
  `await asyncio.Event().wait()` (idle forever). Тогда задача жива,
  supervisor спокоен, отчёт шлёт только `periodic_report_loop`.
- **Двойной daily-отчёт** — историческая ошибка: одновременно работали
  `daily_report_loop` (старый, schedule.daily_report_hour) и
  `periodic_report_loop` (новый, periodic_report.hour). Сейчас при
  `periodic_report.enabled=true` старый уходит в idle (см. выше).
- **Бот залип в петле «session NotAuthorized → relogin deferred»** — баг
  `_safe_to_relogin` в `main.py`. Условие включало `not s.paused and not
  s.waiting_resume` — relogin блокировался когда бот в паузе по расписанию
  или после stop_sum. Сессия PO протухала ночью, relogin не запускался
  никогда, бот зависал. Фикс (commit 83e9302): оставили только
  `mg_step == 0 and pending_trade is None` — paused/waiting безопасны для
  relogin (нет активных сделок). Признак бага в логах: `relogin deferred
  (reason=NotAuthorized) — unsafe state (MG cycle?)` каждые 30с подряд при
  фактическом mg_step=0.
- **WS открыт 24/7** — бот не закрывает WS-соединение в paused/waiting/day-off,
  только не открывает сделки. Аналитика (`_record_signals_phase`) пишется
  всегда независимо от паузы — это сознательное решение чтобы не терять
  ночные сигналы для market snapshot фильтрации (этап 2). PO видит активную
  сессию постоянно — это нормально (как открытая вкладка у юзера), не палево.

## Architecture you should already know

- **3 SM states**: FREE / LOCKED / SEARCH (current_pair=None when searching)
- **4 levels of "no trade"** (этап 3+):
  - **SKIP** — общая проходимость провалена в одиночку (просто не торгуется, в bans не кладём)
  - **PAUSE** (60 мин) — провалена ТОЛЬКО проходимость последних свечей
  - **TEMP_PAUSE** (6ч default) — обе проходимости провалены одновременно. Per-pair, не глобальный.
  - **BAN** (6ч/12ч default) — серия LOSS-ов > max_losses_in_row
- **Trade mode (OTC / regular / mixed)** — `filter.trade_mode`. Контролирует
  только ТОРГОВЛЮ (открытие реальных сделок). Аналитика (signals в БД через
  `_record_signals_phase`) пишется по ОБОИМ типам всегда — это broad pool для
  будущих переанализов. Per-pair payout-порог: `filter.min_payout` для OTC,
  `filter.min_payout_regular` для обычных (default 80, обычно ниже).
  Helper `_pair_matches_trade_mode(sym)` в state_machine, врезки в
  `_free_scan_step`, `_in_cycle_search_step`, `_open_and_track` (safety guard).
  Дефолт `trade_mode=otc` сохраняет старое поведение.
- **Двойной фильтр проходимости**:
  - **Общая проходимость** (`min_wr1`, default 60%) — % первой плюсовой сделки за 1000 свечей
  - **Проходимость последних свечей** (`min_wr1_recent`, default 75%) — то же за 200 свечей
- **Search-mode**: после payout drop / max_trades — НЕ выбирать одну пару, а сканировать все допустимые
- **Search-mode фильтр ослаблен** (этап 3+): когда мы уже в цикле и ищем следующую пару — игнорируется PAUSE и TEMP_PAUSE (провал recent проходимости). Учитываются только BAN (max_loss_streak), payout floor, switched_pairs (anti-bounce). Юзерская логика: «уже потеряли деньги, добиваем цикл, форма пары вторична».
- **Sticky current_pair**: пара которая в активном цикле (mg_step > 0) принудительно остаётся в `_tracked` даже если её score ухудшился во время цикла. Цикл должен довестись до WIN/stop_sum.
- **Asset categories**: forex / crypto / stocks / indices / commodities — multi-checkbox в Mini App
- **NB**: глобальный «day_off» механизм удалён в этапе 3+. Если все пары не прошли — main loop крутится впустую, рескан раз в 60с автоматически освобождает пары когда форма улучшится.
- **3 live-метрики на Главной Mini App + в TG-отчёте** (поверх daily_summary):
  - **⏱️ Макс. без торговли** — самый длинный непрерывный gap между сделками.
    Окно зависит от `schedule.enabled`: при `true` = текущий торговый день
    `[start_hour, end_hour]` в TZ из `telegram.daily_report_timezone` (ночь
    игнорируется); при `false` = rolling 24h. Helper `trading_day_window(cfg)`
    в `api/server.py`. Метод `journal.max_no_trade_gap(since, until, mode)`.
  - **📉 Мин. payout за сутки** — `MIN(payout)` среди сделок последних 24h
    (rolling, любой исход). Контроль payout-floor.
  - **📉 Макс. минусов подряд** — `MAX(mg_step)` среди WIN-сделок за 24h.
    DRAW не считается LOSS-ом и не сбрасывает цикл (refund → бот повторяет
    тот же шаг МГ). Поэтому считается через mg_step→WIN, а не через
    chronological streak (которая в `daily_summary.max_loss_streak` всё ещё
    есть, но юзеру не показывается).
- **🎰 Строка «В МГ-цикле»** на Главной — показывает все активные циклы с
  номером сделки в формате `EURUSD_otc(3), BTC_otc(1)`. Число — 1-indexed
  trade number (1 = первая сделка, 2 = после 1-го LOSS и т.д.). В parallel
  mode итерирует `parallel_cycles` из `/api/status`. В legacy — `current_pair`
  + `mg_step+1` если mg_step>0. Размещена под «Tracked пары».
- **Stage 1.2 — Broad analytics pool** (юзер: «аналитика должна писаться
  ВСЕГДА независимо от фильтра»). `self._scan_pool: set[str]` =
  все пары прошедшие базовый scan (payout + asset_categories). Заполняется
  в `_rescan_pairs`. `_record_signals_broad_loop` (новая фоновая задача
  каждые 60с) делает REST-fetch для пар в `_scan_pool − _tracked` и пишет
  signals в БД через `journal.insert_signal`. Гарантирует что аналитика
  собирается по всем парам прошедшим payout-фильтр, не только торгуемым.
- **Stage 1.3 — WR-blacklist по аналитике**. `filter.min_pair_wr_actual`
  (default 0 = выкл). Если ≥1, в `_eligible_for_new_cycle` считается
  counterfactual WR пары из БД signals (exp_wins[1]=2-бар, 30 дней,
  N≥30 для значимости) через `journal.pair_wr_from_signals()`. Пары с
  WR<порог не торгуются, но в аналитику пишутся через broad loop. Кэш
  `_pair_wr_cache` (TTL 10 мин) защищает от SQL на каждом тике.
- **Stage 2 — Auto-expiry per-pair × hour**. Модуль
  `strategy/expiry_optimizer.py`. Раз в 4ч (фоновая задача
  `expiry_optimizer_loop`) `compute_pair_hour_expiry()` агрегирует
  `signals.exp_wins` за 30 дней → для каждой (sym, hour_local Kyiv) ячейки
  ищет оптимум 1-5 баров (N≥8 для значимости). Результат в
  `state_kv["pair_hour_expiry"]` dict. В `_open_and_track` и
  `_open_parallel_trade` функция `pair_hour_expiry_lookup(journal, sym,
  current_hour_Kyiv)` возвращает оптимум; fallback на
  `trading.expiry_seconds`. Toggle `filter.auto_expiry_enabled` (default true).
- **Stage 3 — Parallel trading** (юзерская фича: «несколько пар
  одновременно в МГ»). Toggle `trading.parallel_pairs` (default false),
  `trading.max_parallel_pairs` (default 3). При parallel=true главный
  диспатч в main loop уходит в `_parallel_step` вместо free_scan/in_cycle.
  Использует `RuntimeState.pair_cycles: dict[symbol→cycle_state]` вместо
  единого current_pair/mg_step. Каждый cycle: `{direction, mg_step,
  cycle_loss, pending_trade, losses_streak, started_at, trades_count}`.
  Phase 1 обрабатывает существующие циклы (ищет signal для след MG-шага),
  Phase 2 открывает новые если active < max_par. `_open_parallel_trade`
  возвращается сразу + спавнит fire-and-forget task `_watch_parallel_close`
  который ждёт expiry+poll close event. `_on_trade_closed_parallel`
  обновляет per-cycle state не трогая legacy fields. Sticky tracked для
  pair_cycles гарантирует live candles. Bust → ban на ban_hours +
  cleanup_pair_cycle. Миграция legacy→parallel при первом включении
  toggle (legacy active cycle copy → pair_cycles[current_pair]).
- **balance_pct — процент депо на цикл** (юзер: «брать 10-14% от общего
  депозита, вмещать N сделок»). `trading.auto_base_amount.balance_pct`
  (default 100 = весь баланс, backwards-compat). Формула в
  `_recalc_base_from_balance`:
  `base = floor((balance × pct/100) / sum_factor × 10)/10`. Гарантия: при
  всех N LOSS теряется не более чем balance × pct/100. Триггер пересчёта
  при изменении этого ключа через API срабатывает мгновенно (в
  `api/server.py:recalc_keys`).
- **`_safe_to_relogin` (Stage 3-aware)** — блокирует relogin если есть
  pending_trade в state ИЛИ в любом из pair_cycles. Также проверяет
  mg_step>0 в legacy state.
- **`_is_safe_for_recalc` (Stage 3-aware)** — блокирует recalc base_amount
  если есть активные parallel cycles (mg_step>0 или pending_trade).
  Изменение base в середине цикла ломает recovery-математику.

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
