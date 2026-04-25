"""Automatic Pocket Option session refresh via headless Chromium.

Logs into pocketoption.com using email+password with HUMAN-LIKE behavior:
  - Char-by-char typing with realistic delays (60-160 ms)
  - Random pauses between actions (300-1200 ms)
  - Mouse movement to elements before clicking
  - Standard Chrome User-Agent + viewport
  - Disabled webdriver fingerprint
  - Random timing for periodic re-login (not exact 24h ticks)

Triggered by:
  - Bot startup if PO_AUTO_RELOGIN=1 and no current ssid
  - Server sends NotAuthorized event mid-session
  - Daily scheduled refresh at random time

Browser opens for ~30-60 seconds then closes — no persistent overhead.
"""

import asyncio
import json
import logging
import random
import re
import time
from typing import Optional

logger = logging.getLogger(__name__)


PO_LOGIN_URL = "https://pocketoption.com/en/login/"
PO_TRADING_URL = "https://pocketoption.com/en/cabinet/demo-quick-high-low/"


async def _human_type(page, selector: str, text: str):
    """Type chars one-by-one with random per-char delay (60-160 ms)."""
    el = await page.query_selector(selector)
    if not el:
        raise RuntimeError(f"selector {selector} not found")
    await el.click()
    for ch in text:
        await page.keyboard.type(ch, delay=random.randint(60, 160))
        if random.random() < 0.05:        # occasional micro-pauses
            await asyncio.sleep(random.uniform(0.1, 0.3))


async def _human_pause(min_s: float = 0.4, max_s: float = 1.4):
    await asyncio.sleep(random.uniform(min_s, max_s))


async def fetch_fresh_ssid(
    email: str,
    password: str,
    is_demo: bool = True,
    timeout_sec: int = 90,
) -> Optional[dict]:
    """Run headless Chromium → login → capture WebSocket auth frame.

    Returns:
        {"ssid": str, "uid": int, "ws_url": str, "is_demo": bool} on success
        None on failure
    """
    if not email or not password:
        logger.error("auto-relogin: email or password missing")
        return None

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("playwright not installed — auto-relogin disabled")
        return None

    captured: dict = {}

    def _on_ws(ws):
        # Hook every WS connection to capture the auth frame
        async def _on_frame_sent(payload_or_data):
            # Payload is sometimes str, sometimes object — normalize.
            data = getattr(payload_or_data, "payload", None) or payload_or_data
            if not isinstance(data, str):
                return
            if not data.startswith("42[\"auth\""):
                return
            try:
                # Parse the 42[...] frame
                m = re.match(r'^42(\[.+\])$', data, re.DOTALL)
                if not m:
                    return
                arr = json.loads(m.group(1))
                if not isinstance(arr, list) or len(arr) < 2:
                    return
                payload = arr[1]
                if not isinstance(payload, dict):
                    return
                if not captured:
                    captured["ssid"] = payload.get("session")
                    captured["uid"] = int(payload.get("uid") or 0)
                    captured["is_demo"] = bool(payload.get("isDemo"))
                    captured["ws_url"] = ws.url
                    logger.info("auto-relogin: captured ssid (uid=%d, demo=%s, ws=%s)",
                                captured["uid"], captured["is_demo"], captured["ws_url"])
            except Exception:
                logger.exception("auth frame parse failed")
        try:
            ws.on("framesent", _on_frame_sent)
        except Exception:
            pass

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        try:
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/147.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                timezone_id="Europe/Kyiv",
            )
            # Hide automation fingerprints
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en', 'ru']});
                window.chrome = { runtime: {} };
            """)
            page = await context.new_page()
            page.on("websocket", _on_ws)

            # 1. Open login page
            await page.goto(PO_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
            await _human_pause(1.0, 2.5)        # человек "оглядывается" на странице

            # 2. Type credentials char-by-char with realistic delays
            try:
                await _human_type(page, "input[name=email], input[type=email]", email)
                await _human_pause(0.4, 1.0)
                await _human_type(page, "input[name=password], input[type=password]", password)
                await _human_pause(0.5, 1.5)

                # 3. Move mouse to submit button before clicking
                btn = await page.query_selector("button[type=submit], form button")
                if btn:
                    box = await btn.bounding_box()
                    if box:
                        # Move from random nearby point to button center (curved-ish)
                        steps = random.randint(8, 15)
                        cx = box["x"] + box["width"] / 2
                        cy = box["y"] + box["height"] / 2
                        await page.mouse.move(cx + random.randint(-200, 200),
                                              cy + random.randint(-150, 150))
                        await asyncio.sleep(random.uniform(0.1, 0.3))
                        await page.mouse.move(cx, cy, steps=steps)
                        await _human_pause(0.2, 0.6)
                    await btn.click()
                else:
                    await page.keyboard.press("Enter")
            except Exception:
                logger.exception("login form fill failed")
                return None

            # 2. Wait for WS auth frame (handled in event listener)
            deadline = time.time() + timeout_sec
            while time.time() < deadline:
                if captured:
                    break
                await asyncio.sleep(1)
                # Trigger demo navigation if still on dashboard
                cur_url = page.url
                if "demo-quick" not in cur_url and is_demo and "cabinet" in cur_url:
                    try:
                        await page.goto(PO_TRADING_URL, wait_until="domcontentloaded", timeout=15000)
                    except Exception:
                        pass

            if not captured.get("ssid"):
                logger.error("auto-relogin: did not capture auth within %ds", timeout_sec)
                return None

            # 3. Verify isDemo matches what was requested. If not, the captured
            # session won't work for our trading mode — caller should re-run.
            if captured["is_demo"] != is_demo:
                logger.warning("captured isDemo=%s but wanted %s — caller may need to switch",
                               captured["is_demo"], is_demo)

            return captured
        finally:
            try: await browser.close()
            except Exception: pass
