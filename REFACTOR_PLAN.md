# REFACTOR_PLAN — реструктуризация PO-Sig Bot

Полная структура работ по реорганизации Mini App + backend под новую модель.
Документ ведётся для: (а) ничего не потерять, (б) идти по этапам, (в) видеть прогресс.

**Дата начала:** 2026-04-29
**Текущий стабильный коммит:** `stable-pre-strategy-removal`
**Рабочая ветка:** `refactor/remove-strategy`

---

## Цели

1. **Упростить структуру Mini App** — 3 главных раздела вместо 6 разрозненных вкладок
2. **Убрать терминологию virtual/real** — все CONSENSUS-сигналы равноправны, флаг `entered` различает их
3. **Дать гибкость в Martingale** — ротация пар + перенос неиспользованных перекрытий
4. **Обогатить аналитику** — фильтрация по часам, диапазонам времени, экспорт, применить-как-фильтр

---

## ГЛОССАРИЙ (новая терминология)

| Термин | Значение |
|---|---|
| **Signal** | любой CONSENSUS-сигнал прошедший фильтр стратегии. ВСЕ signals равноправны — независимо вошёл бот или был занят |
| **entered** | флаг True/False у signal — реально ли бот открыл сделку |
| **trade** | сделка которую бот открыл = signal с entered=True. Имеет payout, profit, balance_after |
| **exp_wins** | массив [w1..w5] — что было бы на каждой экспирации (вычисляется по 5 след. барам) |
| **tracked pair** | пара прошедшая фильтр filter_1000 — в торговом обороте сейчас |

В UI **никакого «virtual»** — везде «сигналы» / «сделки».

---

## Архитектурные принципы

1. **БД signals пишется всегда** — каждый CONSENSUS-сигнал на tracked-паре, независимо вошёл бот или нет
2. **Retention** — 6 месяцев default (configurable 6-12), удаление по 1-месячным бакетам
3. **Profit-метрики** считают только `entered=True AND result=WIN`
4. **Аналитика** считает все signals (entered + не-entered) — никакого разделения на virtual/real, флаг `entered` нужен только для расчёта Profit
5. **Market snapshots собираем** — для **каждого** сигнала записываем параметры рынка на момент входа: ATR(14) 1m, ATR(14) 5m, EMA20 1m, EMA50 5m, RSI14, QQE Factor, размер свечи / ATR ratio. Это нужно чтобы потом можно было фильтровать «торговать только при ATR в диапазоне X-Y» и подбирать оптимальные условия рынка
6. **Постоянная пере-фильтрация** — `filter_1000` критерии (WR1-1000, WR1-200, payout) проверяются на каждой сделке, не только при первичном scan

## ❌ НЕ ТРОГАТЬ (критично)

Эти компоненты **НЕ изменяются** ни на одном этапе. Только перемещаются в UI:

- `strategy/consensus.py` — сама стратегия CONSENSUS 4/5
- `strategy/indicators.py` — RSI, EMA, ATR, BB, QQE расчёты
- `strategy/registry.py` — система загрузки стратегий
- `strategy/_template.py` — шаблон для новых стратегий
- `strategy/filter_1000.py` — выбор пар (нужен для торговли)
- `strategy/user/*.py` — пользовательские стратегии
- Все параметры индикаторов (rsiPeriod, qqeFactor, atrPeriod, bbStdDev, htfMaPeriod и т.д.) — остаются как есть
- DEFAULT_PARAMS в consensus.py — не меняется

В Mini App вкладка **Настройки стратегии** — это просто **новое визуальное расположение** существующих параметров, без переписывания их логики или дефолтов.

---

## ЭТАП 0 — подготовка (СДЕЛАНО)

- [x] Tag `stable-pre-strategy-removal` создан
- [x] Branch `refactor/remove-strategy` создан и запушен
- [ ] Hetzner snapshot — **рекомендую сделать** перед удалением (опционально, но безопасно)

---

## ЭТАП 1 — УДАЛЕНИЕ (готов к старту)

### 1.1 Mini App (HTML + JS) — ✅ DONE

- [x] `miniapp/index.html`: удалены вкладки `tab-analytics`, `tab-hourly`, `tab-expiry` + кнопки в `<header>`
- [x] `miniapp/index.html`: построена новая структура — 3 топ-таба (Главная / Настройки бота / Стратегия) с подвкладками внутри Стратегии (Список / Настройки / Аналитика-placeholder)
- [x] `miniapp/app.js`: удалены функции и обработчики (~700 строк):
  - `loadAnalytics()`, `loadHourly()`, `loadExpiry()`, `renderAnalyticsTable()`
  - `_lastHourlyData`, `_lastExpiryData`, `hourlyState`, `analyticsState`
  - Все `.range-btn`, `.hour-range-btn`, `.exp-*-btn` обработчики
  - `btn-hourly-*` (export, apply, clear, backfill, reset)
  - `btn-expiry-*` (load, export)
  - `refreshHourlyFilterStatus()`, `downloadCSV()`, `fmt()`, `pctClass()`, `streakClass()`
