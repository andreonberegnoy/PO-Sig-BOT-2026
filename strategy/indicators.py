"""Numeric helpers: 1:1 port of math functions from CONSENSUS 4/5 JS.
All return lists of the same length as input, with None for undefined values —
matching JS `null` semantics so logic ports verbatim.
"""

from typing import List, Optional

Num = Optional[float]


def sma(v: List[Num], p: int) -> List[Num]:
    n = len(v)
    out: List[Num] = [None] * n
    s = 0.0
    for i in range(n):
        s += (v[i] or 0)
        if i >= p:
            s -= (v[i - p] or 0)
        if i >= p - 1:
            out[i] = s / p
    return out


def ema(v: List[Num], p: int) -> List[Num]:
    n = len(v)
    out: List[Num] = [None] * n
    k = 2.0 / (p + 1)
    pr: Optional[float] = None
    for i in range(n):
        x = v[i]
        if x is None:
            continue
        pr = x if pr is None else x * k + pr * (1 - k)
        out[i] = pr
    return out


def wma(v: List[Num], p: int) -> List[Num]:
    n = len(v)
    out: List[Num] = [None] * n
    d = (p * (p + 1)) / 2
    for i in range(p - 1, n):
        s = 0.0
        for j in range(p):
            s += (v[i - j] or 0) * (p - j)
        out[i] = s / d
    return out


def rma(v: List[Num], p: int) -> List[Num]:
    """Wilder's smoothing — matches Pine rma / JS rma in source."""
    n = len(v)
    out: List[Num] = [None] * n
    pr: Optional[float] = None
    s = 0.0
    for i in range(n):
        x = v[i] if v[i] is not None else 0
        if i < p:
            s += x
            if i == p - 1:
                pr = s / p
                out[i] = pr
        else:
            pr = (pr * (p - 1) + x) / p  # type: ignore
            out[i] = pr
    return out


def ma(v: List[Num], per: int, kind: str) -> List[Num]:
    if kind == "SMA": return sma(v, per)
    if kind == "WMA": return wma(v, per)
    return ema(v, per)  # default EMA


def stdev(v: List[Num], period: int) -> List[Num]:
    means = sma(v, period)
    n = len(v)
    out: List[Num] = [None] * n
    for i in range(period - 1, n):
        s = 0.0
        m = means[i]
        if m is None:
            continue
        for j in range(period):
            d = (v[i - j] or 0) - m
            s += d * d
        out[i] = (s / period) ** 0.5
    return out


# ================= RSI / QQE =================

def rsi(closes: List[float], p: int) -> List[Num]:
    n = len(closes)
    out: List[Num] = [None] * n
    g = [0.0] * n
    l = [0.0] * n
    for i in range(1, n):
        d = closes[i] - closes[i - 1]
        g[i] = d if d > 0 else 0.0
        l[i] = -d if d < 0 else 0.0
    aG = rma(g, p)
    aL = rma(l, p)
    for i in range(n):
        if aG[i] is None or aL[i] is None:
            continue
        if aL[i] == 0:
            out[i] = 100.0
            continue
        out[i] = 100.0 - 100.0 / (1 + aG[i] / aL[i])
    return out


