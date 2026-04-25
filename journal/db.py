"""SQLite journal for trades + bans + daily stats.

Tables:
  trades      — каждая закрытая сделка
  bans        — пары с >max_losses_in_row, срок истечения
  sessions    — запуски бота (для отчётов 24ч)
  state_kv    — произвольные key/value для восстановления
"""

import json
import logging
import sqlite3
import time
from dataclasses import asdict
from typing import Any, Optional

logger = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id      TEXT UNIQUE,
    symbol        TEXT NOT NULL,
    action        TEXT NOT NULL,       -- call | put
    amount        REAL NOT NULL,
    profit        REAL NOT NULL,
    result        TEXT NOT NULL,       -- WIN | LOSS | DRAW
    payout        INTEGER,
    mg_step       INTEGER DEFAULT 0,   -- шаг мартингейла (0 = базовый)
    open_ts       INTEGER,
    close_ts      INTEGER,
    balance_after REAL,
    mode          TEXT NOT NULL        -- paper | real
);
CREATE INDEX IF NOT EXISTS idx_trades_close_ts ON trades(close_ts);
CREATE INDEX IF NOT EXISTS idx_trades_symbol    ON trades(symbol);

CREATE TABLE IF NOT EXISTS bans (
    symbol      TEXT PRIMARY KEY,
    banned_at   INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    reason      TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  INTEGER NOT NULL,
    ended_at    INTEGER,
    mode        TEXT,
    start_balance REAL,
    end_balance   REAL
);

CREATE TABLE IF NOT EXISTS state_kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
"""


class Journal:
    def __init__(self, path: str):
        import os
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------- trades ----------

    def log_trade(self, trade: dict):
        cols = ("trade_id","symbol","action","amount","profit","result","payout",
                "mg_step","open_ts","close_ts","balance_after","mode")
        placeholders = ",".join("?" * len(cols))
        row = tuple(trade.get(c) for c in cols)
        self.conn.execute(
            f"INSERT OR REPLACE INTO trades ({','.join(cols)}) VALUES ({placeholders})", row)
        self.conn.commit()

    def trades_since(self, ts: int) -> list[sqlite3.Row]:
        self.conn.row_factory = sqlite3.Row
        cur = self.conn.execute(
            "SELECT * FROM trades WHERE close_ts >= ? ORDER BY close_ts", (ts,))
        return cur.fetchall()

    # ---------- bans ----------

    def ban(self, symbol: str, hours: int, reason: str = ""):
        now = int(time.time())
        expires = now + hours * 3600
        self.conn.execute(
            "INSERT OR REPLACE INTO bans (symbol, banned_at, expires_at, reason) VALUES (?,?,?,?)",
            (symbol, now, expires, reason))
        self.conn.commit()

    def is_banned(self, symbol: str) -> bool:
        now = int(time.time())
        cur = self.conn.execute(
            "SELECT 1 FROM bans WHERE symbol=? AND expires_at>?", (symbol, now))
        return cur.fetchone() is not None

    def active_bans(self) -> list[tuple[str, int]]:
        now = int(time.time())
        cur = self.conn.execute(
            "SELECT symbol, expires_at FROM bans WHERE expires_at > ? ORDER BY expires_at", (now,))
        return cur.fetchall()

    def prune_bans(self):
        self.conn.execute("DELETE FROM bans WHERE expires_at < ?", (int(time.time()),))
        self.conn.commit()

    # ---------- session ----------

    def start_session(self, mode: str, balance: float) -> int:
        cur = self.conn.execute(
            "INSERT INTO sessions (started_at, mode, start_balance) VALUES (?,?,?)",
            (int(time.time()), mode, balance))
        self.conn.commit()
        return cur.lastrowid

    def end_session(self, session_id: int, balance: float):
        self.conn.execute(
            "UPDATE sessions SET ended_at=?, end_balance=? WHERE id=?",
            (int(time.time()), balance, session_id))
        self.conn.commit()

    # ---------- key/value state ----------

    def set(self, key: str, val: Any):
        self.conn.execute(
            "INSERT OR REPLACE INTO state_kv (k, v, updated_at) VALUES (?,?,?)",
            (key, json.dumps(val), int(time.time())))
        self.conn.commit()

    def get(self, key: str, default=None):
        cur = self.conn.execute("SELECT v FROM state_kv WHERE k=?", (key,))
        row = cur.fetchone()
        return json.loads(row[0]) if row else default

    def delete(self, key: str):
        self.conn.execute("DELETE FROM state_kv WHERE k=?", (key,))
        self.conn.commit()

    # ---------- reports ----------

    def daily_summary(self, since_ts: int, mode: str) -> dict:
        self.conn.row_factory = sqlite3.Row
        cur = self.conn.execute(
            """SELECT result, SUM(profit) as total_profit, SUM(amount) as total_amount, COUNT(*) as n
               FROM trades WHERE close_ts>=? AND mode=? GROUP BY result""",
            (since_ts, mode))
        rows = cur.fetchall()
        stat = {r["result"]: r for r in rows}
        wins   = stat.get("WIN",  {"n":0,"total_profit":0})["n"]
        losses = stat.get("LOSS", {"n":0,"total_profit":0})["n"]
        draws  = stat.get("DRAW", {"n":0,"total_profit":0})["n"]

        # Net PnL
        cur = self.conn.execute(
            "SELECT COALESCE(SUM(profit),0) - COALESCE(SUM(amount),0) FROM trades WHERE close_ts>=? AND mode=? AND result='LOSS'",
            (since_ts, mode))
        # Simpler: profit field is payout - amount on win; 0 on loss; so net = sum(profit) - sum(amount_where_loss)
        # But to be safe: compute net as sum(profit) for wins minus sum(amount) for losses
        cur = self.conn.execute(
            "SELECT COALESCE(SUM(profit),0) FROM trades WHERE close_ts>=? AND mode=? AND result='WIN'",
            (since_ts, mode))
        win_gain = cur.fetchone()[0] or 0
        cur = self.conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM trades WHERE close_ts>=? AND mode=? AND result='LOSS'",
            (since_ts, mode))
        loss_cost = cur.fetchone()[0] or 0
        net = win_gain - loss_cost

        # Pair switches — count distinct symbol blocks in chronological order
        cur = self.conn.execute(
            "SELECT symbol FROM trades WHERE close_ts>=? AND mode=? ORDER BY close_ts",
            (since_ts, mode))
        syms = [r["symbol"] for r in cur.fetchall()]
        switches = sum(1 for i in range(1, len(syms)) if syms[i] != syms[i-1])

        # Max consecutive losses
        cur = self.conn.execute(
            "SELECT result FROM trades WHERE close_ts>=? AND mode=? ORDER BY close_ts",
            (since_ts, mode))
        results = [r["result"] for r in cur.fetchall()]
        max_streak = cur_streak = 0
        for r in results:
            if r == "LOSS":
                cur_streak += 1
                max_streak = max(max_streak, cur_streak)
            else:
                cur_streak = 0

        # Active bans count
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM bans WHERE banned_at>=?", (since_ts,))
        bans_24h = cur.fetchone()[0]

        total = wins + losses + draws
        wr = wins / (wins + losses) * 100 if (wins + losses) else 0
        return {
            "wins": wins, "losses": losses, "draws": draws, "total": total,
            "net_profit": round(net, 2), "win_rate": round(wr, 1),
            "pair_switches": switches, "max_loss_streak": max_streak,
            "bans_24h": bans_24h,
        }

    def close(self):
        try: self.conn.close()
        except Exception: pass
