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
6. **Постоянная пере-фильтрация** — `filter_1000` критерии (общая проходимость 1000 свечей, проходимость последних 200, payout) проверяются на каждой сделке, не только при первичном scan

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

### 1.2 Backend (api/server.py) — ✅ DONE

- [x] Удалены endpoints (~870 строк):
  - `GET /api/pair_stats`, `GET /api/hourly_stats`, `GET /api/expiry_stats`
  - `GET /api/hour_whitelist`, `POST /api/apply_hour_whitelist`, `POST /api/clear_hour_whitelist`
  - `POST /api/reset_hourly_stats`, `POST /api/backfill_virtual_signals`
- [x] Удалён неиспользуемый `import asyncio` из server.py

### 1.3 State machine (trading/state_machine.py) — ✅ DONE

- [x] Удалены методы:
  - `_virtual_signals_loop` (фоновая задача целиком, ~140 строк)
  - `_persist_exp_wins` (~50 строк)
  - `_hour_allowed` (~25 строк)
  - `_pair_stats_logger_loop` (фоновая задача, ~45 строк)
- [x] Убран вызов `_hour_allowed` в `_eligible_for_new_cycle` и в scan loop
- [x] Убрано чтение `hour_expiry_overrides` в `_open_and_track`
- [x] Убрано обращение к `_persist_exp_wins` в `_on_trade_closed`
- [x] Убран `asyncio.create_task(self._pair_stats_logger_loop())` из `run()`

### 1.4 main.py — ✅ DONE

- [x] Убрана регистрация задачи `virtual_signals` в `tasks` и `_RESTARTABLE`

### 1.5 Журнал (journal/db.py) — ✅ DONE

- [x] Удалены из SCHEMA:
  - `CREATE TABLE virtual_signals` + индексы
  - `CREATE TABLE pair_stats_log` + индексы
  - `CREATE TABLE payout_log` + индексы
- [x] Удалены методы (db.py сократился с ~720 до 236 строк):
  - `update_exp_wins`
  - `insert_virtual_signal`, `pending_virtual_signals`, `settle_virtual_signal`
  - `hourly_stats` (старый — будет переписан в этапе 2)
  - `log_payout`, `last_payout`, `payout_log_since`, `winning_trade_payouts`, etc.
  - `log_pair_stats`, `pair_stats_since`
- [x] `_migrate_columns` теперь делает `DROP TABLE IF EXISTS virtual_signals/pair_stats_log/payout_log` при запуске (миграция старых БД)
- [x] Бонус: удалён `_payout_logger_loop` из `feed/po_direct.py` (писал в удалённую таблицу)
- [x] Бонус: TG-команда `hourly` в bot.py теперь возвращает alert «удалена в рефакторинге»

### 1.6 Документация — ✅ DONE (в коммитах 030a88b/2dc79b4)

- [x] `README.md`: убраны упоминания вкладок Аналитика/По часам/Экспирация, описана новая структура
- [x] `CLAUDE.md`: добавлен warning блок про активный рефакторинг + ссылка на REFACTOR_PLAN.md
- [x] `REFACTOR_PLAN.md`: live document с прогрессом по этапам

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
33. `wr1_long_at_signal` (общая проходимость за 1000 свечей этой пары на момент сигнала)
34. `wr1_recent_at_signal` (проходимость за последние 200 свечей)

Эти данные собираются `_persist_market_snapshot()` в момент закрытия каждого CONSENSUS-сигнала и сохраняются в таблицу `signals`.

#### Источники данных
- Из cached candles (`sm._candles`) — последние N баров до signal_ts
- Расчёты используют существующие функции из `strategy/indicators.py` (rsi/qqe, htf_trend, atr, sma, bollinger, candle_aligned) — **без переписывания**
- Голоса передаются прямо из `Signal.votes` dict который уже формирует CONSENSUS
- Общая проходимость / проходимость последних свечей берутся из `_pair_scores[symbol]` (актуальные на тот момент)

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
    wr1_long_at_signal REAL,                 -- общая проходимость (1000 свечей)
    wr1_recent_at_signal REAL,               -- проходимость последних свечей (200)

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
- Проверять общую проходимость, проходимость последних, max_loss_streak, payout текущей пары
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

## ЭТАП 3 — ВТОРИЧНЫЕ УЛУЧШЕНИЯ — ✅ DONE

- [x] Day-of-week drill-down (toggle часы↔дни в drill-down раскрытии пары)
- [x] Активные пары на главном — карточка «Активный цикл» с current/original/direction/trades_on_pair/cycle_switches/carry/switched_pairs
- [x] Day-off видимость в Status — отдельная карточка с countdown
- [x] Фотография графика на каждой сделке — _notify_open_async шлёт PNG на КАЖДОЙ MG-ступени (этап 2)
- [x] Постоянная фильтрация (1000+200) на каждой сделке — `_verify_current_pair_still_passes` после LOSS
- [x] Мобильная адаптация — `@media (max-width:760px)` sticky first column в analytics-table

