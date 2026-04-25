# Railway Deploy

Бот подключается к Pocket Option напрямую по WebSocket (без постоянного Chromium). Headless Chromium запускается только эпизодически для авто-обновления сессии (~раз в 12 часов или по событию `NotAuthorized`) через storage_state — куки реального браузера.

## Файлы деплоя

- [Dockerfile](Dockerfile) — образ на базе `mcr.microsoft.com/playwright/python:v1.48.0-noble` (Chromium предустановлен для авто-релогина).
- [railway.toml](railway.toml) — Railway config (Dockerfile builder, `restart-on-failure`, до 10 попыток).
- [requirements.txt](requirements.txt) — Python-зависимости. **Playwright запинен на 1.48.0** под версию образа — не обновляй без апгрейда базового образа.
- [tools/make_storage_state.py](tools/make_storage_state.py) — локальный скрипт для генерации `state.json` (cookies + localStorage).

## Архитектура

```
Pocket Option ←─ wss:// ──→ feed/po_direct.py ──→ trading/state_machine.py
                                ↑                          │
                                │ ssid (~12h)              │ /resume, /pause, /stop
                                │                          ↓
                       feed/auto_relogin.py          tg/bot.py (Telegram)
                       (Playwright headless,
                        куки из state.json)
```

## Шаги первого деплоя

### 1. Создать проект на Railway
Railway Dashboard → **New Project** → **Deploy from GitHub repo** → выбрать этот репозиторий.

### 2. Добавить Volume и привязать к сервису
Без тома **state.db и candles.db не переживают рестарты** — мартингейл сбрасывается после каждого деплоя.

```bash
brew install railway
railway login
railway link        # выбрать проект и сервис
railway volume add  # создать том, mount path: /app/data, size: 1 GB
railway volume attach -v <volume-name>   # привязать к сервису
```

Проверка: `railway volume list` — должно быть `Attached to: <service>`.

### 3. Сгенерировать storage_state локально
**Однократно** (потом — раз в ~30 дней при истечении):

```bash
cd "/path/to/repo"
pip3 install playwright && playwright install chromium
python3 tools/make_storage_state.py
```

Откроется окно Chromium → залогинься в Pocket Option (email + пароль, 2FA если есть) → дождись трейдинговой страницы → закрой модалки если появятся → нажми Enter в терминале. Файл `state.json` сохранится.

### 4. Залить storage_state в Railway

```bash
railway variables --set "PO_STORAGE_STATE_B64=$(base64 -i state.json | tr -d '\n')"
```

Railway автоматически пересоберёт сервис.

### 5. Остальные env-переменные

```bash
railway variables --set "PO_SSID=<initial ssid>"
railway variables --set "PO_UID=<your uid>"
railway variables --set "PO_IS_DEMO=0"     # 1 = demo, 0 = real
railway variables --set "TELEGRAM_TOKEN=<bot token>"
railway variables --set "TELEGRAM_CHAT_ID=<your chat id>"
```

`PO_SSID/PO_UID` — берутся из DevTools (вкладка Network → WS-фрейм `42["auth", {...}]` после успешного логина). Нужны только для первого старта; дальше auto-relogin сам поддерживает свежий ssid через storage_state.

| Переменная | Обязательная | Описание |
|---|---|---|
| `PO_SSID` | ✓ | Стартовый session id (далее обновляется автоматически) |
| `PO_UID` | ✓ | Числовой UID аккаунта |
| `PO_IS_DEMO` | ✓ | `1` = demo, `0` = real |
| `PO_STORAGE_STATE_B64` | ✓ | base64 от `state.json` для авто-релогина |
| `TELEGRAM_TOKEN` | ✓ | Токен Telegram-бота |
| `TELEGRAM_CHAT_ID` | ✓ | Твой chat_id |
| `PO_RELOGIN_HOURS` | — | Период плановой смены ssid (по умолчанию 12) |
| `PO_WS_URL` | — | Override WS endpoint (по умолчанию `api-eu.po.market`) |

