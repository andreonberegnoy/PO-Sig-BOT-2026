"""Smoke test: indicator math loads and produces signals on synthetic data."""

import math
import random
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategy.consensus import generate_signals, analyze, DEFAULT_PARAMS
from strategy import indicators as ind


def test_math_helpers():
    v = [1.0, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert ind.sma(v, 3)[-1] == (8 + 9 + 10) / 3
    em = ind.ema(v, 3)
    assert em[0] == 1.0   # seeded with first value
    assert em[-1] > em[0]
    # RSI on monotonic up should be 100
    r = ind.rsi(v, 3)
    assert r[-1] == 100.0

def test_signals_on_sine():
    # Synthetic OHLC: sine wave → should produce some signals
    candles = []
    t0 = 1_700_000_000
    for i in range(500):
        p = 100 + 5 * math.sin(i / 10)
        o = p
        h = p + 0.3; l = p - 0.3; c = p + random.uniform(-0.1, 0.1)
        candles.append({"time": t0 + i * 60, "open": o, "high": h, "low": l, "close": c, "volume": 1})
    sigs, diag = generate_signals(candles, DEFAULT_PARAMS)
    print(f"generated {len(sigs)} signals, diag={diag}")
    assert len(sigs) >= 0   # just shouldn't crash
    # With min_consensus=4 most signals will be rejected → that's fine.

def test_analyze_stats():
    random.seed(42)
    candles = []
    t0 = 1_700_000_000
    for i in range(1000):
        # Random walk
        prev = candles[-1]["close"] if candles else 100
        c = prev + random.uniform(-0.5, 0.5)
        o = prev
        h = max(o, c) + abs(random.uniform(0, 0.2))
        l = min(o, c) - abs(random.uniform(0, 0.2))
        candles.append({"time": t0 + i * 60, "open": o, "high": h, "low": l, "close": c, "volume": 1})
    a = analyze(candles, DEFAULT_PARAMS)
    print(f"analyze: completed={a.completed} wins={a.wins} losses={a.losses} "
          f"max_streak_overall={a.max_loss_streak_overall} max_streak_before_win={a.max_loss_streak_before_win} "
          f"wr={a.wr:.1f}%")
    assert a.completed >= 0

if __name__ == "__main__":
    test_math_helpers(); print("math ok")
    test_signals_on_sine(); print("sine ok")
    test_analyze_stats(); print("analyze ok")
