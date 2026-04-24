"""po-signals.com feed — CDP hook, listens to two socket.io WS connections.

Parses:
  - common.assets_list  → {symbol: {payout, max_payout, label, is_otc, ...}}
  - user.{real,demo}.initial_data → balance, limits
  - user.{real,demo}.update_balance → live balance
  - user.{real,demo}.open_trade.success → our own trade opened (confirmation)
  - user.{real,demo}.close_trade.success → trade result (WIN/LOSS)
  - tick → live OHLCV candle updates (ticker2)

Exposes:
  feed.assets          dict[symbol] -> asset_info
  feed.balance_real    float | None
  feed.balance_demo    float | None
  feed.on_trade_close  callable(trade_dict)
  feed.on_asset_update callable(assets_dict)
  feed.get_candles(symbol, period, limit)
  feed.send_ws(event_name, payload)  → raw socket.io emit via injected page
"""

import asyncio
import base64
import json
import logging
import re
import time
from collections import defaultdict, deque
from typing import Any, Callable, Optional

import msgpack

logger = logging.getLogger(__name__)

# Script injected before page load to capture both WS connections by reference.
# We hook the WebSocket constructor and save refs keyed by URL substring.
WS_HOOK_SCRIPT = r"""
(() => {
  try {
    if (window.__posigHooked) { console.log('__POSIG_HOOK__ already hooked'); return; }
    window.__posigHooked = true;
    window.__posigUserWS = null;
    window.__posigTickWS = null;
    window.__posigAllWS = [];
    const _Orig = window.WebSocket;
    if (!_Orig) { console.log('__POSIG_HOOK__ no WebSocket global'); return; }

    // --- layer 1: patch window.WebSocket for future `new WebSocket(...)` calls ---
    function Patched(url, protocols) {
      const ws = protocols ? new _Orig(url, protocols) : new _Orig(url);
      try {
        window.__posigAllWS.push(ws);
        if (url && url.includes('/po-ws/')) window.__posigUserWS = ws;
        else if (url && url.includes('ticker2.po-signals')) window.__posigTickWS = ws;
      } catch (e) {}
      return ws;
    }
    Patched.prototype = _Orig.prototype;
    for (const k of ['CONNECTING','OPEN','CLOSING','CLOSED']) Patched[k] = _Orig[k];
    window.WebSocket = Patched;

    // --- layer 2: patch prototype.send to catch instances created via
    // closured refs to the ORIGINAL constructor (bundler captured WebSocket
    // into its scope before our hook ran). Any WS instance ultimately calls
    // the SAME prototype.send, so this catches them all on their first send. ---
    const _origSend = _Orig.prototype.send;
    _Orig.prototype.send = function(data) {
      try {
        const u = this.url || '';
        if (!window.__posigAllWS.includes(this)) window.__posigAllWS.push(this);
        if (u.includes('/po-ws/')) window.__posigUserWS = this;
        else if (u.includes('ticker2.po-signals')) window.__posigTickWS = this;
        // Log outgoing frames on user WS to help diagnose open_trade format.
        // Skip socket.io heartbeats ("2") — they're noise.
        if (u.includes('/po-ws/') && typeof data === 'string' && data !== '2' && data !== '3') {
          console.log('__POSIG_SEND__' + data);
        }
      } catch (e) {}
      return _origSend.apply(this, arguments);
    };

    console.log('__POSIG_HOOK__ installed OK at', location.href);
  } catch (e) {
    console.log('__POSIG_HOOK__ error:', String(e));
  }
})();
"""


