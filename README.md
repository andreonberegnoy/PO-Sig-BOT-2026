# MY PO-SIG BOT

Торговый бот для Pocket Option с **прямым подключением через WebSocket**, индикатором **CONSENSUS 4/5**, **Telegram Mini App** для визуального управления и **системой пользовательских стратегий**. Работает 24/7 на Railway без браузера.

---

## Архитектура

```
config.yaml                    все параметры (фильтр, мартингейл, индикатор)
.env                           секреты (PO_SSID, telegram токены) — не в git
main.py                        оркестратор: feed + strategy + api + telegram + supervisor
Dockerfile                     mcr.microsoft.com/playwright/python (Chromium для relogin)
railway.toml                   Railway deploy config
├── feed/
│   ├── po_direct.py           прямое WS-подключение к PO + heartbeat watchdog
│   ├── auto_relogin.py        Playwright авто-релогин через storage_state
│   └── history.py             адаптер fetch_candles над feed.subscribe()
├── strategy/
│   ├── consensus.py           встроенная CONSENSUS 4/5 (1:1 с JS-индикатором)
│   ├── indicators.py          RSI / QQE / EMA / Bollinger / ATR / HTF
│   ├── filter_1000.py         прогон по 1060 свечам, бан, приоритеты
│   ├── registry.py            плагин-система (загрузка пользовательских)
│   ├── _template.py           готовый шаблон для новых стратегий
│   └── user/                  *.py — пользовательские (загруженные через UI)
├── trading/
│   ├── ws_client.py           open_trade через feed.send_open_order
│   ├── state_machine.py       свободный скан + сигнал-гейтнутый мартингейл + stall-watchdog
│   └── window_manager.py      legacy (po-signals); не используется на PO direct
├── api/
│   ├── server.py              FastAPI: REST endpoints + Mini App static
│   └── auth.py                верификация Telegram WebApp initData
├── miniapp/
│   ├── index.html             3 вкладки: Status / Settings / Strategies
│   ├── style.css              dark theme
│   └── app.js                 vanilla JS, без сборки
├── journal/
│   ├── db.py                  SQLite: trades, bans, sessions, kv_store
│   └── candles_db.py          SQLite: candles persist между рестартами
├── tg/
│   ├── bot.py                 команды /status /ping /pause и т.п. + авто-рестарт polling
│   └── chart.py               рендер свечного графика для Telegram
└── tools/                     standalone-утилиты (probe, smoke-test)
```

---

## Многоуровневая система стабильности

Бот защищён **6 независимыми слоями** — каждый из них подхватывает управление, если предыдущий не справился.

```
Слой 1 — WS Heartbeat Watchdog (po_direct.py)
  ├─ каждые 30с: проверяет время последнего фрейма от сервера
  ├─ каждые 25с: отправляет проактивный Engine.IO ping ("2")
  └─ тишина > 90с → ws.close() → auto_reconnect_loop подхватывает

Слой 2 — auto_reconnect_loop (НИКОГДА не сдаётся)
  ├─ Фаза 1 (быстрая): 10 попыток, экспоненциальный backoff до 60с
  ├─ HTTP 401/403 → немедленный fast-path relogin, цикл продолжается
  └─ Фаза 2 (бесконечная): каждые 120с навсегда + relogin каждые 3 раунда

Слой 3 — Stall Watchdog в state_machine
  ├─ tracked пары есть, но нет ни одного тика > 10 мин
  └─ уведомление в TG + принудительный ws.close()

Слой 4 — Защита главного торгового цикла
  └─ try/except вокруг каждой итерации: ошибка логируется, цикл продолжается

Слой 5 — Telegram polling с авто-рестартом
  └─ aiogram упал → перезапуск через 5-60с (exponential backoff)

Слой 6 — Task Supervisor (main.py)
  ├─ каждые 30с: проверяет что state_machine/daily_report/periodic_report живы
  └─ упала задача → уведомление в TG + автоматический рестарт через 5с
```

### Что фиксировали конкретно

