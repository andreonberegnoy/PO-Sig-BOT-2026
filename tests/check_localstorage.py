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
        const entries = {};
        const bot = {};
        for (let i = 0; i < localStorage.length; i++) {
            const k = localStorage.key(i);
            if (k.startsWith('__botSignals')) bot[k] = localStorage.getItem(k);
        }
        return {
            totalKeys: localStorage.length,
            botKeys: Object.keys(bot),
            botLast: bot['__botSignals_last'],
            sampleVal: Object.keys(bot).find(k => k !== '__botSignals_last') ? bot[Object.keys(bot).find(k => k !== '__botSignals_last')] : null,
        };
    }""")
    print(json.dumps(result, indent=2, ensure_ascii=False)[:2000])

asyncio.run(main())