### Ключевая фича этапа 3 — apply-as-filter (multi-dim)

Кнопка «🎯 Построить фильтр» в Аналитике:
1. Берёт текущий срез (period + hour_from/to + dow checkboxes)
2. Из winning signals (exp_wins[expiry_bars-1]==1) считает quantile 10-90
   для ATR ratio / BB position / RSI MA / candle/ATR ratio
3. Собирает множества `hours_allowed` / `dow_allowed` (где были winners)
4. Минимумы payout / votes_total
5. Возвращает редактируемую форму — пользователь МОЖЕТ менять любое значение
6. Сохраняется per-strategy в `state_kv["signal_filter:<name>"]`
7. При торговле в `_check_signal` snapshot проверяется против фильтра —
   неподходящие сигналы отбрасываются (но в `signals` они всё равно пишутся
   для аналитики, чтобы было видно сколько фильтр режет)
8. Бейдж 🎯 в статусе показывает что фильтр активен

---

## ПОРЯДОК ВЫПОЛНЕНИЯ

| # | Что | Когда | Статус |
|---|---|---|---|
| 0 | Tag + Branch + (опц.) Snapshot | сделано | ✅ |
| 1 | Удаление аналитики | этап 1 | ✅ |
| 2.1 | Новая таблица signals + collector | этап 2 | ✅ |
| 2.2 | Новая Mini App структура (3 раздела) | этап 2 | ✅ |
| 2.3 | Аналитика — главная таблица | этап 2 | ✅ |
| 2.4 | Аналитика — фильтры + сортировка | этап 2 | ✅ |
| 2.5 | Аналитика — раскрытие 24h детально | этап 2 | ✅ |
| 2.6 | Аналитика — apply-as-filter (per-strategy multi-dim) | этап 3 | ✅ |
| 2.7 | Гибкий Martingale | этап 2 | ✅ |
| 2.8 | Расписание + выходные | этап 2 | ✅ |
| 2.9 | TG-уведомления полные | этап 2 | ✅ |
| 2.10 | Retention scheduler | этап 2 | ✅ |
| 3.1 | Day-of-week drill-down (toggle часы↔дни) | этап 3 | ✅ |
| 3.2 | Активные пары виджет на Главной | этап 3 | ✅ |
| 3.3 | Day-off видимость в Status | этап 3 | ✅ |
| 3.4 | Постоянная пере-фильтрация (§6) | этап 3 | ✅ |
| 3.5 | Мобильная адаптация таблиц (sticky 1-я колонка) | этап 3 | ✅ |
| — | Чистка legacy MG-ключей (limit_trades_per_pair_*) | этап 3 | ✅ |

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

---

## POST-3 — корректировки после первых дней работы

После деплоя этапов 1-3 по итогам реальной торговли внесены доработки:

### Терминология (commit 301ea94)
- `WR1` → **«общая проходимость»** (% первой плюсовой сделки за 1000 свечей)
- `WR1_recent` → **«проходимость последних свечей»** (% за 200 свечей)
- Лейблы изменены везде: настройки, аналитика, HUD-карточка чарта, ⓘ описания
- Логика без изменений — только понятные термины

### Расширение системы пауз: 4 уровня вместо 3

| Уровень | Триггер | Срок |
|---|---|---|
| **SKIP** | только общая проходимость провалена (одна) | без bans, просто не торгуется |
| **PAUSE** | только проходимость последних провалена | 60 мин (filter.pause_minutes) |
| **TEMP_PAUSE** ⭐ NEW | обе проходимости провалены ОДНОВРЕМЕННО | 6ч (filter.temp_pause_hours) |
| **BAN** | max_loss_streak > N | 6ч/12ч (filter.ban_hours) |

### Глобальный day_off удалён

Раньше: когда tracked={} → весь бот в 6-часовой паузе (`state.day_off_until`). Теперь: per-pair temp_pause полностью замещает. Если все пары провалены — main loop крутится впустую, рескан раз в 60с. TG-уведомления про day_off убраны.

Backwards-compat: старый `filter.day_off_hours` в state_kv override автоматически мигрируется на `filter.temp_pause_hours` при первом запуске после деплоя.

### Аналитика: «Best exp %» → «Best exp #»

Колонка теперь показывает **средний номер бара первой победы** (1.0–5.0):
- 1.0 = идеально (всегда выигрывает на 1-м баре)
- 5.0 = плохо (только на последнем)
- Не учитывает сигналы где все 5 экспираций — LOSS
- Цветовая подсветка: ≤1.5 зелёный, ≤2.5 жёлтый, ≤3.5 оранжевый, >3.5 красный

Логика старого `wr_best` (% сигналов где хоть какая экспирация выиграла) **остаётся** в API response, просто колонка в UI заменена на более информативную.

