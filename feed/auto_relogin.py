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
import base64
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


def _make_ws_capture():
    """Build a captured-dict + websocket listener that pulls auth frames.

    Returns (captured: dict, on_ws: callable). The dict is mutated when an
    auth frame is observed."""
    captured: dict = {}

    def _on_ws(ws):
        async def _on_frame_sent(payload_or_data):
            data = getattr(payload_or_data, "payload", None) or payload_or_data
            if not isinstance(data, str):
                return
            if not data.startswith("42[\"auth\""):
                return
            try:
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

    return captured, _on_ws


async def fetch_fresh_ssid_via_state(
    state_b64: str,
    is_demo: bool = True,
    timeout_sec: int = 60,
    overall_timeout_sec: int = 180,
) -> Optional[dict]:
    """Use a pre-saved storage_state (cookies+localStorage from a real browser
    login) to skip the login form. Cloudflare won't challenge a returning
    visitor with valid cookies. Generated locally via tools/make_storage_state.py."""
    try:
        return await asyncio.wait_for(
            _fetch_via_state_impl(state_b64, is_demo, timeout_sec),
            timeout=overall_timeout_sec,
        )
    except asyncio.TimeoutError:
        logger.error("auto-relogin (state): HARD TIMEOUT after %ds", overall_timeout_sec)
        return None
    except Exception:
        logger.exception("auto-relogin (state): unexpected error")
        return None


async def _fetch_via_state_impl(state_b64: str, is_demo: bool, timeout_sec: int) -> Optional[dict]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("playwright not installed")
        return None

    # Decode + write state.json to a temp file (Playwright wants a path or dict)
    try:
        raw = base64.b64decode(state_b64)
        state_obj = json.loads(raw)
    except Exception:
        logger.exception("auto-relogin (state): failed to decode PO_STORAGE_STATE_B64")
        return None

    captured, on_ws = _make_ws_capture()
    logger.info("auto-relogin (state): launching chromium with saved session")

    async with async_playwright() as p:
        browser = await asyncio.wait_for(p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        ), timeout=60)
        try:
            context = await browser.new_context(
                storage_state=state_obj,
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/147.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale="en-US",
            )
            await context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            page = await context.new_page()
            page.on("websocket", on_ws)

            target = PO_TRADING_URL if is_demo else "https://pocketoption.com/en/cabinet/quick-high-low/"
            logger.info("auto-relogin (state): navigating to %s", target)
            try:
                await page.goto(target, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                logger.warning("auto-relogin (state): goto failed (%s) — continuing, WS may still fire", e)
            logger.info("auto-relogin (state): URL=%s, waiting for WS auth frame…", page.url)

            deadline = time.time() + timeout_sec
            while time.time() < deadline and not captured:
                await asyncio.sleep(1)

            if not captured.get("ssid"):
                logger.error("auto-relogin (state): no auth frame within %ds — session likely expired", timeout_sec)
                return None
            return captured
        finally:
            try: await browser.close()
            except Exception: pass


async def fetch_fresh_ssid(
    email: str,
    password: str,
    is_demo: bool = True,
    timeout_sec: int = 90,
    overall_timeout_sec: int = 240,
) -> Optional[dict]:
    """Outer wrapper with hard timeout — Playwright can hang on launch in
    low-memory containers (Railway), which would freeze the whole bot."""
    try:
        return await asyncio.wait_for(
            _fetch_fresh_ssid_impl(email, password, is_demo, timeout_sec),
            timeout=overall_timeout_sec,
        )
    except asyncio.TimeoutError:
        logger.error("auto-relogin: HARD TIMEOUT after %ds — Playwright stuck",
                     overall_timeout_sec)
        return None
    except Exception:
        logger.exception("auto-relogin: unexpected error")
        return None


async def _fetch_fresh_ssid_impl(
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

    logger.info("auto-relogin: starting headless Chromium (this takes ~30-60s)")

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

    logger.info("auto-relogin: entering async_playwright()")
    async with async_playwright() as p:
        logger.info("auto-relogin: launching chromium…")
        browser = await asyncio.wait_for(p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-features=IsolateOrigins,site-per-process",
                "--disable-gpu",
            ],
        ), timeout=60)
        logger.info("auto-relogin: chromium launched")
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
            logger.info("auto-relogin: context created, opening new page")
            page = await context.new_page()
            page.on("websocket", _on_ws)
            logger.info("auto-relogin: page opened")

            # 1. Open login page
            logger.info("auto-relogin: navigating to %s", PO_LOGIN_URL)
            try:
                await page.goto(PO_LOGIN_URL, wait_until="load", timeout=60000)
            except Exception as e:
                logger.warning("auto-relogin: goto load failed (%s), trying domcontentloaded", e)
                try:
                    await page.goto(PO_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
                except Exception as e2:
                    logger.warning("auto-relogin: goto domcontentloaded also failed (%s)", e2)
            logger.info("auto-relogin: page loaded, current URL: %s", page.url)
            # Wait for the login form to actually appear (commit fires very early)
            email_selector = "input[name=email], input[type=email], input[name='email']"
            try:
                await page.wait_for_selector(email_selector, timeout=45000, state="visible")
                logger.info("auto-relogin: email input visible")
            except Exception:
                # Dump page state so we can see what's blocking (CF challenge? iframe?)
                logger.warning("auto-relogin: email input not visible after 45s, URL=%s", page.url)
                try:
                    title = await page.title()
                    body_text = await page.evaluate("() => document.body ? document.body.innerText.slice(0, 500) : 'no body'")
                    inputs = await page.evaluate(
                        "() => Array.from(document.querySelectorAll('input')).map(i => ({name:i.name, type:i.type, id:i.id, placeholder:i.placeholder}))"
                    )
                    iframes = await page.evaluate(
                        "() => Array.from(document.querySelectorAll('iframe')).map(f => f.src)"
                    )
                    html = await page.evaluate(
                        "() => document.documentElement ? document.documentElement.outerHTML.slice(0, 3000) : 'no html'"
                    )
                    ready = await page.evaluate("() => document.readyState")
                    logger.warning("auto-relogin: title=%r readyState=%s", title, ready)
                    logger.warning("auto-relogin: body[:500]=%r", body_text)
                    logger.warning("auto-relogin: inputs=%s", inputs)
                    logger.warning("auto-relogin: iframes=%s", iframes)
                    logger.warning("auto-relogin: html[:3000]=%s", html)
                except Exception:
                    logger.exception("auto-relogin: page dump failed")
            await _human_pause(1.0, 2.5)

            # 2. Type credentials char-by-char with realistic delays
            try:
                logger.info("auto-relogin: typing email")
                await _human_type(page, "input[name=email], input[type=email]", email)
                await _human_pause(0.4, 1.0)
                logger.info("auto-relogin: typing password")
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
                    logger.info("auto-relogin: clicking submit")
                    await btn.click()
                else:
                    logger.info("auto-relogin: pressing Enter (no submit button found)")
                    await page.keyboard.press("Enter")
            except Exception:
                logger.exception("login form fill failed")
                return None
            logger.info("auto-relogin: form submitted, waiting for WS auth frame…")

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