- [x] `miniapp/app.js`: разделён `loadSettings` на `loadGlobalSettings` (общие) + `loadStrategyParams` (indicator params)
- [x] `miniapp/app.js`: добавлена sub-tab навигация для Стратегии
- [x] `miniapp/style.css`: удалены стили `.analytics`, `.range-selector`, `.range-btn`, `.btn-tiny` (analytics-only)
- [x] `miniapp/style.css`: добавлены стили `.subtabs`, `.subtab`, `.subtab-panel`

### 1.2 Backend (api/server.py)

- [ ] Удалить endpoints:
  - `GET /api/pair_stats`
  - `GET /api/hourly_stats`
  - `GET /api/hour_whitelist`
  - `POST /api/apply_hour_whitelist`
  - `POST /api/clear_hour_whitelist`
  - `POST /api/reset_hourly_stats`
  - `GET /api/expiry_stats`
  - `POST /api/backfill_virtual_signals`

### 1.3 State machine (trading/state_machine.py)

- [ ] Удалить методы:
  - `_virtual_signals_loop` (фоновая задача целиком)
  - `_persist_exp_wins`
  - `_hour_allowed`
- [ ] Убрать вызов `_hour_allowed` в `_eligible_for_new_cycle`
- [ ] Убрать чтение `hour_expiry_overrides` в `_open_and_track`
- [ ] Убрать обращение к `_persist_exp_wins` в `_on_trade_closed`

### 1.4 main.py

- [ ] Убрать регистрацию задачи `virtual_signals` в `tasks` и `_RESTARTABLE`

### 1.5 Журнал (journal/db.py)

- [ ] Удалить из SCHEMA:
  - `CREATE TABLE virtual_signals`
  - `CREATE TABLE pair_stats_log`
  - `CREATE TABLE payout_log`
  - индексы для них
- [ ] Удалить методы:
  - `update_exp_wins`
  - `insert_virtual_signal`
  - `pending_virtual_signals`
  - `settle_virtual_signal`
  - `hourly_stats` (старый — будет переписан в этапе 2 под новую модель)
  - `log_pair_stats` (если есть)
- [ ] В `_migrate_columns`: удалить добавление `exp_wins` колонки + добавить миграцию `ALTER TABLE trades DROP COLUMN exp_wins`
- [ ] Миграция времени запуска: `DROP TABLE IF EXISTS virtual_signals/pair_stats_log/payout_log`

### 1.6 Документация

- [ ] `README.md`: удалить упоминания вкладок Аналитика/По часам/Экспирация
- [ ] `CLAUDE.md`: убрать ссылки на virtual_signals, hourly_stats и пр. (если есть)

### Критерии готовности этапа 1

- [ ] Mini App открывается, видны 3 вкладки: Статус / Настройки / Стратегии
- [ ] Бот запускается без ошибок
- [ ] Бот торгует через CONSENSUS (логи показывают `tracked pairs:`, `scan: tracked=N`)
- [ ] При закрытии сделки запись в `trades` происходит
- [ ] `docker compose ps` показывает `healthy`

---

## ЭТАП 2 — ПОСТРОЕНИЕ НОВОЙ СТРУКТУРЫ

### 2.1 Mini App — новая структура вкладок