### Прочие доработки
- HTF-индикатор: возврат к буфер-относительной группировке (1:1 с JS-эталоном PoSignals)
- Hour_local / day_of_week: расчёт из реального wall-clock (PO-timestamps смещены)
- Mini App: ⓘ-кнопки с popover-описаниями для всех настроек
- Mini App: чарт-панель (TradingView Lightweight Charts) с BUY/SELL/✓/✗ маркерами, разворачивается кликом по «📊 Tracked пары»
- Mini App: pin через `tg.disableVerticalSwipes()` + `enableClosingConfirmation()`
- Rescan каждые 60с (раньше 300с) + ручной триггер «🔄 Обновить»
- Periodic report: единый `periodic_report.hour` без зависимости от schedule
- Fix двойного отчёта: при `periodic_report.enabled=true` старый `daily_report_loop`
  не должен слать второй отчёт. Решение — задача уходит в `await asyncio.Event().wait()`
  (idle forever), не возвращает `return`. Иначе supervisor (main.py, каждые 30с)
  считает задачу dead и спамит «🚨 Задача daily_report упала» в TG бесконечно.
- 3 live-метрики на Главной Mini App + в TG-отчёте/`/control` «Сегодня»:
  - **Макс. без торговли** (`journal.max_no_trade_gap`): max gap между сделками
    в окне `trading_day_window(cfg)`. При `schedule.enabled=true` — окно сужается
    до `[start_hour, end_hour]` (ночь игнорируется), иначе rolling 24h.
  - **Мин. payout за сутки** (`journal.min_payout_24h`): MIN payout трейдов
    за последние 24h.
  - **Макс. минусов подряд** (`journal.max_recovered_losses_24h`): MAX(mg_step)
    среди WIN-сделок. DRAW не сбрасывает цикл (refund → повтор того же шага),
    поэтому считается через mg_step, а не chronological LOSS-streak. Старая
    `daily_summary.max_loss_streak` всё ещё в БД-методе, но юзеру не показывается.
- Fix relogin loop в paused: `_safe_to_relogin` в main.py больше не блокирует
  relogin в paused/waiting_resume — только при `mg_step>0` или `pending_trade`.
  Раньше после schedule auto-pause сессия PO протухала, relogin откладывался
  навсегда из-за `not s.paused` в условии — бот зависал в петле «NotAuthorized
  → relogin deferred» до ручного рестарта (commit 83e9302).

### Удаление `consecutive_losses_switch`
По запросу юзера: триггер «N минусов подряд → switch» избыточен в типовой
конфигурации `pair_limits=[3,3]` где `consec=3` — оба триггера срабатывают
одновременно. Оставлены только два switch-триггера на не-последней паре:
- Исчерпан лимит `pair_limits[i]`
- Payout упал ниже `payout_floor`

Поле `RuntimeState.losses_streak_on_pair` сохранено как внутренний счётчик
(инкремент на LOSS, ресет на WIN/DRAW/switch) — может пригодиться для
аналитики, но ничего не триггерит.

### Расширенный candidate-set в in-cycle SEARCH
Когда бот в цикле (mg_step>0) ищет следующую пару после switch (по
исчерпанному лимиту или payout-drop), фильтр кандидатов **ослаблен**:
- IGNORE: `score.pause` (60-мин за recent_wr1), `score.temp_pause`
  (6ч за обе проходимости), `score.allowed=False` (любые мягкие фильтры)
- KEEP: `score.ban` (max_loss_streak — деструктивные паттерны),
  `payout < min_payout`, `switched_pairs` (anti-bounce)

Юзерская логика: «уже сделка в работе на паре, проходимость последних
свечей не должна влиять на поиск новых пар». В CYCLE мы УЖЕ потеряли
деньги, добиваем — форма пары вторична. recent_wr1 имеет смысл только
при ВХОДЕ в цикл (FREE-режим).

### Sticky `current_pair` в `_tracked`
Пара которая сейчас в активном цикле (`state.current_pair` + `mg_step>0`)
**принудительно остаётся** в `self._tracked` при каждом `_rescan_pairs`,
даже если её оценка ухудшилась за время цикла. Иначе цикл мог бы
прерваться из-за временного отвала пары из tracked-set, что нарушало
бы `_in_cycle_step` (опирается на cached candles + WS-subscription
через tracked).

Юзер: «Если она в работе, она не должна уходить с tracked пар пока
ещё в работе».

### Layout `intlist`-инпутов
`intlist` (например `pair_limits = "3,3"`) использует text-инпут.
Раньше тянулся на 100% ширины через базовое CSS-правило, забирая место
у лейбла → лейбл переносился слово-по-слово. Сейчас все scalar-инпуты
в `.setting-row` уравнены: width:120px, flex:0 0 auto, text-align:right,
tabular-nums. Лейбл нормально занимает левую часть строки.
