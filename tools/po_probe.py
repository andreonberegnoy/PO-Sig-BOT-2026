"""Minimal probe: connect to Pocket Option WS directly, authenticate, subscribe
to one asset's M1 candles, print a few ticks. Confirms the API works with our
session before we rewrite the whole bot.

Run:
    python3 tools/po_probe.py

Reads PO_SSID / PO_UID / PO_WS_URL from .env or environment.
"""

import asyncio
import json
import os
import re
import ssl
import sys

try:
    import websockets
    import msgpack
except ImportError:
    print("Missing dep: pip3 install websockets msgpack")
    sys.exit(1)


def load_env():
    env = {}
    try:
        with open(".env", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    # OS env takes priority
    for k in ("PO_SSID", "PO_UID", "PO_WS_URL"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


async def main():
    env = load_env()
    ssid = env.get("PO_SSID")
    uid = int(env.get("PO_UID", "0") or 0)
    ws_url = env.get("PO_WS_URL") or "wss://api-eu.po.market/socket.io/?EIO=4&transport=websocket"

    if not ssid or not uid:
        print("ERROR: PO_SSID and PO_UID must be set in .env")
        return

    print(f"Connecting: {ws_url}")
    ssl_ctx = ssl.create_default_context()
    # macOS-Python often has cert chain issues; this is a probe script, OK to skip
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    # PO checks Origin header — spoof a real browser
    headers = {
        "Origin": "https://pocketoption.com",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/147.0.0.0 Safari/537.36",
    }

    async with websockets.connect(
        ws_url, ssl=ssl_ctx, additional_headers=headers,
        max_size=10 * 1024 * 1024,
    ) as ws:
        print("✓ connected")

        tick_count = 0
        candle_asset = "EURUSD_otc"   # start with one pair

        async def sender():
            # Wait for "0{sid...}" then send "40" to open socket.io namespace
            # Actually server sends 0 first; wait for it before auth.
            pass

        pending_event = None   # event name from preceding "451-" frame

        async def recv_loop():
            nonlocal tick_count, pending_event
            async for raw in ws:
                if isinstance(raw, bytes):
                    # This is the binary payload for the preceding 451- event.
                    try:
                        obj = msgpack.unpackb(raw, raw=False)
                    except Exception:
                        try:
                            obj = json.loads(raw.decode("utf-8"))
                        except Exception:
                            print(f"[BIN undecodable] {len(raw)} bytes head={raw[:20]!r}")
                            continue
                    ev = pending_event or "?"
                    preview = json.dumps(obj, default=str)[:400] if obj is not None else ""
                    print(f"[BIN {ev}] {preview}")
                    pending_event = None
                    # After successful auth, subscribe to one symbol to see tick format
                    if ev == "successauth":
                        print(f"   → subscribing to {candle_asset}")
                        await ws.send('42' + json.dumps([
                            "changeSymbol",
                            {"asset": candle_asset, "period": 60},
                        ]))
                        # Also try loadHistoryPeriod to see if it responds with OHLC
                        import time as _t
                        await ws.send('42' + json.dumps([
                            "loadHistoryPeriod",
                            {
                                "asset": candle_asset,
                                "period": 60,
                                "time": int(_t.time()),
                                "index": 0,
                                "offset": 60 * 1000,   # 1000 minutes back
                            },
                        ]))
                        print(f"   → sent loadHistoryPeriod ({candle_asset}, offset 1000 min)")
                    if ev in ("updateStream", "loadHistoryPeriod"):
                        tick_count += 1
                    continue
                # Engine.IO / Socket.IO framing
                if raw.startswith("0{"):
                    print(f"[0  open] {raw[:120]} → sending 40")
                    await ws.send("40")
                elif raw.startswith("40"):
                    print(f"[40 connect ack] {raw[:60]} → sending auth")
                    auth_payload = {
                        "session": ssid,
                        "isDemo": 0,
                        "uid": uid,
                        "platform": 1,
                        "isFastHistory": True,
                        "isOptimized": True,
                    }
                    await ws.send('42["auth",' + json.dumps(auth_payload) + ']')
                elif raw == "2":
                    await ws.send("3")
                elif raw == "3":
                    pass
                elif raw.startswith("42"):
                    body_start = raw.find("[")
                    if body_start > 0:
                        try:
                            body = json.loads(raw[body_start:])
                            ev = body[0] if body else "?"
                            payload = body[1] if len(body) > 1 else None
                            preview = json.dumps(payload, default=str)[:300] if payload else ""
                            print(f"[42 {ev}] {preview}")
                        except Exception as e:
                            print(f"[42 parse err] {e}")
                elif raw.startswith("451"):
                    # "451-[\"event\",{...placeholder...}]" → next frame is binary
                    m = re.search(r'451-\["([^"]+)"', raw)
                    if m:
                        pending_event = m.group(1)
                    else:
                        print(f"[451 ???] {raw[:80]}")
                else:
                    print(f"[?] {raw[:120]}")
                if tick_count >= 30:
                    print(f"   ✓ got {tick_count} tick events — probe OK")
                    return

        try:
            await asyncio.wait_for(recv_loop(), timeout=60)
        except asyncio.TimeoutError:
            print(f"\ntimeout after 60s. ticks received: {tick_count}")


if __name__ == "__main__":
    asyncio.run(main())
