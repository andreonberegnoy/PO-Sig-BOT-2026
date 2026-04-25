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
) -> str:
    """Render PNG chart and return its path. Uses same buffer + same indicator as bot."""
    if out_path is None:
        out_path = f"/tmp/chart_{symbol}_{int(datetime.now().timestamp())}.png"

    merged_params = {**DEFAULT_PARAMS, **(params or {})}
    sigs, _ = generate_signals(candles, merged_params)
    analysis = analyze(candles, merged_params)

    show = candles[-last_bars:]
    base_idx = len(candles) - len(show)

    times = [datetime.utcfromtimestamp(c["time"]) for c in show]
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

    # Candles
    width = 0.0006
    for i, t in enumerate(times):
        op, cl, hi, lo = opens[i], closes[i], highs[i], lows[i]
        color = "#22c55e" if cl >= op else "#ef4444"
        edge = color
        ax.plot([t, t], [lo, hi], color=edge, linewidth=0.8)
        # body
        low_y = min(op, cl); high_y = max(op, cl)
        ax.fill_between([mdates.date2num(t)-0.0003, mdates.date2num(t)+0.0003],
                        low_y, high_y, color=color, edgecolor=edge, linewidth=0.5)

    # Signal markers — only arrows, no labels
    for sig in sigs:
        if sig.i < base_idx: continue
        ci = sig.i - base_idx
        t = times[ci]
        if sig.side == "buy":
            ax.annotate(
                "▲",
                xy=(t, lows[ci]), xytext=(0, -14), textcoords="offset points",
                ha="center", color="#22c55e", fontsize=16, fontweight="bold",
            )
        else:
            ax.annotate(
                "▼",
                xy=(t, highs[ci]), xytext=(0, 10), textcoords="offset points",
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
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    plt.xticks(rotation=0)
    plt.tight_layout()
    plt.savefig(out_path, facecolor=fig.get_facecolor())
    plt.close(fig)
    return out_path
