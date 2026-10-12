"""
Order lifecycle manager — smart quote updates with continuous book presence.

Each rebalance cycle:
  1. For each active market, compare new quotes against existing live orders.
  2. Only cancel + repost the sides where price moved beyond the configured
     tick threshold, size changed significantly, or the order is stale.
  3. Orders that are still valid stay on the book — zero gap, maximum LP
     scoring uptime.

Safety nets:
  - MAX_ORDER_AGE_SECONDS forces a refresh even if prices haven't moved.
  - force_replace=True (used after detected fills) bypasses the diff and
    does a full cancel-then-replace.
  - Circuit breaker pauses quoting after repeated failures.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, NamedTuple, Optional, Set, Tuple

from loguru import logger
from py_clob_client_v2 import OrderArgs, OrderType

BUY, SELL = "BUY", "SELL"

from config.settings import Settings
from core.client import AsyncClobClient
from core.quoter import Quote, TwoSidedQuotes


def should_replace_price(
    old: float, new: float, tick: float, threshold_ticks: int,
) -> bool:
    """True if |new-old| exceeds *threshold_ticks* of the *market* tick."""
    return abs(new - old) > (tick * threshold_ticks) + 1e-9


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
    outcome: str = "yes"


@dataclass
class FillEvent:
    """Detected fill (partial or full) between cycles."""

    token_id: str
    condition_id: str
    side: str
    price: float
    filled_size: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    trade_id: str = ""
    mid_at_fill: Optional[float] = None


class QuoteUpdateResult(NamedTuple):
    """Return value from cancel_and_replace."""

    posted: List[OrderRecord]
    kept: int


class OrderManager:
    """Manages the full order lifecycle for all active markets."""

    _balance_allowance_hint_logged: bool = False

    def __init__(self, client: AsyncClobClient, settings: Settings) -> None:
        self._client = client
        self._s = settings
        self._live_orders: Dict[str, OrderRecord] = {}
        self._failure_counts: Dict[str, int] = defaultdict(int)
        self._cooldown_until: Dict[str, datetime] = {}
        self._usdc_balance: Optional[float] = None
        self._committed_capital: float = 0.0
        self._market_locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._session_quoted_notional: float = 0.0
        self._tape: Any = None
        self._digest: Any = None
        self._paper: Any = None

    def set_tape(self, tape: Any) -> None:
        """Optional LocalTape — posts/cancels are appended on success."""
        self._tape = tape

    def set_digest(self, digest: Any) -> None:
        self._digest = digest

    def set_paper(self, paper: Any) -> None:
        """Optional PaperBroker — DRY_RUN posts/cancels/pre-checks only."""
        self._paper = paper

    @property
    def usdc_balance(self) -> Optional[float]:
        return self._usdc_balance

    @property
    def committed_capital(self) -> float:
        return self._committed_capital

    def market_lock(self, condition_id: str) -> asyncio.Lock:
        """Return the per-market lock (creates on first access)."""
        return self._market_locks[condition_id]

    @property
    def live_order_count(self) -> int:
        return len(self._live_orders)

    @property
    def open_order_notional(self) -> float:
        """BID notional only (ASK sells owned inventory — excluded from exposure)."""
        return sum(
            rec.price * rec.size
            for rec in self._live_orders.values()
            if rec.side == BUY
        )

    @property
    def open_ask_notional(self) -> float:
        """ASK notional (diagnostic only — excluded from exposure gates)."""
        return sum(
            rec.price * rec.size
            for rec in self._live_orders.values()
            if rec.side == SELL
        )

    def open_order_notional_for_market(self, condition_id: str) -> float:
        """BID notional for one market (ASK excluded from exposure gates)."""
        return sum(
            rec.price * rec.size
            for rec in self._live_orders.values()
            if rec.condition_id == condition_id and rec.side == BUY
        )

    def open_ask_notional_for_market(self, condition_id: str) -> float:
        """ASK notional for one market (excluded from gates; for logging)."""
        return sum(
            rec.price * rec.size
            for rec in self._live_orders.values()
            if rec.condition_id == condition_id and rec.side == SELL
        )

    def drain_quoted_notional(self) -> float:
        """Return accumulated quoted notional since last drain, then reset to zero.

        Captures both hot-path and cold-path posts since the previous drain.
        """
        val = self._session_quoted_notional
        self._session_quoted_notional = 0.0
        return val

    def get_order_ids_for_condition(self, condition_id: str) -> List[str]:
        """Return all live order IDs belonging to a given market."""
        return [
            oid for oid, rec in self._live_orders.items()
            if rec.condition_id == condition_id
        ]

    async def refresh_usdc_balance(self) -> None:
        """Fetch current USDC balance once per cycle for collateral pre-checks."""
        if self._s.DRY_RUN and self._paper is not None:
            self._usdc_balance = float(self._paper.cash)
            self._committed_capital = float(self._paper.committed)
            return
        try:
            from py_clob_client_v2 import BalanceAllowanceParams
            resp = await self._client._call(
                "get_balance_allowance",
                BalanceAllowanceParams(
                    asset_type="COLLATERAL",
                    signature_type=self._s.SIGNATURE_TYPE,
                ),
            )
            if isinstance(resp, dict):
                raw = float(resp.get("balance", 0) or 0)
                self._usdc_balance = raw / 1_000_000 if raw > 1_000 else raw
            else:
                self._usdc_balance = None
            self._committed_capital = 0.0
        except Exception as exc:
            logger.debug("USDC balance fetch failed: {}", exc)
            self._usdc_balance = None

    def _collateral_ok(
        self,
        notional: float,
        side: str = BUY,
        token_id: str = "",
        size: float = 0.0,
    ) -> bool:
        """Soft pre-check: enough USDC (or paper inventory) for this order?"""
        if self._s.DRY_RUN and self._paper is not None:
            if side == SELL:
                return self._paper.precheck_ask(token_id, size)
            return self._paper.precheck_bid(notional)
        if side == SELL:
            return True
        if self._usdc_balance is None:
            return True
        remaining = self._usdc_balance - self._committed_capital
        if notional > remaining:
            logger.debug(
                "Collateral pre-check: need ${:.2f} but only ${:.2f} remaining",
                notional, remaining,
            )
            return False
        return True

    def sync_live_orders_from_paper(self) -> None:
        """Drop/resize local records after paper fills. No-op when no paper."""
        if self._paper is None:
            return
        remaining = self._paper.resting_sizes()
        for oid in list(self._live_orders):
            if oid not in remaining:
                self._live_orders.pop(oid, None)
            else:
                self._live_orders[oid].size = remaining[oid]
        self._committed_capital = float(self._paper.committed)
        self._usdc_balance = float(self._paper.cash)

    # ------------------------------------------------------------------
    # Helpers: find & cancel individual orders
    # ------------------------------------------------------------------

    def _find_live_order(
        self, condition_id: str, token_id: str, side: str
    ) -> Optional[Tuple[str, OrderRecord]]:
        """Find an existing live order matching (condition_id, token_id, side)."""
        for oid, rec in self._live_orders.items():
            if (
                rec.condition_id == condition_id
                and rec.token_id == token_id
                and rec.side == side
            ):
                return (oid, rec)
        return None

    async def _cancel_single(self, order_id: str) -> bool:
        """Cancel one order by ID and remove from local tracking.

        Returns True if the cancel succeeded (or was a dry run).
        Returns False if the API call failed — the order is left in
        _live_orders so it can be retried on the next cycle instead of
        becoming a ghost order that permanently locks tokens.
        """
        rec = self._live_orders.get(order_id)
        try:
            if not self._s.DRY_RUN:
                await self._client.cancel(order_id)
            elif self._paper is not None:
                self._paper.cancel(order_id)
        except Exception as exc:
            err_str = str(exc).lower()
            if "not found" in err_str or "already" in err_str:
                logger.info("Cancel {} — already gone (filled/expired): {}", order_id[:12], exc)
                self._live_orders.pop(order_id, None)
                self._tape_cancel(rec, order_id)
                return True
            logger.warning("Cancel {} FAILED — keeping in tracking to retry: {}", order_id[:12], exc)
            return False
        self._live_orders.pop(order_id, None)
        self._tape_cancel(rec, order_id)
        return True

    def _tape_cancel(self, rec: Optional[OrderRecord], order_id: str) -> None:
        if self._tape is None:
            return
        try:
            self._tape.cancel(
                condition_id=rec.condition_id if rec else "",
                token_id=rec.token_id if rec else "",
                side=rec.side if rec else "",
                price=rec.price if rec else 0.0,
                size=rec.size if rec else 0.0,
                order_id=order_id,
            )
        except Exception:
            pass

    def _tape_post(self, rec: OrderRecord) -> None:
        if self._tape is None:
            return
        try:
            self._tape.post(
                condition_id=rec.condition_id,
                token_id=rec.token_id,
                side=rec.side,
                price=rec.price,
                size=rec.size,
                order_id=rec.order_id,
                outcome=rec.outcome or "yes",
            )
        except Exception:
            pass

    def _remember_computed_quotes(self, quotes: TwoSidedQuotes) -> None:
        """Push YES+NO levels into the tape cache (covers smart-diff keeps)."""
        if self._tape is None:
            return
        try:
            mid = None
            if quotes.yes_quote is not None:
                mid = quotes.yes_quote.mid
            self._tape.remember_two_sided(
                quotes.condition_id,
                mid=mid,
                yes_quote=quotes.yes_quote,
                no_quote=quotes.no_quote,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Stale order cleanup (background safety net)
    # ------------------------------------------------------------------

    async def cancel_stale_orders(self) -> None:
        """Cancel only orders older than MAX_ORDER_AGE_SECONDS (rolling refresh)."""
        now = datetime.now(timezone.utc)
        max_age = timedelta(seconds=self._s.MAX_ORDER_AGE_SECONDS)
        stale = [
            (oid, rec)
            for oid, rec in self._live_orders.items()
            if (now - rec.posted_at) > max_age
        ]
        if not stale:
            return
        logger.info(
            "Cancelling {} stale orders (age > {}s)",
            len(stale),
            self._s.MAX_ORDER_AGE_SECONDS,
        )
        for oid, rec in stale:
            async with self.market_lock(rec.condition_id):
                await self._cancel_single(oid)

    # ------------------------------------------------------------------
    # Main entry point per cycle — SMART DIFF
    # ------------------------------------------------------------------

    async def cancel_and_replace(
        self,
        quotes: TwoSidedQuotes,
        force_replace: bool = False,
    ) -> QuoteUpdateResult:
        """
        Smart quote update.  Compares new quotes against existing live orders
        and only cancels + reposts sides where price moved beyond the tick
        threshold, size changed materially, or the order is stale.

        When force_replace=True (after a fill), does a full cancel-then-replace
        to guarantee the book matches the desired state.
        """
        cid = quotes.condition_id

        if self._is_circuit_broken(cid):
            logger.warning("Circuit breaker active for {} — skipping", cid[:12])
            return QuoteUpdateResult([], 0)

        if force_replace:
            posted = await self._full_cancel_and_replace(quotes)
            self._remember_computed_quotes(quotes)
            return QuoteUpdateResult(posted, 0)

        tick = float(getattr(quotes, "tick_size", None) or 0.01)
        now = datetime.now(timezone.utc)
        max_age = timedelta(seconds=self._s.MAX_ORDER_AGE_SECONDS)

        posted: List[OrderRecord] = []
        kept = 0

        for quote, outcome in (
            (quotes.yes_quote, "yes"),
            (quotes.no_quote, "no"),
        ):
            if quote is None:
                continue

            for side_label, new_price, new_size, sdk_side in [
                ("BID", quote.bid_price, quote.bid_size, BUY),
                ("ASK", quote.ask_price, quote.ask_size, SELL),
            ]:
                existing = self._find_live_order(cid, quote.token_id, side_label)

                # ── Quoter withdrew this side → cancel orphan ──
                if new_size <= 0 or new_price <= 0:
                    if existing:
                        oid, rec = existing
                        logger.info(
                            "Withdrawing {} {} (quoter no longer quoting)",
                            side_label,
                            quote.token_id[:10],
                        )
                        await self._cancel_single(oid)
                    continue

                # ── No existing order → fresh post ──
                if not existing:
                    rec = await self._post_single(
                        cid, quote.token_id, sdk_side, new_price, new_size,
                        outcome=outcome,
                    )
                    if rec:
                        posted.append(rec)
                    continue

                # ── Compare against existing order ──
                oid, old_rec = existing
                price_diff = abs(new_price - old_rec.price)
                age = now - old_rec.posted_at
                is_stale = age > max_age
                price_moved = should_replace_price(
                    old_rec.price, new_price, tick, self._s.ORDER_UPDATE_THRESHOLD_TICKS,
                )
                size_changed = abs(new_size - old_rec.size) > max(
                    0.5, old_rec.size * 0.10
                )

                if not (price_moved or is_stale or size_changed):
                    kept += 1
                    continue

                reasons: List[str] = []
                if price_moved:
                    ticks = price_diff / tick if tick > 0 else 0.0
                    reasons.append(
                        f"price Δ{ticks:.0f}t ({old_rec.price:.4f}→{new_price:.4f})"
                    )
                if is_stale:
                    reasons.append(f"stale {age.total_seconds():.0f}s")
                if size_changed:
                    reasons.append(f"size {old_rec.size:.0f}→{new_size:.0f}")

                logger.info(
                    "Replacing {} {} — {}",
                    side_label,
                    quote.token_id[:10],
                    ", ".join(reasons),
                )

                # ASK orders lock conditional tokens 1:1. POST_BEFORE_CANCEL
                # would double-commit the same tokens and always fail for ASKs.
                # Always cancel-first for ASK; respect the setting for BID.
                cancel_first = (side_label == "ASK") or not self._s.POST_BEFORE_CANCEL

                if cancel_first:
                    cancelled = await self._cancel_single(oid)
                    if not cancelled:
                        kept += 1
                        continue
                    rec = await self._post_single(
                        cid, quote.token_id, sdk_side, new_price, new_size,
                        outcome=outcome,
                    )
                    if rec:
                        posted.append(rec)
                else:
                    rec = await self._post_single(
                        cid, quote.token_id, sdk_side, new_price, new_size,
                        outcome=outcome,
                    )
                    cancelled = await self._cancel_single(oid)
                    if not cancelled:
                        logger.warning(
                            "Old BID {} cancel failed after new post — may have duplicate",
                            oid[:12],
                        )
                    if rec:
                        posted.append(rec)

        if posted:
            self._failure_counts[cid] = 0
        if kept > 0:
            logger.debug(
                "{}: kept {} orders (within {}t threshold)",
                cid[:10],
                kept,
                self._s.ORDER_UPDATE_THRESHOLD_TICKS,
            )

        self._remember_computed_quotes(quotes)
        return QuoteUpdateResult(posted, kept)

    # ------------------------------------------------------------------
    # Full cancel-and-replace (used after fills or forced refresh)
    # ------------------------------------------------------------------

    async def _full_cancel_and_replace(
        self, quotes: TwoSidedQuotes
    ) -> List[OrderRecord]:
        """Unconditional cancel + repost for a market (e.g. after a fill)."""
        cid = quotes.condition_id
        prev_orders = {
            oid: rec
            for oid, rec in self._live_orders.items()
            if rec.condition_id == cid
        }

        if prev_orders:
            logger.info(
                "Force-replacing {} orders for {} (cancel-first, freeing locked tokens)",
                len(prev_orders),
                cid[:10],
            )
            await self._cancel_market_orders(cid, set(prev_orders.keys()))

        posted: List[OrderRecord] = []
        for quote, outcome in (
            (quotes.yes_quote, "yes"),
            (quotes.no_quote, "no"),
        ):
            if quote is None:
                continue
            records = await self._post_quote(cid, quote, outcome=outcome)
            posted.extend(records)

        if posted:
            self._failure_counts[cid] = 0

        return posted

    async def cancel_all_orders(self) -> bool:
        """Emergency / shutdown: cancel every open order.

        Returns True on success (including DRY_RUN). Returns False on API
        failure — the caller decides whether that is fatal (startup) or
        best-effort (shutdown).
        """
        try:
            if self._s.DRY_RUN:
                logger.info("[DRY RUN] Would cancel all {} orders", len(self._live_orders))
                if self._paper is not None:
                    self._paper.cancel_all()
                    self._committed_capital = float(self._paper.committed)
                    self._usdc_balance = float(self._paper.cash)
                self._live_orders.clear()
                return True
            await self._client.cancel_all()
            logger.info("Cancelled all open orders")
            self._live_orders.clear()
            return True
        except Exception as exc:
            logger.error("Failed to cancel all orders: {}", exc)
            return False

    async def reconcile_ghost_orders(self) -> int:
        """Detect and cancel ghost orders (live on CLOB but not tracked locally).

        Returns the number of ghost orders cancelled.  Should be called
        periodically (e.g. every 5-10 minutes) to prevent token lock-up.
        """
        if self._s.DRY_RUN:
            return 0
        try:
            exchange_orders = await self._client.get_orders()
        except Exception as exc:
            logger.warning("Ghost reconciliation — failed to fetch open orders: {}", exc)
            return 0

        if not isinstance(exchange_orders, list):
            return 0

        tracked_ids = set(self._live_orders.keys())
        ghosts: List[Tuple[str, str]] = []
        for order in exchange_orders:
            if not isinstance(order, dict):
                continue
            oid = order.get("id") or order.get("orderID") or order.get("order_id", "")
            if oid and oid not in tracked_ids:
                cid = str(order.get("market") or order.get("condition_id") or "")
                ghosts.append((str(oid), cid))

        if not ghosts:
            return 0

        logger.warning(
            "Ghost reconciliation — found {} orders on CLOB not in local tracking, cancelling",
            len(ghosts),
        )
        cancelled = 0
        for oid, cid in ghosts:
            try:
                if cid:
                    async with self.market_lock(cid):
                        await self._client.cancel(oid)
                else:
                    await self._client.cancel(oid)
                cancelled += 1
                logger.info("Ghost order {} cancelled", oid[:12])
            except Exception as exc:
                logger.warning("Failed to cancel ghost order {}: {}", oid[:12], exc)
        return cancelled

    # ------------------------------------------------------------------
    # Internal: cancel
    # ------------------------------------------------------------------

    async def _cancel_market_orders(
        self, condition_id: str, known_ids: Set[str]
    ) -> None:
        """Cancel all our orders for a given market."""
        if self._s.DRY_RUN:
            for oid in known_ids:
                rec = self._live_orders.pop(oid, None)
                if self._paper is not None:
                    self._paper.cancel(oid)
                self._tape_cancel(rec, oid)
            if self._paper is not None:
                self._committed_capital = float(self._paper.committed)
                self._usdc_balance = float(self._paper.cash)
            return

        bulk_ok = True
        try:
            await self._client.cancel_market_orders(condition_id)
        except Exception:
            bulk_ok = False
            for oid in known_ids:
                try:
                    await self._client.cancel(oid)
                except Exception as exc:
                    err_str = str(exc).lower()
                    if "not found" in err_str or "already" in err_str:
                        logger.info("Cancel {} — already gone: {}", oid[:12], exc)
                    else:
                        logger.warning(
                            "Cancel {} failed — keeping in tracking: {}",
                            oid[:12], exc,
                        )
                        continue
                rec = self._live_orders.pop(oid, None)
                self._tape_cancel(rec, oid)

        if bulk_ok:
            for oid in known_ids:
                rec = self._live_orders.pop(oid, None)
                self._tape_cancel(rec, oid)

    async def cancel_orders_for_market(self, condition_id: str) -> int:
        """Cancel all tracked orders for a specific market.  Returns count cancelled."""
        to_cancel = {
            oid for oid, rec in self._live_orders.items()
            if rec.condition_id == condition_id
        }
        if not to_cancel:
            return 0
        await self._cancel_market_orders(condition_id, to_cancel)
        logger.info("Cancelled {} orders for paused market {}", len(to_cancel), condition_id[:12])
        return len(to_cancel)

    # ------------------------------------------------------------------
    # Internal: post
    # ------------------------------------------------------------------

    async def _post_quote(
        self, condition_id: str, quote: Quote, *, outcome: str = "yes"
    ) -> List[OrderRecord]:
        """Post ask then bid for a single token book.

        ASKs are posted first because they lock conditional tokens (already held),
        preserving the USDC pool for BIDs.  This prevents the scenario where BIDs
        consume all available USDC margin before ASKs can be placed.
        """
        records: List[OrderRecord] = []

        if quote.ask_size > 0 and quote.ask_price > 0:
            ask_rec = await self._post_single(
                condition_id=condition_id,
                token_id=quote.token_id,
                side=SELL,
                price=quote.ask_price,
                size=quote.ask_size,
                outcome=outcome,
            )
            if ask_rec:
                records.append(ask_rec)

        if quote.bid_size > 0 and quote.bid_price > 0:
            bid_rec = await self._post_single(
                condition_id=condition_id,
                token_id=quote.token_id,
                side=BUY,
                price=quote.bid_price,
                size=quote.bid_size,
                outcome=outcome,
            )
            if bid_rec:
                records.append(bid_rec)

        return records

    async def _post_single(
        self,
        condition_id: str,
        token_id: str,
        side: str,
        price: float,
        size: float,
        *,
        outcome: str = "yes",
    ) -> Optional[OrderRecord]:
        """Post one GTC limit order. Returns OrderRecord on success."""
        side_label = "BID" if side == BUY else "ASK"
        notional = price * size
        outcome_s = outcome if outcome in ("yes", "no") else "yes"
        if not self._collateral_ok(notional, side, token_id=token_id, size=size):
            if self._s.DRY_RUN and self._paper is not None:
                remaining = (
                    self._paper.spendable if side == BUY
                    else self._paper.free_ask_size(token_id)
                )
            else:
                remaining = (self._usdc_balance or 0.0) - self._committed_capital
            skip_reason = "collateral" if side == BUY else "inventory"
            logger.info(
                "Skipping {} {} — {} pre-check failed (${:.2f})",
                side_label, token_id[:12], skip_reason, notional,
            )
            if skip_reason == "collateral" and self._tape is not None:
                try:
                    self._tape.skip(
                        condition_id,
                        "collateral",
                        needed_usd=round(notional, 4),
                        available_usd=round(remaining, 4),
                        side=side_label,
                    )
                except Exception:
                    pass
            if skip_reason == "collateral" and self._digest is not None:
                try:
                    self._digest.record_skip(
                        condition_id,
                        "collateral",
                        {
                            "needed_usd": round(notional, 4),
                            "available_usd": round(remaining, 4),
                            "side": side_label,
                        },
                    )
                except Exception:
                    pass
            return None

        if self._s.DRY_RUN:
            logger.info(
                "[DRY RUN] {} {} {:.4f} x {:.1f}  ({})",
                side_label,
                token_id[:12],
                price,
                size,
                condition_id[:12],
            )
            order_id = (
                self._paper.next_order_id()
                if self._paper is not None
                else f"dry_{token_id[:8]}_{side_label}"
            )
            if self._paper is not None:
                posted = self._paper.post(
                    order_id, condition_id, token_id, side_label, price, size,
                )
                if not posted:
                    remaining = (
                        self._paper.spendable if side == BUY
                        else self._paper.free_ask_size(token_id)
                    )
                    if side == BUY and self._tape is not None:
                        try:
                            self._tape.skip(
                                condition_id,
                                "collateral",
                                needed_usd=round(notional, 4),
                                available_usd=round(remaining, 4),
                                side=side_label,
                            )
                        except Exception:
                            pass
                    return None
                self._committed_capital = float(self._paper.committed)
                self._usdc_balance = float(self._paper.cash)
            rec = OrderRecord(
                order_id=order_id,
                token_id=token_id,
                condition_id=condition_id,
                side=side_label,
                price=price,
                size=size,
                outcome=outcome_s,
            )
            self._live_orders[order_id] = rec
            self._session_quoted_notional += notional
            self._tape_post(rec)
            return rec

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
                outcome=outcome_s,
            )
            self._live_orders[order_id] = rec
            self._committed_capital += notional
            self._session_quoted_notional += notional
            self._tape_post(rec)

            logger.info(
                "✅ {} {} {:.4f} × {:.0f} = ${:.2f}  [{}]  → {}",
                side_label,
                token_id[:10],
                price,
                size,
                notional,
                condition_id[:10],
                order_id[:10],
            )
            return rec

        except Exception as exc:
            err_str = str(exc).lower()

            # Classify error: balance errors trip immediately, geoblock alerts, others accumulate
            if "trading restricted" in err_str or "restricted in your region" in err_str:
                logger.critical("GEOBLOCK detected for {} — pausing market", condition_id[:12])
                self._trip_circuit(condition_id)
                return None

            if "duplicated" in err_str:
                logger.debug("Duplicate order for {} {} — existing order still live", side_label, condition_id[:12])
                return None

            if "invalid" in err_str and ("param" in err_str or "amount" in err_str):
                logger.warning("Invalid params for {} {} — skipping (not counted as failure)", side_label, condition_id[:12])
                return None

            self._failure_counts[condition_id] += 1

            if (
                "not enough balance" in err_str or "allowance" in err_str
            ):
                if not OrderManager._balance_allowance_hint_logged:
                    OrderManager._balance_allowance_hint_logged = True
                    hint = (
                        "Hint: ASK rejected (not enough balance/allowance). Bot re-syncs COLLATERAL each cycle."
                        if side_label == "ASK" else
                        "Hint: Fund your Polygon wallet with USDC and set token allowances."
                    )
                    logger.info(hint)
                if side == BUY:
                    self._trip_circuit(condition_id)
                else:
                    logger.info(
                        "ASK balance/allowance error for {} — transient (USDC committed to BIDs), skipping without circuit trip",
                        condition_id[:12],
                    )
                    self._failure_counts[condition_id] = max(0, self._failure_counts[condition_id] - 1)
                return None

            logger.error(
                "Failed to post {} for {}: {} (failures: {})",
                side_label,
                condition_id[:12],
                exc,
                self._failure_counts[condition_id],
            )
            if self._failure_counts[condition_id] >= self._s.CIRCUIT_BREAKER_MAX_FAILURES:
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
        until = datetime.now(timezone.utc) + timedelta(
            seconds=self._s.CIRCUIT_BREAKER_COOLDOWN_SECONDS
        )
        self._cooldown_until[condition_id] = until
        logger.warning(
            "Circuit breaker TRIPPED for {} — pausing until {}",
            condition_id[:12],
            until.isoformat(),
        )
