"""One-shot tool: connect to a (local or remote) Chrome CDP, open po-signals.com,
click the 'Экран' button in the left sidebar, pick the 15-window layout.

Usage (local):
    python3 tools/activate_layout.py

Usage (Railway TCP proxy):
    python3 tools/activate_layout.py --cdp-url http://roundhouse.proxy.rlwy.net:12345

After the script finishes, the 15-window layout is persisted in the Chrome
user-data-dir volume. You can delete the Railway TCP proxy immediately.
"""

import argparse
import asyncio
from playwright.async_api import async_playwright


PROBE = r"""
(() => {
  const out = {};
  // Find the 'Экран' button in the left sidebar
  const items = Array.from(document.querySelectorAll('button, a, div[role=button]'));
  const screen = items.find(el =>
    /^(?:\+\s*)?(Экран|Screen)$/i.test((el.innerText || '').trim())
  );
  out.screen_found = !!screen;
  if (screen) {
    const r = screen.getBoundingClientRect();
    out.screen_bbox = {x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2)};
    out.screen_text = screen.innerText;
  }
  // Look for any grid-like layout option that suggests 15 windows
  out.html_snippet = document.body.innerText.slice(0, 300);
  return out;
})();
"""


PROBE_AFTER_CLICK = r"""
(() => {
  // After clicking 'Экран', a layout chooser should appear. Find options.
  const out = {options: []};
  // Common pattern: modal/dropdown with layout grid options
  const all = Array.from(document.querySelectorAll('button, div[role=button], li, [role=option]'));
  for (const el of all) {
    const txt = (el.innerText || '').trim();
    // Look for things like "15", "5x3", or pattern hints
    if (/^1[45]$|1[45]\s*(?:окон|windows)|5\s*[x×]\s*3|Grid|Сетка/i.test(txt)) {
      const r = el.getBoundingClientRect();
      out.options.push({
        text: txt.slice(0, 50),
        bbox: {x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2)},
        classes: Array.from(el.classList || []).slice(0, 4),
      });
    }
  }
  // Also dump any visible modal's HTML for inspection
  const modal = document.querySelector('[role=dialog], [role=menu], .absolute') ||
                document.querySelector('div[class*=popover i], div[class*=modal i]');
  if (modal) {
    out.modal_text = (modal.innerText || '').slice(0, 400);
  }
  return out;
})();
"""


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp-url", default="http://localhost:9222")
    ap.add_argument("--dry-run", action="store_true", help="just probe, don't click")
    args = ap.parse_args()

    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(args.cdp_url)
    page = None
    for ctx in browser.contexts:
        for p in ctx.pages:
            if "po-signals.com" in p.url:
                page = p; break
        if page: break
    if not page:
        print("po-signals.com tab not found"); return
    print(f"attached: {page.url}")

    probe = await page.evaluate(PROBE)
    print(f"probe: {probe}")

    if not probe.get("screen_found"):
        print("❌ 'Экран' button not found in DOM. Maybe not logged in or wrong page.")
        await browser.close(); await pw.stop(); return

    if args.dry_run:
        print("DRY RUN — not clicking"); await browser.close(); await pw.stop(); return

    # Click the 'Экран' button
    bx = probe["screen_bbox"]
    print(f"clicking 'Экран' at {bx}")
    await page.mouse.click(bx["x"], bx["y"])
    await asyncio.sleep(1.0)

    # Probe for layout options
    layout = await page.evaluate(PROBE_AFTER_CLICK)
    print(f"after-click probe: {layout}")

    if not layout["options"]:
        print("❌ No layout option matching '15' / '5x3' found.")
        print("Modal text (if any):", layout.get("modal_text"))
        print("You'll need to inspect DOM manually and tell me selector.")
        await browser.close(); await pw.stop(); return

    # Pick the first option whose text has "15" or "5x3"
    target = layout["options"][0]
    print(f"clicking layout option: {target}")
    await page.mouse.click(target["bbox"]["x"], target["bbox"]["y"])
    await asyncio.sleep(1.5)

    # Verify — should now see 15 chart windows
    count = await page.evaluate(
        "() => document.querySelectorAll('button span.iconify.i-lucide\\\\:chevron-left').length"
    )
    print(f"verification: {count} chevron-left buttons found "
          f"(expect 15 for multi-chart mode)")

    await browser.close()
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
