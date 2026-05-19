# Экспорт CONSENSUS 4/5 для Bybit

## Что внутри

`consensus_4_5_bybit.pine` — Pine Script v5 с полным портом стратегии
CONSENSUS 4/5 из этого репо. Поддерживает **2 режима выхода:**

1. **Trailing Stop** (рекомендуется) — SL подтягивается за low/high закрытых
   свечей в сторону прибыли. Без фиксированного TP.
2. **Fixed R:R** — SL по low/high сигнальной свечи, TP в N раз дальше.
   R:R настраивается в UI (рекомендую 1:3).

## Результаты бэктеста на РЕАЛЬНЫХ Bybit данных

**Период:** 60 дней (март-май 2026), пары BTCUSDT/SOLUSDT/ETHUSDT perpetual,
таймфрейм 5m, 739 сделок:

| Режим | Total R | WR | Max LOSS streak | Drawdown |
|---|---|---|---|---|
| **Trailing Stop** (рекомендую) | **+128.7 R** | 39% | **14** | **~14%** |
| Fixed R:R 1:5 | +129.9 R | 20% | 32 | ~32% |
| Fixed R:R 1:3 | +101 R | 28% | 28 | ~28% |
| Fixed R:R 1:2 | +71 R | 37% | 14 | ~14% |
| Fixed R:R 1:1 | +5 R ⚠ | 50% | 8 | малый |

**Вывод:** Trailing Stop даёт тот же профит что R:R 1:5, но в 2× меньший drawdown.

## Per-pair (60 дней, Trailing Stop)

| Pair | Сделок | WR | Total R |
|---|---|---|---|
| BTCUSDT | 241 | 39.8% | **+49.8 R** |
| SOLUSDT | 255 | 39.6% | +37.0 R |
| ETHUSDT | 243 | 36.6% | +41.9 R |

**Все 3 пары прибыльны.** Можно торговать все или приоритизировать BTC.

## Как подключить

### Вариант A: TradingView Strategy Tester (бэктест)

1. Открой TradingView → график **BYBIT:BTCUSDT.P** (или SOLUSDT.P / ETHUSDT.P)
2. Поставь таймфрейм **5m**
3. Pine Editor → New → вставь содержимое `consensus_4_5_bybit.pine`
4. Save → Add to Chart
5. В Inputs выбери Exit mode: **Trailing Stop** (по умолчанию)
6. Открой Strategy Tester (внизу) — увидишь historical PnL, WR, max DD

### Вариант B: TradingView Alert → webhook → Bybit

**Нужна:** подписка TradingView Pro ($14.95/мес) для webhook алертов.

1. Установи скрипт на график (как в варианте A)
2. Кликни Alert (значок будильника) → New Alert
3. Condition: **этот скрипт** → выбери **"Any alert() function call"**
4. Webhook URL: твой Bybit-listener (3Commas / Aleeert / свой сервер)
5. Message: `{{strategy.order.alert_message}}` — скрипт уже генерирует JSON:

**На входе:**
```json
{"side":"BUY","symbol":"BTCUSDT.P","entry":62100,"sl":62050,"tp":null,"mode":"Trailing Stop"}
```

**На выходе (когда trailing SL сработает):**
```json
{"side":"CLOSE_LONG","symbol":"BTCUSDT.P","reason":"trailing_sl"}
```

### Вариант C: Свой Python-listener (если есть VPS)

Минимальный FastAPI приёмник для Bybit USDT-perpetual:

```python
from fastapi import FastAPI, Request
from pybit.unified_trading import HTTP

app = FastAPI()
bybit = HTTP(testnet=True, api_key="...", api_secret="...")  # testnet сначала!

POSITION_SIZE_PCT = 0.5  # 0.5% капитала на сделку

def calc_qty(symbol, balance_usd, entry, sl):
    risk_usd = balance_usd * POSITION_SIZE_PCT / 100
    distance = abs(entry - sl)
    qty = risk_usd / distance
    return qty

@app.post("/webhook")
async def webhook(req: Request):
    data = await req.json()
    side = data["side"]

    if side == "BUY":
        bal = float(bybit.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0]["totalEquity"])
        qty = calc_qty(data["symbol"], bal, data["entry"], data["sl"])
        bybit.place_order(
            category="linear",
            symbol=data["symbol"].replace(".P", ""),
            side="Buy",
            orderType="Market",
            qty=str(round(qty, 3)),
            stopLoss=str(data["sl"]),
            takeProfit=str(data["tp"]) if data["tp"] else None,
            slTriggerBy="MarkPrice",
        )
    elif side == "SELL":
        # симметрично для Sell
        ...
    elif side in ("CLOSE_LONG", "CLOSE_SHORT"):
        # закрыть позицию market'ом (но обычно SL уже сработал)
        ...
    return {"ok": True}
```

