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
from pathlib import Path
from typing import Any, Optional

import yaml
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
                [p for p in [getattr(s, "current_pair", None)] if p]
                + list(getattr(s, "switched_pairs", []) or [])
            ) if (getattr(s, "mg_step", 0) > 0 or getattr(s, "current_pair", None)) else [],
            "filter_active": bool(
                journal and registry and
                ((journal.signal_filter_get(registry.active_name) or {}).get("enabled"))
            ),
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
        return {"updated": list(payload.keys()), "cfg": cfg}

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
        rows = journal.analytics_aggregate(
            since_ts=since, strategy_name=strat,
            hour_from=hour_from, hour_to=hour_to, dow=dow_list,
            expiry_bars_default=eb,
        )
        return {
            "period_days": period_days,
            "strategy": strat,
            "hour_from": hour_from, "hour_to": hour_to,
            "dow": dow_list,
            "expiry_bars": eb,
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
        rows = journal.analytics_aggregate(
            since_ts=since, strategy_name=strat,
            expiry_bars_default=eb, only_symbol=symbol, **kwargs,
        )
        return {"symbol": symbol, "period_days": period_days,
                "strategy": strat, "group": group, "rows": rows}

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
        rows = journal.analytics_aggregate(
            since_ts=since, strategy_name=strat,
            hour_from=hour_from, hour_to=hour_to, dow=dow_list,
            expiry_bars_default=eb,
        )
        cols = ["symbol", "signals", "entered", "settled",
                "wr_first", "wr_chosen", "wr_best",
                "pluses", "minuses", "max_loss_streak_to_win",
                "avg_payout", "pct_payout_optimal",
                "avg_votes_total", "avg_atr_ratio", "avg_bb_position",
                "avg_candle_atr_ratio", "avg_rsi_ma", "avg_qqe_trailing",
                "avg_wr1_long", "avg_wr1_recent",
                "wins_real", "losses_real", "wr_real", "profit_real"]
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

    @app.post("/api/analytics/backfill")
    async def analytics_backfill(request: Request, payload: dict = None):
        """Прогнать активную стратегию по всем cached candles и вписать
        исторические signals + market snapshots + exp_wins.
        Идемпотентно — можно жать многократно."""
        _auth(request)
        if not sm:
            raise HTTPException(503, "state machine not ready")
        p = payload or {}
        payout = int(p.get("payout_default", 92))
        limit = p.get("limit_pairs")
        result = await sm.backfill_history(
            payout_default=payout,
            limit_pairs=int(limit) if limit else None,
        )
        return result

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
