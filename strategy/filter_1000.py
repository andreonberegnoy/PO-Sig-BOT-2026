"""Pair filter: runs CONSENSUS over 1000 M1 candles per pair and classifies it.

Rules from spec:
  ≤ max_losses_in_row (обычно 3) минусов подряд → пара ДОПУСТИМА
  2 или 1 минус подряд до плюса → пара в ПРИОРИТЕТЕ (меньше догонов = лучше)
  > max_losses_in_row → пара в БАН на ban_hours (длительный бан, дефолт 12ч)
  WR1 long (% первой плюсовой сделки за 1000 свечей) < min_wr1 → SKIP
  WR1 recent (% за последние 200 свечей) < min_wr1_recent → ПАУЗА на
    pause_minutes (короткая пауза, дефолт 60 мин). После истечения пара авто-
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


# ─── Asset category detection ────────────────────────────────────────────
# PO doesn't expose a clean type field, so we classify by symbol patterns.
# Used by filter.asset_categories to let user choose which classes to trade.
_CRYPTO_TOKENS = frozenset({
    "BTC", "ETH", "LTC", "XRP", "ADA", "DOT", "LINK", "BCH", "SOL",
    "DOGE", "BNB", "TRX", "AVAX", "MATIC", "MNR", "TON", "ATOM",
})
_INDICES = frozenset({"VIX", "SPX", "NDX", "DJI", "DAX", "FTSE", "NIKKEI"})
_METALS = frozenset({"XAU", "XAG", "XPT", "XPD"})
# Stocks/ETFs that PO lists WITHOUT the # prefix (most have #, but some don't)
_STOCK_TICKERS = frozenset({
    "AMZN", "AAPL", "TSLA", "MSFT", "GOOGL", "META", "NFLX", "NVDA",
    "BITB", "MARA", "PLTR", "VISA", "FB", "MCD", "AXP", "JPM", "BA",
    "JNJ", "KO", "PFE", "WMT", "INTC", "CSCO", "ORCL", "IBM",
})


def categorize_symbol(sym: str, asset_info: Optional[dict] = None) -> str:
    """High-level asset class for a PO symbol. Returns one of:
    forex, crypto, stocks, indices, commodities, other.

    Used by `filter.asset_categories` whitelist so user can trade only e.g.
    forex+crypto and skip stocks/indices.
    """
    s = sym.replace("_otc", "").replace("-", "").upper()
    if s.startswith("#") or s.startswith("$"):
        return "stocks"
    if s in _STOCK_TICKERS:
        return "stocks"
    # Commodities (XAUUSD = gold, XAGUSD = silver)
    if s[:3] in _METALS:
        return "commodities"
    if s in _INDICES:
        return "indices"
    # Crypto: known crypto base ticker (BTC, ETH, etc.)
    if s[:3] in _CRYPTO_TOKENS:
        return "crypto"
    # 6-letter all-alpha = forex (EURUSD, GBPJPY, etc.)
    if len(s) == 6 and s.isalpha():
        return "forex"
    return "other"


# Canonical list of categories — used as available options in UI
ASSET_CATEGORIES = ["forex", "crypto", "stocks", "indices", "commodities", "other"]


@dataclass
class PairScore:
    symbol: str
    payout: int
    allowed: bool                     # допустима для торговли
    priority: int                     # чем меньше — тем лучше (0 = идеал)
    ban: bool                         # → длительный бан (ban_hours, дефолт 12ч)
    max_loss_streak: int
    max_loss_streak_before_win: int
    # Long window (statsLookbackBars, дефолт 1000 свечей)
    wins: int
    losses: int
    completed: int
    wr: float
    wr1: float = 0.0                  # WR первой сделки в long окне
    # Recent window (recentLookbackBars, дефолт 200 свечей) — мини-копия
    wins_recent: int = 0
    losses_recent: int = 0
    completed_recent: int = 0
    wr_recent: float = 0.0            # общий WR в recent окне
    wr1_recent: float = 0.0           # WR первой сделки в recent окне
    reason: str = ""
    # Short pause (pause_minutes, дефолт 60 мин): для пар которые провалили recent
    # WR1 фильтр. После истечения автоматически переоцениваются на следующем
    # _rescan_pairs (раз в час). Если снова не пройдут — снова на паузу.
    pause: bool = False
    # Per-pair «временная пауза» (этап 3+): срабатывает когда ОБЕ
    # проходимости (общая wr1 за 1000 свечей И за последние 200) ниже
    # порогов одновременно. Дольше короткой паузы (60 мин), но без
    # учёта payout. Хранится в bans с сроком из cfg.filter.temp_pause_hours.
    temp_pause: bool = False


def classify(
    symbol: str,
    payout: int,
    candles: list[dict],
    params: dict,
    max_losses_in_row: int,
    min_wr1: float = 0.0,
    min_wr1_recent: float = 0.0,
    journal=None,
) -> PairScore:
    """Классифицирует пару: allowed / ban / pause / temp_pause + статистика.

    Если задан `journal` — статы (max_loss_streak, wins/losses/wr/wr1) берутся
    из таблицы `signals` (immutable, см. `journal.signals_in_range` +
    `aggregate_signal_stats`). Это решает проблему HTF buffer-relative repaint
    в `analyze(candles)` где одни и те же бары могут давать 4/5 ↔ 3/5 в
    зависимости от offset → max_loss_streak плясал → фильтр пропускал пары
    которые ДОЛЖЕН был забанить (юзер 2026-05-17: YERUSD_otc реально дала
    3 LOSS подряд, classify видел 0 сигналов и не банила при max_losses=2).

    Fallback на analyze() если journal=None или в БД пусто (новая пара).
    """
    # ── Источник статистики ──
    used_db = False
    if journal is not None and candles and len(candles) >= 100:
        try:
            buf_end_ts = int(candles[-1]["time"])
            long_bars = int(params.get("statsLookbackBars", 1000))
            recent_bars = int(params.get("recentLookbackBars", 200))
            tf_sec = 60
            long_since_ts = buf_end_ts - long_bars * tf_sec
            recent_since_ts = buf_end_ts - recent_bars * tf_sec
            db_signals = journal.signals_in_range(symbol, long_since_ts, buf_end_ts)
            if db_signals:
                from journal.db import Journal as _J
                exp_bar_idx = max(0, min(4, int(params.get("expiryBars", 2)) - 1))
                s = _J.aggregate_signal_stats(
                    db_signals, exp_bar_idx=exp_bar_idx,
                    recent_since_ts=recent_since_ts,
                )
                # Адаптер под старый интерфейс Analysis (поля используются ниже)
                class _A: pass
                a = _A()
                a.completed = s["completed"]
                a.wins = s["wins"]; a.losses = s["losses"]
                a.wr = s["wr"]; a.wr1 = s["wr1"]
                a.wins_recent = s["wins_recent"]; a.losses_recent = s["losses_recent"]
                a.completed_recent = s["completed_recent"]
                a.wr1_recent = s["wr1_recent"]
                a.max_loss_streak_overall = s["max_loss_streak_overall"]
                a.max_loss_streak_before_win = s["max_loss_streak_before_win"]
                used_db = True
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "classify: DB-based stats failed for %s, fallback to analyze(): %s: %s",
                symbol, type(e).__name__, e,
            )
    if not used_db:
        # Fallback: пересчёт через analyze() (HTF buffer-relative — нестабилен)
        a = analyze(candles, params)

    # Compute recent-window WR (общий, не только первая сделка)
    completed_recent_full = a.wins_recent + a.losses_recent
    wr_recent = (a.wins_recent / completed_recent_full * 100.0) if completed_recent_full else 0.0
    score = PairScore(
        symbol=symbol,
        payout=payout,
        allowed=False, priority=999, ban=False,
        max_loss_streak=a.max_loss_streak_overall,
        max_loss_streak_before_win=a.max_loss_streak_before_win,
        wins=a.wins, losses=a.losses, completed=a.completed, wr=a.wr,
        wr1=round(a.wr1, 1),
        wins_recent=a.wins_recent, losses_recent=a.losses_recent,
        completed_recent=a.completed_recent,
        wr_recent=round(wr_recent, 1),
        wr1_recent=round(a.wr1_recent, 1),
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

    # ── Проверка проходимостей (этап 3+) ──
    # Раздельно: ОБЩАЯ ПРОХОДИМОСТЬ (за 1000 свечей) и ПРОХОДИМОСТЬ
    # ПОСЛЕДНИХ СВЕЧЕЙ (200). Решение зависит от того сколько провалено:
    #   ОБЕ < min       → temp_pause (per-pair, ~6ч в bans)
    #   только wr1      → skip (не торгуем, но в bans не кладём)
    #   только wr1_recent → pause (короткая 60-мин в bans)
    long_fail = (min_wr1 > 0 and a.wr1 < min_wr1)
    recent_fail = (min_wr1_recent > 0 and a.completed_recent >= 3
                    and a.wr1_recent < min_wr1_recent)

    if long_fail and recent_fail:
        score.temp_pause = True
        score.reason = (
            f"ВРЕМЕННАЯ ПАУЗА: общая проходимость {a.wr1:.0f}% < {min_wr1:.0f}% "
            f"И проходимость последних {a.wr1_recent:.0f}% < {min_wr1_recent:.0f}%"
        )
        return score
    if long_fail:
        score.reason = (
            f"общая проходимость {a.wr1:.0f}% < {min_wr1:.0f}% — пара не торгуется"
        )
        return score
    if recent_fail:
        score.pause = True
        score.reason = (
            f"проходимость последних свечей {a.wr1_recent:.0f}% < {min_wr1_recent:.0f}% "
            f"→ короткая пауза (плохая форма последних {a.completed_recent} сделок)"
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
    journal=None,
) -> dict[str, PairScore]:
    """Fetch 1000 M1 candles for each qualifying asset, run CONSENSUS, classify.

    Если задан `journal` — classify() возьмёт статы из БД signals
    (immutable), что устраняет HTF buffer-relative repaint в max_loss_streak.
    """
    f_cfg = cfg["filter"]
    ind_cfg = dict(cfg["indicator"])
    # honour stats_lookback_bars from filter config so stats window matches site
    if "stats_lookback_bars" in f_cfg:
        ind_cfg["statsLookbackBars"] = f_cfg["stats_lookback_bars"]
    min_payout = f_cfg["min_payout"]
    # Отдельный payout-порог для не-OTC пар (обычные Forex/stocks). Если не задан
    # — fallback на min_payout. Это позволяет включить обычные пары в analytics-pool
    # с более низкой выплатой (обычно у них payout ниже чем у OTC).
    min_payout_regular = int(f_cfg.get("min_payout_regular", min_payout) or min_payout)
    max_losses = f_cfg["max_losses_in_row"]
    min_wr1 = float(f_cfg.get("min_wr1", 0) or 0)
    min_wr1_recent = float(f_cfg.get("min_wr1_recent", 0) or 0)
    # Asset category whitelist: empty = all allowed; ["forex","crypto"] = only those
    allowed_cats = set(f_cfg.get("asset_categories") or [])
    # honour recent_lookback_bars from filter config so consensus uses same window
    if "recent_lookback_bars" in f_cfg:
        ind_cfg["recentLookbackBars"] = f_cfg["recent_lookback_bars"]
    limit = f_cfg["history_candles"]
    tf = f_cfg["tf"]

    # Pick candidates from assets. Берём ОБА типа пар (OTC + regular) — узкий
    # фильтр по trade_mode применяется уже на этапе ОТКРЫТИЯ сделки в
    # state_machine, не здесь. Это даёт broad pool для analytics: signals
    # пишутся по обоим типам всегда (см. _record_signals_phase).
    def _passes_payout(info):
        threshold = min_payout if info.get("is_otc") else min_payout_regular
        return info["payout"] >= threshold
    candidates = symbols or [
        s for s, info in feed.assets.items()
        if _passes_payout(info)
        and (not allowed_cats or categorize_symbol(s, info) in allowed_cats)
    ]
    cat_msg = f", categories={sorted(allowed_cats)}" if allowed_cats else ""
    payout_msg = (f"min_payout=OTC:{min_payout}%/regular:{min_payout_regular}%"
                  if min_payout != min_payout_regular else f"min_payout={min_payout}%")
    logger.info("scanning %d candidate pairs (%s%s)",
                len(candidates), payout_msg, cat_msg)

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
            score = classify(sym, payout, candles, ind_cfg, max_losses,
                              min_wr1, min_wr1_recent, journal=journal)
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
