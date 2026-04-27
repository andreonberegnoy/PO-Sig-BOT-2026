# MY PO-SIG BOT

Торговый бот для Pocket Option с **прямым подключением через WebSocket**, индикатором **CONSENSUS 4/5**, **Telegram Mini App** для визуального управления и **системой пользовательских стратегий**. Работает 24/7 на VPS / Railway без браузера.

---

## Архитектура

```
config.yaml                    все параметры (фильтр, мартингейл, индикатор)
.env                           секреты (PO_SSID, telegram токены) — не в git
main.py                        оркестратор: feed + strategy + api + telegram + supervisor
Dockerfile                     mcr.microsoft.com/playwright/python (Chromium для relogin)
railway.toml                   Railway deploy config
deploy/                        production-файлы для VPS-деплоя
├── docker-compose.yml         compose с volume /data, healthcheck, log rotation
├── .env.example               шаблон env-переменных
└── po-bot.service             systemd unit для автостарта после reboot
feed/
├── po_direct.py               прямое WS-подключение к PO + heartbeat watchdog + alert callback
├── auto_relogin.py            Playwright авто-релогин через storage_state
└── history.py                 адаптер fetch_candles над feed.subscribe()
strategy/
├── consensus.py               встроенная CONSENSUS 4/5 (1:1 с JS-индикатором). Тройная статистика: общая, recent (200 свечей), wr1
├── indicators.py              RSI / QQE / EMA / Bollinger / ATR / HTF
├── filter_1000.py             прогон по 1060 свечам, 6-уровневый фильтр, категоризация активов
├── registry.py                плагин-система (загрузка пользовательских)
├── _template.py               готовый шаблон для новых стратегий
└── user/                      *.py — пользовательские (загруженные через UI)
trading/
├── ws_client.py               open_trade через feed.send_open_order
├── state_machine.py           3 состояния (FREE/LOCKED/SEARCH) + сигнал-гейтнутый мартингейл + stall-watchdog + hour-whitelist
└── window_manager.py          legacy (po-signals); не используется на PO direct
api/
├── server.py                  FastAPI: REST endpoints + Mini App static + hourly_stats + control
└── auth.py                    верификация Telegram WebApp initData
miniapp/
├── index.html                 5 вкладок: Status / Settings / Strategies / Аналитика / По часам
├── style.css                  dark theme + multi-checkbox UI
└── app.js                     vanilla JS, без сборки. Sort, CSV-экспорт, multi-select
journal/
├── db.py                      SQLite: trades, bans, sessions, kv_store, hourly_stats query
└── candles_db.py              SQLite: candles persist между рестартами
tg/
├── bot.py                     команды /status /ping /pause + авто-рестарт polling + health_watchdog_loop
├── settings_ui.py             категоризированный список настроек для inline-меню
└── chart.py                   рендер свечного графика (с safe_filename + parse_math=False)
tools/
├── make_strategy_pdf.py            генератор PDF-презентации стратегии (STRATEGY.pdf)
├── make_deploy_pdf.py              генератор PDF-шпаргалки по деплою (DEPLOY_CHEATSHEET.pdf)
├── po_control.py                   локальный HTTP-сервер с HTML панелью управления
├── install_control_service.sh      установка po_control.py как launchd-сервиса на Mac
└── ...                             (probe, smoke-test, make_storage_state и др.)
```

---

## Многоуровневая система стабильности

Бот защищён **8 независимыми слоями** — каждый из них подхватывает управление, если предыдущий не справился.

