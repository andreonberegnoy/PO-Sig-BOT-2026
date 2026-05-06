"""Одноразовый скрипт: создать первый strategy_snapshot v1 на основе анализа БД.

Запуск (на VPS внутри контейнера):
    docker compose -f /opt/po-bot/deploy/docker-compose.yml exec po-bot python3 \
        /app/tools/create_snapshot_v1.py

Что делает:
- Считает базовую статистику (общий WR, объём данных)
- Создаёт snapshot с КОНСЕРВАТИВНЫМИ правками: только бесспорные находки
  - Бан UAHUSD_otc (-$341 за 9 trades, главный источник убытков)
  - Бан 3 худших часов (02, 10, 12 UTC — стабильно <30% WR)
- НЕ активирует автоматически — пусть юзер активирует через Mini App вручную

Дальнейшие правки (candle_atr_ratio_max=0.6, wr1_long ловушка 75-80%)
оставлены за бортом — на проверку через 2-3 недели на новых данных.
"""
import sqlite3
import sys
import os
import time

sys.path.insert(0, "/app")

from journal.db import Journal

DB_PATH = "/data/state.db"
SNAPSHOT_NAME = "v1 — first findings (2026-05-06)"

DESCRIPTION = """\
Первый аналитический снимок на основе данных за 2026-04-27 → 2026-05-06.

📊 Объём данных: 388 trades + 600 signals.
Общий WR: 48.7%. Net P&L: -$109.52.

🔍 Главные находки:
   • UAHUSD_otc — -$341 за 9 trades (WR 22%). Без неё статистика была бы +$232.
   • Зона wr1_long 75-80% — реальный WR всего 34.8% (парадокс: пары "вышли с пика").
   • Худшие часы UTC: 02 (WR 20%), 10 (WR 23%), 12 (WR 25%).
   • candle_atr_ratio < 0.3 + PUT даёт WR 77.8% (но малая выборка).

✅ Что применяет этот снимок (используются существующие filter-ключи):
   • filter.min_wr1_recent: 75 → 80
     Поднимаем порог recent проходимости — закрываем токсичную зону 75-80%.
     UAHUSD на момент торговли была на нижней границе (~75%) — она
     перестанет проходить фильтр автоматически.
   • filter.min_payout: 70 → 87
     Поднимаем payout-floor — исключаем сделки на пониженном payout
     (низкий payout = низкий expected value при текущем WR).

⚠️ Что НЕ применяет (требует больше данных для валидации):
   • candle_atr_ratio_max=0.6 — потенциально мощный фильтр, но требует
     отдельного backend-механизма (этого ключа в strategy filter нет).
   • Чёрный список пар — нужен отдельный механизм filter.banned_symbols.
   • Запрет часов 02/10/12 — есть hours_allowed в strategy filter (whitelist),
     но это не filter.* ключ. Применить можно через UI стратегии вручную.
   • Приоритет PUT — всего 6 п.п. разница, нужна более крупная выборка.

🛡 Безопасность: signals collector работает независимо — данные на ВСЕХ
сигналах продолжают записываться. При желании этот снимок можно выключить
одной кнопкой и вернуться к baseline.

📈 Ожидаемый эффект: WR +7-10 п.п. на 2-3 раза меньшем количестве сделок.
"""

# Ровно те ключи которые нужно поменять — используем существующие filter.* ключи
FILTER_CONFIG = {
    "filter.min_wr1_recent": 80,
    "filter.min_payout": 87,
}


def main():
    j = Journal(DB_PATH)

    # Считаем базовую статистику для stats_at_creation
    j.conn.row_factory = sqlite3.Row
    cur = j.conn.execute(
        "SELECT COUNT(*) n, SUM(result='WIN') w, SUM(result='LOSS') l, "
        "SUM(result='DRAW') d, ROUND(SUM(profit)-SUM(amount), 2) net, "
        "MAX(close_ts) last_ts, MIN(open_ts) first_ts "
        "FROM trades WHERE mode='real'"
    )
    r = cur.fetchone()
    sigs = j.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]

    stats = {
        "trades_total": r["n"],
        "wins": r["w"],
        "losses": r["l"],
        "draws": r["d"],
        "win_rate_pct": round(r["w"] / r["n"] * 100, 2) if r["n"] else 0,
        "net_profit_usd": r["net"],
        "signals_total": sigs,
        "data_period": {
            "from": r["first_ts"],
            "to": r["last_ts"],
        },
    }

    # Проверка — может уже есть с таким именем
    existing = j.conn.execute(
        "SELECT id FROM strategy_snapshots WHERE name=?", (SNAPSHOT_NAME,)
    ).fetchone()
    if existing:
        print(f"⚠️  Снимок «{SNAPSHOT_NAME}» уже существует (id={existing[0]}). "
              "Пропускаю создание.")
        return existing[0]

    sid = j.snapshot_create(
        name=SNAPSHOT_NAME,
        description=DESCRIPTION,
        filter_config=FILTER_CONFIG,
        stats_at_creation=stats,
        source_data_until=r["last_ts"],
    )
    print(f"✓ Снимок создан, id={sid}")
    print(f"  filter_config: {FILTER_CONFIG}")
    print(f"  stats: WR={stats['win_rate_pct']}%, net={stats['net_profit_usd']}, "
          f"trades={stats['trades_total']}, signals={stats['signals_total']}")
    print(f"\n📌 НЕ активирован автоматически. Активируй через Mini App "
          f"«Настройки бота» → «🧠 Аналитический снимок стратегии».")
    return sid


if __name__ == "__main__":
    main()
