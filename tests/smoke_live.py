"""Live smoke test: connect to Chrome, fetch 1000 M1 candles, analyze with our port.
Output stats should match what the site HUD shows for the same pair."""

import asyncio
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

from feed.po_feed import PoFeed
from feed.history import fetch_candles
from strategy.consensus import analyze, DEFAULT_PARAMS

async def main():
    feed = PoFeed(mode="real")
    await feed.connect()

    # Pick first OTC pair with payout >= 92
    wanted = ["EURUSD_otc", "EURJPY_otc", "AUDNZD_otc", "AUDCHF_otc"]
    top_pairs = [(s, feed.assets[s]) for s in wanted if s in feed.assets]
    print(f"\nPairs to verify: {[(s, a['payout']) for s, a in top_pairs]}")

    params = {**DEFAULT_PARAMS, "statsLookbackBars": 1000, "recentLookbackBars": 200}
    fetch_limit = 1060
    for sym, info in top_pairs:
        print(f"\n--- {sym} (payout {info['payout']}%) ---")
        candles = await fetch_candles(feed, sym, period=60, limit=fetch_limit)
        candles = candles[-fetch_limit:]
        print(f"  got {len(candles)} candles (lookback window = last 1000)")
        if len(candles) < 500:
            continue
        a = analyze(candles, params)
        print(f"  signals={len(a.signals)} completed={a.completed} wins={a.wins} losses={a.losses}")
        print(f"  WR total={a.wr:.0f}%   WR first-trade={a.wr1:.0f}%")
        print(f"  max losses in row  overall={a.max_loss_streak_overall}  before-win={a.max_loss_streak_before_win}")
        last_n = a.recent_results[-30:]
        print(f"  last {len(last_n)}: {''.join('✓' if x else '✗' for x in last_n)}")

    await feed.close()

if __name__ == "__main__":
    asyncio.run(main())
