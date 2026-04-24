# Railway Deploy

Бот работает в headless-Chromium внутри одного Railway-контейнера. Сессия po-signals.com сбрасывается каждые ~2 часа — авторелогин в [feed/auth.py](feed/auth.py) отрабатывает при каждом `_hard_reconnect`.

## Файлы деплоя

- [Dockerfile](Dockerfile) — образ на базе `playwright/python`, Chromium уже есть
- [scripts/start.sh](scripts/start.sh) — поднимает Chromium с CDP + запускает `main.py`
- [railway.toml](railway.toml) — Railway config (Dockerfile builder, restart-on-failure)
- [.dockerignore](.dockerignore) — не копируем git/логи/tests
- [.env.example](.env.example) — шаблон env-переменных

## Шаги

### 1. Создать проект на Railway
Railway Dashboard → **New Project** → **Deploy from GitHub repo** → выбрать этот репозиторий.

### 2. Добавить Volume
Dashboard → ваш сервис → **Volumes** → **+ New Volume**
- **Mount path:** `/chrome-data`
- **Size:** 1 GB

Этот volume сохраняет cookies и настройку 15-окного режима между рестартами.

### 3. Env-переменные
Dashboard → **Variables** → добавить:

| Переменная | Значение |
|---|---|
| `MODE` | `paper` или `real` |
| `PO_EMAIL` | email от po-signals.com |
| `PO_PASSWORD` | пароль |
| `TELEGRAM_TOKEN` | токен Telegram-бота |
| `TELEGRAM_CHAT_ID` | chat_id |
| `CDP_URL` | `http://localhost:9222` (дефолт подходит) |

### 4. Plan
**Settings → Resources** — включить Hobby plan ($5/мес), 8 GB RAM. Free tier (512 MB) **не хватит** под Chromium.

### 5. Deploy
`git push` → Railway соберёт образ и запустит.

### 6. Первичная активация 15-окного режима
На свежем volume Chromium не знает что надо включать 15-окный layout. Один раз делаешь так:

1. Railway Dashboard → сервис → **Settings → Networking → Private Networking** — включить публичный `9222` TCP порт (или использовать Railway CLI port-forward).
2. У себя на маке: `railway login` → `railway link <project>` → `railway run bash` или port-forward:
   ```bash
   railway run -- curl http://localhost:9222/json/version
   ```
3. Альтернатива — через Chrome на твоём маке:
   - Открой `chrome://inspect/#devices`
   - Configure → добавить `<railway-domain>:9222`
   - В списке появится вкладка po-signals.com — нажать **inspect**
   - В открывшейся DevTools видишь страницу → в самом Chrome-окне (которое на Railway через CDP) через console запустить:
     ```js
     // кликнуть на кнопку "Экран" → "15 окон" — точный селектор надо выяснить через click_recorder
     ```

Проще всего для первого раза: запустить контейнер локально через Docker, зайти `http://localhost:9222` в обычном Chrome, переключить layout руками, затем Docker-volume сгрузить на Railway.

### 7. Логи
Railway Dashboard → **Deployments** → **Logs**. Ищи `feed ready. assets=...` — бот взлетел. Ошибки логина — `auto-login timeout`.

## Риски и мониторинг

- **Sandbox-хак может сломаться**: если po-signals обновит бандл и начнёт сохранять `WebSocket.prototype.send` в замыкание — бот замолчит. Заметно в логе: `user WS not open total=0`. Фикс — смена хука.
- **Session reset**: раз в 2 часа. Авторелогин отрабатывает. Если падает — проверить `PO_EMAIL/PASSWORD`.
- **15-окный layout сбивается**: после обновления сайта layout может сбрасываться. Пересоздать однократно.
- **RAM**: если Chromium начнёт кушать >1 GB — добавить `--memory-pressure-off` в `start.sh` или рестартнуть контейнер по крону.

## Локальный тест Docker-сборки

```bash
docker build -t po-sig-bot .
docker run --rm -it \
  -e PO_EMAIL="..." -e PO_PASSWORD="..." \
  -e TELEGRAM_TOKEN="..." -e TELEGRAM_CHAT_ID="..." \
  -v "$(pwd)/chrome-data:/chrome-data" \
  -p 9222:9222 \
  po-sig-bot
```

Во втором терминале: `curl http://localhost:9222/json` чтобы убедиться что CDP жив.
