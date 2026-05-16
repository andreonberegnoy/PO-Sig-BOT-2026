"""Render a candle chart with BUY/SELL signals + CONSENSUS stats — same view as po-signals HUD.

Usage:
    png_path = render_chart(candles, params, signal_idx, symbol)
"""

import io
import logging
import re
from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime

from strategy.consensus import generate_signals, analyze, DEFAULT_PARAMS

logger = logging.getLogger(__name__)


def _safe_filename(s: str) -> str:
    """Strip characters that are problematic in filenames or shell paths.
    PO uses '#' for stock prefixes (#AAPL_otc) and '-' for some pairs
    (ADA-USD_otc) — neither is forbidden by Linux but '#' can confuse
    URL-handling libraries and TeX-aware label parsers later in the chain."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", s)


def render_chart(
    candles: list[dict],
    params: dict,
    symbol: str,
    last_bars: int = 80,
    out_path: Optional[str] = None,
    tz_name: str = "Europe/Kyiv",
    journal=None,
) -> str:
    """Render PNG chart and return its path. Uses same buffer + same indicator
    as bot. Time axis is rendered in `tz_name` (default Europe/Kyiv) so the
    chart visually matches PocketOption / PoSignals which show local time.

    Если передан `journal` — стрелки и статы берутся из БД таблицы `signals`
    (immutable), а НЕ пересчитываются через generate_signals() на live-буфере.
    Это устраняет «исчезание» сигналов из-за HTF buffer-relative группировки
    (см. strategy/consensus.py:107-113). Юзер 2026-05-16: «важно чтобы
    статистика не менялась, иначе нет смысла анализировать».
    """
    if not candles or len(candles) < 5:
        raise ValueError(f"render_chart: not enough candles for {symbol} ({len(candles) if candles else 0})")
    if out_path is None:
        out_path = f"/tmp/chart_{_safe_filename(symbol)}_{int(datetime.now().timestamp())}.png"

    merged_params = {**DEFAULT_PARAMS, **(params or {})}
    # Стратегия данных:
    #   1) journal задан И в БД есть сигналы в окне → используем БД (immutable)
    #   2) иначе fallback на generate_signals/analyze (legacy путь)
    db_signals: list[dict] | None = None
    db_stats: dict | None = None
    if journal is not None:
        try:
            buf_end_ts = int(candles[-1]["time"])
            # ВАЖНО: lookback по ВРЕМЕНИ, не по длине буфера. Буфер в памяти
            # может быть короче 1000 баров (свежая подписка после ротации
            # _tracked) — тогда привязка к candles[0].time резала бы
            # видимость старых settled сигналов. БД хранит всю историю.
            tf_sec = 60  # стандарт для PO 1m
            long_bars = int(merged_params.get("statsLookbackBars", 1000))
            recent_bars = int(merged_params.get("recentLookbackBars", 200))
            long_since_ts = buf_end_ts - long_bars * tf_sec
            recent_since_ts = buf_end_ts - recent_bars * tf_sec
            db_signals = journal.signals_in_range(symbol, long_since_ts, buf_end_ts)
            if db_signals:
                exp_bar_idx = int(merged_params.get("expiryBars", 2)) - 1
                exp_bar_idx = max(0, min(4, exp_bar_idx))
                from journal.db import Journal as _J
                db_stats = _J.aggregate_signal_stats(
                    db_signals, exp_bar_idx=exp_bar_idx,
                    recent_since_ts=recent_since_ts,
                )
        except Exception as e:
            logger.warning("chart: DB signals lookup failed for %s: %s", symbol, e)
            db_signals = None
            db_stats = None
    if db_signals is None or db_stats is None:
        # Legacy путь: пересчёт generate_signals/analyze. Может «терять»
        # сигналы при сдвиге буфера (см. docstring выше).
        sigs, _ = generate_signals(candles, merged_params)
        analysis = analyze(candles, merged_params)
    else:
        sigs = None
        analysis = None

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

    # Signal markers — два пути:
    #  (a) DB-источник: маппим signal_ts → ближайший candle index в окне `show`
    #  (b) legacy: используем sigs[].i (вычисленный generate_signals)
    if db_signals is not None:
        # show window: candles[base_idx .. len-1]
        # Бинарный поиск по `times` не нужен — окно 80 баров, линейно дёшево.
        show_t = [int(c["time"]) for c in show]
        for sg in db_signals:
            ts = sg["signal_ts"]
            if ts < show_t[0] or ts > show_t[-1]:
                continue
            # ищем индекс с минимальной |ts - bar.time|
            ci = min(range(len(show_t)), key=lambda j: abs(show_t[j] - ts))
            side_buy = (sg["side"] or "").lower() in ("buy", "call")
            if side_buy:
                ax.annotate("▲",
                    xy=(ci, lows[ci]), xytext=(0, -14), textcoords="offset points",
                    ha="center", color="#22c55e", fontsize=16, fontweight="bold")
            else:
                ax.annotate("▼",
                    xy=(ci, highs[ci]), xytext=(0, 10), textcoords="offset points",
                    ha="center", color="#ef4444", fontsize=16, fontweight="bold")
    else:
        for sig in sigs:
            if sig.i < base_idx: continue
            ci = sig.i - base_idx
            if sig.side == "buy":
                ax.annotate("▲",
                    xy=(ci, lows[ci]), xytext=(0, -14), textcoords="offset points",
                    ha="center", color="#22c55e", fontsize=16, fontweight="bold")
            else:
                ax.annotate("▼",
                    xy=(ci, highs[ci]), xytext=(0, 10), textcoords="offset points",
                    ha="center", color="#ef4444", fontsize=16, fontweight="bold")

    # Title + stats plaque (po-signals.com HUD style, ASCII-safe for matplotlib).
    # Источник статов: db_stats если есть, иначе analysis (fallback).
    if db_stats is not None:
        wr_v = db_stats["wr"]
        wr1_v = db_stats["wr1"]
        wr_recent_v = db_stats["wr_recent"]
        wr1_recent_v = db_stats["wr1_recent"]
        completed = db_stats["completed"]
        wins = db_stats["wins"]; losses = db_stats["losses"]
        completed_recent = db_stats["completed_recent"]
        wins_recent = db_stats["wins_recent"]; losses_recent = db_stats["losses_recent"]
        recent_results = db_stats["recent_results"]
        mls_before = db_stats["max_loss_streak_before_win"]
        mls_overall = db_stats["max_loss_streak_overall"]
    else:
        wr_v = analysis.wr; wr1_v = analysis.wr1
        completed = analysis.completed; wins = analysis.wins; losses = analysis.losses
        completed_recent = analysis.completed_recent
        wins_recent = analysis.wins_recent; losses_recent = analysis.losses_recent
        completed_recent_full = wins_recent + losses_recent
        wr_recent_v = (wins_recent / completed_recent_full * 100) if completed_recent_full else 0
        wr1_recent_v = analysis.wr1_recent
        recent_results = analysis.recent_results
        mls_before = analysis.max_loss_streak_before_win
        mls_overall = analysis.max_loss_streak_overall
    wr_color = "#86feac" if wr_v >= 60 else ("#fdf647" if wr_v >= 53 else "#ffffff")
    long_window = int(merged_params.get("statsLookbackBars", 1000))
    recent_window = int(merged_params.get("recentLookbackBars", 200))
    lines = [
        f"◉ CONSENSUS 4/5    Экспир: {merged_params['expiryBars']} бара",
        f"◆ За {long_window} свечей: {completed} сигналов  ✓{wins} ✗{losses}  WR {wr_v:.0f}%",
        f"◆ За {recent_window} свечей: {completed_recent} сигналов  ✓{wins_recent} ✗{losses_recent}  WR {wr_recent_v:.0f}%",
        f"◆ 1-я сделка: long {wr1_v:.0f}%   recent {wr1_recent_v:.0f}%",
        f"◆ Макс. минусов до ✓: {mls_before}    всего: {mls_overall}",
    ]
    if recent_results:
        last = recent_results[-25:]
        lines.append("")
        lines.append(f"Последние {len(last)} сделок: " + "".join("✓" if x else "✗" for x in last))

    txt = "\n".join(lines)
    # Плашка в ЛЕВОМ верхнем углу (юзер 2026-05-16). Title символа смещён
    # вправо чтобы не перекрываться с плашкой.
    ax.text(0.01, 0.97, txt, transform=ax.transAxes, ha="left", va="top",
            color="#e2e8f0", fontsize=10, family="monospace",
            parse_math=False,
            bbox=dict(boxstyle="round,pad=0.6", facecolor="#111827", edgecolor="#334155", alpha=0.95))

    # parse_math=False keeps '$' '#' '_' as literal chars in matplotlib labels.
    # Without this, e.g. "#AAPL_otc" or "BTC$..." may be parsed as mathtext
    # and either render weirdly or raise "missing $".
    # Title слева (юзер: оставить с левой стороны, выше плашки — title
    # рендерится над plot area так что с плашкой не перекрывается).
    ax.set_title(f"{symbol}  (last {len(show)} bars)", color="#e2e8f0",
                 fontsize=11, loc="left", parse_math=False)
    # Time labels at evenly-spaced indices (every ~10 bars)
    step = max(1, len(show) // 8)
    tick_idx = list(range(0, len(show), step))
    ax.set_xticks(tick_idx)
    ax.set_xticklabels([times[i].strftime("%H:%M") for i in tick_idx])
    # Свечи занимают 4/5 ширины графика, 1/5 — пустое пространство справа
    # (юзер 2026-05-16). Формула: total = N + pad, candles_share = N/total = 4/5
    # → pad = N/4.
    right_pad = len(show) * 0.25
    ax.set_xlim(-1, len(show) + right_pad)
    plt.xticks(rotation=0)
    plt.tight_layout()
    plt.savefig(out_path, facecolor=fig.get_facecolor())
    plt.close(fig)
    return out_path
