"""CONSENSUS 4/5 — 1:1 port of the JS indicator.

Exposes two pure functions:
  generate_signals(candles, params) -> list[Signal]
  evaluate_trade(opens, closes, signal, params, n) -> TradeResult

Plus a top-level analyze() that walks history and returns stats needed for the filter:
  - signals list
  - per-signal outcome (completed/firstWin/totalWin/stepsToWin/exitIndex)
  - max_loss_streak_before_win  (важно для фильтра ≤3 минусов подряд)
  - max_loss_streak_overall
  - wr, wr1, completed, losses, wins, recent_results
"""

from dataclasses import dataclass, field
from typing import List, Optional

from strategy.indicators import (
    sma, ema, wma, rma, ma, stdev,
    rsi, qqe, htf_trend, atr, bollinger, candle_aligned,
)

# ---------- default params (mirrors meta.defaultParams) ----------

DEFAULT_PARAMS = {
    "expiryBars": 2,
    "entryMode": "nextBarOpen",
    "martingaleSteps": 0,
    "drawBehavior": "continue",
    "minConsensus": 4,
    "requireAll5OnWeekend": True,
    "rsiPeriod": 14, "rsiSmoothing": 5, "qqeFactor": 4.238,
    "htfMultiplier": 5, "htfMaType": "EMA", "htfMaPeriod": 20,
    "atrPeriod": 14, "atrAvgWindow": 100, "atrMinRatio": 0.7, "atrMaxRatio": 2.0,
    "bbPeriod": 20, "bbStdDev": 2.0, "bbZoneDepth": 0.3,
    "candleMaxAtrMult": 2.0, "candleReqAlign": True,
    "cooldownBars": 3,
    "statsLookbackBars": 1000,
    "recentLookbackBars": 200,
}

# UI schema for Mini App "Параметры стратегии" tab
PARAM_SCHEMA = {
    "minConsensus":         {"type": "int",    "min": 3,    "max": 5,    "label": "Минимум голосов (4-5)"},
    "requireAll5OnWeekend": {"type": "bool",                             "label": "На выходных требовать 5/5"},
    "cooldownBars":         {"type": "int",    "min": 0,    "max": 30,   "label": "Cooldown (бары)"},
    "expiryBars":           {"type": "int",    "min": 1,    "max": 10,   "label": "Экспирация в барах (бэктест)"},
    "entryMode":            {"type": "choice", "options": ["nextBarOpen", "signalBarClose"], "label": "Режим входа"},
    "rsiPeriod":            {"type": "int",    "min": 2,    "max": 50,   "label": "RSI period"},
    "rsiSmoothing":         {"type": "int",    "min": 1,    "max": 30,   "label": "RSI smoothing"},
    "qqeFactor":            {"type": "float",  "min": 1,    "max": 10,   "step": 0.1, "label": "QQE factor"},
    "htfMultiplier":        {"type": "int",    "min": 2,    "max": 60,   "label": "HTF multiplier"},
    "htfMaPeriod":          {"type": "int",    "min": 5,    "max": 200,  "label": "HTF MA period"},
    "htfMaType":            {"type": "choice", "options": ["EMA", "SMA", "WMA", "RMA"], "label": "HTF MA type"},
    "atrPeriod":            {"type": "int",    "min": 5,    "max": 50,   "label": "ATR period"},
    "atrAvgWindow":         {"type": "int",    "min": 20,   "max": 500,  "label": "ATR avg window"},
    "atrMinRatio":          {"type": "float",  "min": 0.1,  "max": 5,    "step": 0.05, "label": "ATR min ratio"},
    "atrMaxRatio":          {"type": "float",  "min": 0.5,  "max": 10,   "step": 0.1, "label": "ATR max ratio"},
    "bbPeriod":             {"type": "int",    "min": 5,    "max": 100,  "label": "BB period"},
    "bbStdDev":             {"type": "float",  "min": 0.5,  "max": 5,    "step": 0.1, "label": "BB std dev"},
    "bbZoneDepth":          {"type": "float",  "min": 0.05, "max": 0.95, "step": 0.05, "label": "BB zone depth"},
    "candleMaxAtrMult":     {"type": "float",  "min": 0.5,  "max": 10,   "step": 0.1, "label": "Max body / ATR"},
    "candleReqAlign":       {"type": "bool",                             "label": "Свеча в направлении"},
}


@dataclass
class Signal:
    side: str          # "buy" | "sell"
    i: int             # bar index
    votes: dict        # {rsi, htf, vol, bb, candle} each 0/1
    total: int         # sum of votes


@dataclass
class TradeResult:
    completed: bool = False
    first_win: bool = False
    total_win: bool = False
    steps_used: int = 0
    steps_to_win: int = -1   # -1 = never won
    exit_index: int = -1


def _is_weekend(unix_time: int) -> bool:
    import datetime
    d = datetime.datetime.utcfromtimestamp(unix_time).weekday()
    # JS getUTCDay: Sun=0, Sat=6; Python weekday: Mon=0, Sun=6 → map
    return d == 6 or d == 5  # Sat, Sun