```
🏠 Главная (Статус)
├─ Текущее состояние бота (mode, balance, current_pair, mg_step)
├─ 🎯 Активные пары — что торгуется СЕЙЧАС (новое)
├─ 💰 Profit за день — только entered=True AND WIN
├─ Кнопки управления циклом
└─ Кнопки Pause/Resume/Refresh

⚙️ Настройки бота (общие)
├─ Базовая ставка (base_amount)
├─ Экспирация по умолчанию (expiry_seconds)
├─ Гибкий Martingale ← здесь!
│   ├─ Общий лимит шагов
│   ├─ Per-pair max trades
│   ├─ Перенос остатка между парами (toggle)
│   ├─ Stop-sum, reset_on_win
│   ├─ Поведение последней пары (торговать до stop_sum)
│   └─ Ручная смена пары засчитывается в счётчик
├─ 🕒 Расписание (рабочие часы)
│   └─ Puppeteer/PO заходит ТОЛЬКО в эти часы
├─ 📅 Не торговать на выходных (toggle)
├─ Категории активов (forex/crypto/stocks/...)
├─ Payout floor, min_payout, ban_hours, pause_hours
└─ Retention аналитики (6/9/12 месяцев)

🧠 Стратегия
├─ Выбор активной стратегии
├─ + Добавить стратегию
└─ [Активная стратегия]
    ├─ ⚙️ Настройки стратегии
    │   └─ ТОЛЬКО indicator params: RSI, EMA, CCI, QQE, BB, ATR, HTF
    └─ 📊 Аналитика
        ├─ Главная таблица: пары × 24h-средние
        ├─ Фильтры периода: 1д / 7д / 30д / 60д / по дате
        ├─ Сортировка по любому столбцу
        ├─ Клик на пару → раскрытие 24-часовой детализации
        ├─ Плашка «Рабочие часы» (визуальный индикатор активного фильтра)
        ├─ Плашка фильтр по времени (13:00-19:00 — суммарно за период)
        ├─ Кнопка «Применить как фильтр» к стратегии (только здесь)
        ├─ Кнопка «Экспорт данных» (CSV)
        └─ Цветовая подсветка: зелёный/жёлтый/оранжевый/красный по WR
```

### 2.2 Колонки в Аналитике (16 + разделитель)

**Основные показатели** (до разделителя):
1. Время / час (если детализация)
2. % Best exp за последние N сделок (фильтруемое окно)
3. Кол-во сигналов (signals total)
4. WR общий
5. Кол-во плюсовых
6. Кол-во минусовых
7. Max losses до плюса
8. % первой сделки (2 мин)
9. % Best exp первой сделки (1-5 мин виртуально)
10. % времени 85-92% выплаты
11. Средняя выплата %

**| РАЗДЕЛИТЕЛЬ |**

**Второстепенные показатели — market snapshots на момент сигнала**

Собираются для ВСЕХ signals. Полный список того что **фактически вычисляет** CONSENSUS (просмотрел код `strategy/consensus.py`):

#### Голоса CONSENSUS (5 индикаторов, каждый 0 или 1)
12. `votes_rsi` — голос RSI/QQE кросса (всегда 1, иначе сигнала бы не было)
13. `votes_htf` — голос HTF (тренд старшего ТФ совпал?)
14. `votes_vol` — голос Volatility (ATR ratio в диапазоне atrMinRatio..atrMaxRatio?)
15. `votes_bb` — голос Bollinger (цена в зоне покупки/продажи?)
16. `votes_candle` — голос Candle (свеча выровнена + размер OK?)
17. `votes_total` — сумма голосов (4 или 5)

#### Числовые значения индикаторов
18. `rsi_ma` — текущее RSI MA значение (RSI period 14, smoothing 5)
19. `qqe_trailing` — текущая QQE trailing line
20. `htf_value` — направление HTF тренда (+1 up / 0 flat / -1 down) на TF = htfMultiplier × 1m (5m по умолчанию)
21. `atr14_1m` — ATR(14) текущий на 1m
22. `atr_avg` — SMA(ATR, atrAvgWindow=100) — средний ATR за 100 баров
23. `atr_ratio` = atr14_1m / atr_avg (насколько волатильность отличается от средней — фильтруем торговлю при ratio < 0.7 или > 2.0)
24. `bb_upper` — верх Bollinger канала
25. `bb_lower` — низ канала
26. `bb_position` = (close − lower) / (upper − lower), значение 0..1 (где находится цена в канале)
27. `candle_body` = abs(close − open) (размер тела свечи)
28. `candle_atr_ratio` = candle_body / atr14_1m (импульсность входа)
29. `candle_direction` — 1 если close>open (бычья), -1 если медвежья, 0 если doji

#### Контекстные мета (для day-of-week и hour-of-day анализа)
30. `hour_local` (0..23 в TZ пользователя)
31. `day_of_week` (0=Mon..6=Sun)
32. `payout_at_signal` (% выплаты PO в момент сигнала)
33. `wr1_long_at_signal` (WR1 за 1000 свечей этой пары на момент сигнала — динамическая метрика стратегии)
34. `wr1_recent_at_signal` (WR1 за 200 свечей — recent form пары)

Эти данные собираются `_persist_market_snapshot()` в момент закрытия каждого CONSENSUS-сигнала и сохраняются в таблицу `signals`.

