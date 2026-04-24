"""Auto-login for po-signals.com.

The site drops session every ~1 hour and requires email+password (no captcha, no 2FA).
This module detects the login page inside the attached CDP browser and fills the form.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)


async def is_on_login_page(page) -> bool:
    """Heuristic: login page URL contains sign-in / login / auth, or has email input."""
    url = page.url.lower()
    if any(k in url for k in ("/sign-in", "/signin", "/login", "/auth/sign-in")):
        return True
    # Fallback: presence of email + password inputs
    try:
        has_email = await page.evaluate(
            "() => !!document.querySelector('input[type=email], input[name=email], input[placeholder*=mail i]')"
        )
        has_pwd = await page.evaluate(
            "() => !!document.querySelector('input[type=password], input[name=password]')"
        )
        return bool(has_email and has_pwd)
    except Exception:
        return False


async def auto_login(page, email: str, password: str, timeout: int = 30) -> bool:
    """Fill the login form and submit. Returns True on success (navigated off login page)."""
    if not email or not password:
        logger.error("auth.email or auth.password is empty in config — cannot auto-login")
        return False

    logger.info("attempting auto-login as %s", email)

    # Fill email (try multiple selectors)
    email_selectors = [
        "input[type=email]",
        "input[name=email]",
        "input[placeholder*=mail i]",
        "input[autocomplete=email]",
    ]
    for sel in email_selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                await el.fill(email)
                logger.debug("email filled via %s", sel)
                break
        except Exception:
            continue

    # Fill password
    pwd_selectors = [
        "input[type=password]",
        "input[name=password]",
        "input[autocomplete*=password]",
    ]
    for sel in pwd_selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                await el.fill(password)
                logger.debug("password filled via %s", sel)
                break
        except Exception:
            continue

    # Click submit
    submit_selectors = [
        "button[type=submit]",
        "button:has-text('Войти')",
        "button:has-text('Sign in')",
        "button:has-text('Log in')",
        "button:has-text('Login')",
        "form button",
    ]
    clicked = False
    for sel in submit_selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                await el.click()
                logger.debug("submit clicked via %s", sel)
                clicked = True
                break
        except Exception:
            continue

    if not clicked:
        logger.error("could not find submit button")
        return False

    # Wait for navigation off login page
    try:
        for _ in range(timeout):
            await asyncio.sleep(1)
            url = page.url.lower()
            if not any(k in url for k in ("sign-in", "signin", "login", "auth")):
                logger.info("auto-login succeeded, on: %s", page.url)
                return True
    except Exception as e:
        logger.error("wait-for-redirect failed: %s", e)

    logger.error("auto-login timeout (still on %s)", page.url)
    return False


async def ensure_logged_in(page, cfg_auth: dict) -> bool:
    """If page is on login screen, perform auto-login. Returns True if session is usable."""
    if not await is_on_login_page(page):
        return True
    return await auto_login(page, cfg_auth.get("email", ""), cfg_auth.get("password", ""))