```
Слой 1 — WS Heartbeat Watchdog (po_direct.py)
  ├─ каждые 30с: проверяет время последнего фрейма от сервера
  ├─ каждые 25с: отправляет проактивный Engine.IO ping ("2")
  └─ тишина > 90с → ws.close() → auto_reconnect_loop подхватывает

Слой 2 — auto_reconnect_loop (НИКОГДА не сдаётся)
  ├─ Hard re-entry guard через _auto_reconnect_in_progress flag
  ├─ Фаза 1 (быстрая): 10 попыток, экспоненциальный backoff до 60с
  ├─ HTTP 401/403 → немедленный fast-path relogin, цикл продолжается
  └─ Фаза 2 (бесконечная): каждые 120с навсегда + relogin каждые 3 раунда

Слой 3 — Stall Watchdog в state_machine
  ├─ tracked пары есть, но нет ни одного тика > 10 мин
  └─ уведомление в TG + принудительный ws.close()

Слой 4 — Initial connect retry (main.py)
  ├─ если первый WS handshake не прошёл — бесконечный цикл retry
  ├─ открытый таймаут handshake = 45с (cold-start friendly)
  └─ каждые 3 неудачи → relogin для обновления ws_url

Слой 5 — Защита главного торгового цикла
  └─ try/except вокруг каждой итерации: ошибка логируется, цикл продолжается

Слой 6 — Telegram polling с авто-рестартом
  └─ aiogram упал → перезапуск через 5-60с (exponential backoff)

Слой 7 — Task Supervisor (main.py)
  ├─ каждые 30с: проверяет что state_machine/daily_report/health_watchdog живы
  └─ упала задача → уведомление в TG + автоматический рестарт через 5с

Слой 8 — Top-level supervisor (main.py)
  ├─ while True вокруг asyncio.run(run(...))
  ├─ ANY uncaught exception → лог + crash marker в БД → перезапуск через 2-60с
  └─ при следующем успешном старте — TG-уведомление "🔴 Бот падал и поднялся"
```

### Проактивные TG-алерты при поломках

Бот сам пишет в Telegram когда что-то ломается — не нужно лезть в логи:

| Триггер | Сообщение | Cooldown |
|---|---|---|
| WS не получает фреймы 90с | `⚠️ WebSocket замолчал на 90с` | 10 мин |
| 10+ неудачных reconnect (~5 мин блок) | `❌ WebSocket не восстанавливается — возможно бан Railway IP` | 30 мин |
| WS восстановился после алерта | `✅ WebSocket восстановлен (попытка N)` | 1 раз |
| Watchdog нашёл проблемы (каждые 30 мин) | `🩺 Watchdog: отсутствуют задачи / баланс недоступен` | 1 час |
| Top-level процесс падал и поднялся | `🔴 Бот падал и перезапустился сам, ошибка: ...` | при старте |

---

## Стратегия — как бот выбирает пары и торгует

### Шаг 1. Сканирование (раз в час)

Для каждой OTC-пары загружается **1000 минутных свечей**, виртуально прогоняется CONSENSUS, считается статистика. Затем последовательно применяются **6 фильтров**:

| № | Фильтр | Условие | Если не прошёл |
|---|---|---|---|
| 1 | `min_payout` | Текущий payout PO ≥ 92% | SKIP |
| 2 | OTC-only | Только пары категории OTC (24/7) | SKIP |
| 3 | `asset_categories` | В списке разрешённых классов (forex/crypto/etc) | SKIP |
| 4 | `completed >= 5` | Минимум 5 виртуальных сделок в окне 1000 свечей | SKIP |
| 5 | `max_loss_streak <= max_losses_in_row` | Не было серии больше 3 минусов подряд | **BAN** на ban_hours |
| 6 | `wr1_long >= min_wr1` | % первой плюсовой сделки за 1000 свечей ≥ 60% | SKIP |
| 7 | `wr1_recent >= min_wr1_recent` | % за последние 200 свечей ≥ 70% (≥3 сделок) | **PAUSE** на pause_hours |

Прошедшие все фильтры пары попадают в `_tracked` — бот живёт на их тиках и ждёт сигнал.

### Шаг 2. Три уровня "не торговать"

| Уровень | Триггер | Срок | Что дальше |
|---|---|---|---|
| **SKIP** | Мало сделок, низкий long WR1, payout упал | до 1 часа | Переоценка на следующем сканировании |
| **PAUSE** | Низкий recent WR1 (200 свечей) | `pause_hours` (1ч) | Авто-переоценка, если опять плохо — снова пауза |
| **BAN** | max_loss_streak > 3 (системно плохая) | `ban_hours` (12ч) | Длительный бан |