class PoFeed:
    def __init__(self, mode: str = "paper", auth_cfg: Optional[dict] = None):
        self.mode = mode  # paper → use demo events; real → real events
        self._acct = "demo" if mode == "paper" else "real"
        self._auth_cfg = auth_cfg or {}

        self._pw = None
        self._browser = None
        self._page = None
        self._cdp = None

        # state
        self.assets: dict[str, dict] = {}
        self.balance_real: Optional[float] = None
        self.balance_demo: Optional[float] = None
        self.user_id: Optional[int] = None
        self.login_real: Optional[int] = None
        self.login_demo: Optional[int] = None
        self.jwt_token: Optional[str] = None

        # candles keyed by (symbol, period)
        self._candles: dict[tuple, deque] = defaultdict(lambda: deque(maxlen=2000))

        # WS plumbing
        self._ws_urls: dict[str, str] = {}       # request_id → url
        self._last_text_event: dict[str, str] = {}  # request_id → event name

        # callbacks
        self.on_trade_open: Optional[Callable] = None
        self.on_trade_close: Optional[Callable] = None
        self.on_assets_update: Optional[Callable] = None
        self.on_balance_update: Optional[Callable] = None
        self.on_tick: Optional[Callable] = None

        self._ready = asyncio.Event()

    # ---------- connect ----------

    async def connect(self, cdp_url: str = "http://localhost:9222"):
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(cdp_url)

        for ctx in self._browser.contexts:
            for p in ctx.pages:
                if "po-signals.com" in p.url:
                    self._page = p
                    break
            if self._page:
                break
        if not self._page:
            raise RuntimeError("po-signals.com tab not found — open it in Chrome first")

        logger.info("attached to page: %s", self._page.url)

        self._cdp = await self._page.context.new_cdp_session(self._page)
        await self._cdp.send("Network.enable")

        # Install hook BEFORE reload so we capture the constructors
        await self._cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": WS_HOOK_SCRIPT})

        # Belt-and-suspenders: re-inject on every frame navigation (CDP script
        # registration can lose the page if the site does SPA-level nav tricks).
        async def _reinject_on_nav(frame):
            try:
                if frame == self._page.main_frame:
                    await self._page.evaluate(WS_HOOK_SCRIPT)
            except Exception as e:
                logger.debug("reinject on nav failed: %s", e)
        self._page.on("framenavigated", lambda f: asyncio.create_task(_reinject_on_nav(f)))

        # Surface hook diagnostic logs from the page
        def _on_console(msg):
            t = msg.text
            if not t:
                return
            if t.startswith("__POSIG_HOOK__"):
                logger.info("page: %s", t)
            elif t.startswith("__POSIG_SEND__"):
                logger.info("WS SEND: %s", t[len("__POSIG_SEND__"):])
        self._page.on("console", _on_console)

        self._cdp.on("Network.webSocketCreated", self._on_ws_created)
        self._cdp.on("Network.webSocketFrameReceived", self._on_ws_recv)
        self._cdp.on("Network.webSocketFrameSent", self._on_ws_sent)

        # If the page isn't already at the trading app, navigate there explicitly.
        # On Railway the Chrome tab lands on https://po-signals.com/ (root) which
        # may redirect to login — we handle that right after.
        # Let page settle — Chrome may still be redirecting after boot.
        for _ in range(20):
            u = self._page.url
            if u and u != "about:blank":
                break
            await asyncio.sleep(0.5)
        logger.info("page settled on: %s", self._page.url)

        # Auto-login if the page landed on a login screen (happens on fresh
        # Chrome user-data-dir, or after ~2h session reset).
        from feed.auth import ensure_logged_in, open_login_modal, auto_login

        try:
            if self._auth_cfg:
                ok = await ensure_logged_in(self._page, self._auth_cfg)
                if not ok:
                    logger.error("auto-login failed during initial connect")
        except Exception:
            logger.exception("ensure_logged_in error during connect")

        # Wait for WS + assets list
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=60)
            logger.info("feed ready. assets=%d real_bal=%s demo_bal=%s",
                        len(self.assets), self.balance_real, self.balance_demo)
            return
        except asyncio.TimeoutError:
            logger.warning("feed not fully ready after 60s — retrying login")

        # Fallback: WS never came up. Likely we landed on /app/charts but without
        # session — site may show a 'Войти' button somewhere. Force open modal
        # and attempt login again, then wait once more.
        if self._auth_cfg and self._auth_cfg.get("email"):
            try:
                if await open_login_modal(self._page):
                    await auto_login(
                        self._page,
                        self._auth_cfg.get("email", ""),
                        self._auth_cfg.get("password", ""),
                    )
                    try:
                        await asyncio.wait_for(self._ready.wait(), timeout=45)
                        logger.info("feed ready after fallback login. assets=%d",
                                    len(self.assets))
                        return
                    except asyncio.TimeoutError:
                        logger.error("feed still not ready after fallback login")
            except Exception:
                logger.exception("fallback login attempt failed")
        logger.warning("feed never became ready — continuing anyway, bot will retry via REST")

    async def close(self):
        try:
            if self._browser: await self._browser.close()
            if self._pw: await self._pw.stop()
        except Exception:
            pass

    # ---------- WS event dispatch ----------

    def _on_ws_created(self, params):
        self._ws_urls[params["requestId"]] = params["url"]
        logger.debug("WS created: %s", params["url"])

    def _on_ws_sent(self, params):
        # We could log our own sent frames here for debugging, but nothing critical.
        pass

    def _on_ws_recv(self, params):
        rid = params["requestId"]
        url = self._ws_urls.get(rid, "")
        pd = params.get("response", {}).get("payloadData", "")
        if not pd:
            return
        try:
            # Text frame
            if pd and pd[0].isdigit():
                self._handle_text_frame(rid, url, pd)
            else:
                self._handle_binary_frame(rid, url, pd)
        except Exception as e:
            logger.exception("frame handler error: %s", e)

    def _handle_text_frame(self, rid: str, url: str, pd: str):
        # Socket.IO v4: "42[...]", "451-[...]", "40", "0{...}"
        m = re.match(r'^(\d+)(-?)(\[(.*)\])?$', pd, re.DOTALL)
        # Extract event name if present
        em = re.search(r'^\d+-?\["([^"]+)"', pd)
        if em:
            ev = em.group(1)
            self._last_text_event[rid] = ev
            # If it's a fully-inline payload (42["event",{...}]), parse JSON after event
            if pd.startswith("42[") and "_placeholder" not in pd:
                try:
                    body = json.loads(pd[2:])  # → [ev, payload]
                    if isinstance(body, list) and len(body) >= 2:
                        self._dispatch(ev, body[1], url)
                except Exception:
                    pass
            # else: payload comes in the following binary frame

    def _handle_binary_frame(self, rid: str, url: str, pd_b64: str):
        try:
            raw = base64.b64decode(pd_b64)
        except Exception:
            return
        obj = None
        # Try JSON first (socket.io default parser base64-encodes JSON payloads)
        try:
            obj = json.loads(raw.decode("utf-8"))
        except Exception:
            # Fall back to msgpack (some events use msgpack parser)
            try:
                obj = msgpack.unpackb(raw, raw=False)
            except Exception:
                logger.debug("undecodable bin frame len=%d head=%r", len(raw), raw[:40])
                return
        ev = self._last_text_event.get(rid, "?")
        self._dispatch(ev, obj, url)

    # ---------- event dispatcher ----------

    def _dispatch(self, event: str, payload: Any, url: str):
        # Quiet ticks first — highest volume
        if event == "tick":
            self._handle_tick(payload)
            return

        # Auth ACK
        if event == "user.auth.success":
            logger.info("user.auth.success")
            return

        # Assets list
        if event == "common.assets_list":
            self._handle_assets_list(payload)
            return

        # Initial data: balance + user id + login
        if event in ("user.initial_data", "user.real.initial_data", "user.demo.initial_data"):
            self._handle_initial_data(payload)
            return

        # Balance update
        if event.startswith("user.") and event.endswith(".update_balance"):
            self._handle_balance_update(event, payload)
            return

        # Trade open confirmation (our own action)
        if event.endswith(".open_trade.success"):
            logger.info("trade opened: %s", str(payload)[:200])
            if self.on_trade_open:
                try: self.on_trade_open(payload)
                except Exception: logger.exception("on_trade_open")
            return

        # Trade closed → result
        if event.endswith(".close_trade.success"):
            logger.info("trade closed: %s", str(payload)[:200])
            if self.on_trade_close:
                try: self.on_trade_close(payload)
                except Exception: logger.exception("on_trade_close")
            return

        # Subscribe / unsubscribe acks — ignore
        if event in ("subscribed", "unsubscribed", "subscribe", "unsubscribe"):
            return

        logger.debug("unhandled event: %s | %s", event, str(payload)[:150])

    def _handle_tick(self, payload):
        # payload = [symbol, tf, candle_time, O, H, L, C, volume, server_ts]
        if not isinstance(payload, (list, tuple)) or len(payload) < 7:
            return
        symbol, tf, t, o, h, l, c = payload[:7]
        v = payload[7] if len(payload) > 7 else 0
        key = (symbol, tf)
        buf = self._candles[key]
        # Update last candle if time matches, else append
        if buf and buf[-1]["time"] == t:
            last = buf[-1]
            last["high"] = max(last["high"], h)
            last["low"]  = min(last["low"], l)
            last["close"] = c
            last["volume"] = v
        else:
            buf.append({"time": t, "open": o, "high": h, "low": l, "close": c, "volume": v})
        if self.on_tick:
            try: self.on_tick(symbol, tf, buf[-1])
            except Exception: logger.exception("on_tick")

    def _handle_assets_list(self, payload):
        # payload = list of {id, symbol, label, type, digits, payout, max_payout, min_timeframe, max_timeframe, scheduled_until}
        if not isinstance(payload, list):
            return
        assets = {}
        for a in payload:
            if not isinstance(a, dict):
                continue
            sym = a.get("symbol")
            if not sym:
                continue
            assets[sym] = {
                "id": a.get("id"),
                "symbol": sym,
                "label": a.get("label"),
                "type": a.get("type"),
                "digits": a.get("digits"),
                "payout": a.get("payout", 0),
                "max_payout": a.get("max_payout", 0),
                "min_tf": a.get("min_timeframe", 60),
                "max_tf": a.get("max_timeframe", 14400),
                "scheduled_until": a.get("scheduled_until", 0),
                "is_otc": "_otc" in sym,
            }
        self.assets = assets
        logger.info("assets_list updated: %d assets", len(assets))
        if self.on_assets_update:
            try: self.on_assets_update(assets)
            except Exception: logger.exception("on_assets_update")
        # Any further ready check
        self._maybe_ready()

    def _handle_initial_data(self, payload):
        if not isinstance(payload, dict):
            return
        self.user_id = payload.get("userId") or payload.get("uid")
        real = payload.get("real") or {}
        demo = payload.get("demo") or {}
        if real:
            self.balance_real = real.get("balance")
            self.login_real = real.get("login")
        if demo:
            self.balance_demo = demo.get("balance")
            self.login_demo = demo.get("login") or demo.get("uid")
        logger.info("initial_data: uid=%s real_bal=%s demo_bal=%s", self.user_id, self.balance_real, self.balance_demo)
        self._maybe_ready()

    def _handle_balance_update(self, event: str, payload):
        # event: user.real.update_balance | user.demo.update_balance
        if not isinstance(payload, dict):
            return
        val = payload.get("balance")
        if ".real." in event:
            self.balance_real = val
        elif ".demo." in event:
            self.balance_demo = val
        if self.on_balance_update:
            try: self.on_balance_update(event, payload)
            except Exception: logger.exception("on_balance_update")

    def _maybe_ready(self):
        if self.assets and (self.balance_real is not None or self.balance_demo is not None):
            self._ready.set()

    # ---------- public ----------

    def get_candles(self, symbol: str, period: int = 60, limit: int = 1000) -> list[dict]:
        buf = self._candles.get((symbol, period))
        if not buf:
            return []
        return list(buf)[-limit:]

    def balance(self) -> Optional[float]:
        return self.balance_demo if self.mode == "paper" else self.balance_real

    def login(self) -> Optional[int]:
        return self.login_demo if self.mode == "paper" else self.login_real

    # ---------- send socket.io frames via page ----------

    async def subscribe_ticker(self, symbol: str, tf: int = 60):
        """Ask ticker2 WS to start streaming this asset/tf."""
        frame = f'42["subscribe",{{"asset":"{symbol}","tf":{tf}}}]'
        await self._page.evaluate(
            f"""(frame) => {{
                const ws = window.__posigTickWS;
                if (ws && ws.readyState === 1) ws.send(frame);
            }}""", frame)
        logger.debug("subscribe ticker %s tf=%d", symbol, tf)

    async def unsubscribe_ticker(self, symbol: str, tf: int = 60):
        frame = f'42["unsubscribe",{{"asset":"{symbol}","tf":{tf}}}]'
        await self._page.evaluate(
            """(frame) => { const ws = window.__posigTickWS; if (ws && ws.readyState===1) ws.send(frame); }""",
            frame)

    async def send_user_ws(self, event: str, payload: dict):
        """Emit socket.io event on the /po-ws/ connection (user/trading).

        Robustness ladder:
          1. Primary ref __posigUserWS
          2. Scan __posigAllWS for any OPEN /po-ws/
          3. Wait up to 9s for auto-reconnect
          4. Reload page, wait for re-auth, retry send
        """
        frame = "42" + json.dumps([event, payload], ensure_ascii=False)
        script = """(frame) => {
            function findOpenUserWs() {
                if (window.__posigUserWS && window.__posigUserWS.readyState === 1) return window.__posigUserWS;
                const all = window.__posigAllWS || [];
                for (let i = all.length - 1; i >= 0; i--) {
                    const w = all[i];
                    try {
                        if (w && w.url && w.url.includes('/po-ws/') && w.readyState === 1) {
                            window.__posigUserWS = w;
                            return w;
                        }
                    } catch (e) {}
                }
                return null;
            }
            const ws = findOpenUserWs();
            if (!ws) {
                const all = window.__posigAllWS || [];
                const summary = all.map(w => (w && w.url ? w.url.slice(-60) + ':rs=' + w.readyState : '?')).join(' | ');
                throw new Error('user WS not open | hooked=' + !!window.__posigHooked + ' total=' + all.length + ' | ' + summary);
            }
            ws.send(frame);
            return true;
        }"""

        # Stage 0: ensure hook is installed in current page context (no-op if already)
        try:
            await self._page.evaluate(WS_HOOK_SCRIPT)
        except Exception as e:
            logger.debug("pre-send hook re-inject failed: %s", e)

        # Stage 1+2+3: up to 6 retries with 1.5s backoff
        last_err = None
        for attempt in range(6):
            try:
                await self._page.evaluate(script, frame)
                logger.debug("sent WS: %s", event)
                return
            except Exception as e:
                last_err = e
                if "user WS not open" not in str(e):
                    raise
                await asyncio.sleep(1.5)

        # Stage 4: force-reload the page to reopen the WS
        logger.warning("user WS dead after 9s — reloading page to reopen")
        await self._hard_reconnect()
        await self._page.evaluate(script, frame)
        logger.info("sent WS after reconnect: %s", event)

    async def _hard_reconnect(self, timeout: int = 25):
        """Reload page and wait for WS auth to complete. Handles login prompt."""
        self._ready.clear()
        # Re-install the hook before reload
        await self._cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": WS_HOOK_SCRIPT})
        await self._page.reload(wait_until="domcontentloaded", timeout=60000)

        # Maybe the reload lands us on the login page → auto-fill
        try:
            from feed.auth import ensure_logged_in
            if self._auth_cfg:
                ok = await ensure_logged_in(self._page, self._auth_cfg)
                if not ok:
                    logger.error("auto-login failed during hard reconnect")
        except Exception:
            logger.exception("ensure_logged_in error")

        # After login (or if already in app), wait for ready signal
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
            logger.info("hard reconnect succeeded")
        except asyncio.TimeoutError:
            logger.error("hard reconnect timeout after %ds", timeout)
            raise RuntimeError("failed to reconnect to po-signals")

    # ---------- HTTP via page (auth cookies) ----------

    async def http_get_json(self, path: str) -> Any:
        """Fetch JSON from po-signals.com inside the page context (cookies shared)."""
        js = """async (p) => {
            const r = await fetch(p, {credentials:'same-origin'});
            if (!r.ok) return {__error: r.status};
            return await r.json();
        }"""
        return await self._page.evaluate(js, path)
