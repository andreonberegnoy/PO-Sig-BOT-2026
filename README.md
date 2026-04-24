# MY PO-SIG BOT

Торговый бот под [po-signals.com](https://po-signals.com/app/charts). Работает через уже залогиненный браузер Chrome (CDP), получает свечи и отправляет сделки через socket.io (без кликов по DOM для трейдов — но клики используются для переключения пар в мульти-чарт окнах). Сигналы считает Python-порт индикатора **CONSENSUS 4/5**, 1:1 с JS-версией на сайте.

## Архитектура

```
config.yaml              все параметры (фильтр, мартингейл, Telegram, индикатор)
main.py                  оркестратор: feed + strategy + trading + journal + telegram
consensus_indicator_export.js   JS-индикатор CONSENSUS 4/5 для визуала на сайте
├── feed/
│   ├── po_feed.py       CDP hook (WebSocket.prototype.send patch), два WS, парсинг
│   └── history.py       REST /api/po/candles/{SYMBOL}?period=60&limit=1060
├── strategy/
│   ├── indicators.py    RSI / QQE / EMA/SMA/WMA/RMA / Bollinger / ATR / HTF
│   ├── consensus.py     порт CONSENSUS 4/5 (generate_signals, analyze, evaluate_trade)
│   └── filter_1000.py   прогон по 1060 свечам → classify (allowed / banned / priority)
├── trading/
│   ├── ws_client.py     open_trade (синтетический trade_id, ждёт баланс-дельту)
│   ├── state_machine.py свободный скан + сигнально-затворенный мартингейл
│   └── window_manager.py автосет 15 мульти-чарт окон + пиннинг MG-пары
├── tools/
│   ├── click_recorder.py   разовая запись кликов для реверса DOM-селекторов
│   ├── windows_probe.py    разовый дамп состояния 15 окон
│   └── autoset_windows.py  standalone-запуск перераспределения окон
├── journal/
│   └── db.py            SQLite: trades, bans, state_kv, sessions
└── tg/
    ├── bot.py           Telegram: /status /pause /resume /stop /bans /test + отчёт
    └── chart.py         рендер свечного графика с сигналом для Telegram
```

## Ключевые решения

### 1. Отправка сделок через sandbox-обход WebSocket
Приложение po-signals.com выполняется в «сандбоксе» — блокирует `window.__*` доступы и работает через изолированный realm. Обычный `window.WebSocket = Patched` хук НЕ срабатывает, т.к. бандлер сохранил оригинальный конструктор в замыкании. Решение: патчим **`WebSocket.prototype.send`** — все экземпляры проходят через один прототип, поэтому первый же `.send()` сайта даёт нам ссылку на сокет. После этого отправляем `user.demo.open_trade` по тому же сокету.

Payload — ровно как у сайта при клике «Купить»: `{asset, amount, action, time}`, **без `login`** (добавление ломает ответ сервера).

### 2. Детект закрытия через баланс-дельту
Ответ `user.demo.open_trade.success` приходит как socket.io binary-event (MessagePack) — бинарный парсер неполный. Вместо парсинга: запоминаем `balance_demo` **ДО отправки фрейма** (после отправки сайт списывает сумму за миллисекунды), после `expiry + 5s` читаем снова. `delta > 0` = WIN, `delta < 0` = LOSS, иначе DRAW. Работает т.к. `one_trade_at_a_time: true`.

### 3. Свежесть данных через живые тики
Сайт стримит M1-свечи по WS только для пар, видимых на экране. В **15-окном мульти-чарт режиме** стримятся все 15. В state_machine тики mirror-ятся в `_candles[symbol]`, REST остаётся fallback'ом. Для пар в окнах задержка бар-клоуз → сигнал — **<1 сек**.

### 4. Автоматическое переключение пар в 15 окнах
`trading/window_manager.py` через координатные клики на кнопки prev/next в каждом окне:
- Каждые **90 сек** (только вне MG-цикла) перебирает окна и заменяет пары с низким payout или дубли на уникальные в диапазоне `[min_payout, 92]%`.
- Перед открытием сделки `ensure_pair_in_window(current_pair)` гарантирует что пара в одном из окон → live-тики на время цикла.

### 5. Мартингейл с сигнал-гейтом
После LOSS бот **не открывает следующую сделку сразу** — ждёт свежий CONSENSUS 4/5 сигнал на текущей паре. Это защищает от слепого «биться против рынка». Логика:
1. LOSS → `mg_step++`, пара закреплена.
2. Ждём новый сигнал на закрытии следующих баров (staleness-гейт 25 сек).
3. Сигнал пришёл → открываем по мартингейл-формуле (`base × 2.1^step`).
4. Если сигнал в противоположном направлении — следуем за ним (обновляем `state.direction`).
5. Если payout пары упал <85% → одна смена пары за цикл (`_pick_switch_pair`).
6. WIN → сброс. Stop-sum / max_steps → `waiting_resume`.

## Что работает

- [x] CDP-подключение к Chrome, два WS (user + ticker2)
- [x] Парсинг socket.io v4 + base64-JSON + MessagePack для входящих событий
- [x] `common.assets_list` → фильтр payout ≥ `min_payout`, тип currency
- [x] Тики → живые M1 свечи в буфер state_machine (live для 15 пар, REST для остальных)
- [x] REST `/api/po/candles/{SYMBOL}` — подгрузка 1060 свечей истории
- [x] Порт CONSENSUS 4/5 **1:1** с JS (RSI-QQE + HTF + ATR + Bollinger + свечной фильтр)
- [x] Фильтр «≤3 минусов подряд за 1000 свечей» + бан на 12ч
- [x] Отправка `open_trade` через patched WebSocket.prototype.send
- [x] Детект WIN/LOSS/DRAW через дельту демо-баланса
- [x] Сигнал-гейт на мартингейле (не догоняет вслепую)
- [x] Смена пары при payout<85% (макс 1 за MG-цикл)
- [x] Стоп-сумма → `waiting_resume`
- [x] День-офф 6ч если ни одна пара не проходит
- [x] 15-окный автосет каждые 90 сек по payout 85-92%
- [x] Пиннинг `current_pair` в окне на время MG-цикла
- [x] Bar-aligned REST-refresh (подстраховка для пар без live-тиков)
- [x] Staleness-гейт 25 сек — опоздавшие сигналы пропускаются
- [x] Telegram: `/status`, `/balance`, `/pause`, `/resume`, `/stop`, `/bans`, `/test`
- [x] Ежедневный отчёт в 7:00

## Запуск

### 1. Chrome с debugging-портом
```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 \
  --user-data-dir=/tmp/po-chrome
```
Или через `open -na "Google Chrome" --args --remote-debugging-port=9222 --user-data-dir="$HOME/chrome-po-debug"`.

Залогиниться в po-signals.com, выбрать аккаунт, **активировать 15-окный мульти-чарт режим** (кнопка «Экран» в левой колонке сайта → 15 окон).

### 2. Зависимости
```bash
pip3 install -r requirements.txt
```

### 3. Бот
```bash
python3 main.py --mode paper   # демо-счёт
python3 main.py --mode real    # реальный счёт
```

Фоном:
```bash
nohup python3 main.py --mode paper > bot.log 2>&1 < /dev/null &
disown
```

### Ручные инструменты
```bash
python3 tools/autoset_windows.py --dry-run   # показать план переключения
python3 tools/autoset_windows.py             # выполнить
python3 tools/windows_probe.py               # дамп состояния 15 окон
```

## Конфигурация (`config.yaml`)

| Секция | Параметр | Дефолт | Смысл |
|---|---|---|---|
| `filter` | `min_payout` | 92 | минимум выплата для входа (%) |
| | `payout_floor` | 85 | ниже этого — смена пары в цикле |
| | `max_losses_in_row` | 3 | >N минусов подряд → бан пары |
| | `history_candles` | 1060 | буфер свечей (HTF-бакеты совпадают с сайтом) |
| | `ban_hours` | 12 | бан пары на сколько часов |
| | `day_off_hours` | 6 | пауза если ни одна пара не проходит |
| `trading` | `base_amount` | 1 | базовая ставка ($) |
| | `expiry_seconds` | 120 | экспирация сделки |
| | `max_pair_switch_per_cycle` | 1 | смен пары до WIN |
| `martingale` | `coefficient` | 2.1 | множитель ставки после LOSS |
| | `max_steps` | 5 | максимум догонов |
| | `stop_sum` | 50 | порог потерь → `/resume` |
| `indicator` | `minConsensus` | 4 | минимум голосов систем (из 5) |
| | `expiryBars` | 2 | экспирация в барах для бэктеста |
| | `rsiPeriod` / `qqeFactor` / `htfMultiplier` / ... | | CONSENSUS 4/5 |
| `misc` | `poll_interval_sec` | 1 | частота скан-цикла |

## Telegram

Команды:
- `/status` — текущая пара, MG-шаг, потери, баланс
- `/balance` — баланс выбранного режима
- `/pause` / `/resume` — пауза / возобновление
- `/stop` — остановить бота
- `/bans` — активные баны пар
- `/chart <SYMBOL>` — свечной график пары
- `/test <SYMBOL> <call|put> [amount]` — тестовая сделка, минуя фильтры
- `/help`

Автоматические уведомления:
- сигнал пойман, вход в сделку + график
- WIN / LOSS / DRAW с обновлением потерь
- новый MG-шаг с суммой и причиной
- смена пары по правилу payout<85%
- стоп-сумма достигнута (ждёт `/resume`)
- день-офф (пауза на 6ч)
- ежедневный отчёт в 7:00: prof/loss, WR, смен пар, макс. серия минусов

## Хранилище

- `journal.db` — SQLite: сделки, баны, сессии, key/value для restore
- `bot.log` — лог (INFO+); содержит `WS SEND: ...` исходящих фреймов для отладки

## Известные ограничения

1. **HTF-дрейф**: `htf_trend(group = i // mult)` зависит от индекса свечи. При буфере 1060 совпадает с сайтом; при других N возможно расхождение ±1-2 сделки / ±1 по max-streak. Trade-off «1:1 порта».
2. **История только для валют**: REST 404/400 на акции (`#AAPL_otc`) и крипту (`ETHUSD_otc`). Только currency OTC.
3. **CDP-сессия**: если Chrome закрыт или нет `--remote-debugging-port` — бот теряет связь. Перезапуск Chrome + бота.
4. **Sandbox-обход через prototype.send**: если сайт обновит бандл и начнёт сохранять `WebSocket.prototype.send` в замыкание тоже — хук перестанет работать. Тогда fallback — клики по DOM-кнопкам «Купить»/«Продать».
5. **15-окный режим обязателен для низкой задержки**: без него live-тики только на 1 пару (текущий график), остальные через REST с 15-сек фоллбеком.
6. **Один Telegram-бот**: polling одного токена нельзя параллелить — иначе `TelegramConflictError`.

## Testing checklist (перед real-mode)

- [ ] Paper: бот поймал сигнал и открыл сделку в демо
- [ ] WIN — мартингейл сбрасывается, возврат в свободный скан
- [ ] LOSS — `mg_step++`, бот ждёт новый сигнал на той же паре
- [ ] Новый сигнал на MG-паре → ставка × 2.1
- [ ] Сигнал в противоположном направлении во время MG → смена direction
- [ ] Падение payout<85% на MG-паре — смена пары (одна за цикл)
- [ ] Стоп-сумма — пауза + уведомление + `/resume` восстанавливает
- [ ] Автосет окон раз в 90с заменил дубли / низкие payout
- [ ] `current_pair` закреплена в окне на время цикла (ensure_pair_in_window)
- [ ] `/status`, `/balance`, `/bans`, `/test` отвечают корректно
- [ ] Ежедневный отчёт в 7:00 приходит
- [ ] После `kill -9` бот при перезапуске начинает с базовой суммы
