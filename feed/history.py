"""Historical candle fetch — hits /api/po/candles/{symbol}?period=60&limit=1050.

Returns list of dicts [{time, open, high, low, close, volume}, ...]
"""

import logging
from typing import Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)


async def fetch_candles(feed, symbol: str, period: int = 60, limit: int = 1050) -> list[dict]:
    path = f"/api/po/candles/{quote(symbol, safe='')}?period={period}&limit={limit}"
    data = await feed.http_get_json(path)
    if not data or (isinstance(data, dict) and data.get("__error")):
        logger.warning("history fetch failed for %s: %s", symbol, data)
        return []

    # PO returns either list of lists or list of dicts — normalize.
    raw = data.get("candles", data) if isinstance(data, dict) else data
    out: list[dict] = []
    for c in raw:
        if isinstance(c, (list, tuple)) and len(c) >= 5:
            out.append({
                "time": int(c[0]),
                "open": float(c[1]),
                "high": float(c[2]),
                "low":  float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5]) if len(c) > 5 else 0.0,
            })
        elif isinstance(c, dict):
            out.append({
                "time": int(c.get("time") or c.get("t") or 0),
                "open": float(c.get("open") or c.get("o") or 0),
                "high": float(c.get("high") or c.get("h") or 0),
                "low":  float(c.get("low") or c.get("l") or 0),
                "close": float(c.get("close") or c.get("c") or 0),
                "volume": float(c.get("volume") or c.get("v") or 0),
            })
    out.sort(key=lambda x: x["time"])
    return out