def qqe(src: List[float], rp: int, sm: int, f: float) -> tuple[List[Num], List[Num]]:
    """Returns (rsiMa, trailing) — 1:1 with JS qqe()."""
    rsi_raw = rsi(src, rp)
    rsi_ma = ema(rsi_raw, sm)
    wl = rp * 2 - 1
    n = len(rsi_ma)
    diff: List[float] = [0.0] * n
    for i in range(1, n):
        if rsi_ma[i] is not None and rsi_ma[i - 1] is not None:
            diff[i] = abs(rsi_ma[i] - rsi_ma[i - 1])  # type: ignore
    dar = rma(rma(diff, wl), wl)
    dar = [None if v is None else v * f for v in dar]

    tr: List[Num] = [None] * n
    for i in range(n):
        if rsi_ma[i] is None or dar[i] is None:
            continue
        pr = tr[i - 1] if i > 0 else None
        prRsi = rsi_ma[i - 1] if i > 0 else None
        up = rsi_ma[i] + dar[i]   # type: ignore
        dn = rsi_ma[i] - dar[i]   # type: ignore
        if pr is None:
            tr[i] = dn if rsi_ma[i] > 50 else up  # type: ignore
            continue
        if rsi_ma[i] > pr and prRsi is not None and prRsi > pr:  # type: ignore
            tr[i] = max(pr, dn)
        elif rsi_ma[i] < pr and prRsi is not None and prRsi < pr:  # type: ignore
            tr[i] = min(pr, up)
        else:
            tr[i] = dn if rsi_ma[i] > pr else up  # type: ignore
    return rsi_ma, tr


# ================= HTF trend (aggregate N M1 bars) =================

def htf_trend(opens, highs, lows, closes, mult: int, ma_period: int, ma_type: str,
              times: Optional[List[int]] = None, tf_seconds: int = 60) -> List[int]:
    """HTF aggregation.

    If `times` is provided, groups are keyed by **absolute wall time** (floor(time/(tf*mult)))
    — stable regardless of where the buffer starts, matches TradingView/real-HUD behavior.
    If `times` is None, falls back to index-based grouping (exact JS 1:1 port).
    """
    n = len(closes)
    htf_closes: List[Optional[float]] = []
    htf_map = [-1] * n

    if times is not None:
        # Time-based grouping: same absolute anchor regardless of buffer slice
        bucket_sec = tf_seconds * mult
        # Remap absolute bucket → dense index (0,1,2,...)
        bucket_to_idx: dict[int, int] = {}
        for i in range(n):
            abs_bucket = times[i] // bucket_sec
            if abs_bucket not in bucket_to_idx:
                bucket_to_idx[abs_bucket] = len(bucket_to_idx)
                htf_closes.append(None)
            g = bucket_to_idx[abs_bucket]
            htf_closes[g] = closes[i]
            htf_map[i] = g
    else:
        for i in range(n):
            group = i // mult
            if len(htf_closes) <= group:
                htf_closes.append(None)
            htf_closes[group] = closes[i]
            htf_map[i] = group

    htf_MA = ma(htf_closes, ma_period, ma_type)
    trend = [0] * n
    for i in range(n):
        hg = htf_map[i]
        if hg < 0 or htf_MA[hg] is None:
            continue
        c = htf_closes[hg]
        # 1:1 parity with JS: `null > number` / `null < number` → both false,
        # so trend[i] simply stays 0. In Python we guard to avoid TypeError.
        if c is None:
            continue
        if c > htf_MA[hg]:
            trend[i] = +1
        elif c < htf_MA[hg]:
            trend[i] = -1
    return trend


# ================= ATR =================

def atr(highs: List[float], lows: List[float], closes: List[float], period: int) -> List[Num]:
    n = len(closes)
    tr = [0.0] * n
    for i in range(1, n):
        h = highs[i]; l = lows[i]; pc = closes[i - 1]
        tr[i] = max(h - l, abs(h - pc), abs(l - pc))
    return rma(tr, period)


# ================= Bollinger =================

def bollinger(closes: List[float], period: int, mult: float):
    basis = sma(closes, period)
    sd = stdev(closes, period)
    n = len(closes)
    upper: List[Num] = [None] * n
    lower: List[Num] = [None] * n
    for i in range(n):
        if basis[i] is None or sd[i] is None:
            continue
        upper[i] = basis[i] + sd[i] * mult  # type: ignore
        lower[i] = basis[i] - sd[i] * mult  # type: ignore
    return {"upper": upper, "lower": lower, "basis": basis}


# ================= Candle alignment =================

def candle_aligned(op: float, cl: float, side: str) -> bool:
    return cl >= op if side == "buy" else cl <= op
