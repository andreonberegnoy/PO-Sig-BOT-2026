"""Direct Pocket Option feed — no browser, no intermediary.

Connects to wss://api-*.po.market/socket.io/ as a normal WebSocket client,
authenticates with the session cookie (ssid) extracted from the user's browser,
subscribes to tick streams, aggregates ticks into M1 (or other period) OHLC
candles, and exposes the same interface as the old po_feed.py so state_machine
can use either transport.

Trade placement: send `openOrder` event on the same WS.

Exposes:
    feed.assets              dict[symbol] -> asset_info
    feed.balance_demo        float | None
    feed.balance_real        float | None
    feed.balance()           current-mode balance
    feed.get_candles(sym, period, limit)
    feed.subscribe(sym, period)
    feed.send_open_order(asset, amount, action, time_sec)
    feed.on_tick             callback(symbol, period, candle_dict)
    feed.on_assets_update    callback(assets_dict)
    feed.on_trade_open       callback(payload)
    feed.on_trade_close      callback(payload)
"""

import asyncio
import json
import logging
import re
import ssl
import time
from collections import defaultdict, deque
from typing import Any, Callable, Optional

import msgpack
import websockets

from journal.candles_db import CandlesDB

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/147.0.0.0 Safari/537.36"
)


class PoDirectFeed:
    def __init__(
        self,
        ssid: str,
        uid: int,
        is_demo: bool = True,
        ws_url: str = "wss://api-eu.po.market/socket.io/?EIO=4&transport=websocket",
        verify_ssl: bool = True,
        candles_db_path: str = "data/candles.db",
        relogin_callback=None,
        relogin_interval_hours: float = 12.0,
        relogin_safe_check=None,    # callable() -> bool, True = OK to relogin now
    ):
        self.ssid = ssid
        self.uid = int(uid)
        self.is_demo = 1 if is_demo else 0
        self.ws_url = ws_url
        self.verify_ssl = verify_ssl

        # state mirrors po-signals feed so state_machine works identically
        self.assets: dict[str, dict] = {}
        self.balance_demo: Optional[float] = None
        self.balance_real: Optional[float] = None
        self.user_id: Optional[int] = self.uid

        self._candles: dict[tuple, deque] = defaultdict(lambda: deque(maxlen=2000))
        self._subscribed: set[tuple] = set()
        self._candles_db = CandlesDB(candles_db_path)
        self._relogin_callback = relogin_callback   # async () -> {"ssid", "uid", "ws_url"}
        self._relogin_safe_check = relogin_safe_check   # bool — gate relogin on safe state
        self._relogin_interval = float(relogin_interval_hours) * 3600
        self._relogin_in_progress = False
        self._relogin_pending_reason: Optional[str] = None
        self._scheduled_relogin_task: Optional[asyncio.Task] = None
        self._pending_event: Optional[str] = None   # for 451- / binary pair
        self._ws: Optional[websockets.ClientConnection] = None
        self._ready = asyncio.Event()
        self._recv_task: Optional[asyncio.Task] = None
        self._running = False

        # callbacks (set externally)
        self.on_tick: Optional[Callable[[str, int, dict], None]] = None
        self.on_assets_update: Optional[Callable[[dict], None]] = None
        self.on_trade_open: Optional[Callable[[dict], None]] = None
        self.on_trade_close: Optional[Callable[[dict], None]] = None
        self.on_balance_update: Optional[Callable[[dict], None]] = None

    # ---------- connect / disconnect ----------

    async def connect(self):
        ssl_ctx = ssl.create_default_context()
        if not self.verify_ssl:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

        logger.info("connecting %s", self.ws_url)
        self._ws = await websockets.connect(
            self.ws_url,
            ssl=ssl_ctx,
            additional_headers={
                "Origin": "https://pocketoption.com",
                "User-Agent": USER_AGENT,
            },
            max_size=16 * 1024 * 1024,
            ping_interval=None,  # we handle socket.io ping ourselves
        )
        self._running = True
        self._recv_task = asyncio.create_task(self._recv_loop(), name="po_direct_recv")

        # wait for successauth
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=30)
            logger.info("feed ready (assets=%d, balance_demo=%s, balance_real=%s)",
                        len(self.assets), self.balance_demo, self.balance_real)
        except asyncio.TimeoutError:
            logger.warning("feed not fully ready after 30s — continuing; incoming data may still settle")

        # Start scheduled relogin loop (only on first connect, not reconnects)
        if (self._relogin_callback and self._scheduled_relogin_task is None
                and self._relogin_interval > 0):
            self._scheduled_relogin_task = asyncio.create_task(
                self._scheduled_relogin_loop(), name="po_relogin",
            )

    async def close(self):
        self._running = False
        try:
            if self._ws:
                await self._ws.close()
        except Exception:
            pass
        if self._recv_task:
            try: await self._recv_task
            except asyncio.CancelledError: pass
            except Exception: logger.exception("recv_task error")
        if self._scheduled_relogin_task and not self._scheduled_relogin_task.done():
            self._scheduled_relogin_task.cancel()

    # ─── auth refresh ───
    def _is_relogin_safe(self) -> bool:
        """Check via callback if it's safe to relogin right now (e.g., not
        during a martingale cycle). Default: always safe."""
        if not self._relogin_safe_check:
            return True
        try:
            return bool(self._relogin_safe_check())
        except Exception:
            logger.exception("relogin safe-check raised, assuming unsafe")
            return False

    async def _do_relogin(self, reason: str = "manual"):
        """Run the relogin callback (typically Playwright auto_relogin), swap
        ssid in-place, reconnect WebSocket. Idempotent — safe if already running.

        Honours `relogin_safe_check` — if it returns False (e.g. martingale
        cycle in progress), defers and tries again later."""
        if not self._relogin_callback or self._relogin_in_progress:
            return
        if not self._is_relogin_safe():
            self._relogin_pending_reason = reason
            logger.info("relogin deferred (reason=%s) — unsafe state (MG cycle?)", reason)
            return
        self._relogin_in_progress = True
        self._relogin_pending_reason = None
        try:
            logger.info("relogin start (reason=%s)", reason)
            fresh = await self._relogin_callback()
            if not fresh or not fresh.get("ssid"):
                logger.error("relogin failed — no fresh ssid returned")
                return
            self.ssid = fresh["ssid"]
            if fresh.get("uid"):
                self.uid = int(fresh["uid"])
            if fresh.get("ws_url"):
                self.ws_url = fresh["ws_url"]
            if "is_demo" in fresh:
                self.is_demo = 1 if fresh["is_demo"] else 0
            logger.info("relogin: new ssid acquired, reconnecting WS…")

            # Tear down current WS and reopen with fresh credentials
            self._ready.clear()
            try:
                if self._ws:
                    await self._ws.close()
            except Exception:
                pass
            # _recv_loop will exit; start a new one via connect()
            await asyncio.sleep(1.0)
            await self.connect()
            # Re-subscribe to all previously tracked pairs
            old_subs = list(self._subscribed)
            self._subscribed.clear()
            for sym, period in old_subs:
                try: await self.subscribe(sym, period)
                except Exception: logger.exception("re-subscribe %s failed", sym)
            logger.info("relogin complete: %d pairs re-subscribed", len(old_subs))
        finally:
            self._relogin_in_progress = False

    async def _scheduled_relogin_loop(self):
        """Periodic relogin every ~relogin_interval hours (with ±2h jitter so
        timing is not robotically exact). Also retries any deferred relogin
        every 60 seconds while the safe-check is False (e.g. waits out an
        active martingale cycle before refreshing the session)."""
        import random
        while self._running:
            jitter = random.uniform(-7200, 7200)   # ±2h
            wait = max(3600, self._relogin_interval + jitter)
            slept = 0.0
            try:
                # Sleep in 60-sec increments so we can pick up deferred relogins
                while slept < wait and self._running:
                    await asyncio.sleep(60)
                    slept += 60
                    if self._relogin_pending_reason and self._is_relogin_safe():
                        reason = self._relogin_pending_reason
                        logger.info("deferred relogin firing now (reason=%s, safe)", reason)
                        await self._do_relogin(reason=reason)
            except asyncio.CancelledError:
                return
            if self._relogin_callback and not self._relogin_in_progress:
                logger.info("scheduled relogin tick (next in ~%dh ± jitter)",
                            int(self._relogin_interval/3600))
                await self._do_relogin(reason="scheduled")

    # ---------- WS recv loop ----------

    async def _recv_loop(self):
        try:
            async for raw in self._ws:
                try:
                    if isinstance(raw, bytes):
                        self._handle_binary(raw)
                    else:
                        await self._handle_text(raw)
                except Exception:
                    logger.exception("frame handler error")
        except websockets.ConnectionClosed as e:
            logger.warning("WS closed: %s", e)
            # Schedule auto-reconnect (without re-login) — server may have
            # temporarily disconnected. This is separate from session-expiry
            # which is handled via `NotAuthorized` event + `_do_relogin`.
            if self._running:
                asyncio.create_task(self._auto_reconnect_loop())
        finally:
            self._running = False

    async def _auto_reconnect_loop(self, max_attempts: int = 10):
        """Reconnect with exponential backoff after a plain WS disconnect.
        Re-uses the existing ssid (no relogin)."""
        import random
        for attempt in range(1, max_attempts + 1):
            wait = min(60, 2 ** attempt + random.uniform(0, 1))
            logger.info("WS auto-reconnect attempt %d in %.1fs", attempt, wait)
            await asyncio.sleep(wait)
            try:
                self._ready.clear()
                self._running = True
                await self.connect()
                # Re-subscribe to all previously tracked pairs
                old_subs = list(self._subscribed)
                self._subscribed.clear()
                for sym, period in old_subs:
                    try: await self.subscribe(sym, period)
                    except Exception: logger.exception("re-subscribe %s failed", sym)
                logger.info("WS auto-reconnect successful (attempt %d, %d pairs)",
                            attempt, len(old_subs))
                return
            except Exception:
                logger.exception("auto-reconnect attempt %d failed", attempt)
        logger.error("auto-reconnect gave up after %d attempts — triggering relogin", max_attempts)
        if self._relogin_callback:
            asyncio.create_task(self._do_relogin(reason="reconnect_exhausted"))

    async def _handle_text(self, raw: str):
        if not raw:
            return
        if raw.startswith("0{"):
            # Engine.IO open → ACK by sending "40" to connect namespace
            await self._ws.send("40")
        elif raw.startswith("40"):
            # Socket.IO CONNECT ack — send auth now
            auth_payload = {
                "session": self.ssid,
                "isDemo": self.is_demo,
                "uid": self.uid,
                "platform": 1,
                "isFastHistory": True,
                "isOptimized": True,
            }
            await self._ws.send('42["auth",' + json.dumps(auth_payload) + ']')
            logger.debug("sent auth frame (isDemo=%d, uid=%d)", self.is_demo, self.uid)
        elif raw == "2":
            await self._ws.send("3")    # engine.io ping → pong
        elif raw == "3":
            pass
        elif raw.startswith("42"):
            # text event with inline JSON payload
            br = raw.find("[")
            if br < 0:
                return
            try:
                body = json.loads(raw[br:])
            except json.JSONDecodeError:
                return
            if isinstance(body, list) and body:
                ev = body[0]
                payload = body[1] if len(body) > 1 else None
                self._dispatch(ev, payload)
        elif raw.startswith("451"):
            # binary-event marker — next WS frame is msgpack bytes for this event
            m = re.search(r'451-\["([^"]+)"', raw)
            if m:
                self._pending_event = m.group(1)
        # ignore other framings (upgrade, 6 noop, etc)

    def _handle_binary(self, raw: bytes):
        # Try msgpack first (PO default), JSON fallback
        obj = None
        try:
            obj = msgpack.unpackb(raw, raw=False)
        except Exception:
            try:
                obj = json.loads(raw.decode("utf-8"))
            except Exception:
                logger.debug("undecodable binary frame len=%d", len(raw))
                return
        ev = self._pending_event or "?"
        self._pending_event = None
        self._dispatch(ev, obj)

    # ---------- event dispatcher ----------

    def _dispatch(self, event: str, payload: Any):
        try:
            if event == "successauth":
                logger.info("authenticated: %s", payload)
                self._ready.set()
                return

            if event == "NotAuthorized":
                logger.warning("session NotAuthorized — triggering relogin")
                if self._relogin_callback and not self._relogin_in_progress:
                    asyncio.create_task(self._do_relogin(reason="NotAuthorized"))
                return

            if event == "updateAssets":
                self._handle_assets(payload)
                return

            if event == "successupdateBalance":
                self._handle_balance(payload)
                return

            if event == "updateStream":
                self._handle_stream(payload)
                return

            if event == "updateHistoryNewFast":
                self._handle_history(payload)
                return

            if event in ("loadHistoryPeriodFast", "loadHistoryPeriod", "updateHistoryNew"):
                self._handle_history_period(payload)
                return

            if event in ("successopenOrder", "openOrder"):
                logger.info("trade opened: %s", str(payload)[:200])
                if self.on_trade_open:
                    try: self.on_trade_open(payload)
                    except Exception: logger.exception("on_trade_open")
                return

            if event in ("successcloseOrder", "closeOrder", "updateClosedDeals"):
                # closeOrder has single trade; updateClosedDeals has list
                logger.info("trade closed event=%s payload=%s", event, str(payload)[:200])
                if self.on_trade_close:
                    try: self.on_trade_close(payload)
                    except Exception: logger.exception("on_trade_close")
                return

            # silent handled (not interesting):
            if event in ("successupdatePending", "successupdateOpenedExpresses",
                         "updateOpenedDeals", "updateCharts"):
                return

            # Log anything else at INFO for diagnosis (PO event catalog varies)
            logger.info("EVENT %s | %s", event, str(payload)[:200])
        except Exception:
            logger.exception("dispatch error for event=%s", event)

    # ---------- specific handlers ----------

    def _handle_assets(self, payload):
        # Schema we observed: each record is
        # [id, symbol, label, type, precision, payout, ?, ?, ?, is_otc_flag,
        #  ?, ?, ?, schedule_ts, open_for_trading, [timeframes], ?, ?, ?]
        if not isinstance(payload, list):
            return
        count = 0
        for rec in payload:
            if not isinstance(rec, list) or len(rec) < 15:
                continue
            try:
                asset_id = rec[0]; sym = rec[1]; label = rec[2]; typ = rec[3]
                payout = int(rec[5]) if rec[5] is not None else 0
                open_flag = bool(rec[14])
                self.assets[sym] = {
                    "id": asset_id, "symbol": sym, "label": label, "type": typ,
                    "payout": payout, "max_payout": payout,
                    "is_otc": sym.endswith("_otc"),
                    "open": open_flag,
                }
                count += 1
            except Exception:
                continue
        logger.info("updateAssets: %d assets parsed", count)
        if self.on_assets_update:
            try: self.on_assets_update(self.assets)
            except Exception: logger.exception("on_assets_update")

    def _handle_balance(self, payload):
        if not isinstance(payload, dict):
            return
        bal = payload.get("balance")
        is_demo = payload.get("isDemo", 0)
        if bal is not None:
            if is_demo:
                self.balance_demo = float(bal)
            else:
                self.balance_real = float(bal)
        if self.on_balance_update:
            try: self.on_balance_update(payload)
            except Exception: logger.exception("on_balance_update")

    def _handle_stream(self, payload):
        # [[symbol, timestamp, price], ...]
        if not isinstance(payload, list):
            return
        for tick in payload:
            if not isinstance(tick, (list, tuple)) or len(tick) < 3:
                continue
            try:
                sym = tick[0]; ts = float(tick[1]); price = float(tick[2])
            except Exception:
                continue
            # aggregate into M1 by default; also update any other subscribed periods
            periods = {p for (s, p) in self._subscribed if s == sym}
            if not periods:
                periods = {60}
            for period in periods:
                self._apply_tick(sym, ts, price, period)

    def _handle_history(self, payload):
        # {asset, period, history: [[timestamp, price], ...]}
        if not isinstance(payload, dict):
            return
        sym = payload.get("asset")
        period = int(payload.get("period") or 60)
        history = payload.get("history") or []
        if not sym:
            return
        for item in history:
            if not item or len(item) < 2:
                continue
            try:
                ts = float(item[0]); price = float(item[1])
            except Exception:
                continue
            self._apply_tick(sym, ts, price, period)
        logger.info("history %s period=%d: %d ticks → %d candles",
                    sym, period, len(history), len(self._candles.get((sym, period), [])))

    def _apply_tick(self, symbol: str, ts: float, price: float, period: int = 60):
        key = (symbol, period)
        buf = self._candles[key]
        bar_time = int(ts // period) * period
        bar_rolled_over = False
        if buf and int(buf[-1]["time"]) == bar_time:
            c = buf[-1]
            if price > c["high"]: c["high"] = price
            if price < c["low"]:  c["low"] = price
            c["close"] = price
        else:
            # new bar — previous bar (buf[-1]) just closed; persist it to DB
            if buf:
                try: self._candles_db.save(symbol, period, buf[-1], is_demo=bool(self.is_demo))
                except Exception: logger.exception("candles_db.save failed")
                bar_rolled_over = True
            buf.append({
                "time": bar_time,
                "open": price, "high": price, "low": price, "close": price,
                "volume": 0,
            })
        if self.on_tick:
            try: self.on_tick(symbol, period, buf[-1])
            except Exception: logger.exception("on_tick")

    # ---------- public API used by state_machine ----------

    def balance(self) -> Optional[float]:
        return self.balance_demo if self.is_demo else self.balance_real

    def login(self) -> Optional[int]:
        return self.uid

    def get_candles(self, symbol: str, period: int = 60, limit: int = 1000) -> list[dict]:
        buf = self._candles.get((symbol, period))
        if not buf:
            return []
        return list(buf)[-limit:]

    async def subscribe(self, symbol: str, period: int = 60, history_limit: int = 1060):
        key = (symbol, period)
        if key in self._subscribed:
            return
        # Pre-fill buffer from local SQLite (persistent history across restarts).
        # Filter by is_demo so demo and real bars never mix.
        try:
            cached = self._candles_db.load(symbol, period, limit=history_limit,
                                           is_demo=bool(self.is_demo))
            if cached:
                self._candles[key].extend(cached)
                logger.info("subscribe %s P%d: loaded %d cached candles from db (demo=%s)",
                            symbol, period, len(cached), bool(self.is_demo))
        except Exception:
            logger.exception("candles_db.load failed for %s", symbol)

        # Subscribe to live stream
        await self._ws.send('42' + json.dumps([
            "changeSymbol", {"asset": symbol, "period": period}
        ]))
        self._subscribed.add(key)
        logger.info("subscribed %s period=%d", symbol, period)

        # Fill gap: if cached last bar is older than now-period, fetch the
        # missing range. This avoids holes in the chart when bot was offline.
        now = int(time.time())
        last_cached_t = int(self._candles[key][-1]["time"]) if self._candles[key] else 0
        gap_sec = now - (last_cached_t + period) if last_cached_t else 0
        if gap_sec > period * 2:
            # We have a hole between last_cached_t and now — fetch.
            logger.info("subscribe %s P%d: gap of %ds in cache, filling…", symbol, period, gap_sec)
            # Request multiple pages until we cover the gap (newest first).
            anchor_ts = now
            for _ in range(15):
                await self._request_history_period(symbol, period, 200, end_ts=anchor_ts)
                await asyncio.sleep(1.0)
                # Stop once the gap is filled (new bars connect to last_cached_t).
                buf = self._candles[key]
                if buf and int(buf[-1]["time"]) >= now - period * 3:
                    # last bar reaches close to "now" — find where new bars start
                    # if newest bar reaches close to now AND we have density, we're good
                    break
                anchor_ts -= 200 * period

        # Request more history if buffer still short (cold start case).
        # Bail early if successive requests don't yield new bars (PO depleted).
        prev_have = -1
        stuck_iters = 0
        for _ in range(10):
            have = len(self._candles[key])
            if have >= history_limit:
                break
            if have == prev_have:
                stuck_iters += 1
                if stuck_iters >= 2:
                    logger.info("history %s P%d: no progress (%d bars), giving up",
                                symbol, period, have)
                    break
            else:
                stuck_iters = 0
            prev_have = have
            oldest_time = int(self._candles[key][0]["time"]) if have else int(time.time())
            await self._request_history_period(symbol, period, history_limit - have, end_ts=oldest_time)
            await asyncio.sleep(1.0)

    async def _request_history_period(
        self, symbol: str, period: int, count: int, end_ts: Optional[int] = None,
    ):
        """Ask PO for `count` historical OHLC bars ending at `end_ts` (default now)."""
        try:
            if end_ts is None:
                end_ts = int(time.time())
            payload = {
                "asset": symbol,
                "period": int(period),
                "time": int(end_ts),
                "offset": int(max(count, 200) * period),
                "index": 0,
            }
            await self._ws.send('42' + json.dumps(["loadHistoryPeriod", payload]))
            logger.debug("requested history: %s P%d end=%d count=%d",
                         symbol, period, end_ts, count)
        except Exception:
            logger.exception("loadHistoryPeriod send failed")

    def _handle_history_period(self, payload):
        """PO response with OHLC bars (format may vary by region)."""
        if not isinstance(payload, dict):
            return
        sym = payload.get("asset")
        period = int(payload.get("period") or 60)
        data = payload.get("data") or payload.get("candles") or payload.get("history") or []
        if not sym or not data:
            return
        # `data` is typically list of [time, open, close, high, low] or
        # list of dicts {time, open, high, low, close}. Normalize.
        bars = []
        for item in data:
            c = None
            if isinstance(item, dict):
                c = {
                    "time": int(item.get("time", 0)),
                    "open": float(item.get("open", 0)),
                    "high": float(item.get("high", 0)),
                    "low": float(item.get("low", 0)),
                    "close": float(item.get("close", 0)),
                    "volume": float(item.get("volume", 0) or 0),
                }
            elif isinstance(item, (list, tuple)):
                # two common orderings in wild: [t,o,c,h,l] or [t,o,h,l,c]
                if len(item) >= 5:
                    t = int(item[0])
                    a, b, d, e = float(item[1]), float(item[2]), float(item[3]), float(item[4])
                    # guess: if b >= d and b >= e (i.e. high >= close, high >= low) treat as [t,o,h,l,c]
                    # else treat as [t,o,c,h,l]
                    # safer: just take max/min of all 4 for high/low, use item[1] as open, item[-1] as close
                    hi = max(a, b, d, e); lo = min(a, b, d, e)
                    c = {"time": t, "open": a, "high": hi, "low": lo, "close": e, "volume": 0}
            if c and c["time"]:
                bars.append(c)
        if not bars:
            return
        bars.sort(key=lambda x: x["time"])
        key = (sym, period)
        # Merge with existing buffer, dedupe by time
        existing = {int(c["time"]): c for c in self._candles[key]}
        for b in bars:
            existing[int(b["time"])] = b
        merged = sorted(existing.values(), key=lambda c: c["time"])
        self._candles[key].clear()
        self._candles[key].extend(merged)
        # Persist to DB
        try: self._candles_db.save_many(sym, period, bars, is_demo=bool(self.is_demo))
        except Exception: logger.exception("candles_db.save_many failed")
        logger.info("history %s P%d: +%d bars → total %d in buffer",
                    sym, period, len(bars), len(self._candles[key]))

    async def unsubscribe(self, symbol: str, period: int = 60):
        key = (symbol, period)
        if key not in self._subscribed:
            return
        await self._ws.send('42' + json.dumps([
            "unsubscribe", {"asset": symbol, "period": period}
        ]))
        self._subscribed.discard(key)

    async def send_open_order(
        self, asset: str, amount: float, action: str, time_sec: int,
    ) -> None:
        """Send openOrder event. action = 'call' (buy) | 'put' (sell)."""
        frame = [
            "openOrder",
            {
                "asset": asset,
                "amount": float(amount),
                "action": action,
                "isDemo": self.is_demo,
                "requestId": int(time.time() * 1000),
                "optionType": 100,  # standard binary option
                "time": int(time_sec),
            },
        ]
        await self._ws.send('42' + json.dumps(frame))
        logger.info("OPEN %s %s %s $%s exp=%ss",
                    "demo" if self.is_demo else "real",
                    asset, action.upper(), amount, time_sec)