### 6. Plan
**Settings → Resources** — Hobby plan ($5/мес) с 1+ GB RAM. На Free tier Playwright не запустится — Chromium хочет ≥800 MB при старте.

### 7. Логи
Railway Dashboard → **Deployments → Logs** или `railway logs`. Признаки нормального старта:

```
auto-relogin enabled via storage_state (cookie-based)
auto-relogin (state): captured ssid (uid=..., demo=..., ws=...)
feed ready (assets=183, balance_demo=..., balance_real=...)
restored state: RuntimeState(...)
🚀 starting — mode=real
```

## Когда придёт уведомление в Telegram

Куки в `state.json` живут ~30 дней. Когда протухнут — `auto-relogin (state): no auth frame within 60s — session likely expired` повторится дважды, после чего бот **поставит торговлю на паузу** и пришлёт в TG пошаговую инструкцию:

> 🔴 Pocket Option session протухла…
> ШАГ 1. На Mac выполни: `python3 tools/make_storage_state.py`
> ШАГ 2. Залогинься в открывшемся Chromium → нажми Enter
> ШАГ 3. `railway variables --set "PO_STORAGE_STATE_B64=$(base64 -i state.json | tr -d '\n')"`
> ШАГ 4. Railway сам пересоберёт. Бот возобновит работу.

После шага 3 деплой автоматически перезапускается, мартингейл/баланс сессии **сохраняются** благодаря volume.

## Что переживает рестарт благодаря тому

Volume mount path: **`/data`** (был `/app/data` — Railway не монтировал когда директория уже создана в Dockerfile через `RUN mkdir`). Mount-point теперь чистый, отдельный ext4-раздел 5GB.

- `state.db` — `runtime_state` (текущая пара цикла, MG step, направление, session_loss, switched_pairs, paused/waiting_resume).
- `candles.db` — буферы свечей по всем парам, разделённые по `is_demo` (демо и реал не смешиваются).
- Журнал сделок (для daily-report и stats lookback).
- **Настройки из Mini App** (`PUT /api/settings`) — записываются в `state.db` под ключом `settings_overrides`. На старте `main.py` накладывает их поверх `config.yaml` (который сбрасывается при каждом ребилде образа). В логе появляется `applied N persisted settings overrides`.
- **Параметры активной стратегии** (`strategy_params:NAME` в `state.db`) и сама **активная стратегия** (`active_strategy`).
- **Загруженные user-стратегии** (Python-файлы) — попадают в `/data/user_strategies/*.py`, не в образ контейнера. Папка переключается автоматически: если есть `/data` → volume, иначе → `strategy/user/` (для локальной разработки).
- **Активные баны пар** в `state.db` (после серии минусов) — переживают деплой.
- **Аналитические снапшоты** (`payout_log`, `pair_stats_log` — см. раздел ниже).

## Защита от смены open/close в исторических барах

PO отдаёт history-бары как `[time, open, close, high, low]` (списками, не объектами). Раньше парсер брал `item[-1]` как close — то есть **low** становился close, что давало:
- инвертированные цвета свечей (бычья → красная и наоборот),
- неправильный размер тела свечи.

Live-тики (`_apply_tick`) собирали OHLC корректно — поэтому **только исторические бары** (батч-догрузка через `loadHistoryPeriod`) рисовались криво. Сейчас auto-detect проверяет обе интерпретации (`[t,o,c,h,l]` и `[t,o,h,l,c]`) и выбирает ту, где `high >= max(o,c,l)` и `low <= min(o,c,h)`.

## Защита от фантомных сигналов

При коротких разрывах WS или OTC-перерывах в буфере свечей могут возникнуть пропуски. Бот защищён двумя слоями:

1. **Buffer keeper** (фоновая задача): каждые 60 сек проверяет плотность последних 120 баров для каждой подписанной пары. Если <95% — догружает страницами по 200 баров через `loadHistoryPeriod`, пока не закроет дыру.
2. **Density guard в state_machine**: перед каждой проверкой сигнала смотрит плотность последних 100 баров. Если <95% — пропускает этот тик с логом `skip <pair> — buffer density N% over 100 bars (gap detected)`. Никаких входов на дырявых данных.

