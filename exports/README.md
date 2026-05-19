# Экспорт CONSENSUS 4/5 на Bybit (через TradingView)

## Что внутри

`consensus_4_5_bybit.pine` — Pine Script v5 с **полным портом** стратегии
CONSENSUS 4/5 из этого репо (`strategy/consensus.py`).

Логика 1:1 с твоим ботом на PocketOption:
- **5 голосов:** RSI/QQE crossover, HTF trend, ATR volatility, Bollinger zone, Candle alignment
- **Требуется 4 из 5** (на выходных опционально 5/5)
- **Cooldown** между сигналами (3 бара по умолчанию)

**Разница от PO-бота — exit logic:** на Bybit нет «экспирации». Вместо неё:
- **SL** = low сигнальной свечи (для BUY) / high (для SELL)
- **TP** = entry + 2 × (entry − SL) — соотношение **R:R 1:2** (настраивается)

## Как использовать

### Вариант A: TradingView Strategy Tester + ручная торговля

1. Открой TradingView → любой график BTCUSDT (или твой инструмент Bybit)
2. Pine Editor → New → вставь содержимое `consensus_4_5_bybit.pine`
3. Save → Add to Chart
4. Открой Strategy Tester (внизу) — увидишь backtest с PnL, WR, max DD

### Вариант B: Автоматическая торговля через webhook

1. **Купи TradingView Pro** ($14.95/мес) — нужны Alert webhooks
2. Установи скрипт на график (как в варианте A)
3. Create Alert → Condition: **"Any alert() function call"** на этом скрипте
4. Webhook URL: твой Bybit-listener (3Commas/Aleeert/собственный)
5. Message: `{{strategy.order.alert_message}}` — скрипт уже генерирует JSON:
   ```json
   {"side":"BUY","symbol":"BTCUSDT.P","sl":62100.5,"tp":62800.2}
   ```

### Вариант C: Собственный listener на Python (если есть хостинг)

Скрипт уже генерирует JSON в `strategy.entry(alert_message=...)`. Простой
Flask/FastAPI listener на твоём VPS принимает webhook от TradingView и
отправляет ордер через `pybit` или `ccxt`:

```python
from fastapi import FastAPI, Request
from pybit.unified_trading import HTTP

app = FastAPI()
bybit = HTTP(testnet=False, api_key="...", api_secret="...")

@app.post("/webhook")
async def webhook(req: Request):
    data = await req.json()
    bybit.place_order(
        category="linear",
        symbol=data["symbol"],
        side="Buy" if data["side"] == "BUY" else "Sell",
        orderType="Market",
        qty="0.01",
        stopLoss=str(data["sl"]),
        takeProfit=str(data["tp"]),
    )
    return {"ok": True}
```

## Параметры скрипта

Все параметры идентичны `config.yaml` бота:
- `Min votes` = 4 (на выходных 5)
- `Cooldown bars` = 3
- `RSI period` = 14, smoothing 5, QQE factor 4.238
- `HTF multiplier` = 5, MA period 20, type EMA
- `ATR period` = 14, avg window 100, ratio 0.7-2.0
- `BB period` = 20, std 2.0, zone depth 0.3
- `Candle align` = true, max body / ATR = 2.0

Можешь подкручивать в UI настроек скрипта (Inputs tab) — изменения мгновенно
пересчитывают chart и backtest.

## Что отличается от PO-бота

| Аспект | PO-бот | Bybit Pine |
|---|---|---|
| Исход сделки | Win/Loss по экспирации (2 бара) | SL/TP уровни |
| Risk:Reward | Фикс. (payout 87-92%) | 1:2 (настраивается) |
| Размер позиции | base × MG-коэффициенты | % от equity |
| Мартингейл | Есть (q=2.1) | НЕТ — отключён |
| Множ. пар | Параллельный режим | По одной паре за вкладку |
| HTF группировка | Buffer-relative | Wall-time (TradingView native) |

**Важно про HTF:** в PO-боте мы используем buffer-relative группировку для
1:1 совпадения с PoSignals JS-индикатором. В Pine используется wall-time
(нативный TradingView), что **более стабильно** (нет repaint) но может давать
небольшие расхождения в signal-set на одних и тех же исторических данных.
Для Bybit это не проблема — там нет такого reference indicator.

## Рекомендации перед запуском с реальными деньгами

1. **Backtest минимум 6 месяцев** на твоей паре в Strategy Tester
2. **Прогон на Bybit testnet** (1-2 недели paper trading)
3. **Начни с малых сумм** ($50-100) пока не подтверждена живая работа
4. **Проверяй разные таймфреймы** — на 1m может быть много шума,
   попробуй 5m/15m
5. **R:R можно тюнить:** 1:1.5 = чаще WIN но меньше profit, 1:3 = реже WIN
   но больше profit. Зависит от WR стратегии.

## Известные ограничения порта

- Pine Script `request.security` использует **wall-time HTF** (стабильно, но
  не 1:1 с buffer-relative из Python). На длинной истории даёт похожие
  результаты, но конкретные сигналы могут отличаться на 5-10%.
- `_is_weekend` в Pine использует `dayofweek(time)` — определяет subj.
  trading day, не UTC день. Минорно для crypto который торгуется 24/7.
- `cooldownBars` использует `bar_index` — работает корректно но reset'ится
  при изменении таймфрейма.

## Структура файлов

```
exports/
├── README.md                       ← этот файл
└── consensus_4_5_bybit.pine        ← Pine Script для TradingView
```
