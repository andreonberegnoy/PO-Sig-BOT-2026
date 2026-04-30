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

    def signals_db_size_bytes(self) -> int:
        """Грубая оценка размера signals — page_count × page_size. Для UI."""
        try:
            import os
            return os.path.getsize(self.path)
        except Exception:
            return 0

    # ---------- analytics aggregations (Stage 2) ----------

    def _signals_filtered(self, since_ts: int, strategy_name: Optional[str],
                          hour_from: Optional[int], hour_to: Optional[int],
                          dow: Optional[list]) -> list[sqlite3.Row]:
        """Загружает signals с фильтрами. hour_from/to inclusive (0..23)."""
        self.conn.row_factory = sqlite3.Row
        sql = ["SELECT * FROM signals WHERE signal_ts >= ?"]
        args: list = [since_ts]
        if strategy_name:
            sql.append("AND strategy_name = ?")
            args.append(strategy_name)
        if hour_from is not None and hour_to is not None:
            if hour_from <= hour_to:
                sql.append("AND hour_local BETWEEN ? AND ?")
                args.extend([hour_from, hour_to])
            else:
                # окно через полночь: [from..23] ∪ [0..to]
                sql.append("AND (hour_local >= ? OR hour_local <= ?)")
                args.extend([hour_from, hour_to])
        if dow:
            placeholders = ",".join("?" * len(dow))
            sql.append(f"AND day_of_week IN ({placeholders})")
            args.extend(dow)
        sql.append("ORDER BY signal_ts")
        cur = self.conn.execute(" ".join(sql), tuple(args))
        return cur.fetchall()

    def _trades_for_signals(self, trade_ids: list[str]) -> dict[str, sqlite3.Row]:
        """Подтягивает trades по списку trade_id (для entered-сигналов)."""
        if not trade_ids:
            return {}
        self.conn.row_factory = sqlite3.Row
        out: dict[str, sqlite3.Row] = {}
        # batched IN
        BATCH = 500
        for i in range(0, len(trade_ids), BATCH):
            chunk = trade_ids[i:i + BATCH]
            placeholders = ",".join("?" * len(chunk))
            cur = self.conn.execute(
                f"SELECT * FROM trades WHERE trade_id IN ({placeholders})", chunk)
            for r in cur.fetchall():
                out[r["trade_id"]] = r
        return out

    def analytics_aggregate(self,
                             since_ts: int,
                             strategy_name: Optional[str] = None,
                             hour_from: Optional[int] = None,
                             hour_to: Optional[int] = None,
                             dow: Optional[list] = None,
                             expiry_bars_default: int = 2,
                             group_by_hour: bool = False,
                             only_symbol: Optional[str] = None) -> list[dict]:
        """Главная агрегационная функция аналитики. Возвращает список dict
        per symbol (или per (symbol, hour) если group_by_hour=True).
        Все WR-метрики используют exp_wins (post-settlement); profit — trades.

        Колонки результата соответствуют плану рефакторинга.
        """
        rows = self._signals_filtered(since_ts, strategy_name, hour_from, hour_to, dow)
        if only_symbol:
            rows = [r for r in rows if r["symbol"] == only_symbol]
        # подтягиваем trades для entered-сигналов
        trade_ids = [r["trade_id"] for r in rows if r["trade_id"]]
        trades_by_id = self._trades_for_signals(trade_ids)

        # группировка
        groups: dict = {}
        for r in rows:
            key = (r["symbol"], r["hour_local"]) if group_by_hour else r["symbol"]
            groups.setdefault(key, []).append(r)

        ebd = max(1, min(5, int(expiry_bars_default or 2)))
        out: list[dict] = []
        for key, items in groups.items():
            symbol = key[0] if group_by_hour else key
            hour = key[1] if group_by_hour else None

            n_total = len(items)
            n_entered = sum(1 for r in items if r["entered"])

            # exp_wins-based аналитика (settled-only)
            settled = [r for r in items if r["exp_wins"]]
            import json as _json
            ew = []
            for r in settled:
                try:
                    ew.append(_json.loads(r["exp_wins"]))
                except Exception:
                    pass

            def _wr(arr_idx: int):
                total = wins = 0
                for w in ew:
                    if arr_idx >= len(w):
                        continue
                    v = w[arr_idx]
                    if v is None:
                        continue
                    total += 1
                    if v == 1:
                        wins += 1
                return (wins / total * 100.0) if total else None

            wr_first  = _wr(0)
            wr_chosen = _wr(ebd - 1)

            # best-exp WR: signal считается plus если ХОТЯ БЫ один w==1
            best_wins = 0
            best_total = 0
            pluses = 0
            minuses = 0
            for w in ew:
                non_null = [x for x in w if x is not None]
                if not non_null:
                    continue
                best_total += 1
                if any(x == 1 for x in non_null):
                    best_wins += 1
                    pluses += 1
                else:
                    minuses += 1
            wr_best = (best_wins / best_total * 100.0) if best_total else None

            # max loss streak до плюса (по экспирации ebd-1 — пользовательской)
            cur_streak = 0
            max_streak = 0
            for w in ew:
                if (ebd - 1) >= len(w):
                    continue
                v = w[ebd - 1]
                if v == 0:
                    cur_streak += 1
                    if cur_streak > max_streak:
                        max_streak = cur_streak
                elif v == 1:
                    cur_streak = 0

            # payout — оптимальное окно 85..92
            payouts = [r["payout_at_signal"] for r in items if r["payout_at_signal"] is not None]
            avg_payout = sum(payouts) / len(payouts) if payouts else None
            in_optimal = sum(1 for p in payouts if 85 <= p <= 92)
            pct_payout_optimal = (in_optimal / len(payouts) * 100.0) if payouts else None

            # market metrics — средние
            def _avg(field):
                vals = [r[field] for r in items if r[field] is not None]
                return (sum(vals) / len(vals)) if vals else None

            avg_atr_ratio       = _avg("atr_ratio")
            avg_bb_position     = _avg("bb_position")
            avg_candle_atr      = _avg("candle_atr_ratio")
            avg_votes_total     = _avg("votes_total")
            avg_rsi_ma          = _avg("rsi_ma")
            avg_qqe_trail       = _avg("qqe_trailing")
            avg_wr1_long        = _avg("wr1_long_at_signal")
            avg_wr1_recent      = _avg("wr1_recent_at_signal")

            # profit — только entered AND WIN
            wins_real = 0
            losses_real = 0
            profit_real = 0.0
            for r in items:
                tid = r["trade_id"]
                if not tid:
                    continue
                t = trades_by_id.get(tid)
                if not t:
                    continue
                if t["result"] == "WIN":
                    wins_real += 1
                    profit_real += float(t["profit"]) - float(t["amount"])
                elif t["result"] == "LOSS":
                    losses_real += 1
                    profit_real -= float(t["amount"])
            wr_real = (wins_real / (wins_real + losses_real) * 100.0) if (wins_real + losses_real) else None

            row = {
                "symbol": symbol,
                "hour": hour,
                "signals": n_total,
                "entered": n_entered,
                "settled": len(ew),
                "wr_first":  None if wr_first is None else round(wr_first, 1),
                "wr_chosen": None if wr_chosen is None else round(wr_chosen, 1),
                "wr_best":   None if wr_best is None else round(wr_best, 1),
                "pluses": pluses,
                "minuses": minuses,
                "max_loss_streak_to_win": max_streak,
                "avg_payout": None if avg_payout is None else round(avg_payout, 1),
                "pct_payout_optimal": None if pct_payout_optimal is None else round(pct_payout_optimal, 1),
                "avg_votes_total": None if avg_votes_total is None else round(avg_votes_total, 2),
                "avg_atr_ratio": None if avg_atr_ratio is None else round(avg_atr_ratio, 3),
                "avg_bb_position": None if avg_bb_position is None else round(avg_bb_position, 3),
                "avg_candle_atr_ratio": None if avg_candle_atr is None else round(avg_candle_atr, 3),
                "avg_rsi_ma": None if avg_rsi_ma is None else round(avg_rsi_ma, 2),
                "avg_qqe_trailing": None if avg_qqe_trail is None else round(avg_qqe_trail, 2),
                "avg_wr1_long": None if avg_wr1_long is None else round(avg_wr1_long, 1),
                "avg_wr1_recent": None if avg_wr1_recent is None else round(avg_wr1_recent, 1),
                "wins_real": wins_real,
                "losses_real": losses_real,
                "wr_real": None if wr_real is None else round(wr_real, 1),
                "profit_real": round(profit_real, 2),
            }
            out.append(row)

        # сортировка
        if group_by_hour:
            out.sort(key=lambda r: (r["symbol"], r["hour"] if r["hour"] is not None else -1))
        else:
            out.sort(key=lambda r: (-r["signals"], r["symbol"]))
        return out

    def profit_today(self, since_ts: int, mode: str) -> dict:
        """Сумма реального profit за сутки (только entered=1 трейды).
        Возвращает {"profit": float, "trades": int, "wins": int, "losses": int}."""
        self.conn.row_factory = sqlite3.Row
        cur = self.conn.execute(
            "SELECT result, amount, profit FROM trades "
            "WHERE close_ts >= ? AND mode = ?",
            (since_ts, mode),
        )
        wins = losses = 0
        prof = 0.0
        for r in cur.fetchall():
            if r["result"] == "WIN":
                wins += 1
                prof += float(r["profit"]) - float(r["amount"])
            elif r["result"] == "LOSS":
                losses += 1
                prof -= float(r["amount"])
        return {
            "profit": round(prof, 2),
            "trades": wins + losses,
            "wins": wins,
            "losses": losses,
        }

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