### Шаг 3. CONSENSUS 4/5 — генерация сигнала

[strategy/consensus.py](strategy/consensus.py) считает 5 систем на каждом закрытом баре:

| № | Система | Условие |
|---|---|---|
| 1 | **RSI-QQE** | RSI(14, smooth=5) пересекает trailing-линию (factor=4.238). Обязательно. |
| 2 | **HTF trend** | Close M5 vs EMA(20). Согласован с направлением. |
| 3 | **Volatility (ATR)** | `ATR(14) / ATR_avg(100) ∈ [0.7, 2.0]`. |
| 4 | **Bollinger zone** | Цена в нижних 30% канала BB(20, 2σ) для BUY или верхних 30% для SELL. |
| 5 | **Candle** | Тело < 2× ATR + направление свечи совпадает. |

**Вход разрешён** при ≥ 4 голосах из 5. Сделка открывается на open следующей минуты, экспирация 120с.

### Шаг 4. Три режима state machine

| Режим | Условие | Что делает |
|---|---|---|
| **FREE** | `mg_step = 0` | Скан всех tracked пар, первый сигнал → базовая сделка |
| **LOCKED** | `mg_step > 0` + `current_pair` задана | Ждёт следующий бар на закреплённой паре (продолжение МГ) |
| **SEARCH** | `mg_step > 0` + `current_pair = None` | Сканирует все пары (исключая switched_pairs), первый сигнал = новая закреплённая пара. МГ-шаг сохраняется. |

**Триггеры перехода в SEARCH:**
- Payout упал ниже `payout_floor` (85%) на текущей паре
- Достигнут лимит `max_trades_on_pair` (если `limit_trades_per_pair_enabled = true`)

**Поведение `switched_pairs` (защита от циклирования на одной паре):**

В пределах одного МГ-цикла пара, на которой произошёл триггер смены, добавляется
в `switched_pairs` и **не используется повторно** до завершения цикла. Когда
список очищается:

| Событие | `switched_pairs` |
|---|---|
| WIN на любой паре в цикле | очищается → все пары снова доступны |
| `/resume` после stop_sum / max_steps | очищается |
| Кнопка "🔄 Сбросить цикл (FREE)" | очищается |
| `martingale.enabled = false` + LOSS | очищается (МГ выкл — каждая сделка независима) |

То есть **в пределах одного цикла** пары не повторяются (защита от слива на одной
плохой паре), **между циклами** все пары снова в игре.

### Шаг 5. Мартингейл с гейтом

```
LOSS → mg_step++, current_pair заморожена
  ↓
Ждём новый CONSENSUS-сигнал на этой паре (freshness < 25с)
  ↓
Сигнал пришёл → открываем base × 2.1^step

WIN → сброс цикла, FREE режим, base $
Stop-sum ($1000) или max_steps (10) → waiting_resume → /resume
```

**Toggle `martingale.enabled`:**
- `true` (дефолт): классический МГ с догонами
- `false`: каждая сделка `base_amount`. После LOSS — сброс цикла, поиск нового сигнала на любой паре. "Плоская" торговля без удвоений.

### Шаг 6. Hour Whitelist (опционально)

После накопления статистики (Mini App → "По часам" → "⭐ Применить как фильтр") бот может торговать **только в определённые часы для определённых пар**. Например: EURUSD_otc только 09:00-10:00 и 14:00-15:00 (где WR ≥ 70%). В другое время — пара пропускается.

### Шаг 7. Детект закрытия сделки

**Приоритет 1** — событие `updateClosedDeals` с полем `profit` (profit > 0 → WIN, < 0 → LOSS).
**Fallback** — баланс-дельта, если событие не пришло за 15 секунд.

---

## Telegram команды

