"""
Inventory seeder — acquires minimal conditional token positions at startup.

The Polymarket CLOB rejects SELL (ASK) orders when the wallet holds zero
balance of the conditional token.  To bootstrap two-sided quoting the bot
buys YES *and* NO tokens via **GTC limit orders** at aggressive prices,
then polls for fills and cancels any unfilled remainder.

Why GTC instead of FOK:
  - FOK is all-or-nothing: on thin books it frequently gets killed even when
    partial liquidity exists.
  - GTC allows partial fills: if 15 of 20 shares are available, we get 15.
  - After a short timeout we cancel the unfilled residual — no open-ended
    risk.

Before even trying, the seeder checks actual order book depth.  If the book
is empty (no asks AND no complementary bids), the market is skipped
immediately — no point wasting 15+ seconds on a dead market.

Safety:
  - Hard per-side USD cap prevents accidental overspend.
  - Multiple attempts with escalating aggressiveness (price walks further
    through the book on each retry).
  - Runs once at startup, not on every cycle.
"""

from __future__ import annotations

import asyncio
import math
from typing import Dict, List, Optional, Set

from loguru import logger
from py_clob_client_v2 import OrderArgs, OrderType

BUY = "BUY"

from config.markets_filter import MarketCandidate
from config.settings import Settings
from core.client import AsyncClobClient
from utils.discord import DiscordNotifier

_TICK_MAP = {
    "0.1": 0.1,
    "0.01": 0.01,
    "0.001": 0.001,
    "0.0001": 0.0001,
}

SEED_TIMEOUT_SECONDS = 5.0
SEED_POLL_INTERVAL = 0.5
SEED_MAX_ATTEMPTS = 3