**Запуск:** `uvicorn listener:app --host 0.0.0.0 --port 8080`
Открыть порт для TradingView (whitelist их IP).

## Money Management (КРИТИЧНО)

| Параметр | Значение | Обоснование |
|---|---|---|
| Risk per trade | **0.25-0.5% капитала** | Streak 14 → max DD 7% при 0.5% |
| Initial capital | $1000 → $5000+ | Чтобы 0.5% было >$5 (Bybit min) |
| Macro stop | -10% капитала | Остановись если просел на 10% |
| Position size | 1-3% deposit | Margin × leverage = exposure |
| Leverage | 5x-10x max | Не выше — ликвидация рядом со SL |

## Параметры скрипта (по умолчанию = mirror config.yaml бота)

| Группа | Параметр | Значение |
|---|---|---|
| Exit | EXIT_MODE | **Trailing Stop** |
| Exit | R:R (если Fixed) | 3.0 |
| Exit | Max hold | 288 баров (24h на 5m) |
| Consensus | Min votes | 4 |
| Consensus | Weekend 5/5 | true |
| Consensus | Cooldown | 3 баров |
| RSI | period/smooth/QQE | 14 / 5 / 4.238 |
| HTF | mult/period/type | 5 / 20 / EMA |
| ATR | period/ratio | 14 / 0.7-2.0 |
| BB | period/std/zone | 20 / 2.0 / 0.3 |
| Candle | align/maxATR | true / 2.0 |

Все настраиваются через UI скрипта в TradingView.

## План перехода на live

**Шаг 1 (1 день):** TradingView Strategy Tester
- Установи скрипт на BTCUSDT.P 5m
- Strategy Tester должен показать ~+50R за 60 дней
- Проверь визуально что стрелки выглядят разумно

**Шаг 2 (2-4 недели):** Bybit Testnet
- Получи API ключи на testnet.bybit.com
- Подключи через webhook (один из вариантов выше)
- Paper trading на $10k виртуальных
- Сравни итог с backtest — должен быть в пределах ±30%

**Шаг 3 (live):** Mainnet с малым риском
- 0.25% риска на сделку (даже streak 20 выдержит без катастрофы)
- Первый месяц — фиксируй каждую сделку в журнал
- Если живой WR ≥ 35% и стабильно — увеличь до 0.5%

**Шаг 4 (оптимизация):**
- Через 3 месяца статистики — посмотри какая пара даёт лучший R
- Возможно стоит торговать только одну (например BTC)
- Подкручивай параметры на бэктесте каждый месяц

## Известные ограничения

1. **HTF group в Pine:** wall-time группировка (нативно для TV). В Python-боте
   buffer-relative. На длинной истории расхождение ~5-10% сигналов. Не критично.
2. **Slippage:** backtest не учитывает slippage. Реальный live может быть на
   0.05-0.1% хуже на market orders. Используй limit orders где возможно.
3. **Funding rate:** на perpetual каждые 8h. Для коротких сделок (median 15 мин)
   почти не страшно, но при timeout (4-24h) может быть значимо.
4. **Sample size:** 60 дней / 739 сделок — статистически OK, но рынок
   изменчив. Следи за реальной WR в первый месяц live.
5. **Spread:** на ликвидных перпах (BTC/SOL/ETH) обычно 0.01-0.05%. Не страшно.

## Структура файлов

```
exports/
├── README.md                       ← этот файл
└── consensus_4_5_bybit.pine        ← Pine Script v5 (Trailing + Fixed R:R)
```
