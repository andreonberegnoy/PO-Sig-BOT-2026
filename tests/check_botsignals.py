"""Verify window.__botSignals is populated by the patched indicator."""

import asyncio, json, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from playwright.async_api import async_playwright

async def main():
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp("http://localhost:9222")
    page = None
    for ctx in browser.contexts:
        for p in ctx.pages:
            if "po-signals.com" in p.url:
                page = p; break
    if not page:
        print("no page"); return
    result = await page.evaluate("""() => {
        const sigs = window.__botSignals || null;
        const keys = sigs ? Object.keys(sigs) : [];
        const out = {};
        for (const k of keys) {
            const v = sigs[k];
            out[k] = {
                ts: new Date(v.ts).toISOString(),
                markers_count: (v.markers||[]).length,
                last_few: (v.markers||[]).slice(-3),
                lastCandleTime: v.lastCandleTime,
            };
        }
        return { has_global: !!window.__botSignals, keys_found: keys, content: out };
    }""")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    await browser.close()
    await pw.stop()

asyncio.run(main())
