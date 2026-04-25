# MY PO-SIG BOT

Торговый бот для Pocket Option с **прямым подключением через WebSocket**, индикатором **CONSENSUS 4/5**, **Telegram Mini App** для визуального управления и **системой пользовательских стратегий**. Работает 24/7 на Railway без браузера.

## Архитектура

```
config.yaml                    все параметры (фильтр, мартингейл, индикатор)
.env                           секреты (PO_SSID, telegram токены) — не в git
main.py                        оркестратор: feed + strategy + api + telegram
Dockerfile                     python:3.12-slim, ~80 MB RAM
railway.toml                   Railway deploy config
├── feed/
│   ├── po_direct.py           прямое подключение к PO (websockets, без Chrome)
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
│   ├── state_machine.py       свободный скан + сигнал-гейтнутый мартингейл
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
│   ├── bot.py                 команды /status /test /pause и т.п.
│   └── chart.py               рендер свечного графика для Telegram
└── tools/                     standalone-утилиты (probe, smoke-test)
```

## Полная стратегия работы

### 1. Подключение к Pocket Option

[feed/po_direct.py](feed/po_direct.py) подключается к `wss://demo-api-eu.po.market/socket.io/` (или `api-eu` для real) от твоего имени. Авторизация через **session cookie (ssid)**, извлечённый из браузера один раз. Сессия живёт ~24 часа.

Преимущества vs предыдущая схема через po-signals:
- **Нет браузера/Chrome/CDP** — чистый Python WS-клиент
- **Низкий fingerprint** — выглядит как обычный SDK-пользователь
- **Дёшево** — 80 МБ RAM, любой VPS / Railway free tier
- **Быстро** — нет CDP overhead

### 2. Свечи

PO стримит **тики** (price-only), наш feed агрегирует их в M1 OHLC. Дополнительно через `loadHistoryPeriod` запрашиваются **исторические OHLC бары** (с пагинацией до 1060 баров).

[journal/candles_db.py](journal/candles_db.py) сохраняет каждый закрытый бар в SQLite → данные **переживают рестарт контейнера**. При старте бот подгружает кеш из БД.

**Заполнение дыр при рестарте:** если последний кешированный бар старше чем `now - 2 × period`, бот автоматически докачивает недостающий диапазон через `loadHistoryPeriodFast` (~1-2 сек на пару). Без этого после downtime график показывал бы пустой промежуток.

### 3. Сигналы — CONSENSUS 4/5

[strategy/consensus.py](strategy/consensus.py) считает 5 систем на каждом закрытом баре:

| № | Система | Условие |
|---|---------|---------|
| 1 | **RSI-QQE** | RSI(14, smooth=5) пересекает trailing-линию (factor=4.238). Обязательно. |
| 2 | **HTF trend** | Close M5 vs EMA(20). Согласован с направлением. |
| 3 | **Volatility (ATR)** | `ATR(14) / ATR_avg(100) ∈ [0.7, 2.0]`. |
| 4 | **Bollinger zone** | Цена в нижних 30% канала BB(20, 2σ) для BUY или верхних 30% для SELL. |
| 5 | **Candle** | Тело < 2× ATR + направление свечи совпадает. |

**Вход разрешён** при ≥ 4 голосах из 5. На выходных — настраивается (`requireAll5OnWeekend`).

### 4. Фильтр пар (раз в 5 мин)

[strategy/filter_1000.py](strategy/filter_1000.py):
1. Currency OTC с payout ≥ 92%
2. Прогон CONSENSUS на 1060 свечах
3. **`max_loss_streak_overall > 3` → БАН** на 12 часов
4. **Приоритет** = `max_loss_streak_before_win`:
   - 0-1 → priority 1 (идеал)
   - 2 → priority 2
   - 3 → priority 3 (макс. допустимо)
5. Скан в порядке priority — лучшие пары первыми

### 5. Скан-цикл (каждую секунду)

Для каждой tracked-пары:
1. Eligible? (не забанена, payout ≥ 92%)
2. Новый закрытый бар?
3. **Staleness < 25 сек** (если опоздали — пропуск)
4. CONSENSUS на закрытом баре → если есть → открываем сделку

### 6. Открытие сделки

1. `ensure_pair_in_window` — для PO direct это `feed.subscribe` (live тики)
2. Запоминаем `pre_balance`
3. Шлём `42["openOrder", {asset, amount, action, time, isDemo, requestId}]`
4. Telegram: 📡 сигнал + график + «Захожу $X»

### 7. Детект закрытия

**Приоритет 1** — событие `updateClosedDeals` от PO с полем `profit`:
- `profit > 0` → WIN
- `profit < 0` → LOSS
- `profit ≈ 0` → DRAW

