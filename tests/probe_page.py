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
    # Get ALL keys from window that contain "bot" or "signal" or "consensus"
    out = await page.evaluate("""() => {
        const keys = Object.keys(window).filter(k =>
            /bot|signal|consensus|markers|chart|asset/i.test(k)
        );
        return {
            keys: keys,
            // Check common locations
            posigHooked: !!window.__posigHooked,
            posigUserWS: !!window.__posigUserWS,
            posigTickWS: !!window.__posigTickWS,
            currentAsset: window.__currentAsset,
            botSignals: window.__botSignals,
            // Iframes?
            iframes_count: document.querySelectorAll('iframe').length,
            // Check workers on page
            workers_api: typeof Worker,
            // Does __scripts or similar exist?
            scripts_globals: Object.keys(window).filter(k => /script/i.test(k)),
        };
    }""")
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))

    # Check frames
    frames = page.frames
    print(f"\nFrames: {len(frames)}")
    for f in frames:
        print(f"  - {f.url[:80]}")
        try:
            has_sig = await f.evaluate("() => !!window.__botSignals")
            if has_sig:
                sigs = await f.evaluate("() => ({keys: Object.keys(window.__botSignals||{}), sample: window.__botSignals})")
                print(f"    FOUND __botSignals in frame: {sigs}")
        except Exception as e:
            print(f"    err: {e}")

    await browser.close()
    await pw.stop()

asyncio.run(main())
