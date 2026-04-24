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


async def is_on_landing_page(page) -> bool:
    """Landing page has a 'Войти' button in the header but no form yet.
    We detect it by URL being root and presence of that button."""
    url = page.url.lower().rstrip("/")
    if url not in ("https://po-signals.com", "https://po-signals.com/en", "https://po-signals.com/ru"):
        return False
    try:
        has_signin_btn = await page.evaluate(
            "() => !!Array.from(document.querySelectorAll('button, a')).find(b => "
            "  /войти|sign\\s*in|log\\s*in/i.test((b.innerText || '').trim()))"
        )
        return bool(has_signin_btn)
    except Exception:
        return False


async def open_login_modal(page) -> bool:
    """Click the 'Войти' button in the header to open the login modal."""
    try:
        clicked = await page.evaluate(
            """() => {
                const btn = Array.from(document.querySelectorAll('button, a')).find(b =>
                    /^(войти|sign\\s*in|log\\s*in)$/i.test((b.innerText || '').trim()));
                if (!btn) return false;
                btn.click();
                return true;
            }"""
        )
        if not clicked:
            logger.warning("could not find 'Войти' button on landing page")
            return False
        logger.info("clicked 'Войти' button — waiting for modal")
        # Wait up to 10s for an email input to appear
        for _ in range(20):
            await asyncio.sleep(0.5)
            has_email = await page.evaluate(
                "() => !!document.querySelector('input[type=email], input[name=email]')"
            )
            if has_email:
                logger.info("login modal opened")
                return True
        logger.warning("login modal did not appear within 10s")
        return False
    except Exception as e:
        logger.exception("open_login_modal failed: %s", e)
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
    """Session check: if login form visible → fill it; if landing page → click
    'Войти' to open modal, then fill. Returns True if session is usable."""
    # Case 1: login form already visible (either /sign-in URL or opened modal)
    if await is_on_login_page(page):
        return await auto_login(page, cfg_auth.get("email", ""), cfg_auth.get("password", ""))
    # Case 2: landing page with a 'Войти' button we need to click
    if await is_on_landing_page(page):
        if not await open_login_modal(page):
            return False
        return await auto_login(page, cfg_auth.get("email", ""), cfg_auth.get("password", ""))
    # Otherwise assume session is active
    return True
