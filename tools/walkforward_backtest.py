"""Walk-forward backtest для адаптивной экспирации.

Проверяет: если бы мы каждый день D брали best_exp_by_wlb из аналитики
по окну [D-train_days .. D-gap_days], а торговали бы этой экспирацией
сигналы дня D — какой WR получили бы в out-of-sample?

Если in-sample WR (на тренировочном окне) сильно расходится с realized
WR (на дне D) — модель переобучена.

Использование:
    python -m tools.walkforward_backtest \\
        --db /data/state.db \\
        --train-days 30 --gap-days 1 --window-days 30 \\
        --min-sample 20 --strategy consensus_default

Печатает:
  - WR_in_sample (по best_exp_by_wlb на train-окне)
  - WR_out_of_sample (как ту же экспирацию реально отыграл день D)
  - delta = OOS - IS  (если стабильно отрицательный — переобучение)
  - coverage % — какая доля сигналов была отфильтрована (best_exp_by_wlb=None)

Не модифицирует БД, не торгует. Просто читает signals и печатает отчёт.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

# Позволяем запускать как `python tools/walkforward_backtest.py` из корня репо.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from journal.stats import wilson_lower_bound, decay_weight  # noqa: E402


def _wr_per_exp_for_window(rows: list[sqlite3.Row],
                            min_sample: int,
                            half_life_days: float | None) -> dict[str, dict]:
    """Возвращает {symbol: {best_exp: int|None, wr_per_exp: {1..5: %},
                              wlb_per_exp: {1..5: %}, n_per_exp: {1..5}}}.
    Та же логика что в journal.db.analytics_aggregate, но автономно."""
    by_sym: dict[str, list[tuple[list, float]]] = defaultdict(list)
    now_ts = int(time.time())
    for r in rows:
        if not r["exp_wins"]:
            continue
        try:
            arr = json.loads(r["exp_wins"])
        except Exception:
            continue
        age = max(0, now_ts - int(r["signal_ts"]))
        w = decay_weight(age, half_life_days)
        by_sym[r["symbol"]].append((arr, w))

    out: dict[str, dict] = {}
    for sym, items in by_sym.items():
        wr_pe = {}
        wlb_pe = {}
        n_pe = {}
        for i in range(1, 6):
            tw = ww = 0.0
            for arr, weight in items:
                if i - 1 >= len(arr):
                    continue
                v = arr[i - 1]
                if v is None:
                    continue
                tw += weight
                if v == 1:
                    ww += weight
            n_pe[i] = tw
            wr_pe[i] = (ww / tw * 100.0) if tw > 0 else None
            wlb_pe[i] = (wilson_lower_bound(ww, tw) * 100.0) if tw > 0 else None
        eligible = [(i, wlb_pe[i]) for i in range(1, 6)
                    if wlb_pe[i] is not None and n_pe[i] >= min_sample]
        best = max(eligible, key=lambda kv: kv[1])[0] if eligible else None
        out[sym] = {
            "best_exp": best,
            "wr_per_exp": wr_pe,
            "wlb_per_exp": wlb_pe,
            "n_per_exp": n_pe,
        }
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, help="path to state.db")
    ap.add_argument("--strategy", default=None, help="filter by strategy_name")
    ap.add_argument("--symbol", default=None, help="filter by single symbol")
    ap.add_argument("--train-days", type=int, default=30,
                    help="окно тренировки (дней) перед каждым OOS днём")
    ap.add_argument("--gap-days", type=int, default=1,
                    help="зазор между концом train-окна и днём OOS (anti-leak)")
    ap.add_argument("--window-days", type=int, default=30,
                    help="сколько дней OOS-тестирования (от --start-days-ago)")
    ap.add_argument("--start-days-ago", type=int, default=None,
                    help="конец OOS-окна (дней назад от now). По умолчанию = gap_days")
    ap.add_argument("--min-sample", type=int, default=20)
    ap.add_argument("--decay-half-life-days", type=float, default=None,
                    help="exp decay; null = выкл")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # Базовый запрос
    where = ["exp_wins IS NOT NULL"]
    params: list = []
    if args.strategy:
        where.append("strategy_name = ?")
        params.append(args.strategy)
    if args.symbol:
        where.append("symbol = ?")
        params.append(args.symbol)
    where_sql = " AND ".join(where)

    now_ts = int(time.time())
    end_offset = args.start_days_ago if args.start_days_ago is not None else args.gap_days
    oos_end_ts = now_ts - end_offset * 86400
    oos_start_ts = oos_end_ts - args.window_days * 86400

    # Сводные метрики OOS
    total_oos_signals = 0
    total_oos_filtered = 0
    total_oos_evaluated = 0
    total_oos_wins = 0
    is_wr_sum = 0.0
    oos_wr_sum = 0.0
    n_pairs_with_decision = 0

    print(f"=== Walk-forward backtest ===")
    print(f"DB: {args.db}")
    print(f"strategy={args.strategy or 'ALL'}, symbol={args.symbol or 'ALL'}")
    print(f"train_days={args.train_days}, gap_days={args.gap_days}, "
          f"window_days={args.window_days}, min_sample={args.min_sample}, "
          f"half_life={args.decay_half_life_days}")
    print(f"OOS окно: {oos_start_ts} .. {oos_end_ts} "
          f"({args.window_days} дн, заканчивается {end_offset}д назад)")
    print()

    # Идём по дням OOS
    day_sec = 86400
    for day_idx in range(args.window_days):
        d_start = oos_start_ts + day_idx * day_sec
        d_end = d_start + day_sec

        # Train: [d_start - gap*day - train*day .. d_start - gap*day]
        train_end = d_start - args.gap_days * day_sec
        train_start = train_end - args.train_days * day_sec

        # train rows
        sql_train = (f"SELECT symbol, signal_ts, exp_wins FROM signals "
                     f"WHERE {where_sql} AND signal_ts >= ? AND signal_ts < ?")
        train_rows = conn.execute(sql_train, (*params, train_start, train_end)).fetchall()
        if not train_rows:
            continue

        decisions = _wr_per_exp_for_window(train_rows, args.min_sample,
                                            args.decay_half_life_days)
        n_pairs_with_decision += sum(1 for s, d in decisions.items() if d["best_exp"])

        # oos rows (день D)
        oos_rows = conn.execute(sql_train, (*params, d_start, d_end)).fetchall()

        for r in oos_rows:
            total_oos_signals += 1
            sym = r["symbol"]
            dec = decisions.get(sym)
            if not dec or dec["best_exp"] is None:
                total_oos_filtered += 1
                continue
            try:
                arr = json.loads(r["exp_wins"])
            except Exception:
                continue
            i = dec["best_exp"]
            if i - 1 >= len(arr):
                continue
            v = arr[i - 1]
            if v is None:
                continue  # draw — не считаем ни плюс ни минус
            total_oos_evaluated += 1
            if v == 1:
                total_oos_wins += 1
            # IS WR для этой экспирации (на train-окне)
            is_wr_sum += dec["wr_per_exp"][i] or 0.0
            oos_wr_sum += (100.0 if v == 1 else 0.0)

    print(f"Сигналов OOS:           {total_oos_signals}")
    print(f"  отфильтровано (нет best_exp): {total_oos_filtered}  "
          f"({total_oos_filtered / total_oos_signals * 100.0:.1f}%)" if total_oos_signals else "")
    print(f"  оценено:              {total_oos_evaluated}")
    if total_oos_evaluated:
        oos_wr = total_oos_wins / total_oos_evaluated * 100.0
        is_wr_avg = is_wr_sum / total_oos_evaluated
        oos_wr_avg = oos_wr_sum / total_oos_evaluated
        print(f"WR_in_sample (avg):    {is_wr_avg:.1f}%   (модель ожидала)")
        print(f"WR_out_of_sample:      {oos_wr:.1f}%   (что получилось в реальности)")
        print(f"delta:                 {oos_wr - is_wr_avg:+.1f} п.п.")
        print()
        if oos_wr - is_wr_avg < -10:
            print("⚠️  OOS сильно ниже IS — модель переобучена. "
                  "Не включай адаптивную экспирацию или ужесточи min_sample.")
        elif oos_wr >= 55 and oos_wr - is_wr_avg > -5:
            print("✓ Стабильно. Адаптивную экспирацию можно включать.")
        else:
            print("◌ Пограничный результат. Прогон ещё с другими параметрами.")
    else:
        print("Нет OOS-сигналов для оценки. Увеличь --window-days или собери больше истории.")


if __name__ == "__main__":
    main()
