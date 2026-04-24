"""Attach to the running Chrome (CDP port 9222), install a 60-second click
recorder on po-signals.com, and dump everything to stdout.

Usage:
    python3 tools/click_recorder.py

Then on the site: click the pair dropdown, pick a pair, etc. Every click will
print a compact line with the target element's tag / classes / text / CSS path.
"""

import asyncio
from playwright.async_api import async_playwright

INSTALL_SCRIPT = r"""
(() => {
  if (window.__clickRec) return 'already';
  window.__clickRec = true;
  window.__clickLog = [];

  function pathOf(el) {
    const parts = [];
    while (el && el.nodeType === 1 && parts.length < 6) {
      let s = el.tagName.toLowerCase();
      if (el.id) s += '#' + el.id;
      if (el.classList && el.classList.length)
        s += '.' + Array.from(el.classList).slice(0, 4).join('.');
      parts.unshift(s);
      el = el.parentElement;
    }
    return parts.join(' > ');
  }

  document.addEventListener('click', (e) => {
    const el = e.target;
    const info = {
      ts: Date.now(),
      tag: el.tagName,
      id: el.id || null,
      classes: Array.from(el.classList || []),
      text: (el.innerText || '').slice(0, 60).replace(/\s+/g, ' ').trim(),
      data: Object.fromEntries(
        Array.from(el.attributes || []).filter(a => a.name.startsWith('data-'))
          .map(a => [a.name, a.value])
      ),
      path: pathOf(el),
      bbox: (() => { const r = el.getBoundingClientRect();
                     return {x: Math.round(r.x), y: Math.round(r.y),
                             w: Math.round(r.width), h: Math.round(r.height)}; })(),
    };
    window.__clickLog.push(info);
    console.log('__CLICKREC__' + JSON.stringify(info));
  }, true);
  return 'installed';
})();
"""


async def main():
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp("http://localhost:9222")
    page = None
    for ctx in browser.contexts:
        for p in ctx.pages:
            if "po-signals.com" in p.url:
                page = p
                break
        if page:
            break
    if not page:
        print("po-signals.com tab not found")
        return
    print(f"attached: {page.url}")

    # capture console logs
    def on_console(msg):
        t = msg.text
        if t and t.startswith("__CLICKREC__"):
            print(t[len("__CLICKREC__"):])
    page.on("console", on_console)

    result = await page.evaluate(INSTALL_SCRIPT)
    print(f"install result: {result}")
    print("Recording clicks for 60 seconds — please open pair dropdown, pick a pair, etc.")
    print("-" * 80)

    await asyncio.sleep(60)

    # dump the final log from page memory as well (in case console was missed)
    log = await page.evaluate("window.__clickLog || []")
    print("-" * 80)
    print(f"final log size: {len(log)}")
    for item in log[-20:]:
        print(item)

    await browser.close()
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