## Устойчивость WebSocket-соединения

PO периодически разрывает WS (idle, региональные миграции, сетевые блипы). Бот сам восстанавливается:

1. **`_recv_loop` ловит `ConnectionClosed`** и спавнит `_auto_reconnect_loop` (один раз — повторные спавны игнорируются).
2. **`_auto_reconnect_loop`** делает до 10 попыток с экспоненциальным backoff (2/4/8/.../60 сек), каждая повторно подписывает все пары что были до разрыва.
3. **Если 10 попыток не помогли** — тригернется `_do_relogin(reason="reconnect_exhausted")` через storage_state для свежей сессии.
4. **`subscribe()` проверяет состояние WS up-front**: если соединение мёртвое — один раз логирует `subscribe X: WS not open — triggering reconnect` и тригернёт reconnect, вместо каскада из 30+ ошибок `1005`.
5. **Фоновые задачи (buffer keeper, scheduled relogin) живут поверх реконнектов** — флаг `_running` сбрасывается только при явном `close()`, не при разрыве WS.

## Защита от двойного relogin

Auto-relogin через storage_state запускает Playwright Chromium. Параллельный запуск двух экземпляров приводил к гонке: один захватывал ssid с трейдингового WS, другой — с аналитического (events-po.com), бот получал битый токен.

Защита:
- **Module-level `asyncio.Lock`** в [feed/auto_relogin.py](feed/auto_relogin.py): второй вызов `fetch_fresh_ssid_via_state` сразу возвращает `None` с warning, не запускает второй Chromium.
- **Фильтр захвата ssid**: ловится только `42["auth"]` фрейм с WS-ом, чей URL содержит `api-*.po.market` (трейдинг). Аналитика/события игнорируются с debug-логом.

## Скорость входа в сделку

При сигнале `state_machine` сначала **отправляет openOrder в PO**, и только потом запускает рендер графика и отправку в Telegram **в фоне** (`asyncio.create_task`). Так matplotlib рендер (~3-5 сек) и Telegram-загрузка PNG (~5-30 сек) не блокируют сделку. Время от сигнала до фактического `tc.open_trade()` — миллисекунды. График приходит в TG уже после открытия — это нормально, он только для сверки.

## HTTP 401/403 fast-path

Если PO отвергает WS-handshake с `HTTP 401/403`, это значит ssid протух. Стандартный auto-reconnect с тем же ssid бесполезен — все 10 попыток упали бы с тем же кодом (~6 минут простоя). Бот детектит это тремя способами одновременно (`e.response.status_code` / `e.status_code` / поиск `"HTTP 403"` в `str(e)`) и **сразу запускает relogin**, минуя retry-цикл. Восстановление за ~5 секунд вместо 6 минут.

## Свечной график в Telegram

Чарт что приходит в TG при каждом сигнале — рендерится с особенностями для прямой сверки с PocketOption / PoSignals:

- **Индекс-based ось X**: каждая свеча занимает свой слот, дыры (минуты которые не пришли с PO) визуально отсутствуют — выглядит гладко как у референсных сайтов. Подписи времени `HH:MM` ставятся на 8 равномерных позиций.
- **Таймзона** — `Europe/Kyiv` (берётся из `cfg.daily_report_timezone`), не UTC. Совпадает с тем что показывает PocketOption и PoSignals в браузере.
- **HUD-плашка справа** в стиле PoSignals: общая %WR, 1-я сделка, макс минусов до WIN, последние 25 результатов значками `✓`/`✗`. Цифры считаются на окне `statsLookbackBars=1000`.
- **Маркеры сигналов** `▲`/`▼` рисуются в момент рендера. Если бар менялся ретроактивно (HTF EMA пересчиталась когда M5-бар закрылся) — старый маркер может пропасть. Это «репэйнт», свойство индикатора, не баг бота. Сама сделка отправлена в момент сигнала и не отменяется.