**Fallback** — баланс-дельта (если событие не пришло за 15 сек):
- `delta > 0` → WIN, `delta + amount` = gross return
- `delta < 0` → LOSS
- `delta ≈ 0` → DRAW

### 8. Мартингейл с сигнал-гейтом

```
LOSS → mg_step++, current_pair заморожена
  ↓
Ждём новый CONSENSUS-сигнал на этой паре (staleness 25 сек)
  ↓
Сигнал пришёл → открываем сделку с base × 2.1^step
  │ (на step=1: $2.10, step=2: $4.41, ..., step=10: $1668)
  ├─ если сигнал в ту же direction → классический МГ
  └─ если в противоположную → следуем (обновляем direction)

После смены пары (max 1 раз за цикл) → торгуем ЛЮБОЙ сигнал
   на новой паре игнорируя дальнейшее падение payout

WIN → сброс цикла, FREE режим, $1
Stop-sum ($1000 потерь) или max_steps (10) → waiting_resume
   Ждёт /resume в Telegram
```

## Telegram Mini App

`https://po-sig-bot-2026-production.up.railway.app/` (твой URL)

Подключается через @BotFather → Bot Settings → Menu Button. Авторизация — Telegram WebApp `initData`, доступ только указанному `chat_id`.

### Вкладки

1. **Статус**
   - Режим, баланс, активная стратегия
   - Tracked / live / в бане
   - Текущая пара, MG-шаг
   - Сумма первой сделки, экспирация
   - Потери сессии, пауза
   - Кнопки Pause / Resume / Обновить

2. **Настройки** — два уровня:
   - **🧠 Параметры стратегии: \<имя активной\>** (динамически) — параметры из `DEFAULT_PARAMS` текущей стратегии. Для CONSENSUS это 20 полей: minConsensus, RSI, HTF, ATR, BB, candle. Каждая стратегия хранит свои параметры **отдельно** в `journal.db` и не теряет их при переключении.
   - **Глобальные** (общие для всех стратегий):
     - 🔍 Фильтр пар
     - 💰 Торговля
     - 🎰 Мартингейл

   Глобальные пишутся в `config.yaml`, параметры стратегии — в `journal.db` под ключом `strategy_params:<name>`. При переключении активной стратегии секция «Параметры стратегии» автоматически обновляется на параметры новой.

3. **Стратегии**
   - Список встроенных + пользовательских
   - **▶ Активировать** на неактивных
   - **⏹ Выключить (на consensus)** на активной кастомной
   - **🗑 Удалить** на неактивных пользовательских
   - **Загрузка кода**: textarea + кнопка «📋 Загрузить шаблон» (подгружает [strategy/_template.py](strategy/_template.py)) → правишь → «💾 Сохранить и активировать»

## Стратегии — как писать свои

Полная инструкция в [STRATEGY.md](STRATEGY.md). Минимум:

```python
from dataclasses import dataclass

NAME = "My Strategy"
DEFAULT_PARAMS = {"fastPeriod": 9, "slowPeriod": 21}

# (опционально) Схема для красивой UI в Mini App
PARAM_SCHEMA = {
    "fastPeriod": {"type": "int", "min": 2, "max": 50,  "label": "Fast SMA"},
    "slowPeriod": {"type": "int", "min": 5, "max": 200, "label": "Slow SMA"},
}

@dataclass
class Signal:
    side: str          # "buy" или "sell"
    i: int             # индекс бара сигнала
    votes: dict
    total: int

def generate_signals(candles, params):
    # candles: [{time, open, high, low, close, volume}, ...]
    # вернуть (list[Signal], diag_dict)
    return [], {}
```

- Сигнал засчитывается только если `signals[-1].i == len(candles) - 1` (на последнем закрытом баре).
- Если `PARAM_SCHEMA` отсутствует — бот сам угадает типы (int/float/bool), но без min/max.
- После загрузки через Mini App параметры твоей стратегии **сохраняются отдельно** от других, видны в секции «🧠 Параметры стратегии: \<твоё имя\>».

## Конфигурация