| Баг | Симптом | Исправление |
|-----|---------|-------------|
| **Freeze при смене пары** | После LOSS и смены пары бот зависал навсегда | `_in_cycle_step` теперь загружает history + WS-подписку для новой пары прямо при переключении |
| **Frozen WS** | TCP жив, но данные не идут — бот думает что всё ок | Heartbeat watchdog с таймером `_last_frame_ts` |
| **Race condition relogin** | `_do_relogin` закрывает WS → `_recv_loop` запускал параллельный `_auto_reconnect` | `_recv_loop` проверяет `_relogin_in_progress` перед спавном reconnect-задачи |
| **Reconnect сдаётся после 10 попыток** | Если сеть упала надолго или relogin тоже завис — бот умирал | Бесконечный цикл с фазой 2 |
| **Нет тиков после реконнекта** | Пары подписаны, WS живой, но тики не приходят | Stall watchdog принудительно переподключает |
| **Умершие asyncio-задачи** | state_machine или polling падали тихо | Task supervisor перезапускает с TG-уведомлением |
| **Застрял на паре без сигнала** | Бот ждёт consensus на одной паре часами | Кнопки 🔀/🔄 в `/status` для ручного управления |

---

## Telegram команды

| Команда | Описание |
|---------|---------|
| `/status` | Состояние + **inline-кнопки управления циклом** |
| `/ping` | 🩺 Полная диагностика WS/задач + кнопки реконнекта |
| `/balance` | Текущий баланс |
| `/pause` | Поставить на паузу |
| `/resume` | Возобновить / сбросить stop-sum |
| `/stop` | Остановить бота |
| `/bans` | Активные баны пар |
| `/chart SYMBOL` | Свечной график пары |
| `/test SYMBOL call\|put [amount]` | Тестовая сделка |
| `/help` | Список команд |

### Команда /status — управление циклом

Отправь `/status` и получишь текущее состояние **с кнопками**:

```
Режим: real
Пара: JODCNY_otc
МГ-шаг: 1  (сумма $2.73)
Сделок на паре: 1
Смен пары в цикле: 1
Потери за сессию: $1.30
Пауза: нет   Стоп-сумма: нет
Баланс: 205.91
Tracked пар: 15  |  live-тиков: 37675 (активных пар 33)

[🔀 Сменить пару (МГ сохранить)]  [🔄 Сбросить цикл (FREE)]
[⏸ Пауза]                          [🔃 Обновить]
```

Inline-кнопки (видны только когда идёт МГ-цикл):

| Кнопка | Что делает |
|--------|-----------|
| 🔀 **Сменить пару (МГ сохранить)** | Берёт лучшую доступную пару по фильтру, **МГ-шаг и ставка сохраняются** |
| 🔄 **Сбросить цикл (FREE)** | МГ → 0, пара → None, бот заново ищет сигнал на любой паре |
| ⏸ / ▶️ **Пауза / Продолжить** | Быстрое управление без набора команды |
| 🔃 **Обновить** | Обновить текст статуса in-place |

> **Когда нажать 🔀:** пара долго молчит (нет сигнала) — сменить, не теряя МГ-шаг  
> **Когда нажать 🔄:** хочешь начать цикл заново с базовой ставки ($1)

### Команда /ping — диагностика и WS-управление

```
🩺 Диагностика бота
✅ WS: последний фрейм 4с назад
🔌 WebSocket: open
💳 Баланс: $205.91
🤖 SM: ▶️ активен  |  пара: JODCNY_otc  |  МГ-шаг: 1
📡 Пар в трекере: 15  |  активных тик-потоков: 33  |  тиков всего: 37 675
✅ Все критичные задачи живы (7 всего)
🕐 2026-04-26 14:35:12
```

| Кнопка | Действие |
|--------|---------|
| 🔄 **Реконнект WS** | Принудительно закрыть WebSocket → авто-реконнект (~5с) |
| 🔑 **Relogin (новый SSID)** | Запустить Playwright прямо сейчас → свежий токен (~30-60с) |
| 🔃 **Обновить статус** | Обновить диагностику in-place |

### Автоуведомления

- `📡` Сигнал + график
- `✅ WIN` / `❌ LOSS` / `➖ DRAW`
- `🔄` Смена пары (payout упал ниже floor)
- `🛑` Stop-sum или max_steps → ждёт `/resume`
- `⚠️` Нет тиков 10 мин на N парах — принудительный реконнект
- `🚨` Задача упала — перезапускаю автоматически (Task Supervisor)
- `🔴` Сессия PO протухла — пошаговая инструкция по обновлению cookies
- `📊` Ежедневный отчёт (WR, прибыль, баланс)

---

## Полная стратегия работы

### 1. Подключение к Pocket Option

[feed/po_direct.py](feed/po_direct.py) подключается к `wss://api-eu.po.market/socket.io/` напрямую. Авторизация через **session cookie (ssid)**. Сессия живёт ~12-24 часа и **обновляется автоматически** через Playwright + storage_state (~раз в 12 часов или по `NotAuthorized`).

### 2. Свечи

PO стримит **тики** (price-only), бот агрегирует их в M1 OHLC. Дополнительно через `loadHistoryPeriod` запрашиваются **исторические OHLC бары** (с пагинацией до 1060 баров).

