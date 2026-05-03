"""Локальный preview Mini App для визуальной проверки изменений.

Запуск:
    python tools/preview_miniapp.py

Что делает:
- создаёт временный Journal с подставными trades (один разрыв 1ч 43м, другой 22мин);
- запускает FastAPI без реального PO-подключения;
- ставит mock-feed (balance=$278.22) и mock-state (FREE);
- слушает на http://127.0.0.1:8090/miniapp/ — auth выключена потому что bot_token=""

Открой в браузере http://127.0.0.1:8090/miniapp/ и проверь строку
«⏱️ Макс. без торговли» в карточке «Состояние» Главной вкладки.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from journal.db import Journal
from api.server import create_app


class MockFeed:
    def balance(self): return 278.22


class MockState:
    current_pair = None
    mg_step = 0
    session_loss = 0.0
    paused = False
    waiting_resume = False
    day_off_until = 0
    cycle_unused_carry = 0
    cycle_switches = 0
    switched_pairs = []
    original_pair = None
    direction = None
    trades_on_pair = 0


class MockSM:
    state = MockState()
    _tracked = {"EURUSD_otc", "GBPUSD_otc"}
    _tick_counts = {"EURUSD_otc": 5}


class MockRegistry:
    active_name = "consensus"


def main():
    db_path = tempfile.mktemp(suffix="-preview.db")
    j = Journal(db_path)
    now = int(time.time())

    # Сценарий: 24/7, окно = последние 24 часа.
    # Заполняем сутки сделками каждые 30 минут — кроме одного «провала» 1ч 43м.
    # Trailing gap (последний close → now) делаем ~5 мин, чтобы он не съел макс.
    T0 = now - 24 * 3600
    BAD_GAP_AFTER_IDX = 6     # после 6-й сделки вставим длинный gap

    rows = []
    cursor = T0 + 5 * 60      # первая сделка через 5 мин после старта окна
    idx = 0
    while True:
        idx += 1
        # сделка длится 2 минуты
        o, c = cursor, cursor + 120
        if c > now - 5 * 60:
            break
        result = "WIN" if idx % 2 == 1 else "LOSS"
        profit = 1.92 if result == "WIN" else 0.0
        rows.append((f"t{idx}", "EURUSD_otc", "call", 1.0, profit, result, 92, 0,
                     o, c, 100.0 + idx * 0.1, "real"))
        # gap между сделками: обычно 30 мин, после 6-й — 1ч 43м
        gap = (1 * 3600 + 43 * 60) if idx == BAD_GAP_AFTER_IDX else 30 * 60
        cursor = c + gap

    # «Trailing» сделка прямо перед now — чтобы gap last_close → now был мал
    rows.append(("t-last", "EURUSD_otc", "call", 1.0, 1.92, "WIN", 92, 0,
                 now - 5 * 60, now - 3 * 60, 110.0, "real"))

    for r in rows:
        j.conn.execute(
            "INSERT INTO trades(trade_id,symbol,action,amount,profit,result,payout,"
            "mg_step,open_ts,close_ts,balance_after,mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            r,
        )
    j.conn.commit()

    gap = j.max_no_trade_gap(now - 86400, now, "real")
    print(f"[preview] max_no_trade_gap = {gap}s = {gap//3600}ч {(gap%3600)//60}м "
          f"(ожидаем ~1ч 43м)")

    cfg = {
        "mode": "real",
        "trading": {"base_amount": 1.6, "expiry_seconds": 120},
        "schedule": {"enabled": False, "start_hour": 9, "end_hour": 22},
        "telegram": {"daily_report_timezone": "Europe/Kyiv"},
    }
    cfg_path = tempfile.mktemp(suffix="-cfg.yaml")
    with open(cfg_path, "w") as f:
        yaml.safe_dump(cfg, f)

    app = create_app(
        cfg=cfg, config_path=cfg_path,
        registry=MockRegistry(), sm=MockSM(), feed=MockFeed(),
        journal=j, bot_token="",  # token=="" → auth выключена
        allowed_chat_id=0,
    )

    import uvicorn
    print("[preview] http://127.0.0.1:8090/miniapp/")
    uvicorn.run(app, host="127.0.0.1", port=8090, log_level="warning")


if __name__ == "__main__":
    main()