class InventorySeeder:
    """One-shot seeder that ensures the wallet holds at least min_size of each token."""

    def __init__(
        self,
        client: AsyncClobClient,
        settings: Settings,
        discord: DiscordNotifier,
    ) -> None:
        self._client = client
        self._s = settings
        self._discord = discord

    async def seed_all(
        self, markets: List[MarketCandidate]
    ) -> tuple[Set[str], Dict[str, Dict[str, float]]]:
        """Seed every active market that needs it — markets seeded in parallel.

        Returns (failed_cids, seed_costs) where:
          - failed_cids: condition_ids where seeding FAILED (caller excludes
            them from quoting — one-sided quotes don't score for LP rewards).
          - seed_costs: {condition_id: {"yes": usd_spent, "no": usd_spent}}
            for markets that were actively seeded this run.  Used by the
            caller to initialise inventory cost basis with the real fill
            price rather than a mid-price estimate.
        """
        if not markets:
            return set()

        budget = self._s.TOTAL_CAPITAL * self._s.SEED_BUDGET_PCT
        budget = min(budget, self._s.SEED_MAX_USD_PER_SIDE * len(markets) * 2)
        per_market_budget = budget / max(len(markets), 1)

        logger.info(
            "── Inventory seeder: checking {} market(s), budget ${:.0f} (${:.0f}/market) ──",
            len(markets), budget, per_market_budget,
        )

        sem = asyncio.Semaphore(3)

        async def _seed_one(market: MarketCandidate) -> tuple[float, bool, str, float, float]:
            async with sem:
                spent, ok, yes_cost, no_cost = await self._seed_market(market, per_market_budget)
                return spent, ok, market.condition_id, yes_cost, no_cost

        results = await asyncio.gather(
            *[_seed_one(m) for m in markets],
            return_exceptions=True,
        )

        total_spent = 0.0
        seeded = 0
        failed_cids: Set[str] = set()
        seed_costs: Dict[str, Dict[str, float]] = {}

        for i, res in enumerate(results):
            if isinstance(res, Exception):
                logger.error("Seed exception for {}: {}", markets[i].condition_id[:12], res)
                failed_cids.add(markets[i].condition_id)
                continue
            spent, ok, cid, yes_cost, no_cost = res
            if spent > 0:
                total_spent += spent
                seeded += 1
                seed_costs[cid] = {"yes": yes_cost, "no": no_cost}
            if not ok:
                failed_cids.add(cid)

        if seeded:
            logger.info(
                "Seeding complete: {} market(s), ~${:.2f} spent (${:.0f} budget remaining for quoting)",
                seeded, total_spent, self._s.TOTAL_CAPITAL - total_spent,
            )
            await self._discord.send_alert(
                title="Inventory Seeded",
                message=(
                    f"Seeded **{seeded}** market(s) with ~**${total_spent:.2f}**.\n"
                    f"USDC remaining for quoting: ~**${self._s.TOTAL_CAPITAL - total_spent:.0f}**"
                ),
                level="info",
            )
        else:
            logger.info("Seeding complete: all markets already have inventory")

        if failed_cids:
            logger.warning(
                "Seeding FAILED for {} market(s) — these will be skipped (one-sided quotes don't score): {}",
                len(failed_cids),
                ", ".join(c[:12] for c in failed_cids),
            )

        return failed_cids, seed_costs

    async def _seed_market(
        self, market: MarketCandidate, budget_remaining: float = float("inf"),
    ) -> tuple[float, bool, float, float]:
        """Seed YES + NO for one market.

        Returns (total_usd_spent, success, yes_cost, no_cost).
        success=True if BOTH sides either already had inventory or were seeded;
        False if either side failed.  yes_cost/no_cost are the USD actually
        spent per side (used by the caller to initialise cost basis correctly).
        """
        if not market.token_id_yes or not market.token_id_no:
            return 0.0, False, 0.0, 0.0

        mid = await self._fetch_mid(market.token_id_yes)
        if mid is None:
            logger.warning(
                "Seed skip {} — cannot fetch midpoint",
                market.condition_id[:12],
            )
            return 0.0, False, 0.0, 0.0

        target = max(self._s.SEED_SHARES, market.min_size)
        est_cost = target * max(mid, 1.0 - mid) * 2
        if est_cost > budget_remaining:
            logger.info(
                "Seed skip {} — est. ${:.0f} exceeds remaining budget ${:.0f}",
                market.condition_id[:12], est_cost, budget_remaining,
            )
            return 0.0, False, 0.0, 0.0

        tick = _TICK_MAP.get(market.tick_size, 0.01)
        per_side_budget = min(
            self._s.SEED_MAX_USD_PER_SIDE,
            budget_remaining / 2,
        )

        sides = [
            ("YES", market.token_id_yes, mid),
            ("NO", market.token_id_no, round(1.0 - mid, 4)),
        ]

        async def _do_side(label: str, token_id: str, token_mid: float):
            return await self._seed_one_side(
                market=market,
                label=label,
                token_id=token_id,
                token_mid=token_mid,
                tick=tick,
                max_cost=per_side_budget,
            )

        results = await asyncio.gather(
            _do_side(*sides[0]),
            _do_side(*sides[1]),
            return_exceptions=True,
        )

        yes_cost = 0.0
        no_cost = 0.0
        both_ok = True
        for i, res in enumerate(results):
            if isinstance(res, Exception):
                logger.error("Seed side exception for {}: {}", market.condition_id[:12], res)
                both_ok = False
            elif res is None:
                both_ok = False
            else:
                if i == 0:
                    yes_cost = res
                else:
                    no_cost = res

        return yes_cost + no_cost, both_ok, yes_cost, no_cost

    # ------------------------------------------------------------------
    # Order book depth check
    # ------------------------------------------------------------------

    async def _check_book_depth(
        self,
        token_id: str,
        side: str,
        min_size: float,
    ) -> tuple[bool, float]:
        """Check if there's meaningful liquidity on the book.

        For a BUY seed, we need either:
          - asks on this token's book, OR
          - enough opposing bids that the CLOB can mint via complement matching

        Returns (has_liquidity, best_price).
        """
        try:
            book = await self._client.get_order_book(token_id)
            if book is None:
                return False, 0.0

            asks = []
            if hasattr(book, "asks"):
                asks = book.asks or []
            elif isinstance(book, dict):
                asks = book.get("asks", [])

            total_ask_size = 0.0
            best_ask = 0.0
            for level in asks:
                if hasattr(level, "size"):
                    sz = float(level.size)
                    px = float(level.price)
                elif isinstance(level, dict):
                    sz = float(level.get("size", 0))
                    px = float(level.get("price", 0))
                else:
                    continue
                total_ask_size += sz
                if best_ask == 0.0 or px < best_ask:
                    best_ask = px

            if total_ask_size >= min_size:
                return True, best_ask

            bids = []
            if hasattr(book, "bids"):
                bids = book.bids or []
            elif isinstance(book, dict):
                bids = book.get("bids", [])

            total_bid_size = sum(
                float(getattr(l, "size", 0) if hasattr(l, "size") else l.get("size", 0) if isinstance(l, dict) else 0)
                for l in bids
            )

            if total_ask_size + total_bid_size < 1:
                return False, 0.0

            return True, best_ask if best_ask > 0 else 0.0

        except Exception as exc:
            logger.debug("Book depth check failed for {}: {}", token_id[:16], exc)
            return True, 0.0

    # ------------------------------------------------------------------
    # Seed one side (YES or NO)
    # ------------------------------------------------------------------

    async def _seed_one_side(
        self,
        market: MarketCandidate,
        label: str,
        token_id: str,
        token_mid: float,
        tick: float,
        max_cost: float = float("inf"),
    ) -> Optional[float]:
        """Ensure we hold enough tokens to post ASK orders at min_size.

        Uses GTC limit orders with a timeout: post at aggressive price, wait
        for fills, cancel remaining.  Retries with escalating aggressiveness.

        Returns USD cost on success, None on failure.
        """
        initial_balance = await self._client.get_conditional_balance(token_id)
        target = max(self._s.SEED_SHARES, market.min_size)

        if initial_balance >= target:
            logger.debug(
                "Seed skip {} {} — already holds {:.1f} (target {})",
                market.condition_id[:12],
                label,
                initial_balance,
                target,
            )
            return 0.0

        # ── Fast check: is there any liquidity to buy from? ──
        has_liq, best_ask = await self._check_book_depth(
            token_id, "asks", target
        )
        if not has_liq:
            logger.warning(
                "Seed skip {} {} — order book is EMPTY (no asks, no opposing bids). "
                "Market has no active liquidity.",
                market.condition_id[:12],
                label,
            )
            return None

        deficit = math.ceil(target - initial_balance)

        if self._s.DRY_RUN:
            price = round(
                token_mid + self._s.SEED_AGGRESSIVE_TICKS * tick, 4
            )
            logger.info(
                "[DRY RUN] Would seed {} {} — {} shares @ ${:.4f}",
                market.condition_id[:12],
                label,
                deficit,
                price,
            )
            return 0.0

        for attempt in range(1, SEED_MAX_ATTEMPTS + 1):
            current_bal = await self._client.get_conditional_balance(token_id)
            remaining = math.ceil(target - current_bal)
            if remaining <= 0:
                break

            extra_ticks = self._s.SEED_AGGRESSIVE_TICKS + (attempt - 1) * 2
            aggressive_price = round(token_mid + extra_ticks * tick, 4)
            aggressive_price = max(0.01, min(0.99, aggressive_price))

            if best_ask > 0:
                aggressive_price = max(aggressive_price, round(best_ask + tick, 4))

            estimated_cost = aggressive_price * remaining
            effective_cap = min(self._s.SEED_MAX_USD_PER_SIDE, max_cost)
            if estimated_cost > effective_cap:
                logger.warning(
                    "Seed skip {} {} — est. ${:.2f} > cap ${:.2f}",
                    market.condition_id[:12],
                    label,
                    estimated_cost,
                    effective_cap,
                )
                return None

            logger.info(
                "Seeding {} {} — {} shares @ ${:.4f} GTC attempt {}/{} (est. ${:.2f})",
                market.condition_id[:12],
                label,
                remaining,
                aggressive_price,
                attempt,
                SEED_MAX_ATTEMPTS,
                estimated_cost,
            )

            order_id = await self._post_gtc_and_wait(
                token_id=token_id,
                price=aggressive_price,
                size=remaining,
                market_label=f"{market.condition_id[:12]} {label}",
                target=target,
            )

            final_bal = await self._client.get_conditional_balance(token_id)
            acquired = final_bal - current_bal

            if final_bal >= target:
                logger.info(
                    "✅ Seed {} {} complete — acquired {:.0f} shares (balance {:.0f})",
                    market.condition_id[:12],
                    label,
                    acquired,
                    final_bal,
                )
                return acquired * aggressive_price

            if acquired > 0:
                logger.info(
                    "Seed {} {} partial — got {:.0f}/{} shares, retrying…",
                    market.condition_id[:12],
                    label,
                    acquired,
                    remaining,
                )
            else:
                logger.warning(
                    "Seed {} {} attempt {} — 0 shares filled at ${:.4f}, escalating price",
                    market.condition_id[:12],
                    label,
                    attempt,
                    aggressive_price,
                )

        final_bal = await self._client.get_conditional_balance(token_id)
        total_acquired = final_bal - initial_balance
        if total_acquired > 0:
            logger.info(
                "Seed {} {} — acquired {:.0f} shares total (target was {})",
                market.condition_id[:12],
                label,
                total_acquired,
                target,
            )
            return total_acquired * token_mid
        else:
            logger.error(
                "Seed {} {} FAILED after {} attempts — "
                "ASK orders will be disabled for this token.",
                market.condition_id[:12],
                label,
                SEED_MAX_ATTEMPTS,
            )
            return None

    # ------------------------------------------------------------------
    # GTC post + wait + cancel helper
    # ------------------------------------------------------------------

    async def _post_gtc_and_wait(
        self,
        token_id: str,
        price: float,
        size: int,
        market_label: str,
        target: float = 0.0,
    ) -> Optional[str]:
        """Post a GTC limit buy, poll for fill, cancel residual.

        Instead of sleeping the full timeout, polls the balance every
        SEED_POLL_INTERVAL and exits early as soon as the target is reached.
        """
        order_id: Optional[str] = None
        filled_early = False
        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=BUY,
            )
            resp = await self._client.create_and_post_order(
                order_args, order_type=OrderType.GTC
            )

            if isinstance(resp, dict):
                order_id = resp.get("orderID", "") or resp.get("id", "")

            if not order_id:
                logger.warning(
                    "Seed {} — no order ID in response: {}",
                    market_label,
                    resp,
                )
                return None

            polls = int(SEED_TIMEOUT_SECONDS / SEED_POLL_INTERVAL)
            for i in range(polls):
                await asyncio.sleep(SEED_POLL_INTERVAL)
                if target > 0 and i % 2 == 1:
                    bal = await self._client.get_conditional_balance(token_id)
                    if bal >= target:
                        filled_early = True
                        break

        except Exception as exc:
            err = str(exc).lower()
            if "not enough balance" in err or "allowance" in err:
                logger.warning(
                    "Seed {} failed — insufficient USDC or allowance: {}",
                    market_label,
                    exc,
                )
            else:
                logger.error("Seed {} order failed: {}", market_label, exc)
            return None
        finally:
            if order_id:
                try:
                    await self._client.cancel(order_id)
                    await asyncio.sleep(0.3)
                except Exception:
                    pass

        return order_id

    async def _fetch_mid(self, token_id_yes: str) -> Optional[float]:
        """Fetch current midpoint for the YES token. Returns None on failure."""
        try:
            resp = await self._client.get_midpoint(token_id_yes)
            if resp is None:
                return None
            if isinstance(resp, dict):
                raw = resp.get("mid", 0)
            else:
                raw = resp
            mid = float(raw)
            if 0.01 <= mid <= 0.99:
                return mid
        except (TypeError, ValueError):
            pass
        return None
