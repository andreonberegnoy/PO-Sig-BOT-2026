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
        # Optional region pin: when set, bot stays on this URL even if PO's
        # Playwright login captures a different one (e.g. user wants api-eu
        # but PO routes account to api-msk). Has a circuit breaker — if PO
        # rejects the preferred URL 3 times → temporary fallback. See
        # PoDirectFeed.preferred_ws_url for details.
        preferred_ws_url = (os.environ.get("PO_PREFERRED_WS_URL")
                            or (cfg.get("po") or {}).get("preferred_ws_url"))
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
            preferred_ws_url=preferred_ws_url,
        )
        if preferred_ws_url:
            log.info("region pin active: PO_PREFERRED_WS_URL=%s", preferred_ws_url)
        # Wire journal in for analytics (payout snapshots).
        feed._journal_for_logging = journal
        # Initial connect with infinite retry. The in-feed _auto_reconnect_loop
        # only kicks in *after* a successful connect; if the very first WS
        # handshake fails (Railway cold-start network blip, Cloudflare, PO
        # endpoint rotated, IP blackholed by CF, etc.) the exception would
        # otherwise kill the process. Mirror the auto-reconnect backoff here
        # so the bot survives a flaky first handshake without a container
        # restart. Also: every N failed attempts we trigger relogin to refresh
        # the ws_url — if PO migrated the account to another regional endpoint
        # (api-msk, api-l, api-ap, …) Playwright login captures the new URL.
        import random as _rnd
        import socket as _socket
        from urllib.parse import urlparse as _urlparse

        def _tcp_probe(url: str, timeout_sec: float = 5.0) -> tuple[bool, str]:
            """Best-effort TCP reachability probe. Returns (ok, detail)."""
            try:
                u = _urlparse(url)
                host = u.hostname or ""
                port = u.port or (443 if u.scheme in ("wss", "https") else 80)
                if not host:
                    return False, "empty hostname"
                try:
                    addrs = _socket.getaddrinfo(host, port, type=_socket.SOCK_STREAM)
                except _socket.gaierror as ge:
                    return False, f"DNS fail: {ge}"
                family, socktype, proto, _, sockaddr = addrs[0]
                s = _socket.socket(family, socktype, proto)
                s.settimeout(timeout_sec)
                try:
                    s.connect(sockaddr)
                    return True, f"TCP ok ({sockaddr[0]}:{sockaddr[1]})"
                finally:
                    try: s.close()
                    except Exception: pass
            except _socket.timeout:
                return False, "TCP timeout (host appears blackholed)"
            except OSError as oe:
                return False, f"TCP error: {oe}"
            except Exception as e:
                return False, f"probe error: {e}"

        attempt = 0
        relogin_after = 3   # trigger relogin after this many consecutive failures
        while True:
            attempt += 1
            # Quick pre-flight: tells us instantly whether the host is even
            # reachable, so a 45s WS timeout doesn't hide a clear network issue.
            ok, detail = _tcp_probe(feed.ws_url)
            if ok:
                log.info("pre-flight: %s — proceeding to WS handshake", detail)
            else:
                log.warning("pre-flight: %s — WS handshake will likely time out", detail)
            try:
                await feed.connect()
                if attempt > 1:
                    log.info("initial WS connect succeeded on attempt %d", attempt)
                break
            except Exception as e:
                wait = min(60.0, 2 ** min(attempt, 6) + _rnd.uniform(0, 1))
                log.warning("initial WS connect attempt %d failed: %s — retrying in %.1fs",
                            attempt, e, wait)
                # Clean up partial state before next attempt.
                try:
                    feed._running = False
                    if getattr(feed, "_ws", None) is not None:
                        try: await feed._ws.close()
                        except Exception: pass
                        feed._ws = None
                except Exception:
                    pass
                # Every N failures, run relogin to refresh ssid + ws_url.
                # Playwright goes via pocketoption.com (different infra than
                # api-eu.po.market), so even if the WS endpoint is blackholed,
                # the login itself usually works and returns the *current*
                # ws_url that PO is serving for this account — which may be a
                # different region than what we have hardcoded.
                if relogin_cb is not None and attempt % relogin_after == 0:
                    log.warning("initial connect: %d consecutive failures — running relogin "
                                "to refresh ssid and discover current ws_url", attempt)
                    try:
                        fresh = await asyncio.wait_for(relogin_cb(), timeout=180)
                        if fresh and fresh.get("ssid"):
                            old_url = feed.ws_url
                            feed.ssid = fresh["ssid"]
                            if fresh.get("uid"):
                                feed.uid = int(fresh["uid"])
                            if fresh.get("ws_url"):
                                feed.ws_url = fresh["ws_url"]
                            if "is_demo" in fresh:
                                feed.is_demo = 1 if fresh["is_demo"] else 0
                            if feed.ws_url != old_url:
                                log.warning("relogin: ws_url changed %s → %s — endpoint rotated",
                                            old_url, feed.ws_url)
                            else:
                                log.info("relogin: ssid refreshed (ws_url unchanged)")
                        else:
                            log.error("relogin returned no ssid — likely Cloudflare blocked "
                                      "Playwright; consider redeploying Railway service "
                                      "to get a fresh egress IP")
                    except asyncio.TimeoutError:
                        log.error("relogin timed out after 180s — Playwright stuck")
                    except Exception:
                        log.exception("relogin during initial-connect retry failed")
                await asyncio.sleep(wait)
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

    # Wire user-visible WS health alerts to Telegram. PoDirectFeed will call
    # feed.on_alert(text) when the WS freezes / can't reconnect / etc.
    # This is *the* fix for "I don't know when the bot stopped working".
    if hasattr(feed, "on_alert"):
        feed.on_alert = tg.notify

    # If a previous run() invocation crashed, the supervisor in main() left a
    # marker in the journal. Now that TG is up, send a one-time alert so the
    # user knows about the outage. (Best-effort — wrap in try.)
    try:
        crash_marker = journal.get("last_supervisor_crash")
        if crash_marker:
            import datetime as _dt
            ts = crash_marker.get("ts", 0)
            err = (crash_marker.get("error") or "")[:300]
            when = _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "?"
            # Escape HTML special chars in error to avoid breaking parse_mode=HTML
            err_safe = err.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            try:
                await tg.notify(
                    f"🔴 Бот падал и перезапустился сам.\n"
                    f"Когда: {when}\n"
                    f"Ошибка: <code>{err_safe}</code>\n\n"
                    f"Сейчас всё работает. Если хочешь — посмотри логи Railway.",
                    parse_mode="HTML",
                )
            except Exception:
                # Fallback to plain text
                await tg.notify(
                    f"🔴 Бот падал и перезапустился сам.\n"
                    f"Когда: {when}\nОшибка: {err}\n\n"
                    f"Сейчас всё работает. Если хочешь — посмотри логи Railway."
                )
            journal.delete("last_supervisor_crash")
    except Exception:
        log.exception("supervisor-crash alert failed (non-fatal)")

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
        asyncio.create_task(sm._virtual_signals_loop(), name="virtual_signals"),
        asyncio.create_task(tg.run_polling(), name="tg_polling"),
        asyncio.create_task(tg.daily_report_loop(), name="daily_report"),
        asyncio.create_task(tg.periodic_report_loop(), name="periodic_report"),
        asyncio.create_task(tg.health_watchdog_loop(), name="health_watchdog"),
    ]
    if api_task: tasks.append(api_task)

    # ── Task supervisor: monitors critical asyncio tasks and restarts them
    # if they die unexpectedly. This is the last line of defence — if
    # state_machine or tg_polling crash despite all inner guards, the
    # supervisor brings them back automatically.
    _RESTARTABLE = {
        "state_machine": lambda: sm.run(),
        "virtual_signals": lambda: sm._virtual_signals_loop(),
        "daily_report":  lambda: tg.daily_report_loop(),
        "periodic_report": lambda: tg.periodic_report_loop(),
        "health_watchdog": lambda: tg.health_watchdog_loop(),
        # tg_polling has its own restart loop now; supervisor keeps it as a
        # belt-and-suspenders backup.
    }

    async def _supervisor():
        while not stop_event.is_set():
            await asyncio.sleep(30)
            if stop_event.is_set():
                return
            for name, factory in _RESTARTABLE.items():
                # Find existing task by name
                existing = next(
                    (t for t in asyncio.all_tasks() if t.get_name() == name and not t.done()),
                    None
                )
                if existing:
                    continue   # still running — all good
                log.error("supervisor: task '%s' is dead — restarting in 5s", name)
                try:
                    await tg.notify(f"🚨 Задача <b>{name}</b> упала — перезапускаю автоматически…",
                                    parse_mode="HTML")
                except Exception:
                    pass
                await asyncio.sleep(5)
                if stop_event.is_set():
                    return
                try:
                    new_task = asyncio.create_task(factory(), name=name)
                    tasks.append(new_task)
                    log.info("supervisor: task '%s' restarted", name)
                except Exception:
                    log.exception("supervisor: failed to restart task '%s'", name)

    tasks.append(asyncio.create_task(_supervisor(), name="supervisor"))

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

    # Top-level supervisor: NEVER let the process die from an uncaught
    # exception. If run() blows up for any reason (network, third-party lib,
    # whatever), we wait with exponential backoff and start it over again.
    # The only ways out are SIGINT (Ctrl-C) and SIGTERM via stop_all().
    log = logging.getLogger("main")
    import time, random
    crash_count = 0
    last_crash_ts = 0.0
    while True:
        try:
            asyncio.run(run(cfg, config_path=args.config))
            # run() returned cleanly (stop_event was set) → exit normally
            break
        except KeyboardInterrupt:
            log.info("KeyboardInterrupt — exiting")
            break
        except SystemExit:
            raise
        except BaseException as e:
            now = time.time()
            # Reset crash counter if last crash was long ago (stable run)
            if now - last_crash_ts > 600:
                crash_count = 0
            crash_count += 1
            last_crash_ts = now
            wait = min(60.0, 2 ** min(crash_count, 6) + random.uniform(0, 1))
            log.exception(
                "TOP-LEVEL crash #%d in run() — restarting in %.1fs (process stays alive)",
                crash_count, wait,
            )
            # Persist a marker so the next successful startup notifies TG.
            # We can't notify TG directly here — TG bot was created inside
            # run() which just blew up, so its event loop is gone. The next
            # run() will read this marker and ping the user.
            try:
                import traceback as _tb
                from journal.db import Journal as _J
                _journal_path = (cfg.get("journal", {}) or {}).get("path") or "journal/journal.sqlite"
                _j = _J(_journal_path)
                _j.set("last_supervisor_crash", {
                    "ts": int(now),
                    "error": f"{type(e).__name__}: {e}",
                    "traceback": _tb.format_exc()[-2000:],
                    "crash_count": crash_count,
                })
                _j.close()
            except Exception:
                log.exception("failed to persist supervisor crash marker")
            try:
                time.sleep(wait)
            except KeyboardInterrupt:
                log.info("KeyboardInterrupt during supervisor backoff — exiting")
                break


if __name__ == "__main__":
    main()
