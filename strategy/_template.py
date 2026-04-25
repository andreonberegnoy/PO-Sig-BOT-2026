"""ШАБЛОН СТРАТЕГИИ для PO-Sig Bot.

Скопируй это в Mini App → Стратегии → Загрузить → измени логику и сохрани.

ИНТЕРФЕЙС (обязательный):

    NAME: str                    — название стратегии (любая строка)
    DEFAULT_PARAMS: dict         — словарь параметров со значениями по умолчанию
    generate_signals(candles, params) -> tuple[list[Signal], dict]

ВХОД:
    candles  — список dict вида:
                  [{"time": int, "open": float, "high": float, "low": float,
                    "close": float, "volume": float}, ...]
                Бары упорядочены по возрастанию time. Последний бар может быть
                ещё не закрыт; обычно используют candles[:-1] чтобы анализировать
                только закрытые.
    params   — словарь с твоими параметрами (DEFAULT_PARAMS, переопределённые
               через config.yaml или Mini App).

ВЫХОД:
    Кортеж (signals, diag):
        signals — list[Signal] где Signal — объект из strategy.consensus.Signal:
                    Signal(side="buy"|"sell", i=индекс_бара_сигнала,
                           votes=dict_with_counts, total=int_total_votes)
        diag    — словарь с диагностической инфой (можно пустой)

ВАЖНО:
    - Бот возьмёт ПОСЛЕДНИЙ сигнал в списке (signals[-1]).
    - Если индекс этого сигнала != len(candles)-1 — игнорируется (неактуально).
    - Сигнал должен быть на ПОСЛЕДНЕМ закрытом баре чтобы войти в сделку.
    - side="buy" → action "call" (рост), side="sell" → action "put" (падение).

ПРИМЕР НИЖЕ — простая SMA-cross стратегия. Пиши свою логику, сохраняй.
"""

from dataclasses import dataclass


# ─── метаданные ───────────────────────────────────────────────────────
NAME = "SMA Cross Example"

DEFAULT_PARAMS = {
    "fastPeriod": 9,
    "slowPeriod": 21,
}

# (опционально) Схема для красивой UI в Mini App
# Без неё бот сам угадает типы (int/float/bool) — но min/max/label/options не задать.
PARAM_SCHEMA = {
    "fastPeriod": {"type": "int", "min": 2, "max": 50,  "label": "Fast SMA period"},
    "slowPeriod": {"type": "int", "min": 5, "max": 200, "label": "Slow SMA period"},
}


# ─── стандартный Signal-объект (обязательный формат) ───────────────────
@dataclass
class Signal:
    side: str
    i: int
    votes: dict
    total: int


# ─── вспомогательные функции (можешь дописывать свои) ──────────────────
def sma(values: list[float], period: int) -> list[float | None]:
    """Простое скользящее среднее. Возвращает list той же длины, None для warmup."""
    out = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= period:
            s -= values[i - period]
        if i >= period - 1:
            out[i] = s / period
    return out


# ─── обязательная функция ──────────────────────────────────────────────
def generate_signals(candles: list[dict], params: dict) -> tuple[list[Signal], dict]:
    """Найти точки пересечения быстрой и медленной SMA → сигналы buy/sell."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    closes = [c["close"] for c in candles]
    fast = sma(closes, p["fastPeriod"])
    slow = sma(closes, p["slowPeriod"])

    signals: list[Signal] = []
    for i in range(1, len(candles)):
        if fast[i] is None or slow[i] is None or fast[i-1] is None or slow[i-1] is None:
            continue
        # Cross up — fast пересекает slow снизу вверх → BUY
        if fast[i-1] <= slow[i-1] and fast[i] > slow[i]:
            signals.append(Signal(side="buy", i=i, votes={"cross": 1}, total=1))
        # Cross down → SELL
        elif fast[i-1] >= slow[i-1] and fast[i] < slow[i]:
            signals.append(Signal(side="sell", i=i, votes={"cross": 1}, total=1))

    return signals, {"signals_found": len(signals)}


# ─── (опционально) функция оценки бэктеста ─────────────────────────────
# Если её НЕТ, бот использует стандартную evaluate_trade из strategy.consensus.
# Можно переопределить если у тебя нестандартная логика выхода (например, TP/SL).
#
# def evaluate_trade(opens, closes, signal, params, n) -> TradeResult:
#     ...
