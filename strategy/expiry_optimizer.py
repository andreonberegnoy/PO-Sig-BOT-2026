"""Авто-оптимизация экспирации per-pair × hour.

Идея:
  В таблице `signals` каждый сигнал имеет `exp_wins` — массив [w1,w2,w3,w4,w5]
  показывающий выиграл бы trade при экспирации 1/2/3/4/5 баров после сигнала.
  По историческим данным можно найти оптимум для каждой пары в каждом часе:
  например EURUSD в 14:00 лучше работает на 5 баров (WR 56%) чем на 2 (51%).

  Этот модуль периодически (раз в 4ч) пересчитывает оптимумы и сохраняет
  в state_kv. Бот при открытии сделки читает таблицу и использует оптимум
  для (sym, hour) если есть данные ≥ min_count, иначе fallback на дефолт.

Формат хранения в БД:
  state_kv["pair_hour_expiry"] = {
    "EURUSD_otc:14": {"bars": 5, "wr": 56.4, "n": 23},
    "AEDCNY_otc:18": {"bars": 2, "wr": 73.9, "n": 17},
    ...
  }

Использование:
  optimum = pair_hour_expiry_lookup(journal, "EURUSD_otc", 14)
  if optimum: bars = optimum["bars"]
  else: bars = fallback_bars  # из cfg.trading.expiry_seconds
"""
from __future__ import annotations
import json
import time
import logging
from collections import defaultdict
from typing import Optional

logger = logging.getLogger(__name__)

# Минимум сигналов в ячейке (sym, hour) чтобы статистика была значимой
MIN_COUNT_PER_CELL = 8
# За сколько дней брать данные (свежие важнее, рынок меняется)
LOOKBACK_DAYS = 30
# Сколько разных экспираций тестируем (exp_wins имеет до 5 элементов)
EXPIRY_BARS = [1, 2, 3, 4, 5]


def compute_pair_hour_expiry(journal) -> dict:
    """SQL-агрегация: для каждой (symbol, hour_local) находит оптимальную экспирацию.

    Returns:
      dict { "SYMBOL:HOUR": {"bars": int, "wr": float, "n": int}, ... }

    Только ячейки с N >= MIN_COUNT_PER_CELL попадают в результат.
    """
    since = int(time.time()) - LOOKBACK_DAYS * 86400
    cur = journal.conn.execute(
        "SELECT symbol, hour_local, exp_wins FROM signals "
        "WHERE signal_ts >= ? AND exp_wins IS NOT NULL AND hour_local IS NOT NULL",
        (since,),
    )

    # Агрегируем wins per (sym, hour, expiry_bar_index)
    # stats[(sym, hour)][bar_idx] = {"win": int, "total": int}
    stats: dict[tuple[str, int], dict[int, dict]] = defaultdict(
        lambda: {i: {"win": 0, "total": 0} for i in range(5)}
    )
    for sym, hour, exp_wins_str in cur:
        try:
            ew = json.loads(exp_wins_str)
        except Exception:
            continue
        if not ew or not isinstance(ew, list):
            continue
        cell = stats[(sym, int(hour))]
        for i, won in enumerate(ew[:5]):
            cell[i]["total"] += 1
            if won:
                cell[i]["win"] += 1

    # Выбираем лучший bar_idx для каждой ячейки (sym, hour)
    result: dict[str, dict] = {}
    for (sym, hour), bars_data in stats.items():
        # Считаем total по 2-бар (default) чтобы решить достаточно ли данных
        n_total = bars_data[1]["total"]  # 2-бар = index 1
        if n_total < MIN_COUNT_PER_CELL:
            continue
        # Находим bar_idx с лучшим WR (среди тех у кого total>=min_count)
        best_idx = None
        best_wr = -1.0
        for i in range(5):
            d = bars_data[i]
            if d["total"] < MIN_COUNT_PER_CELL:
                continue
            wr_pct = d["win"] / d["total"] * 100.0
            if wr_pct > best_wr:
                best_wr = wr_pct
                best_idx = i
        if best_idx is None:
            continue
        result[f"{sym}:{hour}"] = {
            "bars": best_idx + 1,  # 1-based для пользователя/UI
            "wr": round(best_wr, 1),
            "n": bars_data[best_idx]["total"],
        }

    logger.info("pair_hour_expiry: computed %d cells (sym×hour) from last %d days",
                len(result), LOOKBACK_DAYS)
    return result


def pair_hour_expiry_lookup(journal, symbol: str, hour: int) -> Optional[dict]:
    """Быстрый lookup: для (symbol, hour) — есть ли оптимизированная экспирация?

    Returns:
      {"bars": int, "wr": float, "n": int} если данные есть и значимы,
      None если данных нет (fallback на дефолтную экспирацию из cfg).
    """
    try:
        table = journal.get("pair_hour_expiry") or {}
    except Exception:
        return None
    return table.get(f"{symbol}:{hour}")


async def expiry_optimizer_loop(journal, sleep_seconds: int = 4 * 3600):
    """Периодический пересчёт оптимумов. Запускается как asyncio task в main.py.

    По умолчанию раз в 4 часа. Первый запуск через 60 секунд после старта
    (даём боту прогреться).
    """
    import asyncio
    await asyncio.sleep(60)
    while True:
        try:
            table = compute_pair_hour_expiry(journal)
            journal.set("pair_hour_expiry", table)
            if table:
                logger.info("expiry_optimizer: saved %d cells", len(table))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("expiry_optimizer crashed, continuing in %ds", sleep_seconds)
        await asyncio.sleep(sleep_seconds)
