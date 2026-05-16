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

-- Strategy snapshots: версионированные «слепки» аналитических находок.
-- Каждый snapshot — bundle настроек фильтра, выведенный из анализа БД на дату X.
-- Только один может быть active=1 одновременно. При активации значения из
-- filter_config накладываются поверх settings_overrides (с сохранением
-- backup_config для отката). Signals collector работает независимо — продолжает
-- писать ВСЕ детектируемые сигналы, не зависит от снимков.
CREATE TABLE IF NOT EXISTS strategy_snapshots (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL UNIQUE,
    description       TEXT,                          -- markdown: что нашли, почему такие правки
    filter_config     TEXT NOT NULL,                 -- JSON {dotted_key: value}
    backup_config     TEXT,                          -- JSON: то что было в overrides до активации
    stats_at_creation TEXT,                          -- JSON: общий WR/N на момент создания
    source_data_until INTEGER,                       -- unix-ts: последний trade включённый в анализ
    active            INTEGER NOT NULL DEFAULT 0,    -- 0/1
    created_at        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_active ON strategy_snapshots(active);
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

    def insert_signal(self, snap: dict) -> int:
        """INSERT OR IGNORE — UNIQUE(symbol, signal_ts, strategy_name) защищает
        от дублей при повторных проходах по тому же закрытому бару.
        Returns rowid вставленной строки, либо 0 если был дубликат.
        Падает на 0 = falsy для совместимости с прежними `if inserted:` проверками."""
        row = tuple(snap.get(c) for c in _SIGNAL_COLS)
        placeholders = ",".join("?" * len(_SIGNAL_COLS))
        cur = self.conn.execute(
            f"INSERT OR IGNORE INTO signals ({','.join(_SIGNAL_COLS)}) VALUES ({placeholders})",
            row,
        )
        self.conn.commit()
        return cur.lastrowid if cur.rowcount > 0 else 0

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

    def signals_in_range(self, symbol: str, since_ts: int, until_ts: int) -> list[dict]:
        """Возвращает все зафиксированные сигналы по паре в окне [since_ts, until_ts].

        Используется чартом и pair_card как ИММУТАБЕЛЬНЫЙ источник истории —
        в отличие от generate_signals() который пересчитывается на live-буфере
        и может «терять» сигналы из-за HTF buffer-relative группировки
        (см. strategy/consensus.py:107-113). Юзер 2026-05-16: «важно чтобы
        статистика не менялась, иначе нет смысла анализировать».

        Returns list of {signal_ts, side, exp_wins (parsed list|None)}.
        """
        cur = self.conn.execute(
            "SELECT signal_ts, side, exp_wins FROM signals "
            "WHERE symbol=? AND signal_ts >= ? AND signal_ts <= ? "
            "ORDER BY signal_ts",
            (symbol, int(since_ts), int(until_ts)),
        )
        out: list[dict] = []
        for ts, side, ew in cur:
            parsed = None
            if ew:
                try:
                    parsed = _json.loads(ew)
                except Exception:
                    pass
            out.append({"signal_ts": int(ts), "side": side, "exp_wins": parsed})
        return out

    @staticmethod
    def aggregate_signal_stats(signals_list: list[dict], exp_bar_idx: int = 1,
                                recent_since_ts: Optional[int] = None) -> dict:
        """Считает аналитику из списка signals (immutable, из БД).

        Args:
          signals_list: вывод signals_in_range()
          exp_bar_idx: какой бар экспирации использовать (0=1бар, 1=2бар, ...)
          recent_since_ts: если задано — посчитать отдельно метрики «recent»
            для сигналов с signal_ts >= recent_since_ts.

        Returns dict с полями совместимыми с Analysis dataclass (для подмены
        в chart/pair_card без правки UI).
        """
        wins = 0; losses = 0; settled = 0
        wins_recent = 0; losses_recent = 0; settled_recent = 0
        seq: list[int] = []
        cur_streak = 0; max_streak_overall = 0
        tmp_streak = 0; max_streak_before_win = 0
        for s in signals_list:
            ew = s.get("exp_wins")
            if not ew or len(ew) <= exp_bar_idx:
                continue  # ещё не settled или некорректные данные
            outcome = ew[exp_bar_idx]
            if outcome is None:
                # DRAW — не считаем ни в плюс, ни в минус (как в _on_trade_closed)
                continue
            settled += 1
            is_win = bool(outcome)
            seq.append(1 if is_win else 0)
            if is_win:
                wins += 1
                # фиксируем streak-до-WIN
                if tmp_streak > max_streak_before_win:
                    max_streak_before_win = tmp_streak
                tmp_streak = 0
                cur_streak = 0
            else:
                losses += 1
                cur_streak += 1
                tmp_streak += 1
                if cur_streak > max_streak_overall:
                    max_streak_overall = cur_streak
            # recent окно
            if recent_since_ts is not None and s["signal_ts"] >= recent_since_ts:
                settled_recent += 1
                if is_win:
                    wins_recent += 1
                else:
                    losses_recent += 1
        wr = (wins / settled * 100) if settled else 0.0
        wr_recent = (wins_recent / settled_recent * 100) if settled_recent else 0.0
        # signals_count_recent — сколько ВСЕГО сигналов в recent окне (включая
        # ещё не settled), а не только settled.
        if recent_since_ts is not None:
            signals_recent = sum(1 for s in signals_list
                                  if s["signal_ts"] >= recent_since_ts)
        else:
            signals_recent = 0
        return {
            "signals_count": len(signals_list),
            "completed": settled,
            "wins": wins, "losses": losses, "wr": wr,
            "wr1": wr,  # counterfactual: wr1 ≡ wr (нет различия MG-recovery)
            "wins_recent": wins_recent, "losses_recent": losses_recent,
            "completed_recent": settled_recent, "wr_recent": wr_recent,
            "wr1_recent": wr_recent,
            "signals_recent": signals_recent,
            "recent_results": seq,
            "max_loss_streak_overall": max_streak_overall,
            "max_loss_streak_before_win": max_streak_before_win,
        }

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
                             group_by_dow: bool = False,
                             only_symbol: Optional[str] = None,
                             min_sample_size: int = 20,
                             decay_half_life_days: Optional[float] = None) -> list[dict]:
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
            if group_by_hour:
                key = (r["symbol"], r["hour_local"])
            elif group_by_dow:
                key = (r["symbol"], r["day_of_week"])
            else:
                key = r["symbol"]
            groups.setdefault(key, []).append(r)

        ebd = max(1, min(5, int(expiry_bars_default or 2)))
        from .stats import wilson_lower_bound, decay_weight
        now_ts = int(time.time())
        # half-life: None/<=0 = выкл; вес 1.0 для всех сигналов.
        hl_days = decay_half_life_days if (decay_half_life_days and decay_half_life_days > 0) else None
        min_n = max(1, int(min_sample_size or 1))

        out: list[dict] = []
        grouped = group_by_hour or group_by_dow
        for key, items in groups.items():
            symbol = key[0] if grouped else key
            hour = key[1] if group_by_hour else None
            dow_v = key[1] if group_by_dow else None

            n_total = len(items)
            n_entered = sum(1 for r in items if r["entered"])

            # exp_wins-based аналитика (settled-only).
            # Для каждого settled-сигнала запоминаем вес (decay по возрасту):
            # ew_w = [(parsed_wins_array, weight), ...]
            settled = [r for r in items if r["exp_wins"]]
            import json as _json
            ew_w: list[tuple[list, float]] = []
            for r in settled:
                try:
                    parsed = _json.loads(r["exp_wins"])
                except Exception:
                    continue
                # sqlite3.Row не поддерживает .get(); читаем напрямую.
                sig_ts = r["signal_ts"] if "signal_ts" in r.keys() else None
                age = max(0, now_ts - int(sig_ts or now_ts))
                w = decay_weight(age, hl_days)
                ew_w.append((parsed, w))
            # Бэк-совместимый список без весов — старая логика ниже.
            ew = [a for a, _ in ew_w]

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

            def _wr_weighted(arr_idx: int) -> tuple[float, float]:
                """Возвращает (sum_wins, sum_total) по экспирации arr_idx
                с учётом decay-веса. None-значения пропускаются."""
                tw = 0.0
                ww = 0.0
                for arr, weight in ew_w:
                    if arr_idx >= len(arr):
                        continue
                    v = arr[arr_idx]
                    if v is None:
                        continue
                    tw += weight
                    if v == 1:
                        ww += weight
                return ww, tw

            wr_first  = _wr(0)
            wr_chosen = _wr(ebd - 1)

            # best-exp WR: signal считается plus если ХОТЯ БЫ один w==1
            # best_exp_bar: на каком баре (1..5) случилась первая победа.
            # Среднее по всем выигравшим сигналам — чем меньше тем лучше.
            best_wins = 0
            best_total = 0
            pluses = 0
            minuses = 0
            best_bars_sum = 0
            best_bars_count = 0
            for w in ew:
                non_null = [x for x in w if x is not None]
                if not non_null:
                    continue
                best_total += 1
                # Найти ПЕРВЫЙ бар где w==1 (1-indexed для UX)
                first_win_bar = None
                for bar_idx, v in enumerate(w):
                    if v == 1:
                        first_win_bar = bar_idx + 1   # 1..5
                        break
                if first_win_bar is not None:
                    best_wins += 1
                    pluses += 1
                    best_bars_sum += first_win_bar
                    best_bars_count += 1
                else:
                    minuses += 1
            wr_best = (best_wins / best_total * 100.0) if best_total else None
            best_exp_bar = (best_bars_sum / best_bars_count) if best_bars_count else None

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
            wr_real_wlb = wilson_lower_bound(wins_real, wins_real + losses_real) if (wins_real + losses_real) else None

            # ── per-expiration WR (1..5) + Wilson lower bound ──
            # n_for_exp_i — количество сигналов, у которых для бара i есть
            # определённый исход (не None). С decay это сумма весов; без —
            # обычное число. Wilson корректно работает с дробными.
            wr_per_exp: dict[int, Optional[float]] = {}
            n_per_exp: dict[int, float] = {}
            wlb_per_exp: dict[int, Optional[float]] = {}
            for i in range(1, 6):
                wins_w, total_w = _wr_weighted(i - 1)
                n_per_exp[i] = total_w
                if total_w > 0:
                    wr_per_exp[i] = wins_w / total_w * 100.0
                    wlb_per_exp[i] = wilson_lower_bound(wins_w, total_w)
                else:
                    wr_per_exp[i] = None
                    wlb_per_exp[i] = None

            # Wilson для существующих агрегатов wr_first / wr_chosen.
            # Считаем по тем же данным что и wr_first/wr_chosen, но через
            # weighted-сумму чтобы decay (если включён) применялся однородно.
            _w_first, _t_first = _wr_weighted(0)
            wr_first_wlb = wilson_lower_bound(_w_first, _t_first) if _t_first > 0 else None
            _w_chosen, _t_chosen = _wr_weighted(ebd - 1)
            wr_chosen_wlb = wilson_lower_bound(_w_chosen, _t_chosen) if _t_chosen > 0 else None

            # best_exp_by_wlb: какая экспирация (1..5) даёт max(wlb)
            # при условии n_per_exp[i] >= min_sample_size. Если ни одна —
            # None (надо использовать fallback в decision-функции).
            eligible = [(i, wlb_per_exp[i]) for i in range(1, 6)
                        if wlb_per_exp[i] is not None and n_per_exp[i] >= min_n]
            if eligible:
                best_exp_by_wlb = max(eligible, key=lambda kv: kv[1])[0]
                best_wlb = max(eligible, key=lambda kv: kv[1])[1]
            else:
                best_exp_by_wlb = None
                best_wlb = None

            row = {
                "symbol": symbol,
                "hour": hour,
                "dow": dow_v,
                "signals": n_total,
                "entered": n_entered,
                "settled": len(ew),
                "wr_first":  None if wr_first is None else round(wr_first, 1),
                "wr_chosen": None if wr_chosen is None else round(wr_chosen, 1),
                "wr_best":   None if wr_best is None else round(wr_best, 1),
                "best_exp_bar": None if best_exp_bar is None else round(best_exp_bar, 2),
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
                # ── новые поля (Wilson + per-expiration) ──
                # Точечный WR (%) по каждой экспирации 1..5.
                "wr_1": None if wr_per_exp[1] is None else round(wr_per_exp[1], 1),
                "wr_2": None if wr_per_exp[2] is None else round(wr_per_exp[2], 1),
                "wr_3": None if wr_per_exp[3] is None else round(wr_per_exp[3], 1),
                "wr_4": None if wr_per_exp[4] is None else round(wr_per_exp[4], 1),
                "wr_5": None if wr_per_exp[5] is None else round(wr_per_exp[5], 1),
                # Wilson lower bound (%) по каждой экспирации 1..5.
                # При decay-весах это «честный» нижний WR с учётом давности.
                "wr_1_wlb": None if wlb_per_exp[1] is None else round(wlb_per_exp[1] * 100.0, 1),
                "wr_2_wlb": None if wlb_per_exp[2] is None else round(wlb_per_exp[2] * 100.0, 1),
                "wr_3_wlb": None if wlb_per_exp[3] is None else round(wlb_per_exp[3] * 100.0, 1),
                "wr_4_wlb": None if wlb_per_exp[4] is None else round(wlb_per_exp[4] * 100.0, 1),
                "wr_5_wlb": None if wlb_per_exp[5] is None else round(wlb_per_exp[5] * 100.0, 1),
                # Sample size per expiration (с учётом decay-весов если включён).
                # Округляем чтобы UI понимал «достоверность» цифры.
                "n_for_exp_1": round(n_per_exp[1], 2) if hl_days else int(n_per_exp[1]),
                "n_for_exp_2": round(n_per_exp[2], 2) if hl_days else int(n_per_exp[2]),
                "n_for_exp_3": round(n_per_exp[3], 2) if hl_days else int(n_per_exp[3]),
                "n_for_exp_4": round(n_per_exp[4], 2) if hl_days else int(n_per_exp[4]),
                "n_for_exp_5": round(n_per_exp[5], 2) if hl_days else int(n_per_exp[5]),
                # Wilson для существующих метрик — для UI колонки «честный WR».
                "wr_first_wlb":  None if wr_first_wlb  is None else round(wr_first_wlb  * 100.0, 1),
                "wr_chosen_wlb": None if wr_chosen_wlb is None else round(wr_chosen_wlb * 100.0, 1),
                "wr_real_wlb":   None if wr_real_wlb   is None else round(wr_real_wlb   * 100.0, 1),
                # Адаптивная экспирация: 1..5 или None если ни одна
                # экспирация не прошла фильтр n >= min_sample_size.
                "best_exp_by_wlb": best_exp_by_wlb,
                "best_exp_wlb_value": None if best_wlb is None else round(best_wlb * 100.0, 1),
                "min_sample_size": min_n,
                "decay_half_life_days": hl_days,
            }
            out.append(row)

        # сортировка
        if group_by_hour:
            out.sort(key=lambda r: (r["symbol"], r["hour"] if r["hour"] is not None else -1))
        elif group_by_dow:
            out.sort(key=lambda r: (r["symbol"], r["dow"] if r["dow"] is not None else -1))
        else:
            out.sort(key=lambda r: (-r["signals"], r["symbol"]))
        return out

    # ---------- per-strategy signal filter (Stage 3) ----------

    def build_filter_preview(self,
                              strategy_name: str,
                              since_ts: int,
                              hour_from: Optional[int] = None,
                              hour_to: Optional[int] = None,
                              dow: Optional[list] = None,
                              expiry_bars: int = 2,
                              use_best_exp: bool = False) -> dict:
        """Анализирует winning signals в указанном срезе и возвращает
        предлагаемый фильтр (не сохраняет). Юзер потом может отредактировать
        значения перед save.

        'Выигрышная' = exp_wins[expiry_bars-1]==1, либо ANY w==1 если use_best_exp.
        Числовые границы — quantile 10..90 чтобы отсечь outliers.
        """
        rows = self._signals_filtered(since_ts, strategy_name, hour_from, hour_to, dow)
        import json as _json
        eb_idx = max(0, min(4, int(expiry_bars) - 1))
        wins: list[sqlite3.Row] = []
        for r in rows:
            if not r["exp_wins"]:
                continue
            try:
                ew = _json.loads(r["exp_wins"])
            except Exception:
                continue
            if use_best_exp:
                if any(v == 1 for v in ew if v is not None):
                    wins.append(r)
            else:
                if eb_idx < len(ew) and ew[eb_idx] == 1:
                    wins.append(r)

        n_total = len(rows)
        n_wins = len(wins)
        based_on = {
            "total_signals": n_total, "winning_signals": n_wins,
            "since_ts": since_ts,
            "hour_from": hour_from, "hour_to": hour_to, "dow": dow,
            "expiry_bars": expiry_bars, "use_best_exp": use_best_exp,
        }
        if n_wins < 5:
            return {
                "enabled": False,
                "based_on": based_on,
                "warning": f"мало выигрышных сигналов ({n_wins}). Нужно ≥5 — продолжай собирать.",
            }

        def _q(vals, q):
            s = sorted(v for v in vals if v is not None)
            if not s:
                return None
            k = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
            return s[k]

        def _range(field, qlow=0.1, qhigh=0.9):
            vals = [r[field] for r in wins if r[field] is not None]
            if len(vals) < 3:
                return None, None
            # Для маленьких выборок (< 10) quantile 10/90 даёт чисто
            # min/max — берём их явно. Для больших — обрезаем outliers.
            if len(vals) < 10:
                lo, hi = min(vals), max(vals)
            else:
                lo, hi = _q(vals, qlow), _q(vals, qhigh)
            if isinstance(lo, float):
                lo = round(lo, 4)
            if isinstance(hi, float):
                hi = round(hi, 4)
            return lo, hi

        atr_lo, atr_hi = _range("atr_ratio")
        bb_lo,  bb_hi  = _range("bb_position")
        cnd_lo, cnd_hi = _range("candle_atr_ratio")
        rsi_lo, rsi_hi = _range("rsi_ma")

        payouts = [r["payout_at_signal"] for r in wins if r["payout_at_signal"] is not None]
        votes   = [r["votes_total"] for r in wins if r["votes_total"] is not None]
        hours   = sorted({r["hour_local"] for r in wins if r["hour_local"] is not None})
        dows    = sorted({r["day_of_week"] for r in wins if r["day_of_week"] is not None})

        return {
            "enabled": True,
            "based_on": based_on,
            "atr_ratio_min":  atr_lo, "atr_ratio_max":  atr_hi,
            "bb_position_min": bb_lo, "bb_position_max": bb_hi,
            "candle_atr_ratio_max": cnd_hi,
            "rsi_ma_min":     rsi_lo, "rsi_ma_max":     rsi_hi,
            "payout_min":     min(payouts) if payouts else None,
            "votes_total_min": min(votes) if votes else None,
            "hours_allowed":  hours,
            "dow_allowed":    dows,
        }

    def signal_filter_get(self, strategy_name: str) -> Optional[dict]:
        return self.get(f"signal_filter:{strategy_name}")

    def signal_filter_set(self, strategy_name: str, spec: dict):
        spec = dict(spec or {})
        spec.setdefault("created_at", int(time.time()))
        spec["updated_at"] = int(time.time())
        self.set(f"signal_filter:{strategy_name}", spec)

    def signal_filter_delete(self, strategy_name: str):
        self.delete(f"signal_filter:{strategy_name}")

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

    def pair_wr_from_signals(self, symbol: str,
                              min_count: int = 30,
                              exp_bar_index: int = 1,
                              days_lookback: int = 30) -> Optional[float]:
        """Считает counterfactual WR конкретной пары из таблицы signals.
        Используется WR-based blacklist'ом: пары с WR<X% не торгуются, хотя
        в аналитику продолжают писаться.

        Args:
            symbol: имя пары (например "EURJPY_otc")
            min_count: минимальное число signals чтобы считать WR значимым
                       (иначе вернёт None — нет данных)
            exp_bar_index: какую экспирацию проверять. 1 = 2-бар (default),
                           0=1-бар, 2=3-бар и т.д.
            days_lookback: за сколько дней брать. 0 = все.

        Returns:
            WR в процентах (0.0-100.0) или None если данных недостаточно.
        """
        import time as _t
        since = int(_t.time()) - days_lookback * 86400 if days_lookback > 0 else 0
        cur = self.conn.execute(
            "SELECT exp_wins FROM signals WHERE symbol=? AND signal_ts >= ? "
            "AND exp_wins IS NOT NULL",
            (symbol, since),
        )
        wins = 0; total = 0
        for r in cur:
            try:
                ew = json.loads(r[0])
                if ew and len(ew) > exp_bar_index:
                    total += 1
                    if ew[exp_bar_index]:
                        wins += 1
            except Exception:
                continue
        if total < min_count:
            return None
        return wins / total * 100.0

    # ---------- bans ----------

    def ban(self, symbol: str, hours: int = 0, reason: str = "", minutes: int = 0):
        """Зафиксировать пару в бане. Длительность можно задавать
        часами (hours) ИЛИ минутами (minutes) ИЛИ обоими (суммируются).
        Используется и для длительных банов (`ban_hours`, дефолт 12ч),
        и для коротких пауз (`pause_minutes`, дефолт 60 мин)."""
        now = int(time.time())
        expires = now + int(hours) * 3600 + int(minutes) * 60
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

    # ---------- strategy snapshots ----------
    # Версионированные слепки настроек фильтра, выведенные из анализа БД.
    # Применяются поверх settings_overrides когда active=1. Backup_config хранит
    # то что было в overrides ДО активации — позволяет откатиться при выключении.

    def snapshot_list(self) -> list:
        """Все snapshots, сначала активный, потом по дате создания (новые первые)."""
        self.conn.row_factory = sqlite3.Row
        rows = self.conn.execute(
            "SELECT id, name, description, filter_config, stats_at_creation, "
            "source_data_until, active, created_at FROM strategy_snapshots "
            "ORDER BY active DESC, created_at DESC"
        ).fetchall()
        result = []
        for r in rows:
            result.append({
                "id": r["id"], "name": r["name"], "description": r["description"],
                "filter_config": json.loads(r["filter_config"]),
                "stats_at_creation": json.loads(r["stats_at_creation"]) if r["stats_at_creation"] else None,
                "source_data_until": r["source_data_until"],
                "active": bool(r["active"]),
                "created_at": r["created_at"],
            })
        return result

    def snapshot_get_active(self) -> dict | None:
        """Активный snapshot или None если нет."""
        self.conn.row_factory = sqlite3.Row
        r = self.conn.execute(
            "SELECT id, name, description, filter_config, backup_config, "
            "stats_at_creation, source_data_until, created_at "
            "FROM strategy_snapshots WHERE active=1 LIMIT 1"
        ).fetchone()
        if not r: return None
        return {
            "id": r["id"], "name": r["name"], "description": r["description"],
            "filter_config": json.loads(r["filter_config"]),
            "backup_config": json.loads(r["backup_config"]) if r["backup_config"] else {},
            "stats_at_creation": json.loads(r["stats_at_creation"]) if r["stats_at_creation"] else None,
            "source_data_until": r["source_data_until"],
            "created_at": r["created_at"],
        }

    def snapshot_create(self, name: str, description: str, filter_config: dict,
                        stats_at_creation: dict | None = None,
                        source_data_until: int | None = None) -> int:
        """Создать новый snapshot. Возвращает id. Не активирует автоматически."""
        cur = self.conn.execute(
            "INSERT INTO strategy_snapshots "
            "(name, description, filter_config, stats_at_creation, source_data_until, active, created_at) "
            "VALUES (?, ?, ?, ?, ?, 0, ?)",
            (name, description, json.dumps(filter_config),
             json.dumps(stats_at_creation) if stats_at_creation else None,
             source_data_until, int(time.time()))
        )
        self.conn.commit()
        return cur.lastrowid

    def snapshot_activate(self, snapshot_id: int) -> dict:
        """Активировать snapshot. Деактивирует другой если был активен.
        Сохраняет в backup_config текущие значения keys из overrides
        (чтобы можно было откатить). Накладывает filter_config на overrides.
        Возвращает {"applied_keys": [...], "snapshot": {...}}.
        """
        self.conn.row_factory = sqlite3.Row
        # Деактивируем все остальные
        self.conn.execute("UPDATE strategy_snapshots SET active=0 WHERE active=1")
        # Получаем целевой snapshot
        r = self.conn.execute(
            "SELECT filter_config FROM strategy_snapshots WHERE id=?", (snapshot_id,)
        ).fetchone()
        if not r:
            self.conn.commit()
            raise ValueError(f"snapshot id={snapshot_id} not found")
        filter_cfg = json.loads(r["filter_config"])
        # Текущие overrides
        overrides = self.get("settings_overrides") or {}
        # Backup тех ключей которые snapshot затрагивает
        backup = {k: overrides.get(k) for k in filter_cfg.keys()}
        # Накладываем snapshot на overrides
        for k, v in filter_cfg.items():
            overrides[k] = v
        self.set("settings_overrides", overrides)
        # Помечаем snapshot активным + сохраняем backup
        self.conn.execute(
            "UPDATE strategy_snapshots SET active=1, backup_config=? WHERE id=?",
            (json.dumps(backup), snapshot_id)
        )
        self.conn.commit()
        return {
            "applied_keys": list(filter_cfg.keys()),
            "filter_config": filter_cfg,
            "backup_config": backup,
        }

    def snapshot_deactivate(self, snapshot_id: int) -> dict:
        """Деактивировать snapshot. Восстанавливает значения из backup_config —
        там где был None (т.е. ключ не был в overrides до активации) — удаляет ключ.
        """
        self.conn.row_factory = sqlite3.Row
        r = self.conn.execute(
            "SELECT backup_config, filter_config FROM strategy_snapshots "
            "WHERE id=? AND active=1", (snapshot_id,)
        ).fetchone()
        if not r:
            raise ValueError(f"snapshot id={snapshot_id} not active")
        backup = json.loads(r["backup_config"]) if r["backup_config"] else {}
        filter_cfg = json.loads(r["filter_config"])
        overrides = self.get("settings_overrides") or {}
        for k in filter_cfg.keys():
            prev = backup.get(k)
            if prev is None:
                overrides.pop(k, None)        # ключа не было — удалить
            else:
                overrides[k] = prev           # был какой-то — вернуть
        self.set("settings_overrides", overrides)
        self.conn.execute(
            "UPDATE strategy_snapshots SET active=0, backup_config=NULL WHERE id=?",
            (snapshot_id,)
        )
        self.conn.commit()
        return {"restored_keys": list(filter_cfg.keys())}

    def snapshot_delete(self, snapshot_id: int) -> None:
        """Удалить snapshot. Если он активен — сначала деактивирует."""
        self.conn.row_factory = sqlite3.Row
        r = self.conn.execute(
            "SELECT active FROM strategy_snapshots WHERE id=?", (snapshot_id,)
        ).fetchone()
        if r and r["active"]:
            self.snapshot_deactivate(snapshot_id)
        self.conn.execute("DELETE FROM strategy_snapshots WHERE id=?", (snapshot_id,))
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

        # Net PnL.
        # ВАЖНО: trades.profit = ПОЛНАЯ выплата брокера (стейк + выигрыш) для
        # WIN, 0 для LOSS, ≈amount для DRAW (refund). Чистый выигрыш на WIN
        # это (profit - amount). Убыток на LOSS это amount. DRAW нейтрален.
        # Раньше считали net = SUM(profit_win) - SUM(amount_loss), что
        # завышало результат на сумму стейков всех WIN-сделок.
        cur = self.conn.execute(
            "SELECT COALESCE(SUM(profit - amount),0) FROM trades "
            "WHERE close_ts>=? AND mode=? AND result='WIN'",
            (since_ts, mode))
        win_gain = cur.fetchone()[0] or 0
        cur = self.conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM trades "
            "WHERE close_ts>=? AND mode=? AND result='LOSS'",
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

    def max_no_trade_gap(self, since_ts: int, until_ts: int, mode: str) -> int:
        """Максимальный непрерывный промежуток БЕЗ сделок в окне [since_ts, until_ts]
        (секунды). Учитывает:
          - gap до первой сделки (since → first.open_ts)
          - gap-ы между сделками (prev.close_ts → next.open_ts)
          - текущий gap (last.close_ts → until_ts)
        Если сделок не было вовсе → возвращает (until - since).
        Если until <= since → возвращает 0.
        """
        if until_ts <= since_ts:
            return 0
        cur = self.conn.execute(
            "SELECT open_ts, close_ts FROM trades "
            "WHERE close_ts >= ? AND close_ts <= ? AND mode = ? "
            "ORDER BY open_ts",
            (since_ts, until_ts, mode),
        )
        rows = cur.fetchall()
        if not rows:
            return until_ts - since_ts
        max_gap = 0
        prev_close = since_ts
        for r in rows:
            o = int(r[0] or r[1])  # если open_ts NULL — fallback на close_ts
            c = int(r[1] or 0)
            if o < prev_close:
                # перекрытие (например параллельная сделка) — не считаем
                continue
            gap = o - prev_close
            if gap > max_gap:
                max_gap = gap
            prev_close = max(prev_close, c)
        # last open gap до сейчас
        last_gap = until_ts - prev_close
        if last_gap > max_gap:
            max_gap = last_gap
        return max(0, int(max_gap))

    def max_recovered_losses_24h(self, since_ts: int, mode: str) -> Optional[int]:
        """Максимальная глубина восстановления цикла за сутки. Это mg_step
        WIN-сделки — сколько минусов цикл вытянул до плюса.
        Пример: цикл LOSS→LOSS→LOSS→WIN ⇒ recovered_losses=3 (WIN на mg_step=3).
        Берётся максимум среди WIN-сделок за окно. None если нет WIN."""
        cur = self.conn.execute(
            "SELECT MAX(mg_step) FROM trades "
            "WHERE close_ts >= ? AND mode = ? AND result = 'WIN'",
            (since_ts, mode),
        )
        v = cur.fetchone()[0]
        return int(v) if v is not None else None

    def min_payout_24h(self, since_ts: int, mode: str) -> Optional[int]:
        """Минимальный payout (%) среди сделок открытых за последние сутки
        (close_ts >= since_ts). None если сделок не было.
        Учитываются ВСЕ исходы (WIN/LOSS/DRAW) — это «худший процент по которому
        бот вообще согласился войти», полезно для контроля payout-floor."""
        cur = self.conn.execute(
            "SELECT MIN(payout) FROM trades "
            "WHERE close_ts >= ? AND mode = ? AND payout IS NOT NULL",
            (since_ts, mode),
        )
        v = cur.fetchone()[0]
        return int(v) if v is not None else None

    def close(self):
        try: self.conn.close()
        except Exception: pass
