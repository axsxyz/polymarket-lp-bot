"""
Order lifecycle manager — cancel stale quotes, post fresh ones.

Each rebalance cycle:
  1. For each active market, cancel existing orders for that market.
  2. Post the new bid + ask from the Quoter.
  Fill detection is handled separately by FillCollector (core/fill_collector.py)
  via the CLOB trades API — not by order-set differencing.

Design notes:
  - We cancel-then-replace rather than amend because the CLOB API has no
    native amend-in-place.  The gap between cancel and new-post is typically
    < 1 second — acceptable for LP farming on long-dated markets.
  - DRY_RUN mode logs the orders that *would* be posted without touching
    the exchange.
  - A circuit breaker per market pauses quoting after repeated failures.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

from loguru import logger
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

from config.settings import Settings
from core.client import AsyncClobClient
from core.quoter import Quote, TwoSidedQuotes

CIRCUIT_BREAKER_MAX_FAILURES = 3
CIRCUIT_BREAKER_COOLDOWN_SECONDS = 300


@dataclass
class OrderRecord:
    """Local bookkeeping for an order we posted."""

    order_id: str
    token_id: str
    condition_id: str
    side: str
    price: float
    size: float
    posted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class FillEvent:
    """Detected fill (partial or full) between cycles."""

    token_id: str
    condition_id: str
    side: str
    price: float
    filled_size: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class OrderManager:
    """Manages the full order lifecycle for all active markets."""

    def __init__(self, client: AsyncClobClient, settings: Settings) -> None:
        self._client = client
        self._s = settings
        # order_id → OrderRecord
        self._live_orders: Dict[str, OrderRecord] = {}
        # condition_id → consecutive failure count
        self._failure_counts: Dict[str, int] = defaultdict(int)
        # condition_id → earliest time we can retry
        self._cooldown_until: Dict[str, datetime] = {}
        # fill history (bounded ring buffer via list slicing)
        self._recent_fills: List[FillEvent] = []

    @property
    def recent_fills(self) -> List[FillEvent]:
        return list(self._recent_fills[-200:])

    @property
    def live_order_count(self) -> int:
        return len(self._live_orders)

    def drain_new_fills(self) -> List[FillEvent]:
        """Return accumulated fills and clear the buffer. Exactly-once delivery."""
        fills = self._recent_fills
        self._recent_fills = []
        return fills

    @property
    def open_order_notional(self) -> float:
        """Sum of price * size for all tracked live orders."""
        return sum(rec.price * rec.size for rec in self._live_orders.values())

    async def cancel_stale_orders(self) -> None:
        """Cancel only orders older than MAX_ORDER_AGE_SECONDS (rolling refresh)."""
        now = datetime.now(timezone.utc)
        max_age = timedelta(seconds=self._s.MAX_ORDER_AGE_SECONDS)
        stale = [
            (oid, rec) for oid, rec in self._live_orders.items()
            if (now - rec.posted_at) > max_age
        ]
        if not stale:
            return
        logger.info("Cancelling {} stale orders (age > {}s)", len(stale), self._s.MAX_ORDER_AGE_SECONDS)
        for oid, rec in stale:
            try:
                if not self._s.DRY_RUN:
                    await self._client.cancel(oid)
                self._live_orders.pop(oid, None)
            except Exception as exc:
                logger.debug("Failed to cancel stale order {}: {}", oid[:12], exc)

    # ------------------------------------------------------------------
    # Main entry point per cycle
    # ------------------------------------------------------------------

    async def cancel_and_replace(
        self,
        quotes: TwoSidedQuotes,
    ) -> List[OrderRecord]:
        """
        Cancel existing orders for this market, detect fills from the gap,
        and post fresh quotes.  Returns the newly posted OrderRecords.
        """
        cid = quotes.condition_id

        if self._is_circuit_broken(cid):
            logger.warning("Circuit breaker active for {} — skipping", cid[:12])
            return []

        # 1. Snapshot orders we think are live for this market
        prev_orders = {
            oid: rec
            for oid, rec in self._live_orders.items()
            if rec.condition_id == cid
        }

        # 2. Cancel them
        await self._cancel_market_orders(cid, set(prev_orders.keys()))

        # 3. Post fresh quotes (fill detection handled by FillCollector)
        posted: List[OrderRecord] = []
        for quote in (quotes.yes_quote, quotes.no_quote):
            if quote is None:
                continue
            records = await self._post_quote(cid, quote)
            posted.extend(records)

        if posted:
            self._failure_counts[cid] = 0

        return posted

    async def cancel_all_orders(self) -> None:
        """Emergency / shutdown: cancel every open order."""
        try:
            if self._s.DRY_RUN:
                logger.info("[DRY RUN] Would cancel all {} orders", len(self._live_orders))
                self._live_orders.clear()
                return
            await self._client.cancel_all()
            logger.info("Cancelled all open orders")
            self._live_orders.clear()
        except Exception as exc:
            logger.error("Failed to cancel all orders: {}", exc)

    async def periodic_cleanup(self) -> None:
        """
        Safety net: cancel everything and rebuild from scratch.
        Called every ~4 hours by the scheduler to clear any ghost orders.
        """
        logger.info("Periodic cleanup — cancelling all orders")
        await self.cancel_all_orders()

    # ------------------------------------------------------------------
    # Internal: cancel
    # ------------------------------------------------------------------

    async def _cancel_market_orders(
        self, condition_id: str, known_ids: Set[str]
    ) -> None:
        """Cancel all our orders for a given market."""
        if self._s.DRY_RUN:
            for oid in known_ids:
                self._live_orders.pop(oid, None)
            return

        # Try batch cancel by market first (faster if SDK supports it)
        try:
            await self._client.cancel_market_orders(condition_id)
        except Exception:
            # Fallback: cancel individually
            for oid in known_ids:
                try:
                    await self._client.cancel(oid)
                except Exception as exc:
                    logger.debug("Cancel {} failed (may already be filled): {}", oid[:12], exc)

        for oid in known_ids:
            self._live_orders.pop(oid, None)

    # ------------------------------------------------------------------
    # Internal: detect fills
    # ------------------------------------------------------------------

    async def _detect_fills(self, prev_orders: Dict[str, OrderRecord]) -> None:
        """Stub — fill detection is now handled by FillCollector via trades API.

        Contract: on any API error, abort without recording fills.
        This method is retained for interface compatibility but is a no-op.
        See core/fill_collector.py for the real implementation.
        """
        pass

    # ------------------------------------------------------------------
    # Internal: post
    # ------------------------------------------------------------------

    async def _post_quote(
        self, condition_id: str, quote: Quote
    ) -> List[OrderRecord]:
        """Post bid and ask for a single token book. Returns records of posted orders."""
        records: List[OrderRecord] = []

        # BID (we buy this token)
        bid_rec = await self._post_single(
            condition_id=condition_id,
            token_id=quote.token_id,
            side=BUY,
            price=quote.bid_price,
            size=quote.bid_size,
        )
        if bid_rec:
            records.append(bid_rec)

        # ASK (we sell this token)
        ask_rec = await self._post_single(
            condition_id=condition_id,
            token_id=quote.token_id,
            side=SELL,
            price=quote.ask_price,
            size=quote.ask_size,
        )
        if ask_rec:
            records.append(ask_rec)

        return records

    async def _post_single(
        self,
        condition_id: str,
        token_id: str,
        side: str,
        price: float,
        size: float,
    ) -> Optional[OrderRecord]:
        """Post one GTC limit order. Returns OrderRecord on success."""
        side_label = "BID" if side == BUY else "ASK"

        if self._s.DRY_RUN:
            logger.info(
                "[DRY RUN] {} {} {:.4f} x {:.1f}  ({})",
                side_label,
                token_id[:12],
                price,
                size,
                condition_id[:12],
            )
            return OrderRecord(
                order_id=f"dry_{token_id[:8]}_{side_label}",
                token_id=token_id,
                condition_id=condition_id,
                side=side_label,
                price=price,
                size=size,
            )

        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=side,
            )
            resp = await self._client.create_and_post_order(order_args)

            order_id = ""
            if isinstance(resp, dict):
                order_id = resp.get("orderID", "") or resp.get("id", "")

            if not order_id:
                logger.warning(
                    "No order ID in response for {} {} — resp: {}",
                    side_label,
                    token_id[:12],
                    resp,
                )
                return None

            rec = OrderRecord(
                order_id=order_id,
                token_id=token_id,
                condition_id=condition_id,
                side=side_label,
                price=price,
                size=size,
            )
            self._live_orders[order_id] = rec

            logger.debug(
                "Posted {} {} {:.4f}x{:.1f} → {}",
                side_label,
                token_id[:12],
                price,
                size,
                order_id[:12],
            )
            return rec

        except Exception as exc:
            self._failure_counts[condition_id] += 1
            logger.error(
                "Failed to post {} for {}: {} (failures: {})",
                side_label,
                condition_id[:12],
                exc,
                self._failure_counts[condition_id],
            )
            if self._failure_counts[condition_id] >= CIRCUIT_BREAKER_MAX_FAILURES:
                self._trip_circuit(condition_id)
            return None

    # ------------------------------------------------------------------
    # Circuit breaker
    # ------------------------------------------------------------------

    def _is_circuit_broken(self, condition_id: str) -> bool:
        until = self._cooldown_until.get(condition_id)
        if until is None:
            return False
        if datetime.now(timezone.utc) >= until:
            del self._cooldown_until[condition_id]
            self._failure_counts[condition_id] = 0
            logger.info("Circuit breaker reset for {}", condition_id[:12])
            return False
        return True

    def _trip_circuit(self, condition_id: str) -> None:
        until = datetime.now(timezone.utc) + timedelta(seconds=CIRCUIT_BREAKER_COOLDOWN_SECONDS)
        self._cooldown_until[condition_id] = until
        logger.warning(
            "Circuit breaker TRIPPED for {} — pausing until {}",
            condition_id[:12],
            until.isoformat(),
        )
