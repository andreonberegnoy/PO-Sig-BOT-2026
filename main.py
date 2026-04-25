"""Main orchestrator — wires feed, strategy, state machine, journal, telegram.

Run:
  python3 main.py                    # uses config.yaml
  python3 main.py --mode paper       # override mode
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import yaml

from trading.ws_client import TradeClient
from trading.state_machine import StateMachine
from journal.db import Journal
from tg.bot import TelegramBot


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (ignores python-dotenv dep)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                # Strip surrounding quotes if present
                if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                    v = v[1:-1]
                os.environ.setdefault(k, v)
    except FileNotFoundError:
        pass


def _env_override(cfg: dict) -> dict:
    """Override config secrets from environment variables. Keeps local dev
    convenient (config.yaml works) while letting Railway/Docker inject
    credentials without committing them."""
    env = os.environ
    if env.get("MODE"):
        cfg["mode"] = env["MODE"]
    if env.get("CDP_URL"):
        cfg["cdp_url"] = env["CDP_URL"]
    if env.get("PO_EMAIL"):
        cfg.setdefault("auth", {})["email"] = env["PO_EMAIL"]
    if env.get("PO_PASSWORD"):
        cfg.setdefault("auth", {})["password"] = env["PO_PASSWORD"]
    if env.get("TELEGRAM_TOKEN"):
        cfg.setdefault("telegram", {})["token"] = env["TELEGRAM_TOKEN"]
    if env.get("TELEGRAM_CHAT_ID"):
        cfg.setdefault("telegram", {})["chat_id"] = int(env["TELEGRAM_CHAT_ID"])
    return cfg


def load_config(path: str) -> dict:
    _load_dotenv(".env")
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return _env_override(cfg)


def setup_logging(cfg: dict):
    level = getattr(logging, (cfg["storage"].get("log_level") or "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(cfg["storage"]["log_path"]),
            logging.StreamHandler(sys.stdout),
        ],
    )


async def run(cfg: dict, config_path: str = "config.yaml"):
    log = logging.getLogger("main")

    # 1. Journal
    db_path = cfg["storage"]["db_path"]
    # One-time migration: if persistent DB doesn't exist yet but legacy
    # journal.db (from before storage path change) does — copy into volume so
    # historical trades and runtime_state survive the deploy.
    import shutil
    legacy_db = "journal.db"
    if not os.path.exists(db_path) and os.path.exists(legacy_db):
        try:
            os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
            shutil.copy2(legacy_db, db_path)
            log.info("migrated legacy journal.db → %s", db_path)
        except Exception:
            log.exception("legacy journal copy failed")

    journal = Journal(db_path)

    # Apply persistent settings overrides (saved via Mini App PUT /api/settings).
    # config.yaml lives in the container image and is reset on each deploy;
    # journal kv_store sits on the volume and survives. Overlay survives.
    try:
        overrides = journal.get("settings_overrides") or {}
        log.info("settings_overrides at startup: %d keys → %s",
                 len(overrides), list(overrides.keys()) if overrides else "(none)")
        if overrides:
            def _set_dotted(d, key, value):
                parts = key.split(".")
                cur = d
                for p in parts[:-1]:
                    if p not in cur or not isinstance(cur[p], dict):
                        cur[p] = {}
                    cur = cur[p]
                cur[parts[-1]] = value
            for key, value in overrides.items():
                _set_dotted(cfg, key, value)
                log.info("  override applied: %s = %r", key, value)
    except Exception:
        log.exception("loading settings_overrides failed")

    # 1b. Strategy registry (built-in + user-uploaded plugins).
    # Pass current cfg["indicator"] for one-time migration: if no per-strategy
    # params yet, seed consensus with user's existing config.yaml indicator settings.
    from strategy.registry import StrategyRegistry
    registry = StrategyRegistry(journal=journal, global_indicator_cfg=cfg.get("indicator") or {})

    # 2. Feed — prefer direct Pocket Option WS if PO_SSID is set, else legacy CDP.
    ssid = os.environ.get("PO_SSID") or (cfg.get("po") or {}).get("ssid")
    if ssid:
        from feed.po_direct import PoDirectFeed
        uid = int(os.environ.get("PO_UID") or (cfg.get("po") or {}).get("uid") or 0)
        ws_url = (os.environ.get("PO_WS_URL")
                  or (cfg.get("po") or {}).get("ws_url")
                  or "wss://api-eu.po.market/socket.io/?EIO=4&transport=websocket")
        # PO session is captured with a specific isDemo flag (from when user
        # logged into the site). Match the session: if user extracted ssid
        # while on demo account → PO_IS_DEMO=1; while on real → PO_IS_DEMO=0.
        # Fallback: infer from config mode.
        is_demo_env = os.environ.get("PO_IS_DEMO")
        if is_demo_env is not None:
            is_demo = is_demo_env.strip() in ("1", "true", "yes")
        else:
            is_demo = (cfg["mode"] == "paper")
        # Optional: auto-relogin. Two modes (preferred → fallback):
        #   1. PO_STORAGE_STATE_B64 — base64 of state.json from a real browser
        #      login (generated locally via tools/make_storage_state.py).
        #      Bypasses Cloudflare since cookies make us a "returning visitor".
        #   2. PO_EMAIL + PO_PASSWORD — Playwright fills the form. Blocked by
        #      Cloudflare on most datacenter IPs (Railway, Fly, etc).
        relogin_cb = None
        notify_holder: dict = {}            # filled after TelegramBot is created
        consecutive_failures = {"n": 0}     # avoid spamming on every reconnect

        async def _notify_session_expired():
            fn = notify_holder.get("fn")
            if not fn:
                return
            msg = (
                "🔴 Pocket Option session протухла — нужно обновить cookies (~раз в месяц).\n"
                "Торговля остановлена пока не обновишь.\n\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "ШАГ 1. Открой Terminal на Mac и выполни:\n\n"
                '<code>cd "/Users/andrii/Desktop/Cloude Projects/MY PO-SIG BOT"\n'
                "python3 tools/make_storage_state.py</code>\n\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "ШАГ 2. Откроется окно Chromium:\n"
                "   • залогинься в Pocket Option (email + пароль)\n"
                "   • дождись пока загрузится трейдинговая страница\n"
                "   • закрой модалку Unlock A New Location если появится\n"
                "   • вернись в Terminal и нажми Enter\n\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "ШАГ 3. Залей новые cookies в Railway (одна команда):\n\n"
                '<code>railway variables --set "PO_STORAGE_STATE_B64=$(base64 -i state.json | tr -d \'\\n\')"</code>\n\n'
                "━━━━━━━━━━━━━━━━━━\n"
                "ШАГ 4. Railway сам пересоберёт контейнер. Через ~1-2 минуты "
                "придёт сообщение что бот снова работает."
            )
            try:
                await fn(msg, parse_mode="HTML")
            except TypeError:
                # Old notify signature — fallback to plain text without <code>
                plain = msg.replace("<code>", "").replace("</code>", "")
                try: await fn(plain)
                except Exception: log.exception("notify session-expired failed")
            except Exception:
                log.exception("notify session-expired failed")

        po_state_b64 = os.environ.get("PO_STORAGE_STATE_B64")
        po_email = os.environ.get("PO_EMAIL")
        po_password = os.environ.get("PO_PASSWORD")

        if po_state_b64:
            from feed.auto_relogin import fetch_fresh_ssid_via_state
            async def _relogin():
                fresh = await fetch_fresh_ssid_via_state(po_state_b64, is_demo=is_demo)
                if fresh:
                    consecutive_failures["n"] = 0
                else:
                    consecutive_failures["n"] += 1
                    if consecutive_failures["n"] == 2:   # notify once after second fail
                        await _notify_session_expired()
                return fresh
            relogin_cb = _relogin
            log.info("auto-relogin enabled via storage_state (cookie-based)")
        elif po_email and po_password:
            from feed.auto_relogin import fetch_fresh_ssid
            async def _relogin():
                return await fetch_fresh_ssid(po_email, po_password, is_demo=is_demo)
            relogin_cb = _relogin
            log.info("auto-relogin enabled via email/password (may be blocked by Cloudflare)")

        # Will be wired to state_machine.state.mg_step==0 once sm exists
        feed_relogin_safe_check = lambda: True

        feed = PoDirectFeed(
            ssid=ssid, uid=uid,
            is_demo=is_demo,
            ws_url=ws_url,
            verify_ssl=False,   # macOS-Python cert issue; PO is wss:// pinned anyway
            relogin_callback=relogin_cb,
            relogin_interval_hours=float(os.environ.get("PO_RELOGIN_HOURS") or 12.0),
            relogin_safe_check=lambda: feed_relogin_safe_check(),
        )
        # Wire journal in for analytics (payout snapshots).
        feed._journal_for_logging = journal
        await feed.connect()
    else:
        from feed.po_feed import PoFeed
        feed = PoFeed(mode=cfg["mode"], auth_cfg=cfg.get("auth") or {})
        await feed.connect(cfg["cdp_url"])
    bal = feed.balance()
    journal.start_session(cfg["mode"], bal)

    # 3. Trade client
    tc = TradeClient(feed, mode=cfg["mode"])

    # 4. Telegram (bot first so state machine can call notify)
    tg = TelegramBot(cfg)
    # Wire notify into relogin closure (was created before tg existed)
    try:
        notify_holder["fn"] = tg.notify
    except NameError:
        pass    # po_feed mode — no relogin closure
    stop_event = asyncio.Event()
    async def stop_all():
        stop_event.set()

    # 5. State machine
    sm = StateMachine(cfg, feed, tc, journal, notify=tg.notify, send_chart=tg.send_chart)
    sm.registry = registry   # wire active-strategy switch in

    # Wire feed's relogin safe-check to state machine: don't re-login during
    # an active MG cycle (would lose WS mid-trade), an in-flight trade
    # (pending_trade set), or when paused.
    def _safe_to_relogin():
        try:
            s = sm.state
            return (s.mg_step == 0 and s.pending_trade is None
                    and not s.paused and not s.waiting_resume)
        except Exception:
            return True
    feed_relogin_safe_check = _safe_to_relogin

    tg.attach(state_machine=sm, journal=journal, feed=feed, stop_cb=stop_all)

    # Install signal handlers
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(stop_all()))
        except NotImplementedError:
            pass

    log.info("🚀 starting — mode=%s  balance=%s", cfg["mode"], bal)

    # 6. FastAPI / Mini App (best-effort; missing deps shouldn't kill the bot)
    api_task = None
    try:
        import uvicorn
        from api.server import create_app
        api_app = create_app(
            cfg=cfg, config_path=config_path,
            registry=registry, sm=sm, feed=feed, journal=journal,
            bot_token=cfg.get("telegram", {}).get("token", ""),
            allowed_chat_id=int(cfg.get("telegram", {}).get("chat_id") or 0),
        )
        api_port = int(os.environ.get("PORT") or os.environ.get("API_PORT") or 8080)
        api_config = uvicorn.Config(api_app, host="0.0.0.0", port=api_port,
                                    log_level="info", access_log=True)
        api_server = uvicorn.Server(api_config)
        api_task = asyncio.create_task(api_server.serve(), name="api")
        log.info("🌐 Mini App API on port %d", api_port)
    except ImportError as e:
        log.warning("FastAPI/uvicorn not installed — Mini App disabled (%s)", e)
    except Exception:
        log.exception("Mini App startup failed — continuing without it")

    tasks = [
        asyncio.create_task(sm.run(), name="state_machine"),
        asyncio.create_task(tg.run_polling(), name="tg_polling"),
        asyncio.create_task(tg.daily_report_loop(), name="daily_report"),
        asyncio.create_task(tg.periodic_report_loop(), name="periodic_report"),
    ]
    if api_task: tasks.append(api_task)

    # Wait for stop signal
    await stop_event.wait()
    log.info("shutdown requested, cleaning up…")

    await sm.stop()
    for t in tasks:
        t.cancel()
    for t in tasks:
        try: await t
        except asyncio.CancelledError: pass
        except Exception: log.exception("task error")

    await tg.close()
    await feed.close()
    journal.close()
    log.info("bye.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--mode", choices=["paper", "real"], help="override mode")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.mode:
        cfg["mode"] = args.mode
    setup_logging(cfg)

    try:
        asyncio.run(run(cfg, config_path=args.config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
