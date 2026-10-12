"""
Real-time mid-price streaming via the Polymarket CLOB WebSocket market channel.

Design:
  - Connects to wss://ws-subscriptions-clob.polymarket.com/ws/market (public, no auth)
  - Subscribes to price_change / best_bid_ask / book events for active tokens
  - Maintains an in-memory {token_id: mid} cache read by the quoting cycle
  - Fires an event-driven callback when mid moves >= 1 tick from the *order center*
    (order-relative trigger — avoids oscillation churn)
  - Velocity guard: widens on 3+ tick directional moves in 5s
  - Auto-reconnect with exponential backoff on disconnect
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict, deque
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from loguru import logger

WS_ENDPOINT = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_INTERVAL = 10
STALENESS_SECONDS = 60
VELOCITY_WINDOW_SECONDS = 5.0
VELOCITY_TICK_THRESHOLD = 3
MAX_RECONNECT_DELAY = 30


class WebSocketFeed:
    """Streams real-time mid prices from the Polymarket CLOB market channel."""

    def __init__(
        self,
        on_mid_change: Callable[[str, float, bool], Awaitable[None]],
        on_last_trade: Optional[Callable[[str, float, float], Awaitable[None]]] = None,
        on_bbo: Optional[Callable[[str, float, float], Awaitable[None]]] = None,
    ) -> None:
        self._on_mid_change = on_mid_change
        self._on_last_trade = on_last_trade
        self._on_bbo = on_bbo
        self._mid_prices: Dict[str, float] = {}
        self._last_update: Dict[str, float] = {}
        self._order_centers: Dict[str, float] = {}
        self._tick_sizes: Dict[str, float] = {}
        self._recent_moves: Dict[str, deque] = defaultdict(lambda: deque(maxlen=50))
        self._token_ids: List[str] = []
        self._connected: bool = False
        self._ws = None
        self._subscribe_event = asyncio.Event()

    # ------------------------------------------------------------------
    # Public read API (called by quoting cycle — instant, no I/O)
    # ------------------------------------------------------------------

    def get_mid(self, token_id: str) -> Optional[float]:
        """Return latest mid for *token_id*, or None if stale/unknown."""
        ts = self._last_update.get(token_id, 0)
        if time.monotonic() - ts > STALENESS_SECONDS:
            return None
        return self._mid_prices.get(token_id)

    @property
    def connected(self) -> bool:
        return self._connected

    def update_order_center(self, token_id: str, mid: float) -> None:
        """Called after a successful hot-requote to update the trigger baseline."""
        self._order_centers[token_id] = mid

    # ------------------------------------------------------------------
    # Subscription management
    # ------------------------------------------------------------------

    async def subscribe(
        self,
        token_ids: List[str],
        tick_sizes: Dict[str, float],
    ) -> None:
        """Set the active subscription list. Sends (re)subscribe if connected."""
        self._token_ids = list(token_ids)
        self._tick_sizes.update(tick_sizes)
        if self._ws and self._connected:
            await self._send_subscribe()
        self._subscribe_event.set()

    async def _send_subscribe(self) -> None:
        if not self._token_ids or not self._ws:
            return
        msg = json.dumps({
            "assets_ids": self._token_ids,
            "type": "market",
            "custom_feature_enabled": True,
        })
        try:
            await self._ws.send(msg)
            logger.info("WS subscribed to {} tokens", len(self._token_ids))
        except Exception as exc:
            logger.warning("WS subscribe send failed: {}", exc)

    # ------------------------------------------------------------------
    # Background task: connect + process + reconnect
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        """Main background loop — connect, process messages, auto-reconnect."""
        try:
            import websockets  # noqa: F811
        except ImportError:
            logger.error("websockets package not installed — WS feed disabled")
            return

        delay = 1
        while True:
            try:
                async with websockets.connect(
                    WS_ENDPOINT,
                    ping_interval=PING_INTERVAL,
                    ping_timeout=20,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    delay = 1
                    logger.info("WS connected to {}", WS_ENDPOINT)

                    if self._token_ids:
                        await self._send_subscribe()
                    else:
                        logger.debug("WS connected but no tokens to subscribe yet — waiting")
                        await self._subscribe_event.wait()
                        await self._send_subscribe()

                    async for raw_msg in ws:
                        try:
                            msg = json.loads(raw_msg)
                            await self._handle_message(msg)
                        except json.JSONDecodeError:
                            pass
                        except Exception as exc:
                            logger.debug("WS message handling error: {}", exc)

            except asyncio.CancelledError:
                logger.info("WS feed task cancelled — shutting down")
                return
            except Exception as exc:
                self._connected = False
                self._ws = None
                logger.warning("WS disconnected ({}), reconnecting in {}s", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, MAX_RECONNECT_DELAY)

    # ------------------------------------------------------------------
    # Message processing
    # ------------------------------------------------------------------

    async def _handle_message(self, msg: dict) -> None:
        event_type = msg.get("event_type", "")

        if event_type == "book":
            await self._handle_book(msg)
        elif event_type == "price_change":
            await self._handle_price_change(msg)
        elif event_type == "best_bid_ask":
            await self._handle_best_bid_ask(msg)
        elif event_type == "last_trade_price":
            await self._handle_last_trade(msg)

    async def _handle_book(self, msg: dict) -> None:
        """Full orderbook snapshot — extract top-of-book mid."""
        asset_id = msg.get("asset_id", "")
        bids = msg.get("bids") or []
        asks = msg.get("asks") or []
        if not asset_id or (not bids and not asks):
            return
        best_bid = max((float(b["price"]) for b in bids), default=0)
        best_ask = min((float(a["price"]) for a in asks), default=0)
        if best_bid > 0 and best_ask > 0:
            mid = (best_bid + best_ask) / 2.0
            await self._process_mid_update(asset_id, mid)

    async def _handle_price_change(self, msg: dict) -> None:
        """Price level change — extract best_bid/best_ask from each entry."""
        changes = msg.get("price_changes") or []
        for entry in changes:
            asset_id = entry.get("asset_id", "")
            bb = entry.get("best_bid")
            ba = entry.get("best_ask")
            if not asset_id or bb is None or ba is None:
                continue
            best_bid = float(bb)
            best_ask = float(ba)
            if best_bid > 0 and best_ask > 0 and best_ask > best_bid:
                mid = (best_bid + best_ask) / 2.0
                await self._process_mid_update(asset_id, mid)

    async def _handle_best_bid_ask(self, msg: dict) -> None:
        """best_bid_ask event (requires custom_feature_enabled)."""
        asset_id = msg.get("asset_id", "")
        bb = msg.get("best_bid")
        ba = msg.get("best_ask")
        if not asset_id or bb is None or ba is None:
            return
        best_bid = float(bb)
        best_ask = float(ba)
        if best_bid > 0 and best_ask > 0 and best_ask > best_bid:
            mid = (best_bid + best_ask) / 2.0
            await self._process_mid_update(asset_id, mid)
        if self._on_bbo is not None and best_bid > 0 and best_ask > 0:
            try:
                await self._on_bbo(asset_id, best_bid, best_ask)
            except Exception as exc:
                logger.debug("on_bbo callback error: {}", exc)

    async def _handle_last_trade(self, msg: dict) -> None:
        """last_trade_price is a print, not BBO mid.

        Ignored for the mid cache and the staleness clock. A last-sale
        must not make a stale book look fresh or recenter quotes.
        Optional on_last_trade is for the DRY_RUN paper matcher only.
        """
        asset_id = str(msg.get("asset_id") or msg.get("token_id") or "")
        if not asset_id or self._on_last_trade is None:
            return
        try:
            price = float(msg.get("price") or 0)
        except (TypeError, ValueError):
            return
        if price <= 0:
            return
        try:
            size = float(msg.get("size") or msg.get("amount") or 0)
        except (TypeError, ValueError):
            size = 0.0
        try:
            await self._on_last_trade(asset_id, price, size)
        except Exception as exc:
            logger.debug("on_last_trade callback error: {}", exc)

    # ------------------------------------------------------------------
    # Core: mid update + order-relative trigger + velocity guard
    # ------------------------------------------------------------------

    async def _process_mid_update(self, token_id: str, new_mid: float) -> None:
        """Update cache, check trigger, fire callback if appropriate."""
        now = time.monotonic()
        self._mid_prices[token_id] = new_mid
        self._last_update[token_id] = now

        order_mid = self._order_centers.get(token_id)
        if order_mid is None:
            return

        tick = self._tick_sizes.get(token_id, 0.01)
        if abs(new_mid - order_mid) < tick - 1e-9:
            return

        # Velocity guard: check for fast directional movement
        moves = self._recent_moves[token_id]
        moves.append((now, new_mid))
        is_fast = self._check_velocity(moves, tick, now)

        try:
            await self._on_mid_change(token_id, new_mid, is_fast)
        except Exception as exc:
            logger.debug("hot_requote callback error: {}", exc)

    def _check_velocity(
        self, moves: deque, tick: float, now: float,
    ) -> bool:
        """Return True if net directional movement >= VELOCITY_TICK_THRESHOLD ticks in 5s."""
        while moves and now - moves[0][0] > VELOCITY_WINDOW_SECONDS:
            moves.popleft()
        if len(moves) < 2:
            return False
        net_move = moves[-1][1] - moves[0][1]
        return abs(net_move) >= VELOCITY_TICK_THRESHOLD * tick
