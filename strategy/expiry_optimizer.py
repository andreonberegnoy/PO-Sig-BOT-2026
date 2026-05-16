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
import asyncio
import json
import time
import logging
from collections import defaultdict
from typing import Optional

logger = logging.getLogger(__name__)

# Минимум сигналов в ячейке (sym, hour) чтобы статистика была значимой
MIN_COUNT_PER_CELL = 8
# Минимум сигналов для overall-оптимума per-pair (без разбивки по часам).
# Промежуточный fallback: когда конкретный час ещё пустой, но по паре уже
# накоплена общая история — берём её лучшую экспирацию, а не глобальный
# дефолт 2 бара.
MIN_COUNT_PER_PAIR_OVERALL = 30
# За сколько дней брать данные (свежие важнее, рынок меняется)
LOOKBACK_DAYS = 30
# Все экспирации в exp_wins (1-5 баров). Юзер: «адаптивная модель,
# не ограничиваем аналитику» — оптимизатор может выбрать любую от 1 до 5.
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
        # Находим bar_idx с лучшим WR (среди тех у кого total>=min_count).
        # Юзер: адаптивная модель — оптимизатор может выбрать ЛЮБУЮ экспирацию
        # 1-5 баров без ограничений. Counterfactual exp_wins даёт полную картину.
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


def compute_pair_overall_expiry(journal) -> dict:
    """Per-pair оптимум БЕЗ разбивки по часам — промежуточный fallback.

    Использование: если у пары много истории глобально (≥30 сигналов), но
    в конкретном часе данных мало (<8) — лучше взять её собственный
    оптимум чем глобальный дефолт 2 бара.

    Returns:
      dict { "SYMBOL": {"bars": int, "wr": float, "n": int}, ... }
    """
    since = int(time.time()) - LOOKBACK_DAYS * 86400
    cur = journal.conn.execute(
        "SELECT symbol, exp_wins FROM signals "
        "WHERE signal_ts >= ? AND exp_wins IS NOT NULL",
        (since,),
    )

    # stats[sym][bar_idx] = {"win": int, "total": int}
    stats: dict[str, dict[int, dict]] = defaultdict(
        lambda: {i: {"win": 0, "total": 0} for i in range(5)}
    )
    for sym, exp_wins_str in cur:
        try:
            ew = json.loads(exp_wins_str)
        except Exception:
            continue
        if not ew or not isinstance(ew, list):
            continue
        cell = stats[sym]
        for i, won in enumerate(ew[:5]):
            cell[i]["total"] += 1
            if won:
                cell[i]["win"] += 1

    result: dict[str, dict] = {}
    for sym, bars_data in stats.items():
        n_total = bars_data[1]["total"]  # 2-бар как baseline для значимости
        if n_total < MIN_COUNT_PER_PAIR_OVERALL:
            continue
        best_idx = None
        best_wr = -1.0
        for i in range(5):
            d = bars_data[i]
            if d["total"] < MIN_COUNT_PER_PAIR_OVERALL:
                continue
            wr_pct = d["win"] / d["total"] * 100.0
            if wr_pct > best_wr:
                best_wr = wr_pct
                best_idx = i
        if best_idx is None:
            continue
        result[sym] = {
            "bars": best_idx + 1,
            "wr": round(best_wr, 1),
            "n": bars_data[best_idx]["total"],
        }

    logger.info("pair_overall_expiry: computed %d pairs from last %d days",
                len(result), LOOKBACK_DAYS)
    return result


def pair_overall_expiry_lookup(journal, symbol: str) -> Optional[dict]:
    """Lookup overall-оптимума для пары (без разбивки по часам).

    Используется как промежуточный fallback когда конкретный (sym, hour)
    не имеет достаточно данных, но у пары есть общая статистика.
    """
    try:
        table = journal.get("pair_overall_expiry") or {}
    except Exception:
        return None
    return table.get(symbol)


def resolve_expiry_bars(journal, symbol: str, hour: int) -> Optional[dict]:
    """Унифицированный lookup экспирации с двухуровневым fallback.

    Порядок:
      1. (symbol, hour) ≥ 8 сигналов → используем ячейку (источник: "hour")
      2. (symbol) ≥ 30 сигналов overall → per-pair оптимум (источник: "pair")
      3. None → caller использует expiry_seconds из config

    Returns:
      {"bars": int, "wr": float, "n": int, "source": "hour"|"pair"} или None.
    """
    cell = pair_hour_expiry_lookup(journal, symbol, hour)
    if cell:
        return {**cell, "source": "hour"}
    overall = pair_overall_expiry_lookup(journal, symbol)
    if overall:
        return {**overall, "source": "pair"}
    return None


async def expiry_optimizer_loop(journal, sleep_seconds: int = 4 * 3600):
    """Периодический пересчёт оптимумов. Запускается как asyncio task в main.py.

    По умолчанию раз в 4 часа. Первый запуск через 60 секунд после старта
    (даём боту прогреться).

    NB: SQL агрегация выполняется СИНХРОННО в event loop (~50-500мс при
    1.5-30K signals). asyncio.to_thread НЕЛЬЗЯ — sqlite3.Connection создан
    с check_same_thread=True (default), нельзя использовать из других тредов.
    Раз в 4 часа короткая блокировка event loop приемлема.
    """
    await asyncio.sleep(60)
    while True:
        try:
            table = compute_pair_hour_expiry(journal)
            journal.set("pair_hour_expiry", table)
            if table:
                logger.info("expiry_optimizer: saved %d (sym×hour) cells", len(table))
            # Промежуточный fallback per-pair (≥30 сигналов overall) —
            # используется когда конкретный час ещё не накопил данных.
            overall = compute_pair_overall_expiry(journal)
            journal.set("pair_overall_expiry", overall)
            if overall:
                logger.info("expiry_optimizer: saved %d per-pair overall optima",
                            len(overall))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("expiry_optimizer crashed, continuing in %ds", sleep_seconds)
        await asyncio.sleep(sleep_seconds)
