"""Pair filter: runs CONSENSUS over 1000 M1 candles per pair and classifies it.

Rules from spec:
  ≤ max_losses_in_row (обычно 3) минусов подряд → пара ДОПУСТИМА
  2 или 1 минус подряд до плюса → пара в ПРИОРИТЕТЕ (меньше догонов = лучше)
  > max_losses_in_row → пара в БАН на ban_hours (длительный бан, дефолт 12ч)
  WR1 long (% первой плюсовой сделки за 1000 свечей) < min_wr1 → SKIP
  WR1 recent (% за последние 200 свечей) < min_wr1_recent → ПАУЗА на
    pause_hours (короткая пауза, дефолт 1ч). После истечения пара авто-
    переоценивается на следующем _rescan_pairs.
    (требует ≥3 сделок в окне, иначе нет данных — просто SKIP)

Три уровня "не торговать":
  • SKIP    — мало сигналов или другая причина, повтор каждый scan
  • PAUSE   — короткий перерыв (1ч), пара пере-классифицируется автоматом
  • BAN     — длительный бан (12ч), пара плохая системно

Returns classified dict: {symbol: PairScore}
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from strategy.consensus import analyze, DEFAULT_PARAMS
from feed.history import fetch_candles

logger = logging.getLogger(__name__)


@dataclass
class PairScore:
    symbol: str
    payout: int
    allowed: bool                     # допустима для торговли
    priority: int                     # чем меньше — тем лучше (0 = идеал)
    ban: bool                         # → длительный бан (ban_hours, дефолт 12ч)
    max_loss_streak: int
    max_loss_streak_before_win: int
    wins: int
    losses: int
    completed: int
    wr: float
    reason: str = ""
    # Short pause (pause_hours, дефолт 1ч): для пар которые провалили recent
    # WR1 фильтр. После истечения автоматически переоцениваются на следующем
    # _rescan_pairs (раз в час). Если снова не пройдут — снова на паузу.
    pause: bool = False


def classify(
    symbol: str,
    payout: int,
    candles: list[dict],
    params: dict,
    max_losses_in_row: int,
    min_wr1: float = 0.0,
    min_wr1_recent: float = 0.0,
) -> PairScore:
    a = analyze(candles, params)
    score = PairScore(
        symbol=symbol,
        payout=payout,
        allowed=False, priority=999, ban=False,
        max_loss_streak=a.max_loss_streak_overall,
        max_loss_streak_before_win=a.max_loss_streak_before_win,
        wins=a.wins, losses=a.losses, completed=a.completed, wr=a.wr,
    )
    # Not enough signals → skip but don't ban
    if a.completed < 5:
        score.reason = f"слишком мало сделок ({a.completed})"
        return score

    # Any streak > max_losses_in_row → BAN
    if a.max_loss_streak_overall > max_losses_in_row:
        score.ban = True
        score.reason = f"макс. минусов подряд {a.max_loss_streak_overall} > {max_losses_in_row} → бан"
        return score

    # First-trade win rate filter — pair must historically win the first trade
    # (no martingale needed) at least `min_wr1`% of the time. Otherwise skip
    # (not banned — it might recover; just unattractive vs alternatives).
    if min_wr1 > 0 and a.wr1 < min_wr1:
        score.reason = (
            f"WR1 {a.wr1:.0f}% < {min_wr1:.0f}% — низкая проходимость первой сделки"
        )
        return score

    # Recent-form filter — even if long-term WR1 is good, pair must also be
    # performing well in the last N bars (recentLookbackBars, default 200).
    # Catches pairs that historically passed but recently degraded.
    # Failing this filter → PAUSE (short ban, default 1h) instead of full ban.
    # After pause expires the pair is auto-rechecked on next _rescan_pairs.
    if min_wr1_recent > 0 and a.completed_recent >= 3 and a.wr1_recent < min_wr1_recent:
        score.pause = True
        score.reason = (
            f"WR1 recent {a.wr1_recent:.0f}% < {min_wr1_recent:.0f}% → пауза "
            f"(плохая форма последних {a.completed_recent} сделок, переоценка через час)"
        )
        return score

    # Allowed. Priority: fewer consecutive losses before a win = better
    score.allowed = True
    # priority 0 = never had more than 1 loss before win; higher = worse
    score.priority = max(0, a.max_loss_streak_before_win)
    score.reason = (
        f"✓ макс. минусов до +: {a.max_loss_streak_before_win} | "
        f"всего сделок {a.completed} | WR {a.wr:.0f}%"
    )
    return score


async def scan_all_pairs(
    feed,
    cfg: dict,
    symbols: Optional[list[str]] = None,
) -> dict[str, PairScore]:
    """Fetch 1000 M1 candles for each qualifying asset, run CONSENSUS, classify."""
    f_cfg = cfg["filter"]
    ind_cfg = dict(cfg["indicator"])
    # honour stats_lookback_bars from filter config so stats window matches site
    if "stats_lookback_bars" in f_cfg:
        ind_cfg["statsLookbackBars"] = f_cfg["stats_lookback_bars"]
    min_payout = f_cfg["min_payout"]
    max_losses = f_cfg["max_losses_in_row"]
    min_wr1 = float(f_cfg.get("min_wr1", 0) or 0)
    min_wr1_recent = float(f_cfg.get("min_wr1_recent", 0) or 0)
    # honour recent_lookback_bars from filter config so consensus uses same window
    if "recent_lookback_bars" in f_cfg:
        ind_cfg["recentLookbackBars"] = f_cfg["recent_lookback_bars"]
    limit = f_cfg["history_candles"]
    tf = f_cfg["tf"]

    # Pick candidates from assets
    candidates = symbols or [
        s for s, info in feed.assets.items()
        if info["payout"] >= min_payout and info["is_otc"]  # OTC only for weekend-stable trading
    ]
    logger.info("scanning %d candidate pairs (min_payout=%d%%)", len(candidates), min_payout)

    scores: dict[str, PairScore] = {}
    # Run fetches concurrently but bounded
    sem = asyncio.Semaphore(5)

    async def work(sym: str):
        async with sem:
            info = feed.assets.get(sym, {})
            payout = int(info.get("payout", 0))
            try:
                candles = await fetch_candles(feed, sym, period=tf, limit=limit)
            except Exception as e:
                logger.warning("history error %s: %s", sym, e)
                return
            if len(candles) < 200:
                logger.info("skip %s — only %d candles", sym, len(candles))
                return
            score = classify(sym, payout, candles, ind_cfg, max_losses, min_wr1, min_wr1_recent)
            scores[sym] = score

    await asyncio.gather(*[work(s) for s in candidates])

    allowed = [s for s in scores.values() if s.allowed]
    banned  = [s for s in scores.values() if s.ban]
    logger.info("scan done: %d allowed, %d banned, %d total", len(allowed), len(banned), len(scores))
    return scores


def pick_best(scores: dict[str, PairScore], exclude: set[str] = frozenset()) -> Optional[PairScore]:
    """Pick most-promising allowed pair: lowest priority, then highest payout."""
    cand = [s for s in scores.values() if s.allowed and s.symbol not in exclude]
    if not cand:
        return None
    cand.sort(key=lambda s: (s.priority, -s.payout))
    return cand[0]
