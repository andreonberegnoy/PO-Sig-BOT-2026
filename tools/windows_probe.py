"""Scan the po-signals.com DOM to discover the 15 multi-chart windows:
for each window, extract current pair name + bbox of prev/next buttons."""

import asyncio
import json
from playwright.async_api import async_playwright

PROBE = r"""
(() => {
  // Each window's prev/next buttons live inside:
  //   div.relative.flex-1.p-0.min-h-0 > div.absolute.bottom-1.5.right-1.5.z-50 > ...
  // The *window root* is the closest ancestor div.relative that wraps one chart.
  // A reliable pattern: find every button with chevron-left, then walk up until
  // we hit an element that ALSO contains a flag/symbol label.

  const lefts = Array.from(document.querySelectorAll(
    'button span.iconify.i-lucide\\:chevron-left'
  )).map(s => s.closest('button'));

  const rights = Array.from(document.querySelectorAll(
    'button span.iconify.i-lucide\\:chevron-right'
  )).map(s => s.closest('button'));

  // Group by shared ancestor (the window container). Walk up until the ancestor
  // also contains a chevron-right counterpart.
  function findWindowRoot(btn) {
    let el = btn;
    for (let i = 0; i < 10 && el; i++) {
      el = el.parentElement;
      if (!el) return null;
      if (el.querySelector('span.iconify.i-lucide\\:chevron-left') &&
          el.querySelector('span.iconify.i-lucide\\:chevron-right')) {
        // Might need to go one level higher to capture the pair label too.
        // Keep going until we also find a pair-like string.
        const txt = (el.innerText || '');
        if (/[A-Z]{3,6}[\s/\-]+[A-Z]{3,6}|OTC/i.test(txt) || i >= 3) return el;
      }
    }
    return null;
  }

  const seen = new Set();
  const windows = [];
  for (const btn of lefts) {
    const root = findWindowRoot(btn) || btn.parentElement;
    if (!root || seen.has(root)) continue;
    seen.add(root);

    const prev = root.querySelector('button:has(span.iconify.i-lucide\\:chevron-left)')
              || Array.from(root.querySelectorAll('button')).find(b =>
                   b.querySelector('span.iconify.i-lucide\\:chevron-left'));
    const next = root.querySelector('button:has(span.iconify.i-lucide\\:chevron-right)')
              || Array.from(root.querySelectorAll('button')).find(b =>
                   b.querySelector('span.iconify.i-lucide\\:chevron-right'));

    // Pair name: look for a visible short text like "EUR/USD OTC 92%" near the top
    // of the window. Grab ALL short lines and pick the first that matches.
    const lines = (root.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
    let pair = null, payout = null;
    for (const ln of lines) {
      const m = ln.match(/^([A-Z0-9#\-]{2,10}\/[A-Z0-9#\-]{2,10}(?:\s+OTC)?)\s+(\d{1,3})%/i);
      if (m) { pair = m[1].replace(/\s+/g, ' '); payout = parseInt(m[2]); break; }
    }

    const pb = prev ? prev.getBoundingClientRect() : null;
    const nb = next ? next.getBoundingClientRect() : null;
    windows.push({
      pair, payout,
      prev_bbox: pb && {x: Math.round(pb.x+pb.width/2), y: Math.round(pb.y+pb.height/2)},
      next_bbox: nb && {x: Math.round(nb.x+nb.width/2), y: Math.round(nb.y+nb.height/2)},
      first_lines: lines.slice(0, 4),
    });
  }
  return {
    total: windows.length,
    windows: windows.sort((a,b) =>
      (a.next_bbox?.y || 0) - (b.next_bbox?.y || 0) ||
      (a.next_bbox?.x || 0) - (b.next_bbox?.x || 0)
    ),
  };
})();
"""


async def main():
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp("http://localhost:9222")
    page = None
    for ctx in browser.contexts:
        for p in ctx.pages:
            if "po-signals.com" in p.url:
                page = p; break
        if page: break
    if not page:
        print("no page"); return
    print(f"attached: {page.url}")
    result = await page.evaluate(PROBE)
    print(f"windows found: {result['total']}")
    for i, w in enumerate(result["windows"]):
        print(f"  #{i:2d}  pair={w.get('pair')!r:25}  payout={w.get('payout')}  "
              f"prev={w.get('prev_bbox')}  next={w.get('next_bbox')}")
        if not w.get("pair"):
            print(f"       first_lines={w.get('first_lines')}")
    await browser.close()
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