#### Источники данных
- Из cached candles (`sm._candles`) — последние N баров до signal_ts
- Расчёты используют существующие функции из `strategy/indicators.py` (rsi/qqe, htf_trend, atr, sma, bollinger, candle_aligned) — **без переписывания**
- Голоса передаются прямо из `Signal.votes` dict который уже формирует CONSENSUS
- WR1-1000 / WR1-200 берутся из `_pair_scores[symbol]` (актуальные на тот момент)

#### Расширяемость для других стратегий
Если активна не-CONSENSUS стратегия (загруженная пользователем) — собираем минимум: `votes_total`, `rsi_ma` (если стратегия его экспортирует), `atr14_1m`, `bb_position`, `candle_atr_ratio`, `hour_local`, `day_of_week`, `payout_at_signal`. Колонки специфичные для CONSENSUS будут NULL — UI это покажет как «—».

### 2.3 Новая таблица signals (заменяет virtual_signals)

```sql
CREATE TABLE signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name   TEXT NOT NULL,           -- ИЗ КАКОЙ СТРАТЕГИИ родился сигнал (consensus / my_custom / ...)
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,           -- call | put
    signal_ts       INTEGER NOT NULL,        -- entry minute (= bar close + tf)
    entry_close     REAL NOT NULL,
    entered         BOOLEAN NOT NULL DEFAULT 0,
    trade_id        TEXT,                    -- FK к trades.trade_id если entered
    exp_wins        TEXT,                    -- JSON [w1..w5]
    settled_at      INTEGER,

    -- Голоса CONSENSUS (или эквивалент в кастомных стратегиях)
    votes_rsi       INTEGER,                 -- 0/1
    votes_htf       INTEGER,                 -- 0/1
    votes_vol       INTEGER,                 -- 0/1
    votes_bb        INTEGER,                 -- 0/1
    votes_candle    INTEGER,                 -- 0/1
    votes_total     INTEGER,                 -- сумма (4 или 5)

    -- Числовые значения индикаторов на момент сигнала
    rsi_ma          REAL,                    -- RSI MA значение
    qqe_trailing    REAL,                    -- QQE trailing line
    htf_value       INTEGER,                 -- +1 up / 0 flat / -1 down
    atr14_1m        REAL,                    -- ATR(14) на 1m
    atr_avg         REAL,                    -- SMA(ATR, atrAvgWindow=100)
    atr_ratio       REAL,                    -- atr14_1m / atr_avg
    bb_upper        REAL,
    bb_lower        REAL,
    bb_position     REAL,                    -- 0..1, позиция close в канале
    candle_body     REAL,                    -- abs(close - open)
    candle_atr_ratio REAL,                   -- candle_body / atr14_1m
    candle_direction INTEGER,                -- 1=бычья, -1=медвежья, 0=doji

    -- Контекстные мета (для day-of-week / hour-of-day анализа)
    hour_local      INTEGER,                 -- 0..23 в TZ пользователя
    day_of_week     INTEGER,                 -- 0=Mon..6=Sun
    payout_at_signal INTEGER,                -- %
    wr1_long_at_signal REAL,                 -- WR1-1000 этой пары
    wr1_recent_at_signal REAL,               -- WR1-200 этой пары

    UNIQUE(symbol, signal_ts)
);
CREATE INDEX idx_signals_strat ON signals(strategy_name);
CREATE INDEX idx_signals_symts ON signals(symbol, signal_ts);
CREATE INDEX idx_signals_pending ON signals(settled_at, signal_ts);
CREATE INDEX idx_signals_entered ON signals(entered);
CREATE INDEX idx_signals_atr ON signals(atr14_1m);
CREATE INDEX idx_signals_hour ON signals(hour_local);
CREATE INDEX idx_signals_dow ON signals(day_of_week);
```

#### Per-strategy изоляция аналитики

Каждая стратегия имеет **свои signals** (через `strategy_name`). Когда
пользователь в UI кликает на стратегию X и переходит в её Аналитику —
запрос идёт `WHERE strategy_name = 'X'`. Это значит:

- Аналитика consensus и аналитика my_custom — **полностью изолированы**
- Параметры тоже per-strategy (через `strategy_params:<name>` в `state_kv`,
  уже работает)
- Каждая стратегия накапливает свою историю независимо
- При активации новой стратегии — её signals начинают писаться с момента
  активации (старая стратегия больше не пишет, но её данные сохраняются)

Все market params снимаются из cached candles на момент signal_ts через переиспользуемые функции из `strategy/indicators.py`. Добавлена расширяемость для нестандартных стратегий — индикаторы которые не использует загруженная стратегия будут NULL.

### 2.4 Гибкий Martingale (общие настройки)

