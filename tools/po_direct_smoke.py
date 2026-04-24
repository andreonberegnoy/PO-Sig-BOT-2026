"""Smoke test for feed/po_direct.py — connects, subscribes to EURUSD_otc,
aggregates ticks into M1 candles, prints a few candle updates."""

import asyncio
import os
import sys

sys.path.insert(0, ".")
from feed.po_direct import PoDirectFeed


def load_env():
    env = {}
    try:
        with open(".env", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    for k in env:
        os.environ.setdefault(k, env[k])
    return env


async def main():
    load_env()
    ssid = os.environ["PO_SSID"]
    uid = int(os.environ["PO_UID"])
    ws_url = os.environ.get("PO_WS_URL", "wss://api-eu.po.market/socket.io/?EIO=4&transport=websocket")

    feed = PoDirectFeed(ssid=ssid, uid=uid, is_demo=False, ws_url=ws_url, verify_ssl=False)

    tick_count = 0
    def on_tick(sym, period, candle):
        nonlocal tick_count
        tick_count += 1
        if tick_count % 5 == 0:
            print(f"  tick #{tick_count} {sym} P{period} bar@{candle['time']} OHLC={candle['open']:.5f}/{candle['high']:.5f}/{candle['low']:.5f}/{candle['close']:.5f}")
    feed.on_tick = on_tick

    def on_assets(assets):
        good = [s for s, a in assets.items() if a.get("open") and int(a.get("payout", 0)) >= 85]
        print(f"  assets update: total={len(assets)}, tradable & payout>=85: {len(good)}")
    feed.on_assets_update = on_assets

    await feed.connect()
    print(f"connected. balance_demo={feed.balance_demo} balance_real={feed.balance_real} assets={len(feed.assets)}")

    await feed.subscribe("EURUSD_otc", 60)
    print("subscribed to EURUSD_otc M1. Watching 15 seconds...")

    await asyncio.sleep(15)

    candles = feed.get_candles("EURUSD_otc", 60)
    print(f"\nFinal: {tick_count} ticks processed, {len(candles)} M1 candles in buffer")
    if candles:
        print(f"last candle: {candles[-1]}")

    await feed.close()


if __name__ == "__main__":
    asyncio.run(main())
