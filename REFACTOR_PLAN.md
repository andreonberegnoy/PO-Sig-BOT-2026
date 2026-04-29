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
4. **Аналитика** считает все signals (entered + не-entered)
5. **Market snapshots НЕ собираем** — ATR/EMA/RSI на момент сигнала не сохраняем (отказались)
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

### 1.1 Mini App (HTML + JS)

- [ ] `miniapp/index.html`: удалить вкладки `tab-analytics`, `tab-hourly`, `tab-expiry` + соответствующие `<button class="tab">` в `<header>`
- [ ] `miniapp/app.js`: удалить функции и обработчики:
  - `loadAnalytics()`, `loadHourly()`, `loadExpiry()`
  - `_lastHourlyData`, `_lastExpiryData`, `hourlyState`
  - Все `.range-btn`, `.hour-range-btn`, `.exp-scope-btn`, `.exp-source-btn`, `.exp-window-btn` обработчики
  - `btn-hourly-export`, `btn-hourly-apply`, `btn-hourly-clear`, `btn-hourly-backfill`, `btn-hourly-reset`
  - `btn-expiry-load`, `btn-expiry-export`
  - `refreshHourlyFilterStatus()` если есть
- [ ] `miniapp/style.css`: удалить стили `.analytics`, `.range-selector`, `.exp-scope-btn` etc.

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

**Второстепенные показатели** (если будет market snapshots — пока НЕ собираем, отказались):
- ATR на момент входа (плюсовых vs минусовых)
- ATR(14) старший ТФ 5M
- Размер последней свечи / ATR
- Направление 5min EMA50
- EMA20 1m
- RSI на момент входа
- QQE Factor

> Эти колонки требуют market snapshots при сигнале. **Пока решили НЕ собирать** — таблица будет тоньше.

### 2.3 Новая таблица signals (заменяет virtual_signals)

```sql
CREATE TABLE signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,           -- call | put
    signal_ts       INTEGER NOT NULL,        -- entry minute (= bar close + tf)
    entry_close     REAL NOT NULL,
    entered         BOOLEAN NOT NULL DEFAULT 0,    -- бот реально вошёл?
    trade_id        TEXT,                    -- если entered, FK к trades.trade_id
    exp_wins        TEXT,                    -- JSON [w1..w5]
    settled_at      INTEGER,
    UNIQUE(symbol, signal_ts)
);
CREATE INDEX idx_signals_symts ON signals(symbol, signal_ts);
CREATE INDEX idx_signals_pending ON signals(settled_at, signal_ts);
CREATE INDEX idx_signals_entered ON signals(entered);
```

Чистая семантика, никакого «virtual».

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