Логика:
```
Общий лимит шагов: 7 (configurable)
Pair 1: max=3 перекрытий
   if payout < 85% → переход на след. пару
   неиспользованные сделки (3 - использовано) → в резерв
Pair 2: max=3
   if 3 минуса подряд OR payout < 85% → переход
   неиспользованные → в резерв
Pair 3 (последняя в цикле смен):
   доступно = свой лимит + ВСЕ перенесённые
   торгует до stop_sum независимо от payout
```

Правила:
- Ручная смена пары через UI/TG = засчитывается в общее число смен
- Если все смены использованы — на последней паре до stop_sum или WIN
- Настройки хранятся в `cfg.martingale.*`

### 2.5 Постоянная пере-фильтрация

На каждой сделке (не только раз в час при scan):
- Проверять WR1-1000, WR1-200, max_loss_streak, payout текущей пары
- Если что-то не проходит — пометить пару для смены и перейти на SEARCH

### 2.6 TG-уведомления при открытии сделки

Текст должен включать:
- Символ + направление (BUY/SELL)
- Payout %
- Сумма ставки
- МГ-ступень
- Баланс до сделки
- График PNG (всегда, даже на МГ-ступенях)

При закрытии:
- WIN/LOSS, profit
- Баланс после
- Сброс/продолжение МГ
- Накопленный session_loss

### 2.7 Расписание (рабочие часы)

В Настройках бота:
- Toggle «Расписание включено»
- Время начала / конца (например 09:00-22:00 локального TZ)
- Toggle «Не торговать на выходных» (Sat-Sun)
- Поведение: Puppeteer/PO **отключается** вне рабочих часов (не просто пауза, а полное закрытие соединения для экономии ресурсов и анти-детекта)

### 2.8 Retention аналитики

- Default: 6 месяцев
- Configurable: до 12 месяцев
- Cleanup: weekly background task → `DELETE FROM signals WHERE signal_ts < now - retention*30*86400`
- Удаление помесячными бакетами
- Индикатор размера БД в Настройках

---

## ЭТАП 3 — ВТОРИЧНЫЕ УЛУЧШЕНИЯ (после этапа 2)

- [ ] Day-of-week анализ (понедельник vs пятница) — новая колонка в Аналитике
- [ ] Активные пары на главном экране как мини-виджет
- [ ] Day-off видимость (TG-алерт уже есть, добавить в Status)
- [ ] Фотография графика на каждой сделке (включая МГ-ступени)
- [ ] Постоянная фильтрация (1000+200) на каждой сделке
- [ ] Мобильная адаптация всех таблиц аналитики

---

## ПОРЯДОК ВЫПОЛНЕНИЯ

| # | Что | Когда | Статус |
|---|---|---|---|
| 0 | Tag + Branch + (опц.) Snapshot | сделано | ✅ |
| 1 | Удаление аналитики | этап 1 | 🟡 готов к старту |
| 2.1 | Новая таблица signals + collector | этап 2 | ⏳ |
| 2.2 | Новая Mini App структура (3 раздела) | этап 2 | ⏳ |
| 2.3 | Аналитика — главная таблица | этап 2 | ⏳ |
| 2.4 | Аналитика — фильтры + сортировка | этап 2 | ⏳ |
| 2.5 | Аналитика — раскрытие 24h детально | этап 2 | ⏳ |
| 2.6 | Аналитика — apply-as-filter | этап 2 | ⏳ |
| 2.7 | Гибкий Martingale | этап 2 | ⏳ |
| 2.8 | Расписание + выходные | этап 2 | ⏳ |
| 2.9 | TG-уведомления полные | этап 2 | ⏳ |
| 2.10 | Retention scheduler | этап 2 | ⏳ |
| 3.* | Вторичные улучшения | этап 3 | ⏳ |

---

## ОТКАТ В СЛУЧАЕ ПРОБЛЕМ

```bash
# Откат КОДА на стабильное состояние
cd "/Users/andrii/Desktop/Cloude Projects/MY PO-SIG BOT"
git checkout main
git reset --hard stable-pre-strategy-removal
git push origin main --force-with-lease

# Деплой обратно
ssh root@37.27.13.173 'cd /opt/po-bot && git fetch && git checkout main && git reset --hard origin/main && cd deploy && docker compose up -d --build'

# БД signals потеряна — ничего не поделать без Hetzner snapshot
# (вот почему рекомендован snapshot ДО этапа 1)
```

---

## ИЗМЕНЕНИЯ ЭТОГО ПЛАНА

Если что-то меняется в процессе — **редактируем этот файл** (REFACTOR_PLAN.md), коммитим. История изменений сохраняется в git.
