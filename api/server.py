"""FastAPI backend serving Mini App + REST API for the bot.

Runs alongside the bot's main asyncio loop via uvicorn.
Endpoints (under /api):
    GET  /api/status          — bot state snapshot
    GET  /api/settings        — full cfg dict
    PUT  /api/settings        — update cfg by dotted key paths
    GET  /api/strategies      — list strategies
    POST /api/strategies      — upload new user strategy {name, code}
    DELETE /api/strategies/{name}
    POST /api/strategies/{name}/activate

Mini App static files served at /miniapp/*.
Auth: Telegram WebApp `initData` in `X-Init-Data` header verified against
TELEGRAM_TOKEN. Allowed user must match TELEGRAM_CHAT_ID.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Tuple

import yaml

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from api.auth import verify_init_data

logger = logging.getLogger(__name__)


def _set(cfg: dict, key: str, value: Any) -> None:
    parts = key.split(".")
    cur = cfg
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


def trading_day_window(cfg: dict, now_ts: Optional[int] = None) -> Tuple[int, int]:
    """Окно «торгового дня» для аналитики max_no_trade_gap.

    Логика:
      - schedule.enabled=true → окно [today's start_hour, min(now, end_hour)]
        в TZ из telegram.daily_report_timezone. Если now < start_hour сегодня —
        отдаём предыдущие торговые сутки (вчера start..end). Метрика покажет
        максимум простоя ТОЛЬКО в рабочих часах, ночь игнорируется.
      - schedule.enabled=false → rolling 24h: (now - 86400, now). При
        срабатывании периодического отчёта это естественно совпадает с
        интервалом «между двумя отчётами».

    Возвращает (since_ts, until_ts) в UTC unix-секундах.
    """
    now_ts = int(now_ts if now_ts is not None else time.time())
    sched = (cfg.get("schedule") or {})
    if not bool(sched.get("enabled")):
        return now_ts - 86400, now_ts
    try:
        start_hour = int(sched.get("start_hour", 0))
        end_hour = int(sched.get("end_hour", 24))
    except Exception:
        return now_ts - 86400, now_ts
    tz_name = ((cfg.get("telegram") or {}).get("daily_report_timezone") or "UTC")
    try:
        tz = ZoneInfo(tz_name) if ZoneInfo else None
    except Exception:
        tz = None
    now_local = datetime.fromtimestamp(now_ts, tz=tz) if tz else datetime.utcfromtimestamp(now_ts)
    today_start = now_local.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    today_end = now_local.replace(hour=end_hour % 24, minute=0, second=0, microsecond=0)
    if end_hour >= 24:
        today_end = today_end + timedelta(days=1)
    if now_local < today_start:
        # ещё не открылось сегодня — показываем вчерашний день
        today_start -= timedelta(days=1)
        today_end -= timedelta(days=1)
    until_local = min(now_local, today_end)
    return int(today_start.timestamp()), int(until_local.timestamp())


def _save_yaml(cfg: dict, path: str) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)
    except Exception:
        logger.exception("save config.yaml failed")


def create_app(*, cfg: dict, config_path: str, registry, sm, feed, journal, bot_token: str,
               allowed_chat_id: int) -> FastAPI:
    """Build a FastAPI app wired to the running bot's components."""
    app = FastAPI(title="PO-Sig Bot API")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Auth dependency
    def _auth(request: Request) -> dict:
        # In production: check initData. In dev (no token): allow.
        init = request.headers.get("X-Init-Data") or request.query_params.get("initData", "")
        if not bot_token:
            return {}
        user = verify_init_data(init, bot_token)
        if user is None:
            raise HTTPException(401, "invalid initData")
        if allowed_chat_id and user.get("id") != allowed_chat_id:
            raise HTTPException(403, "user not allowed")
        return user

    # ─── status ───
    @app.get("/api/status")
    async def get_status(request: Request):
        _auth(request)
        s = sm.state if sm else None
        balance = feed.balance() if feed else None
        try:
            banned_count = len(journal.active_bans()) if journal else 0
        except Exception:
            banned_count = 0
        # ── минимальный payout + макс. глубина восстановления за 24h ──
        min_payout_24h = None
        max_recovered_24h = None
        try:
            if journal:
                _since = int(time.time()) - 86400
                _mode = cfg.get("mode") or "real"
                min_payout_24h = journal.min_payout_24h(_since, _mode)
                max_recovered_24h = journal.max_recovered_losses_24h(_since, _mode)
        except Exception:
            logger.exception("24h analytics failed")
        # ── max no-trade gap в окне текущего торгового дня ──
        no_trade_gap_s = 0
        no_trade_window = None
        try:
            if journal:
                since_ts, until_ts = trading_day_window(cfg)
                no_trade_gap_s = int(journal.max_no_trade_gap(
                    since_ts, until_ts, cfg.get("mode") or "real",
                ))
                no_trade_window = {
                    "since_ts": since_ts, "until_ts": until_ts,
                    "schedule_enabled": bool((cfg.get("schedule") or {}).get("enabled")),
                }
        except Exception:
            logger.exception("max_no_trade_gap failed")
        return {
            "mode": cfg.get("mode"),
            "balance": balance,
            "current_pair": getattr(s, "current_pair", None),
            "mg_step": getattr(s, "mg_step", 0),
            "session_loss": getattr(s, "session_loss", 0.0),
            "paused": getattr(s, "paused", False),
            "waiting_resume": getattr(s, "waiting_resume", False),
            "tracked_pairs": len(getattr(sm, "_tracked", set()) or set()) if sm else 0,
            "tracked_pairs_list": sorted(list(getattr(sm, "_tracked", set()) or set())) if sm else [],
            "banned_pairs": banned_count,
            "active_strategy": registry.active_name if registry else None,
            "active_syms": sum(1 for v in (getattr(sm, "_tick_counts", {}) or {}).values() if v > 0) if sm else 0,
            "base_amount": (cfg.get("trading") or {}).get("base_amount", 1),
            "expiry_seconds": (cfg.get("trading") or {}).get("expiry_seconds", 120),
            # Этап 3 — расширенный статус для UI
            "day_off_until": getattr(s, "day_off_until", 0),
            "cycle_unused_carry": getattr(s, "cycle_unused_carry", 0),
            "cycle_switches": getattr(s, "cycle_switches", 0),
            "switched_pairs": list(getattr(s, "switched_pairs", []) or []),
            "original_pair": getattr(s, "original_pair", None),
            "direction": getattr(s, "direction", None),
            "trades_on_pair": getattr(s, "trades_on_pair", 0),
            "active_cycle_pairs": (
                # Stage 3: в parallel mode возвращаем пары из pair_cycles,
                # иначе legacy current_pair + switched_pairs.
                list((getattr(s, "pair_cycles", {}) or {}).keys())
                if (cfg.get("trading") or {}).get("parallel_pairs", False)
                else (
                    [p for p in [getattr(s, "current_pair", None)] if p]
                    + list(getattr(s, "switched_pairs", []) or [])
                ) if (getattr(s, "mg_step", 0) > 0 or getattr(s, "current_pair", None)) else []
            ),
            # Parallel cycles details — для UI чтобы показать N активных циклов
            "parallel_mode": bool((cfg.get("trading") or {}).get("parallel_pairs", False)),
            "parallel_cycles": (
                {sym: {"mg_step": c.get("mg_step", 0),
                       "direction": c.get("direction"),
                       "cycle_loss": round(c.get("cycle_loss", 0.0), 2),
                       "trades_count": c.get("trades_count", 0),
                       "has_pending": bool(c.get("pending_trade"))}
                 for sym, c in (getattr(s, "pair_cycles", {}) or {}).items()}
            ),
            "max_parallel_pairs": int((cfg.get("trading") or {})
                                       .get("max_parallel_pairs", 3) or 3),
            "filter_active": bool(
                journal and registry and
                ((journal.signal_filter_get(registry.active_name) or {}).get("enabled"))
            ),
            "max_no_trade_gap_seconds": no_trade_gap_s,
            "no_trade_window": no_trade_window,
            "min_payout_24h": min_payout_24h,
            "max_recovered_losses_24h": max_recovered_24h,
        }

    # ─── settings ───
    @app.get("/api/settings")
    async def get_settings(request: Request):
        _auth(request)
        return cfg

    @app.put("/api/settings")
    async def put_settings(request: Request, payload: dict):
        logger.info("PUT /api/settings: request received, payload keys=%s, has_init_data=%s",
                    list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__,
                    bool(request.headers.get("X-Init-Data") or request.query_params.get("initData")))
        try:
            _auth(request)
        except Exception as e:
            logger.warning("PUT /api/settings: auth FAILED — %s", e)
            raise
        # payload format: {"key.path": value, ...}
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected dict of {dotted_key: value}")
        for key, value in payload.items():
            _set(cfg, key, value)
        # Persist BOTH to file (dev workflow) AND SQLite (survives redeploy:
        # config.yaml is baked into container image, kv_store sits on volume).
        _save_yaml(cfg, config_path)
        if journal:
            try:
                overrides = journal.get("settings_overrides") or {}
                for key, value in payload.items():
                    overrides[key] = value
                journal.set("settings_overrides", overrides)
                logger.info("PUT /api/settings: persisted %d keys to journal (total=%d): %s",
                            len(payload), len(overrides), list(payload.keys()))
            except Exception:
                logger.exception("failed to persist settings_overrides to journal")
        else:
            logger.warning("PUT /api/settings: no journal — change will NOT survive deploy")
        # ── авто-пересчёт base_amount при изменении N/q/auto_base_amount.enabled ──
        # Юзерская логика: «если меняю N/коэф — хочу СРАЗУ увидеть новую ставку»,
        # без ожидания 5-ти WIN-ов. Если бот в активном МГ или сделке — recalc
        # сам отложит до WIN (через _is_safe_for_recalc → _pending_recalc_reason).
        recalc_keys = {
            "martingale.cycle_total_limit",
            "martingale.max_steps",
            "martingale.coefficient",
            "trading.auto_base_amount.enabled",
            "trading.auto_base_amount.balance_pct",  # юзер: «выставил 14% → хочу видеть базу сразу»
            "trading.auto_base_amount.min_amount",
        }
        triggered = [k for k in payload.keys() if k in recalc_keys]
        if triggered and sm and bool(((cfg.get("trading") or {}).get("auto_base_amount") or {}).get("enabled", True)):
            try:
                import asyncio as _aio
                _aio.create_task(sm._recalc_base_from_balance(
                    f"ручное изменение настроек ({', '.join(triggered)})"
                ))
                logger.info("PUT /api/settings: scheduled base_amount recalc (triggered by %s)", triggered)
            except Exception:
                logger.exception("failed to schedule base_amount recalc")
        return {"updated": list(payload.keys()), "cfg": cfg}

    # ─── strategy snapshots (versioned filter bundles) ───
    # Snapshot = «слепок аналитических находок на дату X».
    # Применяется ПОВЕРХ settings_overrides когда active=1.
    # Signals collector работает независимо — пишет ВСЕ сигналы.
    # Это позволяет ужесточить фильтр сейчас, но не потерять диверсификацию
    # данных для будущих переанализов через 2-3 месяца.

    @app.get("/api/snapshots")
    async def list_snapshots(request: Request):
        _auth(request)
        if not journal:
            raise HTTPException(503, "journal not available")
        return {"snapshots": journal.snapshot_list(),
                "active": journal.snapshot_get_active()}

    @app.post("/api/snapshots")
    async def create_snapshot(request: Request, payload: dict):
        _auth(request)
        if not journal:
            raise HTTPException(503, "journal not available")
        name = (payload.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "name required")
        filter_config = payload.get("filter_config") or {}
        if not isinstance(filter_config, dict) or not filter_config:
            raise HTTPException(400, "filter_config must be a non-empty dict")
        try:
            sid = journal.snapshot_create(
                name=name,
                description=payload.get("description") or "",
                filter_config=filter_config,
                stats_at_creation=payload.get("stats_at_creation"),
                source_data_until=payload.get("source_data_until"),
            )
            return {"created_id": sid}
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.put("/api/snapshots/{snapshot_id}/activate")
    async def activate_snapshot(request: Request, snapshot_id: int):
        _auth(request)
        if not journal:
            raise HTTPException(503, "journal not available")
        try:
            result = journal.snapshot_activate(snapshot_id)
            # Применяем filter_config ОТДЕЛЬНО к runtime cfg (чтобы не ждать рестарта)
            for k, v in result["filter_config"].items():
                _set(cfg, k, v)
            _save_yaml(cfg, config_path)
            logger.info("Snapshot id=%d activated, applied keys: %s",
                        snapshot_id, result["applied_keys"])
            return result
        except ValueError as e:
            raise HTTPException(404, str(e))
        except Exception:
            logger.exception("activate snapshot failed")
            raise HTTPException(500, "internal error")

    @app.put("/api/snapshots/{snapshot_id}/deactivate")
    async def deactivate_snapshot(request: Request, snapshot_id: int):
        _auth(request)
        if not journal:
            raise HTTPException(503, "journal not available")
        try:
            # Прежде чем менять — запомним filter_config + backup для применения к runtime cfg
            active = journal.snapshot_get_active()
            if not active or active["id"] != snapshot_id:
                raise HTTPException(400, "snapshot not active")
            backup = active["backup_config"]
            filter_keys = list(active["filter_config"].keys())
            result = journal.snapshot_deactivate(snapshot_id)
            # Применяем backup к runtime cfg
            for k in filter_keys:
                prev = backup.get(k)
                if prev is None:
                    # Был не задан → возвращаем default из config.yaml (через перечитку — но это
                    # потребовало бы load_config, чего мы здесь не делаем). Просто оставляем
                    # текущее значение в cfg как есть; настоящий sync будет при рестарте.
                    # На практике достаточно убрать из overrides — следующая загрузка cfg
                    # подтянет default. Runtime cfg инвалидно для этого ключа до рестарта.
                    pass
                else:
                    _set(cfg, k, prev)
            _save_yaml(cfg, config_path)
            logger.info("Snapshot id=%d deactivated, restored keys: %s",
                        snapshot_id, result["restored_keys"])
            return result
        except ValueError as e:
            raise HTTPException(404, str(e))

    @app.delete("/api/snapshots/{snapshot_id}")
    async def delete_snapshot(request: Request, snapshot_id: int):
        _auth(request)
        if not journal:
            raise HTTPException(503, "journal not available")
        try:
            journal.snapshot_delete(snapshot_id)
            return {"deleted": snapshot_id}
        except Exception as e:
            raise HTTPException(400, str(e))

    # ─── strategies ───
    @app.get("/api/strategies")
    async def list_strategies(request: Request):
        _auth(request)
        return {"strategies": registry.list() if registry else [], "active": registry.active_name if registry else None}

    @app.get("/api/strategies/{name}/code")
    async def get_strategy_code(request: Request, name: str):
        _auth(request)
        # Only user-uploaded strategies expose code (builtin import path)
        from strategy.registry import USER_DIR
        path = USER_DIR / f"{name}.py"
        if path.exists():
            return {"name": name, "code": path.read_text(encoding="utf-8")}
        raise HTTPException(404, "no source available")

    @app.post("/api/strategies")
    async def upload_strategy(request: Request, payload: dict):
        _auth(request)
        name = (payload.get("name") or "").strip()
        code = payload.get("code") or ""
        if not name or not code:
            raise HTTPException(400, "name and code required")
        try:
            strat = registry.add_user_code(name, code)
            return {"ok": True, "name": strat.name}
        except Exception as e:
            raise HTTPException(400, f"strategy load failed: {e}")

    @app.delete("/api/strategies/{name}")
    async def delete_strategy(request: Request, name: str):
        _auth(request)
        ok = registry.remove_user_strategy(name)
        if not ok:
            raise HTTPException(400, "cannot remove (builtin or not found)")
        return {"ok": True}

    @app.post("/api/strategies/{name}/activate")
    async def activate_strategy(request: Request, name: str):
        _auth(request)
        ok = registry.set_active(name)
        if not ok:
            raise HTTPException(404, "strategy not found")
        return {"ok": True, "active": name}

    @app.get("/api/strategies/{name}/params")
    async def get_strategy_params(request: Request, name: str):
        _auth(request)
        s = registry.strategies.get(name)
        if not s:
            raise HTTPException(404, "strategy not found")
        return {
            "name": name,
            "params": s.params,
            "default_params": s.default_params,
            "schema": s.param_schema,
        }

    @app.put("/api/strategies/{name}/params")
    async def put_strategy_params(request: Request, name: str, payload: dict):
        _auth(request)
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected dict {key: value}")
        ok = registry.update_params(name, payload)
        if not ok:
            raise HTTPException(404, "strategy not found")
        return {"ok": True, "params": registry.strategies[name].params}

    # ─── control ───
    @app.post("/api/control/{action}")
    async def control(request: Request, action: str):
        _auth(request)
        if not sm:
            raise HTTPException(503, "state machine not ready")
        import asyncio as _aio
        if action == "pause":
            sm.pause()
            if sm.notify:
                _aio.create_task(sm.notify("⏸ Пауза включена через Mini App. /resume чтобы снять."))
            return {"ok": True}
        if action == "resume":
            if sm.state.waiting_resume:
                sm.resume_after_stop_sum()
                msg = "▶️ Возобновление после стоп-суммы. Мартингейл и потери сброшены."
            else:
                sm.resume()
                msg = "▶️ Торговля возобновлена через Mini App."
            if sm.notify:
                _aio.create_task(sm.notify(msg))
            return {"ok": True}
        if action == "reset_cycle":
            s = sm.state
            old = f"{s.current_pair} MG{s.mg_step}" if s.current_pair else "FREE"
            sm.force_reset_cycle()
            if sm.notify:
                _aio.create_task(sm.notify(
                    f"🔄 Цикл сброшен через Mini App ({old} → FREE). Ищу новый сигнал…"
                ))
            return {"ok": True, "old": old}
        if action == "rescan":
            # Force immediate _rescan_pairs (без ожидания 60с тика).
            # Используется при «pull-to-refresh» в Mini App.
            sm._force_rescan = True
            sm._tick_event.set()   # разбудить main loop
            return {"ok": True}
        if action == "switch_pair":
            if not (sm.state.mg_step > 0 and sm.state.current_pair):
                raise HTTPException(400, "no active cycle to switch")
            old_pair = sm.state.current_pair
            result = await sm.force_switch_pair()
            if not result:
                if sm.notify:
                    _aio.create_task(sm.notify(
                        f"⚠️ Mini App: попытка смены пары {old_pair} — нет tracked-пар или цикла."
                    ))
                return {"ok": False, "old": old_pair, "new": None,
                        "reason": "no tracked pairs or cycle"}
            if sm.notify:
                _aio.create_task(sm.notify(
                    f"🔍 Через Mini App: пара {old_pair} исключена из цикла, "
                    f"перехожу в SEARCH-режим. Войду на первый CONSENSUS-сигнал "
                    f"среди tracked-пар (МГ-шаг сохранён)."
                ))
            return {"ok": True, "old": old_pair, "new": result, "mode": result}
        raise HTTPException(400, f"unknown action: {action}")

    # ─── analytics (Stage 2) ───
    @app.get("/api/analytics/pairs")
    async def analytics_pairs(request: Request,
                               period_days: int = 7,
                               strategy: Optional[str] = None,
                               hour_from: Optional[int] = None,
                               hour_to: Optional[int] = None,
                               dow: Optional[str] = None):
        """Главная аналитическая таблица: per-symbol агрегаты по фильтрам.
        period_days: 1/7/14/30/60. dow: '0,1,2'-формат (Mon=0..Sun=6) или null.
        """
        _auth(request)
        if not journal:
            raise HTTPException(503, "journal not ready")
        import time as _t
        since = int(_t.time()) - max(1, period_days) * 86400
        strat = strategy or (registry.active_name if registry else None)
        dow_list = None
        if dow:
            try:
                dow_list = [int(x) for x in dow.split(",") if x.strip() != ""]
            except Exception:
                dow_list = None
        ind_cfg = (cfg.get("indicator") or {})
        eb = int(ind_cfg.get("expiryBars", 2))
        an_cfg = (cfg.get("analytics") or {})
        min_n = int(an_cfg.get("min_sample_size", 20) or 20)
        hl = an_cfg.get("decay_half_life_days")
        rows = journal.analytics_aggregate(
            since_ts=since, strategy_name=strat,
            hour_from=hour_from, hour_to=hour_to, dow=dow_list,
            expiry_bars_default=eb,
            min_sample_size=min_n,
            decay_half_life_days=hl,
        )
        return {
            "period_days": period_days,
            "strategy": strat,
            "hour_from": hour_from, "hour_to": hour_to,
            "dow": dow_list,
            "expiry_bars": eb,
            "min_sample_size": min_n,
            "decay_half_life_days": hl,
            "rows": rows,
        }

    @app.get("/api/analytics/hourly")
    async def analytics_hourly(request: Request,
                                symbol: str,
                                period_days: int = 30,
                                strategy: Optional[str] = None,
                                group: str = "hour"):
        """Drill-down: разбивка для одной пары. group=hour (24-часовая) или
        group=dow (по дням недели Mon..Sun)."""
        _auth(request)
        if not journal:
            raise HTTPException(503, "journal not ready")
        import time as _t
        since = int(_t.time()) - max(1, period_days) * 86400
        strat = strategy or (registry.active_name if registry else None)
        ind_cfg = (cfg.get("indicator") or {})
        eb = int(ind_cfg.get("expiryBars", 2))
        kwargs = {"group_by_hour": True} if group == "hour" else {"group_by_dow": True}
        an_cfg = (cfg.get("analytics") or {})
        min_n = int(an_cfg.get("min_sample_size", 20) or 20)
        hl = an_cfg.get("decay_half_life_days")
        rows = journal.analytics_aggregate(
            since_ts=since, strategy_name=strat,
            expiry_bars_default=eb, only_symbol=symbol,
            min_sample_size=min_n, decay_half_life_days=hl, **kwargs,
        )
        return {"symbol": symbol, "period_days": period_days,
                "strategy": strat, "group": group,
                "min_sample_size": min_n, "decay_half_life_days": hl,
                "rows": rows}

    @app.get("/api/analytics/csv")
    async def analytics_csv(request: Request,
                             period_days: int = 7,
                             strategy: Optional[str] = None,
                             hour_from: Optional[int] = None,
                             hour_to: Optional[int] = None,
                             dow: Optional[str] = None):
        _auth(request)
        if not journal:
            raise HTTPException(503, "journal not ready")
        import time as _t, io, csv
        since = int(_t.time()) - max(1, period_days) * 86400
        strat = strategy or (registry.active_name if registry else None)
        dow_list = None
        if dow:
            try:
                dow_list = [int(x) for x in dow.split(",") if x.strip() != ""]
            except Exception:
                dow_list = None
        ind_cfg = (cfg.get("indicator") or {})
        eb = int(ind_cfg.get("expiryBars", 2))
        an_cfg = (cfg.get("analytics") or {})
        min_n = int(an_cfg.get("min_sample_size", 20) or 20)
        hl = an_cfg.get("decay_half_life_days")
        rows = journal.analytics_aggregate(
            since_ts=since, strategy_name=strat,
            hour_from=hour_from, hour_to=hour_to, dow=dow_list,
            expiry_bars_default=eb,
            min_sample_size=min_n, decay_half_life_days=hl,
        )
        cols = ["symbol", "signals", "entered", "settled",
                "wr_first", "wr_chosen", "wr_best", "best_exp_bar",
                "pluses", "minuses", "max_loss_streak_to_win",
                "avg_payout", "pct_payout_optimal",
                "avg_votes_total", "avg_atr_ratio", "avg_bb_position",
                "avg_candle_atr_ratio", "avg_rsi_ma", "avg_qqe_trailing",
                "avg_wr1_long", "avg_wr1_recent",
                "wins_real", "losses_real", "wr_real", "profit_real",
                # новые колонки (Wilson + per-exp). В конце — чтобы не ломать
                # существующих парсеров CSV если такие у юзера есть.
                "wr_1", "wr_2", "wr_3", "wr_4", "wr_5",
                "wr_1_wlb", "wr_2_wlb", "wr_3_wlb", "wr_4_wlb", "wr_5_wlb",
                "n_for_exp_1", "n_for_exp_2", "n_for_exp_3", "n_for_exp_4", "n_for_exp_5",
                "wr_first_wlb", "wr_chosen_wlb", "wr_real_wlb",
                "best_exp_by_wlb", "best_exp_wlb_value"]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c, "") if r.get(c) is not None else "" for c in cols])
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(
            buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=analytics_{period_days}d.csv"},
        )

    @app.get("/api/analytics/db_info")
    async def analytics_db_info(request: Request):
        _auth(request)
        if not journal:
            raise HTTPException(503, "journal not ready")
        cur = journal.conn.execute("SELECT COUNT(*) FROM signals")
        n = cur.fetchone()[0]
        cur = journal.conn.execute(
            "SELECT MIN(signal_ts), MAX(signal_ts) FROM signals"
        )
        mn, mx = cur.fetchone()
        return {
            "signals_total": n,
            "oldest_ts": mn,
            "newest_ts": mx,
            "db_size_bytes": journal.signals_db_size_bytes(),
            "retention_days": int((cfg.get("retention") or {}).get("signals_days", 180)),
        }

    # ─── per-strategy signal filter (Stage 3) ───
    @app.post("/api/strategies/{name}/filter/preview")
    async def filter_preview(request: Request, name: str, payload: dict = None):
        """Считает предполагаемые границы фильтра по winning signals в срезе.
        Возвращает spec — НЕ сохраняет. Юзер потом редактирует и шлёт PUT.
        Payload: {period_days, hour_from, hour_to, dow, use_best_exp}.
        """
        _auth(request)
        if not journal:
            raise HTTPException(503, "journal not ready")
        if registry and name not in (registry.strategies or {}):
            raise HTTPException(404, f"strategy '{name}' not found")
        import time as _t
        p = payload or {}
        period_days = int(p.get("period_days") or 30)
        since = int(_t.time()) - max(1, period_days) * 86400
        hf = p.get("hour_from"); ht = p.get("hour_to")
        dow = p.get("dow")
        ind_cfg = (cfg.get("indicator") or {})
        eb = int(ind_cfg.get("expiryBars", 2))
        spec = journal.build_filter_preview(
            strategy_name=name, since_ts=since,
            hour_from=hf, hour_to=ht, dow=dow,
            expiry_bars=eb, use_best_exp=bool(p.get("use_best_exp")),
        )
        return spec

    @app.get("/api/strategies/{name}/filter")
    async def filter_get(request: Request, name: str):
        _auth(request)
        if not journal:
            return {}
        return journal.signal_filter_get(name) or {}

    @app.put("/api/strategies/{name}/filter")
    async def filter_set(request: Request, name: str, spec: dict):
        _auth(request)
        if not journal:
            raise HTTPException(503, "journal not ready")
        if not isinstance(spec, dict):
            raise HTTPException(400, "expected dict")
        # Базовая валидация — обрезать неизвестные поля чтобы не разносить мусор
        ALLOWED = {
            "enabled", "atr_ratio_min", "atr_ratio_max",
            "bb_position_min", "bb_position_max",
            "candle_atr_ratio_max", "rsi_ma_min", "rsi_ma_max",
            "payout_min", "votes_total_min",
            "hours_allowed", "dow_allowed",
            "based_on", "note",
        }
        clean = {k: v for k, v in spec.items() if k in ALLOWED}
        clean["enabled"] = bool(clean.get("enabled", False))
        journal.signal_filter_set(name, clean)
        return {"ok": True, "filter": journal.signal_filter_get(name)}

    @app.delete("/api/strategies/{name}/filter")
    async def filter_delete(request: Request, name: str):
        _auth(request)
        if not journal:
            return {"ok": True}
        journal.signal_filter_delete(name)
        return {"ok": True}

    @app.get("/api/strategies/{name}/filter/export")
    async def filter_export(request: Request, name: str):
        """Скачать активный фильтр как JSON для ручного редактирования."""
        _auth(request)
        if not journal:
            raise HTTPException(503, "journal not ready")
        spec = journal.signal_filter_get(name) or {}
        import json as _json
        from fastapi.responses import PlainTextResponse
        body = _json.dumps(spec, ensure_ascii=False, indent=2)
        return PlainTextResponse(
            body, media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename=filter_{name}.json"},
        )

    @app.get("/api/profit_today")
    async def get_profit_today(request: Request):
        _auth(request)
        if not journal:
            return {"profit": 0, "trades": 0, "wins": 0, "losses": 0}
        import time as _t
        # начало суток в TZ из telegram.daily_report_timezone
        tz_name = (cfg.get("telegram") or {}).get("daily_report_timezone") or "Europe/Kyiv"
        try:
            import pytz, datetime as _dt
            tz = pytz.timezone(tz_name)
            now_local = _dt.datetime.now(tz)
            start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            since = int(start_local.timestamp())
        except Exception:
            since = int(_t.time()) - 86400
        return journal.profit_today(since, cfg.get("mode", "paper"))

    # ─── chart panel: candles / pair_score / payout_pairs ───
    @app.get("/api/candles")
    async def get_candles(request: Request, symbol: str, period: int = 60, limit: int = 1100):
        """Свечи для рендера live-графика. Сначала live cache state_machine,
        fallback в CandlesDB (persistent storage)."""
        _auth(request)
        candles = []
        if sm and hasattr(sm, "_candles"):
            buf = sm._candles.get(symbol) or []
            if len(buf) >= 100:
                candles = buf
        if len(candles) < 100:
            try:
                from journal.candles_db import CandlesDB
                import os as _os
                cdb_path = "/data/candles.db" if _os.path.isdir("/data") else "data/candles.db"
                cdb = CandlesDB(cdb_path)
                is_demo = (cfg.get("mode") == "paper")
                fetched = cdb.load(symbol, period, is_demo=is_demo, limit=limit)
                if fetched:
                    candles = fetched
            except Exception:
                logger.exception("candles_db load failed for %s", symbol)
        windowed = candles[-limit:]
        out = []
        for c in windowed:
            out.append({
                "time":  int(c["time"]),
                "open":  float(c["open"]),
                "high":  float(c["high"]),
                "low":   float(c["low"]),
                "close": float(c["close"]),
            })
        # Маркеры BUY/SELL + ✓/✗ — ИСТОЧНИК таблица `signals` (immutable).
        # Раньше использовался analyze(windowed) что давало repaint при
        # сдвиге буфера (см. tg/chart.py для детального обоснования).
        markers = []
        try:
            from strategy.consensus import DEFAULT_PARAMS
            params = {**DEFAULT_PARAMS, **(cfg.get("indicator") or {})}
            if sm and sm.registry:
                try:
                    params = sm.registry.get_active().merged_params()
                except Exception:
                    pass
            expiry_bars = int(params.get("expiryBars", 2))
            exp_bar_idx = max(0, min(4, expiry_bars - 1))
            if windowed and journal:
                buf_start_ts = int(windowed[0]["time"])
                buf_end_ts = int(windowed[-1]["time"])
                db_signals = journal.signals_in_range(symbol, buf_start_ts, buf_end_ts)
                # Маппинг signal_ts → ближайший candle index
                window_times = [int(c["time"]) for c in windowed]
                for sg in db_signals:
                    ts = sg["signal_ts"]
                    if ts < window_times[0] or ts > window_times[-1]:
                        continue
                    ci = min(range(len(window_times)),
                              key=lambda j: abs(window_times[j] - ts))
                    bar = windowed[ci]
                    side = (sg["side"] or "").lower()
                    is_buy = side in ("buy", "call")
                    markers.append({
                        "time":     int(bar["time"]),
                        "position": "belowBar" if is_buy else "aboveBar",
                        "shape":    "arrowUp"  if is_buy else "arrowDown",
                        "color":    "#22c55e"  if is_buy else "#ef4444",
                        "text":     "BUY" if is_buy else "SELL",
                        "size":     1.0,
                    })
                    # WIN/LOSS маркер на exit-баре: exit_index = ci + expiry_bars
                    # (counterfactual: исход на N-м баре после сигнала).
                    ew = sg.get("exp_wins")
                    if ew and len(ew) > exp_bar_idx:
                        outcome = ew[exp_bar_idx]
                        if outcome is None:
                            continue  # DRAW — без маркера
                        exit_ci = ci + expiry_bars
                        if 0 <= exit_ci < len(windowed):
                            exit_bar = windowed[exit_ci]
                            if outcome:
                                markers.append({
                                    "time":     int(exit_bar["time"]),
                                    "position": "aboveBar" if is_buy else "belowBar",
                                    "shape":    "circle",
                                    "color":    "#22c55e",
                                    "text":     "✓",
                                    "size":     0.8,
                                })
                            else:
                                markers.append({
                                    "time":     int(exit_bar["time"]),
                                    "position": "aboveBar" if is_buy else "belowBar",
                                    "shape":    "square",
                                    "color":    "#ef4444",
                                    "text":     "✗",
                                    "size":     0.8,
                                })
            # LWC требует sorted by time ASC
            markers.sort(key=lambda m: m["time"])
        except Exception:
            logger.exception("markers compute failed for %s", symbol)
        return {"symbol": symbol, "candles": out, "markers": markers, "count": len(out)}

    @app.get("/api/pair_score")
    async def get_pair_score(request: Request, symbol: str):
        """HUD-данные для пары (стиль PoSignals): WR / WR1 / WR1_recent /
        max_streak / последние результаты / payout / allowed/ban/pause."""
        _auth(request)
        if not sm:
            raise HTTPException(503, "state machine not ready")
        # ИСТОЧНИК ИСТИНЫ — таблица `signals` в БД (immutable). Раньше
        # totals считались через analyze(candles) на live-буфере, но HTF
        # использует buffer-relative группировку (strategy/consensus.py:107)
        # из-за чего одни и те же бары давали 4/5 ↔ 3/5 при сдвиге буфера
        # → сигналы «исчезали» из аналитики, totals/sequence плясали.
        # Юзер 2026-05-16: «важно чтобы статистика не менялась, иначе нет
        # смысла анализировать».
        #
        # Сигналы в БД пишутся в момент срабатывания (insert_signal) и
        # больше не пересчитываются. Из PairScore берём только runtime-
        # флаги (allowed/ban/pause/reason). Fallback на analyze() если в
        # БД ещё пусто (новая пара).
        score = sm._pair_scores.get(symbol)
        candles = sm._candles.get(symbol) or []
        recent_results: list[int] = []
        signals_count = 0
        completed = 0
        completed_recent = 0
        signals_recent = 0
        recent_lookback_bars = 200
        expiry_bars = 2
        wr_val: Optional[float] = None
        wr1_val: Optional[float] = None
        wr1_recent_val: Optional[float] = None
        wins_val = 0
        losses_val = 0
        max_loss_streak_val = 0
        max_loss_streak_before_win_val = 0
        if len(candles) >= 100:
            try:
                from strategy.consensus import DEFAULT_PARAMS
                params = {**DEFAULT_PARAMS, **(cfg.get("indicator") or {})}
                expiry_bars = int(params.get("expiryBars", 2))
                if sm.registry:
                    try:
                        params = sm.registry.get_active().merged_params()
                        expiry_bars = int(params.get("expiryBars", 2))
                    except Exception:
                        pass
                recent_lookback_bars = int(params.get("recentLookbackBars", 200))
                long_lookback_bars = int(params.get("statsLookbackBars", 1000))
                # ИСТОЧНИК — БД signals (immutable). См. tg/chart.py.
                # Lookback по времени (не по длине буфера) — буфер может
                # быть короче чем statsLookbackBars если пара только что
                # ротировалась в _tracked. БД хранит всю историю.
                tf_sec = 60
                buf_end_ts = int(candles[-1]["time"])
                long_since_ts = buf_end_ts - long_lookback_bars * tf_sec
                recent_since_ts = buf_end_ts - recent_lookback_bars * tf_sec
                db_signals = journal.signals_in_range(symbol, long_since_ts, buf_end_ts) \
                    if journal else []
                exp_bar_idx = max(0, min(4, expiry_bars - 1))
                stats = type(journal).aggregate_signal_stats(
                    db_signals, exp_bar_idx=exp_bar_idx,
                    recent_since_ts=recent_since_ts,
                ) if db_signals else None
                if stats:
                    recent_results = stats["recent_results"]
                    signals_count = stats["signals_count"]
                    completed = stats["completed"]
                    completed_recent = stats["completed_recent"]
                    wins_val = stats["wins"]
                    losses_val = stats["losses"]
                    wr_val = stats["wr"]
                    wr1_val = stats["wr1"]
                    wr1_recent_val = stats["wr1_recent"]
                    max_loss_streak_val = stats["max_loss_streak_overall"]
                    max_loss_streak_before_win_val = stats["max_loss_streak_before_win"]
                    signals_recent = stats["signals_recent"]
                else:
                    # Fallback на legacy analyze() если в БД пока пусто
                    # (например пара только что появилась).
                    from strategy.consensus import analyze
                    a = analyze(candles, params)
                    recent_results = list(a.recent_results)
                    signals_count = len(a.signals)
                    completed = a.completed
                    completed_recent = a.completed_recent
                    wins_val = a.wins
                    losses_val = a.losses
                    wr_val = a.wr
                    wr1_val = a.wr1
                    wr1_recent_val = a.wr1_recent
                    max_loss_streak_val = a.max_loss_streak_overall
                    max_loss_streak_before_win_val = a.max_loss_streak_before_win
                    from_bar_recent = max(0, (len(candles) - 1) - recent_lookback_bars)
                    signals_recent = sum(1 for ev in a.signals if ev.i >= from_bar_recent)
            except Exception:
                logger.exception("analyze failed for %s", symbol)
        try:
            payout = int((feed.assets.get(symbol) or {}).get("payout") or 0) if feed else 0
        except Exception:
            payout = 0
        return {
            "symbol": symbol,
            "payout": payout,
            # Все статы из одного analyze() — консистентно с чартом.
            "wr": wr_val,
            "wr1": wr1_val,
            "wr1_recent": wr1_recent_val,
            "wins": wins_val,
            "losses": losses_val,
            "completed": completed,
            "signals_count": signals_count,
            # Окно «свежей формы» (по умолчанию 200 баров):
            #   completed_recent — сколько settled-сигналов за окно (для WR1_recent)
            #   signals_recent   — общее число сработок CONSENSUS за окно
            #   recent_lookback_bars — размер окна (для динамической подписи в HUD)
            "completed_recent": completed_recent,
            "signals_recent": signals_recent,
            "recent_lookback_bars": recent_lookback_bars,
            "max_loss_streak": max_loss_streak_val,
            "max_loss_streak_before_win": max_loss_streak_before_win_val,
            # Runtime-флаги из PairScore (не в Analysis): allowed/ban/pause/reason.
            "allowed": getattr(score, "allowed", False) if score else False,
            "ban": getattr(score, "ban", False) if score else False,
            "pause": getattr(score, "pause", False) if score else False,
            "reason": getattr(score, "reason", "") if score else "",
            "recent_results": recent_results,
            "expiry_bars": expiry_bars,
        }

    @app.get("/api/payout_pairs")
    async def get_payout_pairs(request: Request, min_payout: Optional[int] = None):
        """Все доступные пары (из feed.assets) с payout >= min_payout.
        По умолчанию min_payout берётся из cfg.filter.min_payout
        («Мин. payout для первой сделки» в Настройках).
        Передачей ?min_payout=N можно переопределить."""
        _auth(request)
        if min_payout is None:
            min_payout = int((cfg.get("filter") or {}).get("min_payout", 90))
        if not feed:
            return {"pairs": [], "min_payout": min_payout}
        out = []
        for sym, info in (feed.assets or {}).items():
            try:
                p = int(info.get("payout") or 0)
            except Exception:
                continue
            if p < min_payout:
                continue
            out.append({"symbol": sym, "payout": p})
        out.sort(key=lambda x: -x["payout"])
        return {"pairs": out, "count": len(out), "min_payout": min_payout}

    # ─── miniapp static ───
    miniapp_dir = Path("miniapp")
    if miniapp_dir.exists():
        app.mount("/miniapp", StaticFiles(directory=str(miniapp_dir), html=True), name="miniapp")

    @app.get("/", response_class=HTMLResponse)
    async def root():
        idx = miniapp_dir / "index.html"
        if idx.exists():
            return FileResponse(str(idx))
        return HTMLResponse("<h1>PO-Sig Bot</h1><p>API up. Mini App not yet deployed.</p>")

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/api/debug/journal")
    async def debug_journal(request: Request):
        """Show journal db path, size, and all state_kv keys with timestamps —
        helps diagnose why settings don't survive deploys.
        No auth — read-only, no secrets exposed (only key names + sizes)."""
        import os, time
        path = getattr(journal, "path", "unknown") if journal else None
        abs_path = os.path.abspath(path) if path else None
        size = os.path.getsize(path) if path and os.path.exists(path) else None
        rows = []
        if journal:
            try:
                cur = journal.conn.execute(
                    "SELECT k, length(v) as vlen, updated_at FROM state_kv ORDER BY k"
                )
                rows = [{"key": r[0], "value_bytes": r[1],
                         "updated_at": r[2],
                         "updated_ago_sec": int(time.time()) - int(r[2])}
                        for r in cur.fetchall()]
            except Exception as e:
                rows = [{"error": str(e)}]
        # List /app/data so we can see what's actually persisted on the volume
        data_dir_listing = []
        for d in ("/app/data", "data", "/app"):
            try:
                if os.path.isdir(d):
                    items = []
                    for name in sorted(os.listdir(d)):
                        full = os.path.join(d, name)
                        if os.path.isfile(full):
                            try:
                                items.append({
                                    "name": name,
                                    "size": os.path.getsize(full),
                                    "mtime": int(os.path.getmtime(full)),
                                })
                            except Exception:
                                pass
                    data_dir_listing.append({"dir": d, "files": items})
            except Exception:
                pass
        return {
            "db_path_config": path,
            "db_path_abs": abs_path,
            "db_size_bytes": size,
            "cwd": os.getcwd(),
            "keys": rows,
            "filesystem": data_dir_listing,
        }

    @app.get("/strategy_template")
    async def strategy_template():
        path = Path("strategy/_template.py")
        if path.exists():
            return Response(content=path.read_text(encoding="utf-8"),
                            media_type="text/plain; charset=utf-8")
        return Response(content="# template not found", media_type="text/plain")

    return app
