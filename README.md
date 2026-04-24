# MY PO-SIG BOT

Торговый бот под [po-signals.com](https://po-signals.com/app/charts) с индикатором **CONSENSUS 4/5**. Работает как на локальном Chrome через CDP, так и в headless-Chromium на Railway с persistent volume. Использует санбокс-обход через патчинг `WebSocket.prototype.send`, детектит закрытие сделок по дельте баланса, мартингейл с сигнал-гейтом, автоматическое управление 15-окным мульти-чарт layout.

## Архитектура

```
config.yaml              все параметры
main.py                  оркестратор: feed + strategy + trading + journal + telegram
Dockerfile               образ Playwright-python + Chromium
scripts/start.sh         запуск Chromium headless + бот (для Railway)
railway.toml             Railway deploy config
consensus_indicator_export.js   JS-версия CONSENSUS 4/5 для визуала на сайте
├── feed/
│   ├── po_feed.py       CDP hook (WebSocket.prototype.send patch), два WS, парсинг
│   ├── auth.py          авто-логин через клик «Войти» + заполнение модалки
│   └── history.py       REST /api/po/candles/{SYMBOL}?period=60&limit=1060
├── strategy/
│   ├── indicators.py    RSI / QQE / EMA/SMA/WMA/RMA / Bollinger / ATR / HTF
│   ├── consensus.py     порт CONSENSUS 4/5 (generate_signals, analyze, evaluate_trade)
│   └── filter_1000.py   прогон по 1060 свечам → classify (allowed / banned)
├── trading/
│   ├── ws_client.py     send open_trade + синтетический trade_id
│   ├── state_machine.py свободный скан + сигнал-гейтнутый мартингейл
│   └── window_manager.py автосет 15 окон + пиннинг MG-пары
├── tools/
│   ├── click_recorder.py   разовая запись кликов для реверса DOM-селекторов
│   ├── windows_probe.py    дамп состояния 15 окон
│   ├── autoset_windows.py  standalone-запуск перераспределения окон
│   └── activate_layout.py  разовая активация 15-окного layout через CDP
├── journal/
│   └── db.py            SQLite: trades, bans, state_kv, sessions
└── tg/
    ├── bot.py           Telegram: /status /pause /resume /stop /test /chart /bans
    └── chart.py         рендер свечного графика для Telegram
```

## Полная стратегия работы

### 1. Сигналы — CONSENSUS 4/5

На каждом закрытом M1-баре считается 5 независимых систем ([strategy/consensus.py](strategy/consensus.py)):

| № | Система | Условие |
|---|---------|---------|
| 1 | **RSI-QQE** | RSI(14, smooth=5) пересекает trailing-линию с фактором 4.238. **Обязательно**, иначе нет сигнала. |
| 2 | **HTF trend** | Close старшего ТФ (M5) vs EMA(20). Направление должно совпадать с сигналом. |
| 3 | **Volatility (ATR)** | `ATR(14) / ATR_avg(100) ∈ [0.7, 2.0]`. Не мёртвый рынок, не хаос. |
| 4 | **Bollinger zone** | Цена в нижних 30% канала BB(20, 2σ) для BUY или верхних 30% для SELL. |
| 5 | **Candle** | Тело не больше 2× ATR + направление свечи согласовано с сигналом. |

**Вход разрешён** если голосов **≥ 4 из 5** (в выходные — **5 из 5**). Плюс cooldown 3 бара между сигналами одного типа. Python-порт **1:1 с JS-индикатором** (см. `consensus_indicator_export.js`).

### 2. Фильтр пар (раз в 5 мин)

[strategy/filter_1000.py](strategy/filter_1000.py):
1. Из всех активных пар сайта отбираются **currency OTC с payout ≥ 92%**
2. На каждой пары прогоняется CONSENSUS 4/5 по **последним 1060 свечам**
3. Считается **max_loss_streak_before_win** (макс. минусов подряд до плюса) и `max_loss_streak_overall`
4. Классификация:
   - `max_loss_streak_overall > 3` → **БАН** на 12 часов (пара в чёрный список)
   - `max_loss_streak_before_win ≤ 1` → **ПРИОРИТЕТ 1** (идеал: плюс с 0-1 мартом)
   - `max_loss_streak_before_win == 2` → **ПРИОРИТЕТ 2** (ок: один догон)
   - `max_loss_streak_before_win == 3` → **ПРИОРИТЕТ 3** (макс. допустимо)
5. `_pick_switch_pair` при смене внутри цикла **всегда берёт пару с наименьшим priority** (из незабаненных, с payout ≥85%, не в исключениях)
6. Все allowed-пары → в `_tracked` список (обычно 15-25 пар), но в сделку бот идёт по приоритету.

### 3. Свечной поток

- **15 окон мульти-чарт** стримят live-тики через CDP → буфер обновляется <1 сек после закрытия бара
- Для пар вне 15 окон → **bar-aligned REST refresh** на границе минуты
- Запасной путь: обычный REST refresh каждые 15 сек

### 4. Скан-цикл (каждую секунду)

[state_machine.py:_free_scan_step](trading/state_machine.py):
1. Для каждой `tracked` пары — проверяем был ли новый закрытый бар
2. **Staleness-гейт**: если бар закрылся >25 сек назад → **пропустить** (вход уже не актуален)
3. Прогнать CONSENSUS 4/5 на последних свечах
4. Первая пара с сигналом → открыть сделку (break из цикла)

### 5. Открытие сделки

[state_machine.py:_open_and_track](trading/state_machine.py):
1. `ensure_pair_in_window(sym)` — гарантирует что пара в одном из 15 окон для live-тиков
2. Запоминаем `pre_balance = feed.balance_demo` **ДО** отправки фрейма
3. Отправляем `42["user.demo.open_trade", {asset, amount, action, time: 120}]`
4. Сайт списывает сумму с демо-баланса в течение миллисекунд
5. В Telegram: 📡 сигнал + график с индикатором + «Захожу $X»

### 6. Детект закрытия через дельту баланса

Биннарный ответ `open_trade.success` (MessagePack) не парсится — вместо него:
1. Ждём `expiry (120s) + 5s буфера`
2. Читаем текущий `balance_demo`, если не обновился — поллим ещё 10 сек
3. `delta = post_balance - pre_balance`
4. Классификация:
   - `delta > 0` → **WIN**, `profit = delta + amount`
   - `delta < 0` → **LOSS**
   - `delta ≈ 0` → **DRAW** (возврат средств)

Работает потому что `one_trade_at_a_time: true` — в один момент только одна открытая сделка.

### 7. Мартингейл с сигнал-гейтом

**Ключевое отличие от классического мартингейла:** не бьём вслепую, ждём новый консенсусный сигнал на той же паре.

```
LOSS → mg_step++, current_pair заморожена
  ↓
Ждём новый CONSENSUS 4/5 сигнал на current_pair (staleness 25 сек)
  ↓
Сигнал пришёл → открываем сделку с base × 2.1^step
  │
  ├─ если сигнал в ТОЙ ЖЕ direction → классический MG
  └─ если в противоположной → обновляем direction (следуем за рынком)

Если payout на current_pair падает <85% → одна смена пары за цикл
   (_pick_switch_pair выбирает лучшую свободную из _pair_scores)

WIN → сброс цикла, возврат к FREE режиму (база $1)
Stop-sum ($50 суммарных потерь) или max_steps (5) → waiting_resume
   Ждёт /resume в Telegram
```

### 8. 15-окный мульти-чарт (ускорение)

[trading/window_manager.py](trading/window_manager.py):
- Каждые **90 сек** (только когда `mg_step == 0`):
  - DOM-probe находит все 15 окон + их текущие пары + координаты кнопок prev/next
  - Если пара в окне с payout <85% или дубль другой пары → **клик «next»** до уникальной пары в диапазоне payout 85-92%
- `ensure_pair_in_window(sym)` — отдельный вызов перед каждой сделкой: гарантирует что торгуемая пара в окне

## Конфигурация (`config.yaml`)

```yaml
mode: paper               # paper | real
cdp_url: http://localhost:9222

filter:
  min_payout: 92          # минимум выплата для входа
  payout_floor: 85        # ниже — смена пары в цикле
  max_losses_in_row: 3    # бан пары при 4+ подряд
  history_candles: 1060   # совпадает с сайтом для точного HTF
  ban_hours: 12
  day_off_hours: 6

trading:
  base_amount: 1
  expiry_seconds: 120
  one_trade_at_a_time: true
  max_pair_switch_per_cycle: 1

martingale:
  coefficient: 2.1
  max_steps: 5
  stop_sum: 50            # пауза при $50 потерь

indicator:
  minConsensus: 4
  requireAll5OnWeekend: true
  rsiPeriod: 14; rsiSmoothing: 5; qqeFactor: 4.238
  htfMultiplier: 5; htfMaPeriod: 20
  atrPeriod: 14; atrAvgWindow: 100; atrMinRatio: 0.7; atrMaxRatio: 2.0
  bbPeriod: 20; bbStdDev: 2.0; bbZoneDepth: 0.3
  candleMaxAtrMult: 2.0; cooldownBars: 3
```

## Запуск

### Локально
```bash
# 1. Chrome с CDP
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 --user-data-dir=/tmp/po-chrome

# 2. Зависимости
pip3 install -r requirements.txt

# 3. Бот
python3 main.py --mode paper
```

### Railway
См. [DEPLOY.md](DEPLOY.md). Требуется:
- Hobby plan ($5/мес) — под Chromium надо >512 MB RAM
- Persistent Volume на `/chrome-data` — сохраняет сессию и настройку 15 окон
- Env-переменные: `PO_EMAIL`, `PO_PASSWORD`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `MODE`
- Auto-login с 2-часовым session-reset отрабатывает автоматически

## Telegram

Команды:
- `/status` — пара, MG-шаг, потери, баланс
- `/balance` — баланс
- `/pause` / `/resume` — пауза / возобновление
- `/stop` — остановить бота
- `/bans` — активные баны
- `/chart SYMBOL` — свечной график
- `/test SYMBOL call|put [amount]` — тестовая сделка минуя фильтры

Автоматические уведомления:
- 📡 сигнал + график + «Захожу $X»
- ✅ WIN / ❌ LOSS / ➖ DRAW
- 🔄 смена пары (payout<85%)
- 🛑 стоп-сумма или max_steps (ждёт `/resume`)
- 📊 ежедневный отчёт в 7:00: WR, сделки, смен пар, макс-streak

## Известные ограничения

1. **Sandbox-хак через prototype.send** — если сайт обновит бандл и сохранит `WebSocket.prototype.send` в замыкание — бот ослепнет. Мониторинг: если `WS SEND` перестал логиться — чинить хук.
2. **Session reset** каждые ~2 часа — auto-login отрабатывает, но если PoSignals добавит капчу — сломается.
3. **15-окный режим обязателен для скорости**. Без него live-тики только на 1 пару, остальные через REST с 15-60 сек задержкой.
4. **history_candles=1060** должно совпадать с сайтом для точного HTF — иначе расхождения в максимум-streak ±1-2 сделки.
5. **Один Telegram-бот** — polling одного токена нельзя параллелить (`TelegramConflictError`).
6. **REST лаг для некоторых пар** — сайт иногда возвращает свечи с отставанием 1 бара → staleness-гейт отбрасывает такие сигналы (это правильное поведение, но снижает частоту сделок на пары без live-тиков).

## Testing checklist (перед real-mode)

- [ ] `/test SYMBOL call` открывает сделку в демо
- [ ] WIN → мартингейл сбрасывается, возврат в FREE скан
- [ ] LOSS → `mg_step++`, бот ждёт новый сигнал на той же паре
- [ ] Новый сигнал → ставка × 2.1
- [ ] Сигнал в противоположном направлении → смена direction, MG продолжается
- [ ] Payout упал <85% → смена пары (одна за цикл)
- [ ] Стоп-сумма → `waiting_resume`, `/resume` восстанавливает
- [ ] Автосет окон раз в 90с заменил дубли / низкие payout
- [ ] `current_pair` всегда в одном из 15 окон во время цикла
- [ ] Ежедневный отчёт в 7:00
- [ ] После рестарта контейнера (Railway) — session подхватывается из volume