| Команда | Описание |
|---------|---------|
| `/status` | Состояние + **inline-кнопки управления циклом** |
| `/control` | 🎛 главное меню (диагностика, сегодня, баны, hourly, tracked, relogin, reset) |
| `/panel` | 🌐 кнопка открыть HTML панель управления (deploy, restart, logs, backup) |
| `/ping` | 🩺 Полная диагностика WS/задач + кнопки реконнекта |
| `/balance` | Текущий баланс |
| `/pause` | Поставить на паузу |
| `/resume` | Возобновить / сбросить stop-sum |
| `/stop` | Остановить бота |
| `/bans` | Активные баны/паузы пар |
| `/chart SYMBOL` | Свечной график пары (показывает счётчики за 1000 и 200 свечей) |
| `/test SYMBOL call\|put [amount]` | Тестовая сделка |
| `/settings` | Категоризированное меню всех настроек |
| `/help` | Список команд |

### `/status` — управление циклом

Отображает текущее состояние **с кнопками**:
- 🔀 **Сменить пару** (видна только в LOCKED) — переключиться на конкретную пару
- 🔄 **Сбросить цикл (FREE)** — МГ → 0, пара → None
- ⏸ / ▶️ **Пауза / Продолжить**
- 🔃 **Обновить**

В SEARCH режиме поле `Пара:` показывает `🔍 поиск сигнала на всех допустимых`.

### `/ping` — диагностика и WS-управление

Кнопки:
- 🔄 **Реконнект WS** — принудительный close → авто-реконнект
- 🔑 **Relogin (новый SSID)** — Playwright прямо сейчас
- 🔃 **Обновить статус**

### `/control` — главное меню управления

10 inline-кнопок с описаниями (что делает каждая показано в шапке):

| Кнопка | Описание |
|---|---|
| 📊 Диагностика | статус WS, задач, балансы, фрейм-фрешность |
| 💰 Сегодня | сделки за 24ч, WR, профит |
| 🚫 Баны/паузы | пары временно отстранённые с timeleft |
| 📈 Hourly | сводка по часам за 7 дней (WR, profit) |
| 🔍 Tracked | торгуемые пары с counts ✓/✗ за 1000 и 200 свечей |
| 🔑 Force Relogin | обновить SSID через Playwright (~60с) |
| 🔄 Reset cycle | сбросить МГ-цикл в FREE |
| 🌐 Mini App URL | инструкция (URL не доступен из контейнера) |
| 📋 Deploy инструкция | copy-paste команды для VPS |

### `/panel` — HTML панель управления

Возвращает inline-кнопку **🌐 Открыть панель** с URL из env `CONTROL_PANEL_URL`.
Панель крутится локально на Mac (или через cloudflared tunnel для phone-доступа).

---

## Локальная HTML панель управления (на Mac)

[tools/po_control.py](tools/po_control.py) — небольшой Python-сервер на Mac с
красивой HTML-страницей. **10 кнопок с tooltip-описаниями** для всех частых
операций:

