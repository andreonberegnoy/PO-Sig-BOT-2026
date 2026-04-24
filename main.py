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


async def run(cfg: dict):
    log = logging.getLogger("main")

    # 1. Journal
    journal = Journal(cfg["storage"]["db_path"])

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
        feed = PoDirectFeed(
            ssid=ssid, uid=uid,
            is_demo=is_demo,
            ws_url=ws_url,
            verify_ssl=False,   # macOS-Python cert issue; PO is wss:// pinned anyway
        )
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
    stop_event = asyncio.Event()
    async def stop_all():
        stop_event.set()

    # 5. State machine
    sm = StateMachine(cfg, feed, tc, journal, notify=tg.notify, send_chart=tg.send_chart)

    tg.attach(state_machine=sm, journal=journal, feed=feed, stop_cb=stop_all)

    # Install signal handlers
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(stop_all()))
        except NotImplementedError:
            pass

    log.info("🚀 starting — mode=%s  balance=%s", cfg["mode"], bal)

    tasks = [
        asyncio.create_task(sm.run(), name="state_machine"),
        asyncio.create_task(tg.run_polling(), name="tg_polling"),
        asyncio.create_task(tg.daily_report_loop(), name="daily_report"),
    ]

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
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