```yaml
mode: paper                  # paper | real

filter:
  min_payout: 92             # минимум payout для входа
  payout_floor: 85           # ниже → смена пары в цикле
  max_losses_in_row: 3       # >3 минусов подряд → бан
  history_candles: 1060      # размер буфера для HTF
  ban_hours: 12
  day_off_hours: 6
  tf: 60                     # M1

trading:
  base_amount: 1
  expiry_seconds: 120
  one_trade_at_a_time: true
  max_pair_switch_per_cycle: 1

martingale:
  coefficient: 2.1
  max_steps: 10
  stop_sum: 1000             # пауза при $1000 потерь

indicator:
  minConsensus: 4
  requireAll5OnWeekend: false
  rsiPeriod: 14; rsiSmoothing: 5; qqeFactor: 4.238
  htfMultiplier: 5; htfMaPeriod: 20; htfMaType: EMA
  atrPeriod: 14; atrAvgWindow: 100; atrMinRatio: 0.7; atrMaxRatio: 2.0
  bbPeriod: 20; bbStdDev: 2.0; bbZoneDepth: 0.3
  candleMaxAtrMult: 2.0; candleReqAlign: true
  cooldownBars: 3
```

Все параметры можно менять live из Mini App — сохраняются в config.yaml.

## Запуск

### Локально (для разработки)

```bash
git clone https://github.com/andreonberegnoy/PO-Sig-BOT-2026.git
cd PO-Sig-BOT-2026
pip3 install -r requirements.txt

# Извлеки PO ssid (Demo) из браузера:
# pocketoption.com → DevTools → Network → WS → найди /socket.io/?EIO=4
# → Messages → первый исходящий 42["auth", {session: "...", uid: ...}]

cp .env.example .env
# Впиши PO_SSID, PO_UID, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

python3 main.py
```

Mini App локально: `http://localhost:8080/`

### На Railway

См. [DEPLOY.md](DEPLOY.md). Кратко:
1. Push в GitHub
2. Railway → New Project → Deploy from repo
3. Settings → Volumes: mount на `/app/journal` (1 GB)
4. Settings → Networking → Public Domain → target port 8080
5. Variables: `PO_SSID`, `PO_UID`, `PO_WS_URL`, `PO_IS_DEMO`, `MODE`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`
6. @BotFather → Menu Button → URL `https://<твой-домен>.up.railway.app/`

## Telegram команды

- `/status` — пара, MG-шаг, баланс
- `/balance` — баланс
- `/pause` / `/resume` — пауза / возобновление
- `/stop` — остановить
- `/bans` — активные баны
- `/chart SYMBOL` — график пары
- `/test SYMBOL call|put [amount]` — тестовая сделка
- `/settings` — список настроек (inline keyboards, fallback к Mini App)

Автоуведомления:
- 📡 сигнал + график
- ✅ WIN / ❌ LOSS / ➖ DRAW
- 🔄 смена пары
- 🛑 stop_sum / max_steps → ждёт /resume
- 📊 ежедневный отчёт в 7:00

## REST API (для Mini App и интеграций)

- `GET /api/status` — состояние бота
- `GET /api/settings` — текущая cfg
- `PUT /api/settings` — `{"key.path": value}` → обновить
- `GET /api/strategies` — список стратегий (с `params`, `default_params`, `param_schema`)
- `POST /api/strategies` — `{name, code}` загрузить новую
- `DELETE /api/strategies/{name}` — удалить
- `POST /api/strategies/{name}/activate` — активировать
- `GET /api/strategies/{name}/params` — параметры стратегии
- `PUT /api/strategies/{name}/params` — `{key: value}` обновить
- `POST /api/control/pause` / `resume` — управление
- `GET /strategy_template` — текст шаблона

Все эндпоинты требуют валидный `X-Init-Data` header (Telegram WebApp).

## Известные ограничения

1. **Сессия PO живёт ~24ч** — раз в день обновлять `PO_SSID` в Railway Variables (вытащить новый ssid из браузера).
2. **Demo и Real — разные WS** (`demo-api-eu` vs `api-eu`). Котировки могут отличаться от того что видишь на сайте.
3. **History limit ~1060 баров** через `loadHistoryPeriodFast`. Глубже — нужно ждать пока живой бот накопит SQLite.
4. **На Railway free tier** Mini App может быть медленным; рекомендуется Hobby plan ($5/мес).
5. **Strategy plugins не sandboxed** — пользовательский код выполняется в основном процессе. Не загружай чужой код без проверки.

## Testing checklist

- [ ] `/test SYMBOL call` открывает сделку в демо
- [ ] WIN → мартингейл сбрасывается
- [ ] LOSS → ждёт новый сигнал на той же паре, $2.10
- [ ] Payout < 85% → смена пары (одна за цикл)
- [ ] Stop-sum → `/resume` восстанавливает
- [ ] Mini App → Status загружает реальные данные
- [ ] Mini App → Settings → редактирование сохраняется в config.yaml
- [ ] Mini App → Strategies → загрузка кастома + активация работает
- [ ] После рестарта контейнера Railway candles.db не теряется