- 🚀 **Deploy** — git pull + docker rebuild (с автоматическим разруливанием конфликтов config.yaml)
- 🔄 **Restart container**
- 📜 **Last 50 logs** (с фильтром шума)
- 🩺 **Status check** — ps, /health, git, disk, ram
- 🌐 **Mini App URL** — текущий tunnel URL
- 🔁 **Restart Mini App tunnel** ⚠️ (URL изменится)
- 💾 **Backup data/**
- 🔥 **Force rebuild --no-cache** ⚠️
- 👁 **Live logs (10 sec)**
- 📖 **Last 5 commits on VPS**

### Запуск

```bash
# Один раз — установить как launchd-сервис (авто-старт при логине):
./tools/install_control_service.sh install

# Открыть в браузере:
http://localhost:5555/

# Управление сервисом:
./tools/install_control_service.sh status / logs / stop / start / restart
```

### Доступ с телефона

Панель крутится на localhost — для phone-доступа нужен tunnel:

```bash
# На Mac:
./tools/install_control_service.sh tunnel
# → выдаст https://something.trycloudflare.com URL

# На VPS (.env):
CONTROL_PANEL_URL=https://something.trycloudflare.com

# Перезапустить бота:
docker compose up -d --build

# Теперь /panel в Telegram → клик "🌐 Открыть панель" → откроется в браузере телефона
```

В HTML страница есть **кнопка 📋 Копировать** — копирует весь вывод терминала в буфер.

---

## Telegram Mini App

`https://<твой-домен>/` — подключается через @BotFather → Bot Settings → Menu Button.

### Вкладки

1. **Статус** — режим, баланс, пара (или "🔍 поиск"), MG-шаг, потери. Кнопки Pause/Resume + Switch/Reset Cycle.
2. **Настройки** — все параметры стратегии с слайдерами и multi-checkbox.
3. **Стратегии** — встроенные + пользовательские, активация, загрузка кода.
4. **Аналитика** — таблица пар с цветовой кодировкой по WR/payout.
5. **По часам** ← новое — анализ сделок по часам дня:
   - Сводка по часам (все пары)
   - Детальная таблица пара × час с сортировкой
   - Колонка `Avg payout WIN` — средний % выплаты на WIN-сделках
   - ⭐ для пар/часов с WR ≥ 70% и ≥ 5 сделок
   - **📥 Экспорт CSV** (две таблицы)
   - **⭐ Применить как фильтр** — создать hour_whitelist
   - **🔓 Снять фильтр** — отключить hour_whitelist
   - **⤺ reset stats** (маленькая) — soft-reset baseline для нового цикла измерения

### Категории активов (multi-checkbox)

В Настройках → 🔍 Фильтр пар:
```
☐ forex   ☐ crypto   ☐ stocks   ☐ indices   ☐ commodities   ☐ other
```

Пустой набор = все категории. Выбранные = только эти.

---

## Конфигурация

```yaml
mode: real                   # paper | real

filter:
  min_payout: 92             # минимум payout для входа
  payout_floor: 85           # ниже → search-mode (поиск на всех парах)
  max_losses_in_row: 3       # >3 минусов подряд в истории → BAN
  min_wr1: 60                # минимум % 1-й сделки за 1000 свечей
  min_wr1_recent: 70         # минимум % 1-й сделки за 200 свечей (recent form)
  recent_lookback_bars: 200  # окно для recent-статистики
  history_candles: 1060
  stats_lookback_bars: 1000
  ban_hours: 12              # длительный BAN
  pause_hours: 1             # короткая PAUSE за низкий recent WR1
  day_off_hours: 6           # если вообще нет пар — пауза
  asset_categories: []       # [] = все. ["forex","crypto"] = только форекс+крипта
  tf: 60                     # M1

trading:
  base_amount: 1
  expiry_seconds: 120
  one_trade_at_a_time: true
  min_trades_on_pair: 3
  limit_trades_per_pair_enabled: false  # ВКЛ/ВЫКЛ функции лимита (default OFF)
  max_trades_on_pair: 1      # При limit_enabled=true: после N сделок — SEARCH-mode
  max_pair_switch_per_cycle: 1

martingale:
  enabled: true              # false = "плоская" торговля без догонов
  coefficient: 2.1
  max_steps: 10
  stop_sum: 1000
  reset_on_win: true

indicator:
  minConsensus: 4
  expiryBars: 2
  rsiPeriod: 14; rsiSmoothing: 5; qqeFactor: 4.238
  htfMultiplier: 5; htfMaPeriod: 20; htfMaType: EMA
  atrPeriod: 14; atrAvgWindow: 100; atrMinRatio: 0.7; atrMaxRatio: 2.0
  bbPeriod: 20; bbStdDev: 2.0; bbZoneDepth: 0.3
  candleMaxAtrMult: 2.0; cooldownBars: 3
  statsLookbackBars: 1000      # окно "полной" статистики (свечей)
  recentLookbackBars: 200      # окно "свежей" статистики (свечей)

# Optional (не в config.yaml — задаётся через env):
# CONTROL_PANEL_URL=https://...trycloudflare.com  # для /panel command
# PO_PREFERRED_WS_URL=wss://api-eu.po.market/...  # пин региона PO
  statsLookbackBars: 1000

schedule:
  enabled: false
  start_hour: 6
  end_hour: 22
```

Все параметры можно менять **live** из Mini App или TG `/settings` — сохраняются в journal.db, переживают рестарты.

---

## Деплой

### Опция A — Railway (быстро, $5/мес)

1. Push в GitHub → Railway автодеплой через `railway.toml`
2. Volume → mount `/data` (1 GB)
3. Networking → Public Domain → port 8080
4. Variables: `PO_SSID`, `PO_UID`, `PO_IS_DEMO`, `PO_STORAGE_STATE_B64`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`
5. (опц.) `PO_PREFERRED_WS_URL=wss://api-eu.po.market/...` — пин на конкретный регион PO
6. @BotFather → Menu Button → твой Railway URL

См. [DEPLOY.md](DEPLOY.md) для деталей.

### Опция B — VPS (стабильнее, фиксированный IP)

Hetzner / Selectel / DigitalOcean. Преимущества: фиксированный IP (лучше CF reputation), географический контроль, дешевле в долгую.

```bash
# На VPS (Ubuntu 24.04):
apt update && apt upgrade -y
apt install -y curl git nano ufw
ufw allow OpenSSH && ufw allow 8080/tcp && ufw --force enable
curl -fsSL https://get.docker.com | sh

# Клонировать и настроить
cd /opt && git clone https://github.com/andreonberegnoy/PO-Sig-BOT-2026.git po-bot
cd po-bot/deploy
cp .env.example .env
nano .env   # вставить секреты

# Запуск
docker compose up -d --build
docker compose logs -f po-bot
```

Файлы готовые к деплою:
- `deploy/docker-compose.yml` — production compose с volume `/data`, healthcheck, log rotation
- `deploy/.env.example` — шаблон env
- `deploy/po-bot.service` — systemd unit для автостарта (опц.)

### Опция C — локально для разработки

```bash
git clone https://github.com/andreonberegnoy/PO-Sig-BOT-2026.git
cd PO-Sig-BOT-2026
pip3 install -r requirements.txt
cp .env.example .env  # заполни секреты
python3 main.py
```

Mini App: `http://localhost:8080/`

---

## REST API

| Endpoint | Метод | Описание |
|----------|-------|---------|
| `/api/status` | GET | Состояние бота |
| `/api/settings` | GET / PUT | Конфигурация |
| `/api/strategies` | GET / POST | Список / загрузка стратегий |
| `/api/strategies/{name}` | DELETE | Удалить стратегию |
| `/api/strategies/{name}/activate` | POST | Активировать |
| `/api/strategies/{name}/params` | GET / PUT | Параметры стратегии |
| `/api/control/pause` | POST | Пауза |
| `/api/control/resume` | POST | Возобновить |
| `/api/control/switch_pair` | POST | Сменить пару (MG сохр.) |
| `/api/control/reset_cycle` | POST | Сбросить цикл в FREE |
| `/api/pair_stats` | GET | Аналитика по парам |
| `/api/hourly_stats` | GET | Сделки по часам (для Mini App "По часам") |
| `/api/hour_whitelist` | GET | Текущий whitelist часов |
| `/api/apply_hour_whitelist` | POST | Применить фильтр (min_wr, min_trades, range) |
| `/api/clear_hour_whitelist` | POST | Снять фильтр |
| `/api/reset_hourly_stats` | POST | Soft-reset baseline (старые сделки скрыть) |
| `/api/debug/journal` | GET | Диагностика SQLite |
| `/health` | GET | Healthcheck для Docker / monitoring |
| `/strategy_template` | GET | Шаблон стратегии |

Все эндпоинты под `/api/` требуют `X-Init-Data` header (Telegram WebApp initData).

---

## Что переживает рестарт (Volume `/data`)

- `state.db` — `runtime_state` (пара, MG-шаг, session_loss, direction, paused/waiting_resume, switched_pairs)
- `candles.db` — буферы свечей
- Журнал сделок, баны/паузы пар
- Настройки из Mini App (`settings_overrides`)
- Параметры активной стратегии (`strategy_params:<name>`)
- Загруженные пользовательские стратегии (`/data/user_strategies/*.py`)
- Аналитика (`payout_log`, `pair_stats_log`)
- Hour whitelist (`filter.hour_whitelist`)
- Analytics baseline (`analytics.baseline_ts`)
- Crash markers (для алерта "бот падал")

---

## PDF-документация

Два готовых PDF-файла для удобства:

```bash
# Стратегия (11 страниц): как бот выбирает пары, фильтры, МГ
python3 tools/make_strategy_pdf.py    → STRATEGY.pdf

# Шпаргалка по деплою (11 страниц): SSH, git pull, troubleshooting
python3 tools/make_deploy_pdf.py      → DEPLOY_CHEATSHEET.pdf
```

Удобно распечатать, открыть на iPad или сохранить в Saved Messages в TG. Файлы добавлены в `.gitignore` — генерируются из скриптов, всегда актуальны.

---

## Известные ограничения

1. **Сессия PO** — обновляется автоматически каждые ~12ч. Cookies (storage_state) живут ~30 дней; при истечении придёт TG-уведомление с пошаговой инструкцией.
2. **Demo и Real — разные WS** и разные котировки.
3. **History limit ~1060 баров** через PO API.
4. **Hetzner / датацентр-IP** могут блокироваться Cloudflare. Признак: HTTP 403 на handshake к api-*.po.market. Лечится: WARP-туннель / смена региона / VPS в близком геоложении.
5. **Strategy plugins не sandboxed** — пользовательский код исполняется в основном процессе.
6. **Mini App требует HTTPS** — на VPS нужен Cloudflare Tunnel или Caddy + Let's Encrypt.

---

## Testing checklist

- [ ] `/test SYMBOL call` открывает сделку в демо
- [ ] WIN → мартингейл сбрасывается
- [ ] LOSS → ждёт новый сигнал на той же паре, ставка × 2.1
- [ ] Payout < 85% → автоматический переход в SEARCH-mode (не на одну пару)
- [ ] В SEARCH-mode `/status` показывает "🔍 поиск сигнала..."
- [ ] Stop-sum → `/resume` восстанавливает
- [ ] `/status` → кнопки 🔀/🔄 видны при активном МГ-цикле
- [ ] 🔀 Сменить пару → МГ-шаг сохранился
- [ ] 🔄 Сбросить цикл → МГ=0, бот ищет сигнал
- [ ] `/ping` → показывает реальный статус WS, фрейм-фрешность, задачи
- [ ] Mini App → Status загружает реальные данные
- [ ] Mini App → Settings → редактирование сохраняется (включая multi-checkbox категорий)
- [ ] Mini App → По часам → загрузка таблиц, переключение периода, CSV экспорт
- [ ] Mini App → По часам → ⭐ Применить фильтр → бот шлёт TG-уведомление
- [ ] Mini App → Strategies → загрузка кастома + активация работает
- [ ] После рестарта контейнера — candles.db и state.db не теряются
- [ ] Графики приходят в TG для всех типов пар (forex, crypto, stocks с `#`, indices)
- [ ] График показывает счётчики ✓/✗ для обоих окон (1000 и 200 свечей)
- [ ] `/control` → 🔍 Tracked показывает counts для каждой пары
- [ ] `/panel` → клик на кнопку открывает HTML панель в браузере
- [ ] HTML панель: при наведении на кнопку видно описание
- [ ] HTML панель: 🚀 Deploy авто-разруливает конфликт config.yaml
- [ ] HTML панель: 📋 Копировать копирует output в буфер
- [ ] Алерт "WebSocket замолчал" приходит при выдернутой сети
- [ ] Алерт "WebSocket восстановлен" приходит после реконнекта
- [ ] Health watchdog молчит при здоровом боте, шлёт алерт при поломках
