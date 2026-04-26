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

-- Periodic snapshots of payout per asset (every ~5 min, only when value changed)
CREATE TABLE IF NOT EXISTS payout_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol    TEXT NOT NULL,
    ts        INTEGER NOT NULL,
    payout    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payout_log_sym_ts ON payout_log(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_payout_log_ts     ON payout_log(ts);

-- Periodic backtest snapshots (every ~1 hour) of consensus indicator on the
-- current candle buffer for each tracked pair.
CREATE TABLE IF NOT EXISTS pair_stats_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    ts              INTEGER NOT NULL,
    bars_in_window  INTEGER NOT NULL,
    signals_total   INTEGER NOT NULL,
    wins            INTEGER NOT NULL,
    losses          INTEGER NOT NULL,
    wr              REAL NOT NULL,
    wr1             REAL NOT NULL,
    max_streak      INTEGER NOT NULL,
    max_streak_overall INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pair_stats_sym_ts ON pair_stats_log(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_pair_stats_ts     ON pair_stats_log(ts);
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

    # ---------- payout log ----------

    def log_payout(self, symbol: str, payout: int, ts: Optional[int] = None):
        """Append a payout snapshot. Caller is expected to throttle (skip if
        unchanged from previous snapshot) to keep the table small."""
        self.conn.execute(
            "INSERT INTO payout_log (symbol, ts, payout) VALUES (?,?,?)",
            (symbol, int(ts or time.time()), int(payout)),
        )
        self.conn.commit()

    def last_payout(self, symbol: str) -> Optional[int]:
        cur = self.conn.execute(
            "SELECT payout FROM payout_log WHERE symbol=? ORDER BY ts DESC LIMIT 1",
            (symbol,),
        )
        r = cur.fetchone()
        return int(r[0]) if r else None

    def _hour_filter_sql(self, hour_filter: Optional[tuple],
                         tz_offset_sec: int) -> tuple[str, list]:
        """Returns (sql_clause, params) to filter rows by local hour-of-day.
        hour_filter = (start, end) ints in [0,24]. None → no filter."""
        if not hour_filter:
            return "", []
        start, end = int(hour_filter[0]), int(hour_filter[1])
        # SQL: extract hour after applying timezone offset
        hour_expr = f"CAST(strftime('%H', ts + {int(tz_offset_sec)}, 'unixepoch') AS INTEGER)"
        if start <= end:
            return f" AND {hour_expr} >= ? AND {hour_expr} < ?", [start, end]
        else:
            # window crosses midnight (e.g. 22..6) — OR logic
            return f" AND ({hour_expr} >= ? OR {hour_expr} < ?)", [start, end]

    def payout_aggregate(self, since_ts: int, min_payout: int = 92,
                         floor_payout: int = 85,
                         hour_filter: Optional[tuple] = None,
                         tz_offset_sec: int = 0) -> list[dict]:
        """For each symbol, aggregate over [since_ts, now]:
          - n_snapshots, avg_payout, min_payout, max_payout
          - pct_above_min, pct_above_floor
          - first_seen_ts, last_seen_ts
        If hour_filter=(start,end), only rows whose local hour is in window.
        """
        self.conn.row_factory = sqlite3.Row
        hour_sql, hour_params = self._hour_filter_sql(hour_filter, tz_offset_sec)
        cur = self.conn.execute(
            f"""SELECT symbol,
                      COUNT(*)               AS n,
                      AVG(payout)            AS avg_p,
                      MIN(payout)            AS min_p,
                      MAX(payout)            AS max_p,
                      MIN(ts)                AS first_ts,
                      MAX(ts)                AS last_ts,
                      SUM(CASE WHEN payout >= ? THEN 1 ELSE 0 END) AS n_above_min,
                      SUM(CASE WHEN payout >= ? THEN 1 ELSE 0 END) AS n_above_floor
                 FROM payout_log
                WHERE ts >= ?{hour_sql}
             GROUP BY symbol""",
            [min_payout, floor_payout, since_ts] + hour_params,
        )
        out = []
        for r in cur.fetchall():
            n = int(r["n"]) or 1
            out.append({
                "symbol": r["symbol"],
                "n_snapshots": int(r["n"]),
                "avg_payout": float(r["avg_p"] or 0),
                "min_payout": int(r["min_p"] or 0),
                "max_payout": int(r["max_p"] or 0),
                "first_ts":   int(r["first_ts"] or 0),
                "last_ts":    int(r["last_ts"] or 0),
                "pct_above_min":   round(100.0 * (r["n_above_min"] or 0) / n, 1),
                "pct_above_floor": round(100.0 * (r["n_above_floor"] or 0) / n, 1),
            })
        self.conn.row_factory = None
        return out

    # ---------- winning-trade payouts (per pair) ----------

    def win_payout_aggregate(self, since_ts: int, mode: str,
                             after_loss_only: bool = True) -> list[dict]:
        """For each symbol, aggregate the payout of the WIN trade that closed
        a martingale cycle. By default only counts WINs that came AFTER ≥1
        loss in the cycle (mg_step > 0) — these are 'recovered' wins where
        the user's question 'with what payout did it finally close +' applies.

        Returns per-symbol: min_win_payout, avg_win_payout, last_win_payout,
        n_recovered_wins."""
        self.conn.row_factory = sqlite3.Row
        cond = "result='WIN'" + (" AND mg_step > 0" if after_loss_only else "")
        cur = self.conn.execute(
            f"""SELECT symbol,
                       COUNT(*) AS n,
                       MIN(payout) AS min_p,
                       AVG(payout) AS avg_p,
                       MAX(payout) AS max_p
                  FROM trades
                 WHERE close_ts >= ? AND mode = ? AND {cond}
              GROUP BY symbol""",
            (since_ts, mode),
        )
        out = {}
        for r in cur.fetchall():
            out[r["symbol"]] = {
                "n_recovered_wins": int(r["n"] or 0),
                "min_win_payout":   int(r["min_p"] or 0),
                "avg_win_payout":   round(float(r["avg_p"] or 0), 1),
                "max_win_payout":   int(r["max_p"] or 0),
            }
        # Last winning payout per symbol (most recent close_ts)
        cur = self.conn.execute(
            f"""SELECT t.symbol, t.payout, t.close_ts
                  FROM trades t
                  JOIN (SELECT symbol, MAX(close_ts) AS mts
                          FROM trades
                         WHERE close_ts >= ? AND mode = ? AND {cond}
                      GROUP BY symbol) m
                    ON m.symbol = t.symbol AND m.mts = t.close_ts
                 WHERE t.mode = ?""",
            (since_ts, mode, mode),
        )
        for r in cur.fetchall():
            sym = r["symbol"]
            if sym in out:
                out[sym]["last_win_payout"] = int(r["payout"] or 0)
        self.conn.row_factory = None
        return [{"symbol": s, **v} for s, v in out.items()]

    # ---------- pair stats log ----------

    def log_pair_stats(self, symbol: str, snapshot: dict, ts: Optional[int] = None):
        """Append a backtest snapshot from consensus.analyze on current buffer."""
        self.conn.execute(
            """INSERT INTO pair_stats_log
               (symbol, ts, bars_in_window, signals_total, wins, losses,
                wr, wr1, max_streak, max_streak_overall)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                symbol,
                int(ts or time.time()),
                int(snapshot.get("bars", 0)),
                int(snapshot.get("signals_total", 0)),
                int(snapshot.get("wins", 0)),
                int(snapshot.get("losses", 0)),
                float(snapshot.get("wr", 0.0)),
                float(snapshot.get("wr1", 0.0)),
                int(snapshot.get("max_streak", 0)),
                int(snapshot.get("max_streak_overall", 0)),
            ),
        )
        self.conn.commit()

    def pair_stats_aggregate(self, since_ts: int,
                             hour_filter: Optional[tuple] = None,
                             tz_offset_sec: int = 0) -> list[dict]:
        """For each symbol, aggregate backtest snapshots over [since_ts, now]:
          - n_snapshots, avg_wr, avg_wr1, avg_signals, max_max_streak
          - last_wr, last_wr1, last_max_streak (most recent snapshot)
        If hour_filter set, only snapshots whose local hour is in window.
        """
        self.conn.row_factory = sqlite3.Row
        hour_sql, hour_params = self._hour_filter_sql(hour_filter, tz_offset_sec)
        cur = self.conn.execute(
            f"""SELECT symbol,
                      COUNT(*) AS n,
                      AVG(wr) AS avg_wr,
                      AVG(wr1) AS avg_wr1,
                      AVG(signals_total) AS avg_signals,
                      MAX(max_streak_overall) AS max_max_streak,
                      MIN(ts) AS first_ts,
                      MAX(ts) AS last_ts
                 FROM pair_stats_log
                WHERE ts >= ?{hour_sql}
             GROUP BY symbol""",
            [since_ts] + hour_params,
        )
        out = {}
        for r in cur.fetchall():
            out[r["symbol"]] = {
                "symbol": r["symbol"],
                "n_snapshots": int(r["n"]),
                "avg_wr":      round(float(r["avg_wr"] or 0), 1),
                "avg_wr1":     round(float(r["avg_wr1"] or 0), 1),
                "avg_signals": round(float(r["avg_signals"] or 0), 1),
                "max_max_streak": int(r["max_max_streak"] or 0),
                "first_ts":    int(r["first_ts"] or 0),
                "last_ts":     int(r["last_ts"] or 0),
            }
        # Add the most-recent snapshot per symbol for "last" fields
        cur = self.conn.execute(
            f"""SELECT p.symbol, p.wr, p.wr1, p.max_streak, p.max_streak_overall, p.bars_in_window, p.ts
                 FROM pair_stats_log p
                 JOIN (SELECT symbol, MAX(ts) AS mts FROM pair_stats_log
                       WHERE ts >= ?{hour_sql} GROUP BY symbol) m
                  ON m.symbol = p.symbol AND m.mts = p.ts""",
            [since_ts] + hour_params,
        )
        for r in cur.fetchall():
            sym = r["symbol"]
            if sym in out:
                out[sym].update({
                    "last_wr":         round(float(r["wr"]), 1),
                    "last_wr1":        round(float(r["wr1"]), 1),
                    "last_max_streak": int(r["max_streak"]),
                    "last_bars":       int(r["bars_in_window"]),
                })
        self.conn.row_factory = None
        return list(out.values())

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
