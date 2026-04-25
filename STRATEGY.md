# Как писать стратегию для PO-Sig Bot

Стратегия — это Python-файл, который **анализирует свечи и возвращает сигналы**. Бот вызывает её на каждом скан-цикле для каждой отслеживаемой пары.

## Минимальный интерфейс

Каждая стратегия должна иметь:

| Имя | Тип | Описание |
|---|---|---|
| `NAME` | str | Человекочитаемое название |
| `DEFAULT_PARAMS` | dict | Словарь параметров со значениями по умолчанию |
| `generate_signals(candles, params)` | func | Возвращает `(signals: list[Signal], diag: dict)` |

## Объект `Signal`

```python
from dataclasses import dataclass

@dataclass
class Signal:
    side: str          # "buy" → action "call" (рост), "sell" → action "put" (падение)
    i: int             # индекс бара сигнала в массиве candles
    votes: dict        # любая dict-структура с диагностикой ({"rsi": 1, "htf": 1, ...})
    total: int         # общее число "голосов"
```

**Бот возьмёт `signals[-1]`**. Если его `i != len(candles) - 1` (т.е. это не последний бар) — сигнал проигнорируется.

## Формат свечей

```python
candles = [
    {"time": 1777070640, "open": 1.197, "high": 1.198, "low": 1.196, "close": 1.197, "volume": 0},
    ...
]
```

- `time` — Unix timestamp **открытия** бара (закрытие = time + period)
- Бары упорядочены по возрастанию `time`
- Последний бар (`candles[-1]`) обычно ещё не закрыт. Стандартная практика — анализировать `candles[:-1]`

## Простой пример (SMA Cross)

```python
from dataclasses import dataclass

NAME = "SMA Cross"
DEFAULT_PARAMS = {"fastPeriod": 9, "slowPeriod": 21}

@dataclass
class Signal:
    side: str
    i: int
    votes: dict
    total: int

def sma(values, period):
    out = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= period: s -= values[i - period]
        if i >= period - 1: out[i] = s / period
    return out

def generate_signals(candles, params):
    p = {**DEFAULT_PARAMS, **(params or {})}
    closes = [c["close"] for c in candles]
    fast = sma(closes, p["fastPeriod"])
    slow = sma(closes, p["slowPeriod"])
    sigs = []
    for i in range(1, len(candles)):
        if fast[i] is None or slow[i] is None: continue
        if fast[i-1] <= slow[i-1] and fast[i] > slow[i]:
            sigs.append(Signal("buy", i, {"cross": 1}, 1))
        elif fast[i-1] >= slow[i-1] and fast[i] < slow[i]:
            sigs.append(Signal("sell", i, {"cross": 1}, 1))
    return sigs, {"found": len(sigs)}
```

## Где взять готовые индикаторы

В `strategy/indicators.py` уже есть:

```python
from strategy.indicators import (
    sma, ema, wma, rma, ma, stdev,
    rsi, qqe, htf_trend, atr, bollinger, candle_aligned,
)
```

Используй как шорткаты — не надо писать с нуля. Импорт работает в твоей стратегии.

## Полный пример: CONSENSUS-подобная

Смотри **встроенную стратегию** [strategy/consensus.py](strategy/consensus.py) — это эталон. Там 274 строки: RSI-QQE + HTF + ATR + Bollinger + свечной фильтр.

## Как загрузить

**Способ 1 — через Mini App (быстро):**
1. Открой Mini App → вкладка **Стратегии**
2. Введи имя файла (буквы/цифры/_, например `my_macd`)
3. Вставь Python-код
4. Кликни **Сохранить и активировать**
5. Бот сразу начнёт использовать твою стратегию

**Способ 2 — через файл (для разработки):**
1. Создай `strategy/user/my_strategy.py`
2. Перезапусти бота — стратегия автоподгрузится
3. Активируй через Mini App или `/api/strategies/my_strategy/activate`

## Параметры

- `DEFAULT_PARAMS` объединяются с глобальными `cfg["indicator"]` из config.yaml
- Любой параметр стратегии можно править через Mini App → Настройки (если добавишь в `tg/settings_ui.py` schema, или просто через `PUT /api/settings`)

## Если хочешь чтобы я написал/доделал стратегию

Пришли мне в чат:
1. **Описание логики** простыми словами (например: «вход на пробое канала Кельтнера на M5 с подтверждением RSI»)
2. **Параметры** которые хочешь крутить
3. **Логика выхода** (если отличается от стандартной — обычно стандарт: фиксированная экспирация 120 сек)

Я напишу полный файл, проверю на твоём буфере свечей, отдам готовый код для загрузки через Mini App.

## Тестирование стратегии

Перед боевым запуском:

```bash
python3 -c "
from strategy.user.my_strategy import generate_signals, DEFAULT_PARAMS
from journal.candles_db import CandlesDB
db = CandlesDB('journal/candles.db')
candles = db.load('EURUSD_otc', 60, limit=1000)
sigs, diag = generate_signals(candles, DEFAULT_PARAMS)
print(f'Сигналов: {len(sigs)}, диагностика: {diag}')
for s in sigs[-5:]:
    print(s)
"
```

Проверишь сколько сигналов на исторических данных и подкрутишь.
