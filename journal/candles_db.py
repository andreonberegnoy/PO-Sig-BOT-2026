"""SQLite persistence for M1 candles. Survives bot restarts / redeploys.

Schema:
    candles(symbol TEXT, period INT, time INT, open REAL, high REAL, low REAL, close REAL, volume REAL)
    PRIMARY KEY (symbol, period, time)

Usage:
    db = CandlesDB("journal/candles.db")
    db.save_many("EURUSD_otc", 60, [{time, open, high, low, close, volume}, ...])
    candles = db.load("EURUSD_otc", 60, limit=1060)
"""

import logging
import sqlite3
import threading
from typing import Optional

logger = logging.getLogger(__name__)


class CandlesDB:
    def __init__(self, path: str = "data/candles.db"):
        import os
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS candles (
                symbol TEXT NOT NULL,
                period INTEGER NOT NULL,
                time   INTEGER NOT NULL,
                open   REAL, high REAL, low REAL, close REAL, volume REAL,
                PRIMARY KEY (symbol, period, time)
            )
        """)
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_candles_lookup ON candles(symbol, period, time DESC)")

    def save(self, symbol: str, period: int, candle: dict):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO candles(symbol, period, time, open, high, low, close, volume) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (symbol, int(period), int(candle["time"]),
                 float(candle["open"]), float(candle["high"]), float(candle["low"]),
                 float(candle["close"]), float(candle.get("volume", 0) or 0)),
            )

    def save_many(self, symbol: str, period: int, candles: list[dict]):
        if not candles:
            return
        rows = [(symbol, int(period), int(c["time"]),
                 float(c["open"]), float(c["high"]), float(c["low"]),
                 float(c["close"]), float(c.get("volume", 0) or 0))
                for c in candles]
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO candles(symbol, period, time, open, high, low, close, volume) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def load(self, symbol: str, period: int, limit: int = 1060) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT time, open, high, low, close, volume FROM candles "
                "WHERE symbol=? AND period=? ORDER BY time DESC LIMIT ?",
                (symbol, int(period), int(limit)),
            )
            rows = cur.fetchall()
        rows.reverse()   # ascending time
        return [
            {"time": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]}
            for r in rows
        ]

    def last_time(self, symbol: str, period: int) -> Optional[int]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT MAX(time) FROM candles WHERE symbol=? AND period=?",
                (symbol, int(period)),
            )
            row = cur.fetchone()
        return row[0] if row and row[0] is not None else None

    def prune(self, symbol: str, period: int, keep_latest: int = 2000):
        """Keep only `keep_latest` most-recent bars per pair to bound DB size."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM candles WHERE symbol=? AND period=? AND time NOT IN "
                "(SELECT time FROM candles WHERE symbol=? AND period=? ORDER BY time DESC LIMIT ?)",
                (symbol, int(period), symbol, int(period), int(keep_latest)),
            )

    def close(self):
        try: self._conn.close()
        except Exception: pass