HTF группирует бары **по абсолютному настенному времени** (11:45-11:50, 11:50-11:55, ...), не по индексу буфера. Раньше группа плыла при сдвиге буфера → 4/5 сигнал мог стать 3/5 на следующем рендере. Сейчас граница HTF-бара стабильна.

## Аналитика по парам (вкладка «Аналитика» в Mini App)

Бот ведёт долгосрочную статистику для выбора лучших пар — **без дополнительной нагрузки на PocketOption**, всё считается локально из уже-имеющихся данных.

### Что логируется

**`payout_log`** (таблица в `state.db`): каждые 5 минут фоновая задача `_payout_logger_loop` снимает текущий `payout` каждого ассета из `feed.assets` (которое наполняется существующим `updateAssets` WS-фреймом). Запись делается **только если значение изменилось** — БД не разбухает.

**`pair_stats_log`** (таблица в `state.db`): каждый час `_pair_stats_logger_loop` в state_machine прогоняет активную стратегию (`consensus.analyze`) на буфере свечей каждой подписанной пары и сохраняет: `wr`, `wr1` (1-я сделка WR), `wins`, `losses`, `max_streak` (до WIN), `max_streak_overall`, `signals_total`, `bars_in_window`. Чисто локальное вычисление — нулевая нагрузка на PO.

### Что показывает API/UI

`GET /api/pair_stats?range=24h|7d|30d|60d|all` агрегирует по выбранному периоду:
- по `payout_log`: avg/min/max payout, % времени ≥ `min_payout` (92%), % времени ≥ `payout_floor` (85%)
- по `pair_stats_log`: средние и последние WR, max streak overall и last, average signals
- текущий `current_payout` из in-memory feed

В Mini App — вкладка **«Аналитика»** с сортируемой таблицей. Цвета: зелёный для WR ≥60% и streak ≤2, жёлтый средний, красный для WR <45% или streak ≥4. Дефолтная сортировка по гибридному скору `0.5×WR1 + 0.3×%выплат - 5×streak`.

### Накопление

- За **сутки** в `payout_log` появятся сотни строк на каждую активную пару.
- За **месяц** в `pair_stats_log` ~720 часовых снапшотов на пару.
- За **2 месяца** усреднения и выявления стабильных паттернов — больше чем достаточно.

При желании очистить — `DELETE FROM payout_log; DELETE FROM pair_stats_log;` через `railway ssh "sqlite3 /data/state.db ..."`.

## Локальный тест без деплоя

```bash
pip3 install -r requirements.txt
playwright install chromium

export PO_SSID=...
export PO_UID=...
export PO_IS_DEMO=1
export TELEGRAM_TOKEN=...
export TELEGRAM_CHAT_ID=...
export PO_STORAGE_STATE_B64="$(base64 -i state.json | tr -d '\n')"

python3 main.py
```

## Риски

- **Cloudflare блокирует логин** через email/password из дата-центра (Railway, Fly). Поэтому используется storage_state, минующий Cloudflare как «вернувшийся посетитель». Не ставь `PO_EMAIL`/`PO_PASSWORD` без необходимости — они дадут ложное срабатывание попытки логина перед storage_state.
- **Playwright vs Docker base mismatch**: версия `playwright` в `requirements.txt` должна совпадать с тегом образа в Dockerfile, иначе Chromium не найдётся. При апгрейде — обновляй обе строки одновременно.
- **PO меняет региональный WS endpoint**: бот ловит новый `ws_url` при каждом auto-relogin и переподключается. Если бот зацикливается на одном регионе с ошибками — задай `PO_WS_URL=` принудительно.
- **PO_STORAGE_STATE_B64 ≥ 10 KB**: Railway UI обрезает длинные значения при вставке вручную. Используй `railway variables --set` через CLI или Raw editor в дашборде.