# ---------- signal generator ----------

def generate_signals(candles: list[dict], params: dict) -> tuple[list[Signal], dict]:
    p = {**DEFAULT_PARAMS, **(params or {})}
    n = len(candles)
    if n < 2:
        return [], {"rawCount": 0, "rejected": {"htf":0,"atr":0,"bb":0,"candle":0,"cooldown":0}}

    times  = [c["time"]  for c in candles]
    opens  = [c["open"]  for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    closes = [c["close"] for c in candles]

    rsi_ma, trailing = qqe(closes, p["rsiPeriod"], p["rsiSmoothing"], p["qqeFactor"])
    # HTF must group by absolute wall-time (e.g. 11:45-11:50, 11:50-11:55),
    # NOT by buffer-relative index. Otherwise the boundary shifts whenever
    # the buffer slides, causing earlier 4/5 signals to silently become 3/5
    # ("repaint") on later renders. Infer tf from median time-delta.
    tf_sec = 60
    if len(times) >= 3:
        deltas = sorted(times[i+1] - times[i] for i in range(len(times) - 1))
        tf_sec = max(1, int(deltas[len(deltas) // 2]))
    htf = htf_trend(opens, highs, lows, closes, p["htfMultiplier"],
                    p["htfMaPeriod"], p["htfMaType"],
                    times=times, tf_seconds=tf_sec)
    atr_arr = atr(highs, lows, closes, p["atrPeriod"])
    atr_avg = sma(atr_arr, p["atrAvgWindow"])
    bb = bollinger(closes, p["bbPeriod"], p["bbStdDev"])

    events: list[Signal] = []
    last_sig_idx = -10**9
    raw_count = 0
    rejected = {"htf":0, "atr":0, "bb":0, "candle":0, "cooldown":0}

    for i in range(1, n):
        if rsi_ma[i] is None or trailing[i] is None or rsi_ma[i-1] is None or trailing[i-1] is None:
            continue

        cross_up   = rsi_ma[i-1] <= trailing[i-1] and rsi_ma[i] >  trailing[i]
        cross_down = rsi_ma[i-1] >= trailing[i-1] and rsi_ma[i] <  trailing[i]
        if not cross_up and not cross_down:
            continue
        raw_count += 1

        side = "buy" if cross_up else "sell"
        votes = {"rsi": 1, "htf": 0, "vol": 0, "bb": 0, "candle": 0}

        # ② HTF
        htf_ok = (htf[i] == +1) if side == "buy" else (htf[i] == -1)
        votes["htf"] = 1 if htf_ok else 0

        # ③ Volatility
        vol_ok = False
        if atr_arr[i] is not None and atr_avg[i] is not None and atr_avg[i] > 0:
            ratio = atr_arr[i] / atr_avg[i]
            vol_ok = p["atrMinRatio"] <= ratio <= p["atrMaxRatio"]
        votes["vol"] = 1 if vol_ok else 0

        # ④ Bollinger
        bb_ok = False
        up_i = bb["upper"][i]; lo_i = bb["lower"][i]
        if up_i is not None and lo_i is not None:
            rng = up_i - lo_i
            if rng > 0:
                pos = (closes[i] - lo_i) / rng
                if side == "buy":  bb_ok = pos <= p["bbZoneDepth"]
                else:              bb_ok = pos >= (1 - p["bbZoneDepth"])
        votes["bb"] = 1 if bb_ok else 0

        # ⑤ Candle
        candle_ok = True
        if p["candleReqAlign"] and not candle_aligned(opens[i], closes[i], side):
            candle_ok = False
        if candle_ok and atr_arr[i] is not None and atr_arr[i] > 0:
            size = abs(closes[i] - opens[i])
            if size > atr_arr[i] * p["candleMaxAtrMult"]:
                candle_ok = False
        votes["candle"] = 1 if candle_ok else 0

        total = sum(votes.values())
        required = 5 if (p["requireAll5OnWeekend"] and _is_weekend(times[i])) else p["minConsensus"]

        if not votes["htf"]:    rejected["htf"]    += 1
        if not votes["vol"]:    rejected["atr"]    += 1
        if not votes["bb"]:     rejected["bb"]     += 1
        if not votes["candle"]: rejected["candle"] += 1

        if total < required:
            continue
        if i - last_sig_idx <= p["cooldownBars"]:
            rejected["cooldown"] += 1
            continue
        last_sig_idx = i

        events.append(Signal(side=side, i=i, votes=votes, total=total))

    return events, {"rawCount": raw_count, "rejected": rejected}


# ---------- trade evaluator ----------

def evaluate_trade(opens: List[float], closes: List[float], sig: Signal, params: dict, n: int) -> TradeResult:
    p = {**DEFAULT_PARAMS, **(params or {})}
    steps = max(0, int(p["martingaleSteps"] or 0))
    exp = max(1, int(p["expiryBars"] or 1))
    entry_mode = str(p["entryMode"] or "nextBarOpen")

    first_win = False
    last_exit = -1

    for step in range(steps + 1):
        sig_idx = sig.i + step * exp

        if entry_mode == "signalBarClose":
            entry_idx = sig_idx
            if entry_idx >= n:
                return TradeResult(False, first_win, False, step, -1, last_exit)
            entry_price = closes[entry_idx]
        else:
            entry_idx = sig_idx + 1
            if entry_idx >= n:
                return TradeResult(False, first_win, False, step, -1, last_exit)
            entry_price = opens[entry_idx]

        exit_idx = entry_idx + exp - 1
        if exit_idx >= n:
            return TradeResult(False, first_win, False, step, -1, last_exit)

        last_exit = exit_idx
        exit_price = closes[exit_idx]
        if exit_price == entry_price:
            result = "draw"
        elif sig.side == "buy":
            result = "win" if exit_price > entry_price else "loss"
        else:
            result = "win" if exit_price < entry_price else "loss"

        if result == "draw":
            if p["drawBehavior"] == "win":  result = "win"
            if p["drawBehavior"] == "loss": result = "loss"

        if step == 0:
            first_win = (result == "win")

        if result == "win":
            return TradeResult(True, first_win, True, step + 1, step, exit_idx)
        if result == "draw" and p["drawBehavior"] == "continue":
            continue
        # else: loss → next step

    return TradeResult(True, first_win, False, steps + 1, -1, last_exit)


# ---------- aggregate analyzer ----------

@dataclass
class Analysis:
    signals: list[Signal] = field(default_factory=list)
    outcomes: dict = field(default_factory=dict)   # signal.i → TradeResult
    wins: int = 0
    losses: int = 0
    wins1: int = 0           # wins on first trade (no martingale)
    completed: int = 0
    wr: float = 0.0
    wr1: float = 0.0
    max_loss_streak_before_win: int = 0   # longest losing streak that ended with a win
    max_loss_streak_overall: int = 0      # longest losing streak, period
    recent_results: list[int] = field(default_factory=list)   # 1=win 0=loss
    # Recent-window stats (last N bars, configured by recentLookbackBars).
    # Used for the second-tier filter: a pair must be good both long-term
    # (wr1 over full lookback) AND in recent form (wr1_recent over short window).
    wins1_recent: int = 0           # first-trade wins in recent window
    wins_recent: int = 0            # total wins (including MG-recovered) in recent window
    losses_recent: int = 0          # total losses in recent window
    completed_recent: int = 0       # total completed signals in recent window
    wr1_recent: float = 0.0         # % first-trade wins in recent window
    diag: dict = field(default_factory=dict)


def analyze(candles: list[dict], params: dict) -> Analysis:
    p = {**DEFAULT_PARAMS, **(params or {})}
    n = len(candles)
    a = Analysis()
    if n < 100:
        return a

    signals, diag = generate_signals(candles, p)
    a.signals = signals
    a.diag = diag

    lookback = max(1, int(p["statsLookbackBars"]))
    from_bar = max(0, (n - 1) - lookback)
    # Recent-window: smaller, tighter slice for "current form" check.
    # Defaults to 200 bars but configurable via recentLookbackBars.
    recent_lookback = max(1, int(p.get("recentLookbackBars", 200)))
    from_bar_recent = max(0, (n - 1) - recent_lookback)

    opens = [c["open"] for c in candles]
    closes = [c["close"] for c in candles]

    current_streak = 0
    for ev in signals:
        if ev.i < from_bar:
            continue
        r = evaluate_trade(opens, closes, ev, p, n)
        a.outcomes[ev.i] = r
        if not r.completed:
            continue
        a.completed += 1
        if r.first_win:
            a.wins1 += 1
        if r.total_win:
            a.wins += 1
            a.recent_results.append(1)
            if current_streak > a.max_loss_streak_before_win:
                a.max_loss_streak_before_win = current_streak
            current_streak = 0
        else:
            a.losses += 1
            a.recent_results.append(0)
            current_streak += 1
            if current_streak > a.max_loss_streak_overall:
                a.max_loss_streak_overall = current_streak
        # ALSO count this signal toward recent-window stats if it falls
        # within the smaller lookback (200 bars). Track plus/minus separately
        # so we can show user "X plus, Y minus за последние 200 свечей".
        if ev.i >= from_bar_recent:
            a.completed_recent += 1
            if r.first_win:
                a.wins1_recent += 1
            if r.total_win:
                a.wins_recent += 1
            else:
                a.losses_recent += 1

    if a.completed:
        a.wr = a.wins / (a.wins + a.losses) * 100 if (a.wins + a.losses) else 0
        a.wr1 = a.wins1 / a.completed * 100
    if a.completed_recent:
        a.wr1_recent = a.wins1_recent / a.completed_recent * 100
    return a
