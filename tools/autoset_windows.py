"""Auto-assign PoSignals multi-chart windows to top-15 tradable pairs.

Strategy:
  1. Read feed.assets (from running Chrome) → pick top-15 by payout in [85,92]
  2. Probe the 15 windows → current pair per window
  3. For each window whose pair is NOT in the desired-15 (or is a duplicate),
     click ">" until it lands on a desired pair that's not yet taken.

Run:
    python3 tools/autoset_windows.py              # plan + execute
    python3 tools/autoset_windows.py --dry-run    # show plan only, no clicks
"""

import argparse
import asyncio
import json
import sys
from playwright.async_api import async_playwright


PROBE_WINDOWS = r"""
(() => {
  const lefts = Array.from(document.querySelectorAll(
    'button span.iconify.i-lucide\\:chevron-left'
  )).map(s => s.closest('button'));

  function findRoot(btn) {
    let el = btn;
    for (let i = 0; i < 10 && el; i++) {
      el = el.parentElement;
      if (!el) return null;
      if (el.querySelector('span.iconify.i-lucide\\:chevron-left') &&
          el.querySelector('span.iconify.i-lucide\\:chevron-right') &&
          i >= 3) return el;
    }
    return null;
  }

  const seen = new Set();
  const windows = [];
  for (const btn of lefts) {
    const root = findRoot(btn);
    if (!root || seen.has(root)) continue;
    seen.add(root);

    const prev = Array.from(root.querySelectorAll('button')).find(b =>
                   b.querySelector('span.iconify.i-lucide\\:chevron-left'));
    const next = Array.from(root.querySelectorAll('button')).find(b =>
                   b.querySelector('span.iconify.i-lucide\\:chevron-right'));

    const lines = (root.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
    let pair = null, payout = null;
    for (const ln of lines) {
      const m = ln.match(/^([A-Z0-9#\-]{2,10}\/[A-Z0-9#\-]{2,10}(?:\s+OTC)?)\s+(\d{1,3})%/i);
      if (m) { pair = m[1].replace(/\s+/g,' '); payout = parseInt(m[2]); break; }
    }
    // Fallback: first line is pair label, second is payout
    if (!pair && lines[0] && /\//.test(lines[0])) {
      pair = lines[0].trim();
      const pm = (lines[1] || '').match(/(\d{1,3})%/);
      if (pm) payout = parseInt(pm[1]);
    }

    const pb = prev.getBoundingClientRect();
    const nb = next.getBoundingClientRect();
    windows.push({
      pair, payout,
      prev_x: Math.round(pb.x + pb.width/2),  prev_y: Math.round(pb.y + pb.height/2),
      next_x: Math.round(nb.x + nb.width/2),  next_y: Math.round(nb.y + nb.height/2),
    });
  }
  windows.sort((a,b) => a.next_y - b.next_y || a.next_x - b.next_x);
  return windows;
})();
"""


def label_to_symbol(label: str) -> str:
    """'AUD/CHF OTC' -> 'AUDCHF_otc'  ('EUR/USD' -> 'EURUSD')."""
    parts = label.replace("/", "").split()
    base = parts[0]
    suffix = "_otc" if len(parts) > 1 and parts[1].upper() == "OTC" else ""
    return base + suffix


def symbol_to_label(sym: str) -> str:
    s = sym
    otc = ""
    if s.endswith("_otc"):
        otc = " OTC"
        s = s[:-4]
    # Split into 2 halves by currency code length — most are 3/3; a few 4+3, 3+4.
    # PoSignals labels we've seen are all 3/3 so go with that.
    return f"{s[:3]}/{s[3:]}{otc}"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--min-payout", type=int, default=85)
    ap.add_argument("--max-payout", type=int, default=92)
    ap.add_argument("--click-delay", type=float, default=0.4,
                    help="seconds between clicks so UI has time to repaint")
    ap.add_argument("--max-per-window", type=int, default=40,
                    help="safety cap on clicks per window")
    args = ap.parse_args()

    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp("http://localhost:9222")
    page = None
    for ctx in browser.contexts:
        for p in ctx.pages:
            if "po-signals.com" in p.url:
                page = p; break
        if page: break
    if not page:
        print("po-signals.com tab not found"); return
    print(f"attached: {page.url}")

    # Get assets list with payouts from window.assets if exposed, else from initial probe
    # We'll just read payouts of the windows + do many clicks. Desired set is built from
    # whatever pairs we encounter that match payout criteria.
    #
    # To build a stable target set we read the assets list from the existing bot's source
    # of truth if available: a small probe on the initial_data cached in the page.
    # Fallback: accumulate as we click.

    windows = await page.evaluate(PROBE_WINDOWS)
    print(f"found {len(windows)} windows\n")
    for i, w in enumerate(windows):
        print(f"  #{i:2d}  {w['pair']!r:20}  payout={w['payout']}")

    if len(windows) < 2:
        print("Not in 15-window mode? Enable the multi-chart layout on the site first.")
        return

    # Desired = any pair with payout in [min, max]. We'll collect as we click.
    def is_desired(payout):
        return payout is not None and args.min_payout <= payout <= args.max_payout

    # First pass: dedupe and fix out-of-range payouts.
    taken = set()     # labels already assigned and acceptable
    plan = []         # list of actions to perform
    for idx, w in enumerate(windows):
        lbl = (w["pair"] or "").strip()
        if is_desired(w["payout"]) and lbl and lbl not in taken:
            taken.add(lbl)
            plan.append((idx, "keep", lbl))
        else:
            plan.append((idx, "cycle", lbl))

    print("\nInitial plan:")
    for idx, act, lbl in plan:
        print(f"  window {idx:2d}: {act.upper():6}  current={lbl or '?'}")

    if args.dry_run:
        print("\nDRY RUN — no clicks performed.")
        await browser.close(); await pw.stop()
        return

    # Execute: for each 'cycle' window, click next until we land on an acceptable unique pair.
    for idx, act, _ in plan:
        if act != "cycle":
            continue
        w = windows[idx]
        tries = 0
        while tries < args.max_per_window:
            # Click the next button at its coordinate
            try:
                await page.mouse.click(w["next_x"], w["next_y"])
            except Exception as e:
                print(f"  click failed on window {idx}: {e}"); break
            await asyncio.sleep(args.click_delay)
            # Re-probe to read new pair for this window
            fresh = await page.evaluate(PROBE_WINDOWS)
            if idx >= len(fresh):
                print(f"  lost window {idx} during cycling?"); break
            w = fresh[idx]
            tries += 1
            lbl = (w["pair"] or "").strip()
            if is_desired(w["payout"]) and lbl and lbl not in taken:
                taken.add(lbl)
                print(f"  window {idx:2d}: after {tries} click(s) → {lbl} ({w['payout']}%)")
                break
        else:
            print(f"  window {idx:2d}: gave up after {args.max_per_window} clicks")

    # Final state
    final = await page.evaluate(PROBE_WINDOWS)
    print("\nFinal state:")
    for i, w in enumerate(final):
        mark = "✓" if is_desired(w["payout"]) else "✗"
        print(f"  {mark} #{i:2d}  {w['pair']!r:20}  payout={w['payout']}")

    await browser.close()
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
