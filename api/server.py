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
            "banned_pairs": banned_count,
            "active_strategy": registry.active_name if registry else None,
            "active_syms": sum(1 for v in (getattr(sm, "_tick_counts", {}) or {}).values() if v > 0) if sm else 0,
            "base_amount": (cfg.get("trading") or {}).get("base_amount", 1),
            "expiry_seconds": (cfg.get("trading") or {}).get("expiry_seconds", 120),
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

    # ─── pair analytics ───
    @app.get("/api/pair_stats")
    async def pair_stats(request: Request, range: str = "7d", work_hours_only: int = 0):
        """Aggregated per-pair stats over a time range.
        range = 24h | 7d | 30d | 60d | all
        work_hours_only = 1 → filter snapshots to local-hour within
        schedule.start_hour..schedule.end_hour (timezone from telegram cfg)."""
        _auth(request)
        import time as _t, datetime as _dt
        now = int(_t.time())
        ranges = {"24h": 86400, "7d": 7*86400, "30d": 30*86400, "60d": 60*86400}
        if range == "all":
            since = 0
        else:
            since = now - ranges.get(range, 7*86400)
        if not journal:
            return {"error": "no journal"}
        min_p = int((cfg.get("filter") or {}).get("min_payout", 92))
        floor_p = int((cfg.get("filter") or {}).get("payout_floor", 85))
        # Resolve hour-window + tz offset if requested
        hour_filter = None
        tz_offset_sec = 0
        if work_hours_only:
            sched = cfg.get("schedule") or {}
            start_h = int(sched.get("start_hour", 0))
            end_h = int(sched.get("end_hour", 24))
            hour_filter = (start_h, end_h)
            tz_name = (cfg.get("telegram") or {}).get("daily_report_timezone") or "Europe/Kyiv"
            try:
                import pytz
                tz = pytz.timezone(tz_name)
                offset = tz.utcoffset(_dt.datetime.now())
                tz_offset_sec = int(offset.total_seconds()) if offset else 0
            except Exception:
                tz_offset_sec = 0
        payout_rows = journal.payout_aggregate(
            since, min_payout=min_p, floor_payout=floor_p,
            hour_filter=hour_filter, tz_offset_sec=tz_offset_sec,
        )
        stats_rows = journal.pair_stats_aggregate(
            since, hour_filter=hour_filter, tz_offset_sec=tz_offset_sec,
        )
        win_payout_rows = journal.win_payout_aggregate(
            since, mode=cfg.get("mode", "real"), after_loss_only=True,
        )
        # Merge by symbol
        by_sym: dict[str, dict] = {}
        for r in payout_rows:
            by_sym[r["symbol"]] = {**r}
        for r in stats_rows:
            sym = r["symbol"]
            by_sym.setdefault(sym, {"symbol": sym})
            by_sym[sym].update(r)
        for r in win_payout_rows:
            sym = r["symbol"]
            by_sym.setdefault(sym, {"symbol": sym})
            by_sym[sym].update(r)
        # Add current payout from feed.assets if available
        if feed and getattr(feed, "assets", None):
            for sym, info in feed.assets.items():
                cur_p = int(info.get("payout") or 0)
                by_sym.setdefault(sym, {"symbol": sym})["current_payout"] = cur_p
        result = list(by_sym.values())
        # Default sort: pairs with high last_wr1 AND high pct_above_min on top
        def _score(r):
            return (
                (r.get("last_wr1") or 0) * 0.5 +
                (r.get("pct_above_min") or 0) * 0.3 -
                (r.get("last_max_streak") or 0) * 5
            )
        result.sort(key=_score, reverse=True)
        return {
            "range": range,
            "since_ts": since,
            "now_ts": now,
            "min_payout": min_p,
            "floor_payout": floor_p,
            "work_hours_only": bool(work_hours_only),
            "hour_filter": list(hour_filter) if hour_filter else None,
            "pairs": result,
        }

    # ─── hourly stats ───
    @app.get("/api/hourly_stats")
    async def hourly_stats(request: Request, range: str = "7d"):
        """Trades grouped by symbol × hour-of-day (local TZ). Used by Mini App
        'По часам' tab to find best hours for each pair."""
        _auth(request)
        import time as _t, datetime as _dt
        now = int(_t.time())
        time_range = range  # alias, range is the builtin shadowed by arg name
        ranges = {"24h": 86400, "7d": 7*86400, "30d": 30*86400, "60d": 60*86400}
        since = 0 if time_range == "all" else now - ranges.get(time_range, 7*86400)
        if not journal:
            return {"error": "no journal"}
        # Honour user-defined analytics baseline (soft-reset feature)
        baseline = int((cfg.get("analytics") or {}).get("baseline_ts") or 0)
        if baseline > since:
            since = baseline
        # Local TZ for proper hour-of-day grouping (Kyiv UTC+2/3)
        tz_name = (cfg.get("telegram") or {}).get("daily_report_timezone") or "Europe/Kyiv"
        tz_offset_sec = 0
        try:
            import pytz
            tz = pytz.timezone(tz_name)
            offset = tz.utcoffset(_dt.datetime.now())
            tz_offset_sec = int(offset.total_seconds()) if offset else 0
        except Exception:
            pass
        mode = cfg.get("mode")
        rows = journal.hourly_stats(since, mode=mode, tz_offset_sec=tz_offset_sec)
        # Build summary per hour (sum across all pairs).
        # For avg_win_payout we accumulate weighted (sum_payouts / total_wins)
        # rather than averaging averages — that would skew toward sparse pairs.
        from collections import defaultdict
        summary_acc: dict = defaultdict(lambda: {
            "total": 0, "wins": 0, "losses": 0, "draws": 0, "profit": 0.0,
            "sum_win_payout": 0.0,   # accumulator: sum of (avg * wins) per pair
            "min_win_payout": None,
            "max_win_payout": None,
        })
        for r in rows:
            h = r["hour"]
            s = summary_acc[h]
            s["total"] += r["total"]
            s["wins"] += r["wins"]
            s["losses"] += r["losses"]
            s["draws"] += r["draws"]
            s["profit"] += r["profit"]
            if r.get("avg_win_payout") is not None and r["wins"] > 0:
                s["sum_win_payout"] += r["avg_win_payout"] * r["wins"]
            if r.get("min_win_payout") is not None:
                s["min_win_payout"] = (r["min_win_payout"] if s["min_win_payout"] is None
                                       else min(s["min_win_payout"], r["min_win_payout"]))
            if r.get("max_win_payout") is not None:
                s["max_win_payout"] = (r["max_win_payout"] if s["max_win_payout"] is None
                                       else max(s["max_win_payout"], r["max_win_payout"]))
        summary_by_hour = []
        # Reach the builtin range via __builtins__ since the arg name shadows it
        builtin_range = __builtins__["range"] if isinstance(__builtins__, dict) else __builtins__.range
        for h in builtin_range(24):  # ensure all 24 buckets even if zero trades
            s = summary_acc.get(h) or {"total": 0, "wins": 0, "losses": 0,
                                        "draws": 0, "profit": 0.0,
                                        "sum_win_payout": 0.0,
                                        "min_win_payout": None,
                                        "max_win_payout": None}
            completed = s["wins"] + s["losses"]
            wr = (s["wins"] / completed * 100.0) if completed else 0.0
            avg_win_p = (s["sum_win_payout"] / s["wins"]) if s["wins"] > 0 else None
            summary_by_hour.append({
                "hour": h,
                "total": s["total"],
                "wins": s["wins"],
                "losses": s["losses"],
                "draws": s["draws"],
                "wr": round(wr, 1),
                "profit": round(s["profit"], 2),
                "avg_win_payout": round(avg_win_p, 1) if avg_win_p is not None else None,
                "min_win_payout": s["min_win_payout"],
                "max_win_payout": s["max_win_payout"],
            })
        return {
            "range": time_range,
            "tz": tz_name,
            "tz_offset_sec": tz_offset_sec,
            "buckets": rows,
            "summary_by_hour": summary_by_hour,
        }

    # ─── expiry backtest ───
    @app.get("/api/expiry_stats")
    async def expiry_stats(request: Request):
        """Backtest для ВСЕХ доступных OTC пар (не только торгуемых).
        Прогоняет CONSENSUS-стратегию с разными expiryBars (2, 3, 4, 5) на
        исторических свечах каждой пары. Показывает какая экспирация
        чаще закрывается в плюс — для ручного подбора оптимума.

        Использует _candles buffer для tracked пар, для остальных
        дотягивает свечи через fetch_candles (REST). Медленнее на
        первом запуске (~10-30с для 30+ пар), потом кешировано.
        """
        _auth(request)
        if not sm or not feed:
            return {"pairs": [], "expiries": [2, 3, 4, 5],
                    "note": "Бот не запущен или нет связи с фидом."}

        from strategy.consensus import analyze, DEFAULT_PARAMS
        from feed.history import fetch_candles
        base_params = {**DEFAULT_PARAMS, **(cfg.get("indicator") or {})}
        f_cfg = cfg.get("filter", {}) or {}
        if "stats_lookback_bars" in f_cfg:
            base_params["statsLookbackBars"] = f_cfg["stats_lookback_bars"]
        tf = int(f_cfg.get("tf", 60))
        history_limit = int(f_cfg.get("history_candles", 1060))

        EXPIRIES = [2, 3, 4, 5]
        MIN_SIGNALS = 5

        # Все OTC пары из ассетов фида (не только tracked)
        all_otc = [
            (s, info) for s, info in (feed.assets or {}).items()
            if info.get("is_otc") and info.get("open", True)
        ]
        results = []
        fetched = 0
        skipped = 0

        for sym, info in all_otc:
            payout = int(info.get("payout", 0))
            # Сначала пробуем cached buffer от state_machine (tracked пары)
            candles = (sm._candles or {}).get(sym) if sm else None
            if not candles or len(candles) < 100:
                # Дотянуть исторически — может быть медленно для пар не в _tracked
                try:
                    candles = await fetch_candles(feed, sym, period=tf, limit=history_limit)
                    fetched += 1
                except Exception:
                    skipped += 1
                    continue
            if not candles or len(candles) < 100:
                skipped += 1
                continue

            per_expiry: dict = {}
            for exp in EXPIRIES:
                params = {**base_params, "expiryBars": exp}
                try:
                    a = analyze(candles, params)
                except Exception:
                    continue
                completed = a.wins + a.losses
                wr = (a.wins / completed * 100.0) if completed else 0.0
                per_expiry[exp] = {
                    "signals": a.completed,
                    "wins": a.wins,
                    "losses": a.losses,
                    "wr": round(wr, 1),
                    "wr1": round(a.wr1, 1),
                    "max_streak": a.max_loss_streak_overall,
                }
            valid = [(exp, d) for exp, d in per_expiry.items()
                     if d["signals"] >= MIN_SIGNALS]
            if valid:
                best_exp, best_data = max(valid, key=lambda x: x[1]["wr"])
                best_wr = best_data["wr"]
            else:
                best_exp, best_wr = None, 0.0
            results.append({
                "symbol": sym,
                "payout": payout,
                "expiries": per_expiry,
                "best_expiry": best_exp,
                "best_wr": best_wr,
                "candles_used": len(candles),
            })

        results.sort(key=lambda r: (-r["best_wr"], -r["payout"]))

        return {
            "pairs": results,
            "expiries": EXPIRIES,
            "min_signals_for_score": MIN_SIGNALS,
            "note": (
                f"Анализ {len(results)} OTC пар (всех доступных). "
                f"Подтянуто свечей через REST: {fetched}, пропущено: {skipped}. "
                f"Применена CONSENSUS-стратегия из config, меняется только expiryBars (2..5)."
            ),
        }

    # ─── hour-whitelist filter ───
    @app.get("/api/hour_whitelist")
    async def get_hour_whitelist(request: Request):
        """Current active hour-whitelist (which (pair, hour) combos are
        allowed by user-applied filter). Empty dict = filter disabled."""
        _auth(request)
        wl = (cfg.get("filter") or {}).get("hour_whitelist") or {}
        # Count total cells
        count = sum(len(hrs) for hrs in wl.values()) if isinstance(wl, dict) else 0
        return {"whitelist": wl, "count": count}

    @app.post("/api/apply_hour_whitelist")
    async def apply_hour_whitelist(request: Request, payload: dict):
        """Build a {symbol: [hour, hour, ...]} whitelist from current trade
        history filtered by min_wr and min_trades, then activate it. After
        this, the bot will only enter trades when (current_pair, current_hour)
        is in the whitelist."""
        _auth(request)
        if not journal:
            raise HTTPException(503, "no journal")
        min_wr = float(payload.get("min_wr", 70))
        min_trades = int(payload.get("min_trades", 5))
        time_range = payload.get("range", "30d")
        import time as _t, datetime as _dt
        now = int(_t.time())
        ranges = {"24h": 86400, "7d": 7*86400, "30d": 30*86400, "60d": 60*86400}
        since = 0 if time_range == "all" else now - ranges.get(time_range, 30*86400)
        # Same TZ logic as /api/hourly_stats
        tz_name = (cfg.get("telegram") or {}).get("daily_report_timezone") or "Europe/Kyiv"
        tz_offset_sec = 0
        try:
            import pytz
            tz = pytz.timezone(tz_name)
            offset = tz.utcoffset(_dt.datetime.now())
            tz_offset_sec = int(offset.total_seconds()) if offset else 0
        except Exception:
            pass
        mode = cfg.get("mode")
        rows = journal.hourly_stats(since, mode=mode, tz_offset_sec=tz_offset_sec)
        whitelist: dict[str, list[int]] = {}
        for r in rows:
            if r["total"] >= min_trades and r["wr"] >= min_wr:
                whitelist.setdefault(r["symbol"], []).append(int(r["hour"]))
        # Persist as a settings override (survives reboot via journal volume)
        try:
            overrides = journal.get("settings_overrides") or {}
            overrides["filter.hour_whitelist"] = whitelist
            journal.set("settings_overrides", overrides)
        except Exception:
            logger.exception("failed to persist hour_whitelist")
        # Apply live to running cfg
        cfg.setdefault("filter", {})["hour_whitelist"] = whitelist
        count = sum(len(h) for h in whitelist.values())
        if sm and sm.notify:
            import asyncio as _aio
            _aio.create_task(sm.notify(
                f"⭐ Применён фильтр по часам: {count} комбинаций пара×час "
                f"(WR≥{int(min_wr)}%, сделок≥{min_trades}, период {time_range}). "
                f"Бот будет входить только в эти окна."
            ))
        return {"ok": True, "count": count, "whitelist": whitelist,
                "criteria": {"min_wr": min_wr, "min_trades": min_trades, "range": time_range}}

    @app.post("/api/reset_hourly_stats")
    async def reset_hourly_stats(request: Request):
        """Soft reset: don't delete trade rows, just save a baseline timestamp.
        Future hourly_stats queries will only count trades after this point.
        Used when user changes strategy and wants a clean slate for analytics
        without losing real trade history."""
        _auth(request)
        if not journal:
            raise HTTPException(503, "no journal")
        import time as _t
        baseline = int(_t.time())
        try:
            overrides = journal.get("settings_overrides") or {}
            overrides["analytics.baseline_ts"] = baseline
            journal.set("settings_overrides", overrides)
        except Exception:
            logger.exception("failed to persist analytics.baseline_ts")
        cfg.setdefault("analytics", {})["baseline_ts"] = baseline
        if sm and sm.notify:
            import asyncio as _aio, datetime as _dt
            ts_str = _dt.datetime.fromtimestamp(baseline).strftime("%Y-%m-%d %H:%M")
            _aio.create_task(sm.notify(
                f"🧹 Статистика по часам сброшена. Подсчёт начнётся с {ts_str}. "
                f"Старые сделки в БД сохранены, но не будут учитываться в аналитике."
            ))
        return {"ok": True, "baseline_ts": baseline}

    @app.post("/api/clear_hour_whitelist")
    async def clear_hour_whitelist(request: Request):
        """Disable the hour-whitelist filter — bot returns to trading any
        eligible pair regardless of hour."""
        _auth(request)
        try:
            if journal:
                overrides = journal.get("settings_overrides") or {}
                if "filter.hour_whitelist" in overrides:
                    overrides.pop("filter.hour_whitelist", None)
                    journal.set("settings_overrides", overrides)
        except Exception:
            logger.exception("failed to clear hour_whitelist override")
        if "filter" in cfg:
            cfg["filter"].pop("hour_whitelist", None)
        if sm and sm.notify:
            import asyncio as _aio
            _aio.create_task(sm.notify(
                "🔓 Фильтр по часам отключён — торговля по всем подходящим парам без ограничения по времени."
            ))
        return {"ok": True}

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
            new_pair = await sm.force_switch_pair()
            if not new_pair:
                if sm.notify:
                    _aio.create_task(sm.notify(
                        f"⚠️ Mini App: попытка смены пары {old_pair} — нет доступных альтернатив."
                    ))
                return {"ok": False, "old": old_pair, "new": None,
                        "reason": "no eligible pair"}
            if sm.notify:
                _aio.create_task(sm.notify(
                    f"🔀 Через Mini App вручную сменена пара: {old_pair} → {new_pair}"
                ))
            return {"ok": True, "old": old_pair, "new": new_pair}
        raise HTTPException(400, f"unknown action: {action}")

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
