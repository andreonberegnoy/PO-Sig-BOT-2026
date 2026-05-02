"""Generate Pocket Option storage_state.json on your local machine.

Why: Cloudflare blocks headless logins from datacenter IPs (Hetzner / любой VPS).
Workaround: log in once on your laptop in a real browser, save cookies +
localStorage to a file, ship it to the server. PO sessions live ~30 days.

Usage:
  pip install playwright && playwright install chromium
  python3 tools/make_storage_state.py

Then a Chromium window opens. Log in to PO normally (do captcha / 2FA if
needed), wait until you see the trading page. Come back to terminal and
press Enter. state.json is saved in cwd.

Then ship to VPS — один скрипт делает всё (.env update + restart):
  bash tools/update_po_session.sh
"""

import asyncio
import sys
from pathlib import Path


PO_LOGIN_URL = "https://pocketoption.com/en/login/"


async def main():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("playwright not installed. Run: pip install playwright && playwright install chromium")
        sys.exit(1)

    out_path = Path("state.json").absolute()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()
        await page.goto(PO_LOGIN_URL)
        print()
        print("=" * 60)
        print("1. Log in to Pocket Option in the opened browser window.")
        print("2. Wait until you see your trading dashboard.")
        print("3. Come back here and press Enter.")
        print("=" * 60)
        await asyncio.get_event_loop().run_in_executor(None, input)
        await context.storage_state(path=str(out_path))
        print(f"\n✓ Saved: {out_path}")
        print("\nNext step — залить на VPS одной командой:")
        print("  bash tools/update_po_session.sh")
        print("\n(скрипт сам обновит .env на VPS и перезапустит контейнер)")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
