"""SQLite persistence for M1 candles. Survives bot restarts / redeploys.

Schema (v2):
    candles(symbol TEXT, period INT, time INT, is_demo INT,
            open REAL, high REAL, low REAL, close REAL, volume REAL)
    PRIMARY KEY (symbol, period, time, is_demo)

`is_demo` separates demo/real data for the same pair (PO has different
quote streams for the two modes — mixing them produces wrong CONSENSUS
results when switching between accounts).

On first load with v2 schema, any pre-existing v1 data (without is_demo
column) is dropped because we cannot infer its true mode reliably.
"""

import logging
import os
import sqlite3
import threading
from typing import Optional

logger = logging.getLogger(__name__)


class CandlesDB:
    def __init__(self, path: str = "data/candles.db"):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS candles (
                symbol TEXT NOT NULL,
                period INTEGER NOT NULL,
                time   INTEGER NOT NULL,
                is_demo INTEGER NOT NULL DEFAULT 1,
                open   REAL, high REAL, low REAL, close REAL, volume REAL,
                PRIMARY KEY (symbol, period, time, is_demo)
            )
        """)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_candles_lookup "
            "ON candles(symbol, period, is_demo, time DESC)"
        )

    def _migrate(self):
        """Drop legacy v1 candles table (no is_demo column) — its data may be
        a mix of demo and real that we can't safely separate."""
        try:
            cur = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='candles'"
            )
            row = cur.fetchone()
            if row and row[0] and "is_demo" not in row[0]:
                logger.warning("dropping legacy candles table (no is_demo column)")
                self._conn.execute("DROP TABLE candles")
        except Exception:
            logger.exception("schema migration failed (proceeding)")

    def save(self, symbol: str, period: int, candle: dict, is_demo: bool = True):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO candles"
                "(symbol, period, time, is_demo, open, high, low, close, volume) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (symbol, int(period), int(candle["time"]), 1 if is_demo else 0,
                 float(candle["open"]), float(candle["high"]), float(candle["low"]),
                 float(candle["close"]), float(candle.get("volume", 0) or 0)),
            )

    def save_many(self, symbol: str, period: int, candles: list[dict], is_demo: bool = True):
        if not candles:
            return
        flag = 1 if is_demo else 0
        rows = [(symbol, int(period), int(c["time"]), flag,
                 float(c["open"]), float(c["high"]), float(c["low"]),
                 float(c["close"]), float(c.get("volume", 0) or 0))
                for c in candles]
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO candles"
                "(symbol, period, time, is_demo, open, high, low, close, volume) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def load(self, symbol: str, period: int, limit: int = 1060,
             is_demo: bool = True) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT time, open, high, low, close, volume FROM candles "
                "WHERE symbol=? AND period=? AND is_demo=? ORDER BY time DESC LIMIT ?",
                (symbol, int(period), 1 if is_demo else 0, int(limit)),
            )
            rows = cur.fetchall()
        rows.reverse()
        return [
            {"time": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]}
            for r in rows
        ]

    def last_time(self, symbol: str, period: int, is_demo: bool = True) -> Optional[int]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT MAX(time) FROM candles WHERE symbol=? AND period=? AND is_demo=?",
                (symbol, int(period), 1 if is_demo else 0),
            )
            row = cur.fetchone()
        return row[0] if row and row[0] is not None else None

    def prune(self, symbol: str, period: int, is_demo: bool = True, keep_latest: int = 2000):
        flag = 1 if is_demo else 0
        with self._lock:
            self._conn.execute(
                "DELETE FROM candles WHERE symbol=? AND period=? AND is_demo=? "
                "AND time NOT IN (SELECT time FROM candles "
                "WHERE symbol=? AND period=? AND is_demo=? "
                "ORDER BY time DESC LIMIT ?)",
                (symbol, int(period), flag, symbol, int(period), flag, int(keep_latest)),
            )

    def close(self):
        try: self._conn.close()
        except Exception: pass