[journal/candles_db.py](journal/candles_db.py) сохраняет каждый закрытый бар в SQLite → данные **переживают рестарт контейнера**.

### 3. Сигналы — CONSENSUS 4/5

[strategy/consensus.py](strategy/consensus.py) считает 5 систем на каждом закрытом баре:

| № | Система | Условие |
|---|---------|---------|
| 1 | **RSI-QQE** | RSI(14, smooth=5) пересекает trailing-линию (factor=4.238). Обязательно. |
| 2 | **HTF trend** | Close M5 vs EMA(20). Согласован с направлением. |
| 3 | **Volatility (ATR)** | `ATR(14) / ATR_avg(100) ∈ [0.7, 2.0]`. |
| 4 | **Bollinger zone** | Цена в нижних 30% канала BB(20, 2σ) для BUY или верхних 30% для SELL. |
| 5 | **Candle** | Тело < 2× ATR + направление свечи совпадает. |

**Вход разрешён** при ≥ 4 голосах из 5.

### 4. Фильтр пар (раз в 5 мин)

[strategy/filter_1000.py](strategy/filter_1000.py):
1. Currency OTC с payout ≥ 92%
2. Прогон CONSENSUS на 1060 свечах
3. **`max_loss_streak_overall > 3` → БАН** на 12 часов
4. **Приоритет** = `max_loss_streak_before_win` (0-1 → p1, 2 → p2, 3 → p3)
5. Скан в порядке priority — лучшие пары первыми

### 5. Мартингейл с сигнал-гейтом

```
LOSS → mg_step++, current_pair заморожена
  ↓
Ждём новый CONSENSUS-сигнал на этой паре (freshness < 25с)
  ↓
Сигнал пришёл → открываем base × 2.1^step

WIN → сброс цикла, FREE режим, base $
Stop-sum ($1000) или max_steps (10) → waiting_resume → /resume
```

При автоматической смене пары (payout упал < 85%) бот немедленно загружает историю и подписывается на тики — задержки нет.

**Ручное управление циклом** (кнопки в `/status`):

| Ситуация | Действие |
|----------|---------|
| Пара молчит — нет сигнала долго | 🔀 **Сменить пару** — МГ-шаг сохраняется, ставка та же |
| Хочешь начать заново с $1 | 🔄 **Сбросить цикл** — МГ → 0, FREE режим |
| `force_switch_pair()` | Загружает историю + подписку для новой пары автоматически |

### 6. Детект закрытия сделки

**Приоритет 1** — событие `updateClosedDeals` с полем `profit` (profit > 0 → WIN, < 0 → LOSS).
**Fallback** — баланс-дельта, если событие не пришло за 15 секунд.

---

## Конфигурация

```yaml
mode: paper                  # paper | real

filter:
  min_payout: 92             # минимум payout для входа
  payout_floor: 85           # ниже → смена пары в цикле
  max_losses_in_row: 3       # >3 минусов подряд → бан
  history_candles: 1060
  ban_hours: 12
  day_off_hours: 6
  tf: 60                     # M1

trading:
  base_amount: 1
  expiry_seconds: 120
  max_pair_switch_per_cycle: 1

martingale:
  coefficient: 2.1
  max_steps: 10
  stop_sum: 1000

indicator:
  minConsensus: 4
  rsiPeriod: 14; rsiSmoothing: 5; qqeFactor: 4.238
  htfMultiplier: 5; htfMaPeriod: 20; htfMaType: EMA
  atrPeriod: 14; atrAvgWindow: 100; atrMinRatio: 0.7; atrMaxRatio: 2.0
  bbPeriod: 20; bbStdDev: 2.0; bbZoneDepth: 0.3
  candleMaxAtrMult: 2.0; cooldownBars: 3
```

Все параметры можно менять **live** из Mini App — сохраняются в journal.db, переживают рестарты.

---

## Telegram Mini App

`https://<твой-домен>.up.railway.app/`

Подключается через @BotFather → Bot Settings → Menu Button. Авторизация — Telegram WebApp `initData`.

### Вкладки

1. **Статус** — режим, баланс, пара, MG-шаг, потери сессии, кнопки Pause/Resume
2. **Настройки**
   - 🧠 Параметры активной стратегии (индивидуальные, хранятся в journal.db)
   - 🔍 Фильтр пар / 💰 Торговля / 🎰 Мартингейл / ⏰ Расписание
3. **Стратегии** — список встроенных + пользовательских, активация, загрузка кода

---

## Пользовательские стратегии

