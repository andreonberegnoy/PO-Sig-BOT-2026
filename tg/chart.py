"""Render a candle chart with BUY/SELL signals + CONSENSUS stats — same view as po-signals HUD.

Usage:
    png_path = render_chart(candles, params, signal_idx, symbol)
"""

import io
import logging
from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime

from strategy.consensus import generate_signals, analyze, DEFAULT_PARAMS

logger = logging.getLogger(__name__)


def render_chart(
    candles: list[dict],
    params: dict,
    symbol: str,
    last_bars: int = 80,
    out_path: Optional[str] = None,
    tz_name: str = "Europe/Kyiv",
) -> str:
    """Render PNG chart and return its path. Uses same buffer + same indicator
    as bot. Time axis is rendered in `tz_name` (default Europe/Kyiv) so the
    chart visually matches PocketOption / PoSignals which show local time."""
    if out_path is None:
        out_path = f"/tmp/chart_{symbol}_{int(datetime.now().timestamp())}.png"

    merged_params = {**DEFAULT_PARAMS, **(params or {})}
    sigs, _ = generate_signals(candles, merged_params)
    analysis = analyze(candles, merged_params)

    show = candles[-last_bars:]
    base_idx = len(candles) - len(show)

    try:
        import pytz
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = None
    def _to_local(ts: int):
        dt = datetime.utcfromtimestamp(ts).replace(tzinfo=pytz.utc) if tz else datetime.utcfromtimestamp(ts)
        return dt.astimezone(tz) if tz else dt
    times = [_to_local(c["time"]) for c in show]
    opens = [c["open"] for c in show]
    highs = [c["high"] for c in show]
    lows = [c["low"] for c in show]
    closes = [c["close"] for c in show]

    fig, ax = plt.subplots(figsize=(12, 6), dpi=110, facecolor="#0b1220")
    ax.set_facecolor("#0b1220")
    for s in ax.spines.values():
        s.set_color("#334155")
    ax.tick_params(colors="#94a3b8", labelsize=8)
    ax.grid(True, color="#1e293b", linewidth=0.5, alpha=0.6)

    # Index-based x-axis (slot 0..N-1) so missing minutes don't create visual
    # gaps — matches PocketOption / PoSignals visual layout. Time labels get
    # mapped from index → wallclock time below.
    xs = list(range(len(show)))
    half_w = 0.35
    for i in xs:
        op, cl, hi, lo = opens[i], closes[i], highs[i], lows[i]
        color = "#22c55e" if cl >= op else "#ef4444"
        ax.plot([i, i], [lo, hi], color=color, linewidth=0.8)
        low_y = min(op, cl); high_y = max(op, cl)
        ax.fill_between([i - half_w, i + half_w], low_y, high_y,
                        color=color, edgecolor=color, linewidth=0.5)

    # Signal markers
    for sig in sigs:
        if sig.i < base_idx: continue
        ci = sig.i - base_idx
        if sig.side == "buy":
            ax.annotate(
                "▲",
                xy=(ci, lows[ci]), xytext=(0, -14), textcoords="offset points",
                ha="center", color="#22c55e", fontsize=16, fontweight="bold",
            )
        else:
            ax.annotate(
                "▼",
                xy=(ci, highs[ci]), xytext=(0, 10), textcoords="offset points",
                ha="center", color="#ef4444", fontsize=16, fontweight="bold",
            )

    # Title + stats plaque (po-signals.com HUD style, ASCII-safe for matplotlib)
    wr_color = "#86feac" if analysis.wr >= 60 else ("#fdf647" if analysis.wr >= 53 else "#ffffff")
    lines = [
        f"◉ CONSENSUS 4/5    Экспир: {merged_params['expiryBars']} бара",
        f"◆ Общая: {analysis.wr:.0f}%    ✓ {analysis.wins} : ✗ {analysis.losses}",
        f"◆ 1-я сделка: {analysis.wr1:.0f}%",
        f"◆ Макс. минусов до ✓: {analysis.max_loss_streak_before_win}    всего: {analysis.max_loss_streak_overall}",
    ]
    if analysis.recent_results:
        last = analysis.recent_results[-25:]
        lines.append("")
        lines.append(f"Последние {len(last)}: " + "".join("✓" if x else "✗" for x in last))

    txt = "\n".join(lines)
    ax.text(0.99, 0.97, txt, transform=ax.transAxes, ha="right", va="top",
            color="#e2e8f0", fontsize=10, family="monospace",
            bbox=dict(boxstyle="round,pad=0.6", facecolor="#111827", edgecolor="#334155", alpha=0.95))

    ax.set_title(f"{symbol}  (last {len(show)} bars)", color="#e2e8f0", fontsize=11, loc="left")
    # Time labels at evenly-spaced indices (every ~10 bars)
    step = max(1, len(show) // 8)
    tick_idx = list(range(0, len(show), step))
    ax.set_xticks(tick_idx)
    ax.set_xticklabels([times[i].strftime("%H:%M") for i in tick_idx])
    # Extend right side so the last candle sits at ~2/3 of the chart width
    # (1/3 of empty space to the right) — matches PocketOption/PoSignals layout
    # where future space is reserved for the next-bar projection.
    right_pad = len(show) * 0.5
    ax.set_xlim(-1, len(show) + right_pad)
    plt.xticks(rotation=0)
    plt.tight_layout()
    plt.savefig(out_path, facecolor=fig.get_facecolor())
    plt.close(fig)
    return out_path
