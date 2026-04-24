"""Auto-assign PoSignals multi-chart windows to unique pairs in a payout range.

Called from state_machine periodically. Logic mirrors tools/autoset_windows.py
but designed to be safe to call while the bot runs.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)


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

    const next = Array.from(root.querySelectorAll('button')).find(b =>
                   b.querySelector('span.iconify.i-lucide\\:chevron-right'));
    if (!next) continue;

    const lines = (root.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
    let pair = null, payout = null;
    for (const ln of lines) {
      const m = ln.match(/^([A-Z0-9#\-]{2,10}\/[A-Z0-9#\-]{2,10}(?:\s+OTC)?)\s+(\d{1,3})%/i);
      if (m) { pair = m[1].replace(/\s+/g,' '); payout = parseInt(m[2]); break; }
    }
    if (!pair && lines[0] && /\//.test(lines[0])) {
      pair = lines[0].trim();
      const pm = (lines[1] || '').match(/(\d{1,3})%/);
      if (pm) payout = parseInt(pm[1]);
    }
    const nb = next.getBoundingClientRect();
    windows.push({
      pair, payout,
      next_x: Math.round(nb.x + nb.width/2),
      next_y: Math.round(nb.y + nb.height/2),
    });
  }
  windows.sort((a,b) => a.next_y - b.next_y || a.next_x - b.next_x);
  return windows;
})();
"""


async def probe_windows(page) -> list[dict]:
    try:
        return await page.evaluate(PROBE_WINDOWS)
    except Exception as e:
        logger.debug("probe_windows failed: %s", e)
        return []


def _pair_label_to_symbol(label: str) -> str:
    """'AUD/CHF OTC' → 'AUDCHF_otc'  ('EUR/USD' → 'EURUSD')."""
    if not label:
        return ""
    parts = label.replace("/", "").split()
    base = parts[0]
    suffix = "_otc" if len(parts) > 1 and parts[1].upper() == "OTC" else ""
    return base + suffix


async def ensure_pair_in_window(
    page,
    target_symbol: str,
    click_delay: float = 0.35,
    max_per_window: int = 60,
) -> bool:
    """Make sure `target_symbol` is currently displayed in one of the multi-chart
    windows. If not, cycle the window with the lowest payout until it lands on
    `target_symbol`. Returns True on success."""
    if not target_symbol:
        return False
    windows = await probe_windows(page)
    if not windows:
        return False

    # Already present?
    for w in windows:
        if _pair_label_to_symbol(w.get("pair") or "") == target_symbol:
            return True

    # Pick the window least-useful to us (lowest payout, or first one)
    windows_sorted = sorted(
        enumerate(windows),
        key=lambda kv: (kv[1].get("payout") or 0)
    )
    idx, w = windows_sorted[0]

    for tries in range(1, max_per_window + 1):
        try:
            await page.mouse.click(w["next_x"], w["next_y"])
        except Exception as e:
            logger.debug("ensure_pair click failed: %s", e)
            return False
        await asyncio.sleep(click_delay)
        fresh = await probe_windows(page)
        if idx >= len(fresh):
            return False
        w = fresh[idx]
        if _pair_label_to_symbol(w.get("pair") or "") == target_symbol:
            logger.info("ensure_pair_in_window: %s now in window %d after %d click(s)",
                        target_symbol, idx, tries)
            return True
    logger.warning("ensure_pair_in_window: gave up finding %s after %d clicks",
                   target_symbol, max_per_window)
    return False


async def autoset_windows(
    page,
    min_payout: int = 85,
    max_payout: int = 92,
    click_delay: float = 0.35,
    max_per_window: int = 40,
) -> dict:
    """Cycle the 'next' button of each multi-chart window until it holds a
    pair with payout in [min_payout, max_payout] that no other window holds.

    Returns summary: {windows: N, kept: K, cycled: C, unique_good: U}.
    """
    windows = await probe_windows(page)
    if len(windows) < 2:
        return {"windows": len(windows), "kept": 0, "cycled": 0, "unique_good": 0,
                "note": "multi-chart mode not active"}

    def is_desired(p):
        return p is not None and min_payout <= p <= max_payout

    taken = set()
    cycle_indices = []
    for idx, w in enumerate(windows):
        lbl = (w.get("pair") or "").strip()
        if is_desired(w.get("payout")) and lbl and lbl not in taken:
            taken.add(lbl)
        else:
            cycle_indices.append(idx)

    kept = len(taken)
    cycled = 0
    for idx in cycle_indices:
        w = windows[idx]
        tries = 0
        while tries < max_per_window:
            try:
                await page.mouse.click(w["next_x"], w["next_y"])
            except Exception as e:
                logger.debug("click failed window %d: %s", idx, e); break
            await asyncio.sleep(click_delay)
            fresh = await probe_windows(page)
            if idx >= len(fresh):
                break
            w = fresh[idx]
            tries += 1
            lbl = (w.get("pair") or "").strip()
            if is_desired(w.get("payout")) and lbl and lbl not in taken:
                taken.add(lbl)
                cycled += 1
                logger.info("window %d: cycled to %s (%s%%) in %d click(s)",
                            idx, lbl, w.get("payout"), tries)
                break
        else:
            logger.info("window %d: gave up after %d clicks", idx, max_per_window)

    return {
        "windows": len(windows),
        "kept": kept,
        "cycled": cycled,
        "unique_good": len(taken),
    }
