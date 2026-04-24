"""Trade client — opens trades via the authorized /po-ws/ socket.io connection
that the browser already holds. No DOM clicks, no new auth.

Events:
  send   user.{real|demo}.open_trade  {asset, amount, action, time, login}
  recv   user.{real|demo}.open_trade.success  → trade record with id
  recv   user.{real|demo}.close_trade.success → {tradeId, result, profit, ...}

Paper mode uses demo events; real mode uses real.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional, Callable

logger = logging.getLogger(__name__)


@dataclass
class OpenedTrade:
    trade_id: str
    asset: str
    action: str         # "call" | "put"
    amount: float
    payout: int
    open_time: int
    expiry_sec: int


@dataclass
class ClosedTrade:
    trade_id: str
    asset: str
    action: str
    amount: float
    profit: float
    result: str         # "WIN" | "LOSS" | "DRAW"
    close_time: Optional[int] = None
    raw: dict = field(default_factory=dict)


class TradeClient:
    def __init__(self, feed, mode: str = "paper"):
        self.feed = feed
        self.mode = mode
        self.acct = "demo" if mode == "paper" else "real"

        # Pending trades: trade_id → info
        self._pending: dict[str, OpenedTrade] = {}
        # Callbacks
        self.on_opened: Optional[Callable[[OpenedTrade], None]] = None
        self.on_closed: Optional[Callable[[ClosedTrade], None]] = None

        # Hook feed callbacks
        feed.on_trade_open = self._handle_open
        feed.on_trade_close = self._handle_close

        # For awaitable open result
        self._open_waiters: dict[str, asyncio.Future] = {}
        # For awaitable close result: trade_id → future
        self._close_waiters: dict[str, asyncio.Future] = {}

    # ---------- outgoing ----------

    async def open_trade(
        self,
        asset: str,
        amount: float,
        action: str,         # "call" = BUY, "put" = SELL
        time_sec: int,
    ) -> Optional[OpenedTrade]:
        """Send open_trade frame. Returns synthetic OpenedTrade on success; result
        (WIN/LOSS) is resolved later via balance delta in state_machine."""
        import time as _time, uuid
        event = f"user.{self.acct}.open_trade"
        payload = {
            "asset": asset,
            "amount": float(amount),
            "action": action,
            "time": int(time_sec),
        }

        logger.info("OPEN %s %s %s $%s exp=%ss", self.mode.upper(), asset, action.upper(), amount, time_sec)
        try:
            await self.feed.send_user_ws(event, payload)
        except Exception as e:
            logger.exception("send open_trade failed: %s", e)
            return None

        payout = int((self.feed.assets.get(asset) or {}).get("payout", 0))
        return OpenedTrade(
            trade_id=f"syn-{uuid.uuid4().hex[:12]}",
            asset=asset,
            action=action,
            amount=float(amount),
            payout=payout,
            open_time=int(_time.time()),
            expiry_sec=int(time_sec),
        )

    async def wait_for_close(self, trade_id: str, timeout: float) -> Optional[ClosedTrade]:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._close_waiters[trade_id] = fut
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("close timeout for %s", trade_id)
            self._close_waiters.pop(trade_id, None)
            return None

    # ---------- feed callbacks ----------

    def _handle_open(self, payload):
        # payload example (decoded msgpack/json):
        # { "id": "uuid", "asset": "USDPKR_otc", "action": "call", "amount": 1,
        #   "profit": 0.92, "payout": 92, "openTimestamp": ..., "time": 60, ... }
        if not isinstance(payload, dict):
            return
        trade_id = payload.get("id") or payload.get("tradeId")
        asset = payload.get("asset", "")
        action = payload.get("action", "")
        amount = float(payload.get("amount", 0))
        payout = int(payload.get("payout", 0))
        open_ts = int(payload.get("openTimestamp") or payload.get("openTime") or 0)
        exp = int(payload.get("time") or 0)
        trade = OpenedTrade(trade_id, asset, action, amount, payout, open_ts, exp)
        self._pending[trade_id] = trade

        # Resolve any waiting future
        key = f"{asset}:{amount}:{action}:{exp}"
        fut = self._open_waiters.pop(key, None)
        if fut and not fut.done():
            fut.set_result(trade)

        if self.on_opened:
            try: self.on_opened(trade)
            except Exception: logger.exception("on_opened")

    def _handle_close(self, payload):
        # payload example:
        # { "tradeId": "...", "result": "WIN" | "LOSS", "profit": X, "amount": Y, ... }
        if not isinstance(payload, dict):
            return
        trade_id = payload.get("tradeId") or payload.get("id")
        opened = self._pending.pop(trade_id, None)
        result = (payload.get("result") or "").upper()
        profit = float(payload.get("profit", 0))
        amount = float(payload.get("amount", opened.amount if opened else 0))
        closed = ClosedTrade(
            trade_id=trade_id,
            asset=payload.get("asset") or (opened.asset if opened else ""),
            action=payload.get("action") or (opened.action if opened else ""),
            amount=amount,
            profit=profit,
            result=result,
            close_time=int(payload.get("closeTimestamp") or payload.get("closeTime") or 0) or None,
            raw=payload,
        )

        fut = self._close_waiters.pop(trade_id, None)
        if fut and not fut.done():
            fut.set_result(closed)

        if self.on_closed:
            try: self.on_closed(closed)
            except Exception: logger.exception("on_closed")
