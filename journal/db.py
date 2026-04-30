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

-- Этап 2: signals — каждый CONSENSUS (или другой стратегии) сигнал
-- на tracked-паре. Пишется ВСЕГДА, независимо вошёл бот или нет.
-- Если entered=1 — связан с trades через trade_id.
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name   TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,           -- call | put
    signal_ts       INTEGER NOT NULL,        -- close-time бара сигнала
    entry_close     REAL NOT NULL,
    entered         INTEGER NOT NULL DEFAULT 0,
    trade_id        TEXT,
    exp_wins        TEXT,                    -- JSON [w1..w5] (1=win,0=loss,null=draw)
    settled_at      INTEGER,
    -- голоса CONSENSUS (или эквивалент)
    votes_rsi       INTEGER,
    votes_htf       INTEGER,
    votes_vol       INTEGER,
    votes_bb        INTEGER,
    votes_candle    INTEGER,
    votes_total     INTEGER,
    -- индикаторы на момент сигнала
    rsi_ma          REAL,
    qqe_trailing    REAL,
    htf_value       INTEGER,
    atr14_1m        REAL,
    atr_avg         REAL,
    atr_ratio       REAL,
    bb_upper        REAL,
    bb_lower        REAL,
    bb_position     REAL,
    candle_body     REAL,
    candle_atr_ratio REAL,
    candle_direction INTEGER,
    -- контекст
    hour_local      INTEGER,
    day_of_week     INTEGER,
    payout_at_signal INTEGER,
    wr1_long_at_signal REAL,
    wr1_recent_at_signal REAL,
    UNIQUE(symbol, signal_ts, strategy_name)
);
CREATE INDEX IF NOT EXISTS idx_signals_strat   ON signals(strategy_name);
CREATE INDEX IF NOT EXISTS idx_signals_symts   ON signals(symbol, signal_ts);
CREATE INDEX IF NOT EXISTS idx_signals_pending ON signals(settled_at, signal_ts);
CREATE INDEX IF NOT EXISTS idx_signals_entered ON signals(entered);
CREATE INDEX IF NOT EXISTS idx_signals_hour    ON signals(hour_local);
CREATE INDEX IF NOT EXISTS idx_signals_dow     ON signals(day_of_week);
"""


# Колонки snapshot которые collector передаёт в insert_signal — порядок жёсткий.
_SIGNAL_COLS = (
    "strategy_name", "symbol", "side", "signal_ts", "entry_close",
    "entered", "trade_id",
    "votes_rsi", "votes_htf", "votes_vol", "votes_bb", "votes_candle", "votes_total",
    "rsi_ma", "qqe_trailing", "htf_value",
    "atr14_1m", "atr_avg", "atr_ratio",
    "bb_upper", "bb_lower", "bb_position",
    "candle_body", "candle_atr_ratio", "candle_direction",
    "hour_local", "day_of_week", "payout_at_signal",
    "wr1_long_at_signal", "wr1_recent_at_signal",
)


class Journal:
    def __init__(self, path: str):
        import os
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._migrate_columns()

    def _migrate_columns(self):
        """Idempotent schema migrations. Safe to run on existing DBs.

        Этап 1 рефакторинга — drop старых analytics-таблиц если есть.
        Колонка trades.exp_wins остаётся (SQLite не поддерживает удобный
        DROP COLUMN в старых версиях; данные просто перестают использоваться).
        Этап 2 добавит таблицу `signals` с расширенными полями.
        """
        try:
            for tbl in ("payout_log", "pair_stats_log", "virtual_signals"):
                try:
                    self.conn.execute(f"DROP TABLE IF EXISTS {tbl}")
                except Exception:
                    pass
            self.conn.commit()
        except Exception:
            pass

    # ---------- trades ----------

    def log_trade(self, trade: dict):
        cols = ("trade_id","symbol","action","amount","profit","result","payout",
                "mg_step","open_ts","close_ts","balance_after","mode")
        placeholders = ",".join("?" * len(cols))
        row = tuple(trade.get(c) for c in cols)
        self.conn.execute(
            f"INSERT OR REPLACE INTO trades ({','.join(cols)}) VALUES ({placeholders})", row)
        self.conn.commit()

    # ---------- signals (Stage 2) ----------

    def insert_signal(self, snap: dict) -> bool:
        """INSERT OR IGNORE — UNIQUE(symbol, signal_ts, strategy_name) защищает
        от дублей при повторных проходах по тому же закрытому бару.
        Returns True если вставлено, False если уже было."""
        row = tuple(snap.get(c) for c in _SIGNAL_COLS)
        placeholders = ",".join("?" * len(_SIGNAL_COLS))
        cur = self.conn.execute(
            f"INSERT OR IGNORE INTO signals ({','.join(_SIGNAL_COLS)}) VALUES ({placeholders})",
            row,
        )
        self.conn.commit()
        return cur.rowcount > 0

    def mark_signal_entered(self, symbol: str, signal_ts: int, strategy_name: str,
                             trade_id: str):
        """После открытия сделки — связать ту строку signals с trade_id."""
        self.conn.execute(
            "UPDATE signals SET entered=1, trade_id=? "
            "WHERE symbol=? AND signal_ts=? AND strategy_name=?",
            (trade_id, symbol, signal_ts, strategy_name),
        )
        self.conn.commit()

    def pending_signals_to_settle(self, before_ts: int, limit: int = 200) -> list[sqlite3.Row]:
        """Сигналы без exp_wins, у которых сигнальный бар + 5 баров уже в прошлом."""
        self.conn.row_factory = sqlite3.Row
        cur = self.conn.execute(
            "SELECT id, symbol, side, signal_ts, entry_close FROM signals "
            "WHERE settled_at IS NULL AND signal_ts <= ? "
            "ORDER BY signal_ts LIMIT ?",
            (before_ts, limit),
        )
        return cur.fetchall()

    def settle_signal(self, signal_id: int, exp_wins: list):
        import json as _json
        self.conn.execute(
            "UPDATE signals SET exp_wins=?, settled_at=? WHERE id=?",
            (_json.dumps(exp_wins), int(time.time()), signal_id),
        )
        self.conn.commit()

    def signals_since(self, ts: int, strategy_name: Optional[str] = None) -> list[sqlite3.Row]:
        self.conn.row_factory = sqlite3.Row
        if strategy_name:
            cur = self.conn.execute(
                "SELECT * FROM signals WHERE signal_ts >= ? AND strategy_name = ? ORDER BY signal_ts",
                (ts, strategy_name),
            )
        else:
            cur = self.conn.execute(
                "SELECT * FROM signals WHERE signal_ts >= ? ORDER BY signal_ts", (ts,))
        return cur.fetchall()

    def signals_retention_cleanup(self, keep_days: int) -> int:
        """Удаляет signals старше keep_days. Возвращает число удалённых строк."""
        cutoff = int(time.time()) - keep_days * 86400
        cur = self.conn.execute("DELETE FROM signals WHERE signal_ts < ?", (cutoff,))
        self.conn.commit()
        return cur.rowcount or 0

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
