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

- `state.db` — `runtime_state` (текущая пара цикла, MG step, направление, session_loss, switched_pairs, paused/waiting_resume).
- `candles.db` — буферы свечей по всем парам, разделённые по `is_demo` (демо и реал не смешиваются).
- Журнал сделок (для daily-report и stats lookback).

## Защита от фантомных сигналов

При коротких разрывах WS или OTC-перерывах в буфере свечей могут возникнуть пропуски. Бот защищён двумя слоями:

1. **Buffer keeper** (фоновая задача): каждые 60 сек проверяет плотность последних 120 баров для каждой подписанной пары. Если <95% — догружает страницами по 200 баров через `loadHistoryPeriod`, пока не закроет дыру.
2. **Density guard в state_machine**: перед каждой проверкой сигнала смотрит плотность последних 100 баров. Если <95% — пропускает этот тик с логом `skip <pair> — buffer density N% over 100 bars (gap detected)`. Никаких входов на дырявых данных.

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