Минимальный шаблон:

```python
from dataclasses import dataclass

NAME = "My Strategy"
DEFAULT_PARAMS = {"fastPeriod": 9, "slowPeriod": 21}

PARAM_SCHEMA = {
    "fastPeriod": {"type": "int", "min": 2, "max": 50, "label": "Fast SMA"},
}

@dataclass
class Signal:
    side: str   # "buy" | "sell"
    i: int      # индекс бара сигнала
    votes: dict
    total: int

def generate_signals(candles, params):
    # candles: [{time, open, high, low, close, volume}, ...]
    return [], {}
```

Сигнал засчитывается только если `signals[-1].i == len(candles) - 1`.

Полная инструкция — [STRATEGY.md](STRATEGY.md).

---

## Запуск

### Локально

```bash
git clone https://github.com/andreonberegnoy/PO-Sig-BOT-2026.git
cd PO-Sig-BOT-2026
pip3 install -r requirements.txt

cp .env.example .env
# Заполни PO_SSID, PO_UID, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

python3 main.py
```

Mini App: `http://localhost:8080/`

### На Railway

См. [DEPLOY.md](DEPLOY.md). Кратко:
1. Push в GitHub → Railway автодеплой
2. Volume → mount `/data` (1 GB) для сохранения candles.db и state.db
3. Networking → Public Domain → port 8080
4. Variables: `PO_SSID`, `PO_UID`, `PO_IS_DEMO`, `PO_STORAGE_STATE_B64`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`
5. @BotFather → Menu Button → твой URL

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
| `/api/pair_stats` | GET | Аналитика по парам |
| `/strategy_template` | GET | Шаблон стратегии |

Все эндпоинты требуют `X-Init-Data` header (Telegram WebApp).

---

## Что переживает рестарт (Railway Volume)

Volume `/data`:
- `state.db` — `runtime_state` (пара, MG-шаг, session_loss, direction, paused/waiting_resume)
- `candles.db` — буферы свечей (не нужно ждать накопления заново)
- Журнал сделок, баны пар
- Настройки из Mini App (`settings_overrides`)
- Параметры активной стратегии (`strategy_params:<name>`)
- Загруженные пользовательские стратегии (`/data/user_strategies/*.py`)
- Аналитика (`payout_log`, `pair_stats_log`)

---

## Расписание работы

Опциональное торговое окно (Mini App → Настройки → Расписание):
- По умолчанию **выключено** (24/7)
- Поддерживает окно через полночь (`start=22`, `end=6`)
- Активный МГ-цикл **всегда доводится до WIN**, даже вне окна
- При закрытии цикла вне окна — авто-пауза до `start_hour`

---

## Аналитика по парам

Два фоновых логгера (работают 24/7 независимо от паузы):
- **`payout_log`**: каждые 5 мин — снимок payout каждого ассета (только при изменении)
- **`pair_stats_log`**: каждый час — прогон стратегии на буфере каждой пары (WR, max streak, etc.)

В Mini App → вкладка **Аналитика**: сортируемая таблица с цветовой кодировкой.

---

## Известные ограничения

1. **Сессия PO** — обновляется автоматически каждые ~12ч. Cookies (storage_state) живут ~30 дней; при истечении придёт TG-уведомление с пошаговой инструкцией.
2. **Demo и Real — разные WS** и разные котировки.
3. **History limit ~1060 баров** через PO API.
4. **Railway Hobby plan ($5/мес)** — Free tier недостаточно памяти для Playwright (~800 MB RAM при запуске).
5. **Strategy plugins не sandboxed** — пользовательский код исполняется в основном процессе.

---

## Testing checklist

- [ ] `/test SYMBOL call` открывает сделку в демо
- [ ] WIN → мартингейл сбрасывается
- [ ] LOSS → ждёт новый сигнал на той же паре, $2.10
- [ ] Payout < 85% → автосмена пары (история + тики загружаются сразу)
- [ ] Stop-sum → `/resume` восстанавливает
- [ ] `/status` → кнопки 🔀/🔄 видны при активном МГ-цикле
- [ ] 🔀 Сменить пару → МГ-шаг сохранился, торговля продолжается на новой паре
- [ ] 🔄 Сбросить цикл → МГ=0, бот ищет сигнал на любой паре
- [ ] `/ping` → показывает реальный статус WS + кнопки работают
- [ ] Mini App → Status загружает реальные данные
- [ ] Mini App → Settings → редактирование сохраняется
- [ ] Mini App → Strategies → загрузка кастома + активация работает
- [ ] После рестарта Railway — candles.db и state.db не теряются
