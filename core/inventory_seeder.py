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
from typing import Any, Dict, List, Optional, Set

from loguru import logger
from py_clob_client_v2 import OrderArgs, OrderType

BUY = "BUY"

from config.markets_filter import MarketCandidate
from config.settings import Settings
from core.client import AsyncClobClient
from core.resolution_guard import ResolutionGuard
from utils.discord import DiscordNotifier
from utils.helpers import alloc_side_shares

_TICK_MAP = {
    "0.1": 0.1,
    "0.01": 0.01,
    "0.001": 0.001,
    "0.0001": 0.0001,
}

SEED_TIMEOUT_SECONDS = 5.0
SEED_POLL_INTERVAL = 0.5
SEED_MAX_ATTEMPTS = 3
SELL = "SELL"
FLATTEN_TIMEOUT_SECONDS = 5.0
FLATTEN_MAX_ATTEMPTS = 3


def compute_matched_seed_n(
    allocated_capital: float,
    yes_px: float,
    no_px: float,
    per_side_budget: float,
    min_size: float,
    *,
    yes_est_buy: Optional[float] = None,
    no_est_buy: Optional[float] = None,
) -> float:
    """Matched YES=NO share count for atomic seed (not matched dollars).

    ``N = min(ideal_yes, ideal_no)``, then capped by what the expensive
    side can buy under ``per_side_budget``. Floored to a whole share.
    Returns 0 when even ``min_size`` does not fit (caller → cost_cap).
    """
    floor = float(min_size)
    ideal_yes = alloc_side_shares(allocated_capital, yes_px)
    ideal_no = alloc_side_shares(allocated_capital, no_px)
    n = min(ideal_yes, ideal_no)
    yes_buy = float(yes_est_buy if yes_est_buy is not None else yes_px)
    no_buy = float(no_est_buy if no_est_buy is not None else no_px)
    expensive = max(yes_buy, no_buy, 0.01)
    n = min(n, float(per_side_budget) / expensive)
    n = float(math.floor(n + 1e-12))
    if n + 1e-9 < floor:
        return 0.0
    return n


class InventorySeeder:
    """One-shot seeder that acquires ASK inventory sized to planned quotes."""

    def __init__(
        self,
        client: AsyncClobClient,
        settings: Settings,
        discord: DiscordNotifier,
        resolution_guard: Any = None,
    ) -> None:
        self._client = client
        self._s = settings
        self._discord = discord
        self._tape: Any = None
        self._digest: Any = None
        self._paper: Any = None
        self._resolution_guard = resolution_guard
        # Cids that rebalanced a one-sided (0/N) book this cycle — no re-seed.
        self._block_reseed: Set[str] = set()

    def set_tape(self, tape: Any) -> None:
        self._tape = tape

    def set_digest(self, digest: Any) -> None:
        self._digest = digest

    def set_paper(self, paper: Any) -> None:
        """Optional PaperBroker — DRY_RUN credits cash/inventory here."""
        self._paper = paper

    def set_resolution_guard(self, guard: Any) -> None:
        """Share process ResolutionGuard (avoid per-market sticky reload)."""
        self._resolution_guard = guard

    def clear_reseed_blocks(self) -> None:
        """Drop per-cycle one-sided rebalance seed blocks (call each cold cycle)."""
        self._block_reseed.clear()

    def note_onesided_rebalance(self, condition_id: str) -> None:
        """Block seed_all for a cid that just rebalanced 0/N → 0/limit."""
        if condition_id:
            self._block_reseed.add(condition_id)

    def _min_seed_shares(self, market: MarketCandidate) -> float:
        return max(float(self._s.SEED_SHARES), float(market.min_size))

    def _matched_seed_n(
        self,
        market: MarketCandidate,
        yes_px: float,
        no_px: float,
        yes_est_buy: float,
        no_est_buy: float,
        per_side_budget: float,
    ) -> float:
        """Single N for both sides — see :func:`compute_matched_seed_n`."""
        return compute_matched_seed_n(
            market.allocated_capital,
            yes_px,
            no_px,
            per_side_budget,
            self._min_seed_shares(market),
            yes_est_buy=yes_est_buy,
            no_est_buy=no_est_buy,
        )

    def _emit_seed(
        self,
        cid: str,
        yes_cost: float,
        no_cost: float,
        cap: float,
        ok: bool,
        reason: str,
    ) -> None:
        row = {
            "cid": cid,
            "yes_cost": yes_cost,
            "no_cost": no_cost,
            "cap": cap,
            "ok": ok,
            "dry_run": bool(self._s.DRY_RUN),
            "reason": reason,
        }
        try:
            if self._tape is not None:
                self._tape.seed(
                    cid=cid,
                    yes_cost=yes_cost,
                    no_cost=no_cost,
                    cap=cap,
                    ok=ok,
                    dry_run=bool(self._s.DRY_RUN),
                    reason=reason,
                )
            if self._digest is not None:
                self._digest.record_seed(row)
        except Exception as exc:
            logger.warning("Tape seed emit failed: {}", exc)

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

        Atomicity invariant (Issue 1): YES and NO are treated as an atomic
        pair.  ``_seed_market`` enforces a strict pre-check on BOTH sides
        (cost estimate vs cap, order-book depth) before posting anything;
        if either side fails the pre-check, NEITHER side is posted and
        the market is reported in ``failed_cids`` with cost basis 0/0.
        At runtime, sides are seeded sequentially: if the first side
        acquires zero shares, the second side is skipped to avoid
        creating a unilateral directional position.
        """
        if not markets:
            return set(), {}

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
                self._emit_seed(markets[i].condition_id, 0.0, 0.0, 0.0, False, "exception")
                continue
            spent, ok, cid, yes_cost, no_cost = res
            if spent > 0 or (ok and (yes_cost > 0 or no_cost > 0)):
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
        """Seed YES + NO for one market — ATOMIC pair.

        Returns (total_usd_spent, success, yes_cost, no_cost).
        success=True iff BOTH sides either already had inventory or were
        seeded; False otherwise.  yes_cost/no_cost are the USD actually
        spent per side (used by the caller to initialise cost basis).

        Atomicity (Issue 1):
          1. Pre-check BOTH sides up front (token IDs, mid, per-side cost
             estimate vs cap, order-book depth).  If either side cannot
             be seeded, NEITHER side is posted: returns (0, False, 0, 0)
             with a clear warning log — no unilateral position is taken.
          2. Sides are seeded SEQUENTIALLY (YES first, then NO).  If YES
             acquires zero shares, NO is skipped entirely so we never
             enter a one-sided directional position from the seeder.
          3. If YES partially fills and NO fails at runtime the partial
             YES position is still tracked (cost basis recorded) and the
             market is reported in failed_cids; quoting is excluded so
             the residual is liquidated by the next maintenance cycle.
        """
        if not market.token_id_yes or not market.token_id_no:
            self._emit_seed(market.condition_id, 0.0, 0.0, 0.0, False, "no_tokens")
            return 0.0, False, 0.0, 0.0

        if market.condition_id in self._block_reseed:
            logger.info(
                "Seed skip {} — one-sided rebalance this cycle (no re-seed haircut)",
                market.condition_id[:12],
            )
            self._emit_seed(
                market.condition_id, 0.0, 0.0, 0.0, True, "rebalance_onesided_skip",
            )
            return 0.0, True, 0.0, 0.0

        mid = await self._fetch_mid(market.token_id_yes)
        if mid is None:
            logger.warning(
                "WARNING | seeder | Market {} skipped — atomic seed failed "
                "(midpoint unavailable). No position taken.",
                market.condition_id[:12],
            )
            cap = min(self._s.SEED_MAX_USD_PER_SIDE, budget_remaining / 2)
            self._emit_seed(market.condition_id, 0.0, 0.0, cap, False, "no_mid")
            return 0.0, False, 0.0, 0.0

        tick = _TICK_MAP.get(market.tick_size, 0.01)
        per_side_budget = min(
            self._s.SEED_MAX_USD_PER_SIDE,
            budget_remaining / 2,
        )

        # ── Atomic pre-check (Issue 1): cost + book depth on BOTH sides ──
        yes_mid = mid
        no_mid = round(1.0 - mid, 4)

        # Per-side estimate uses the WORST-CASE attempt price (final retry
        # in `_seed_one_side`: extra_ticks = SEED_AGGRESSIVE_TICKS +
        # 2 × (SEED_MAX_ATTEMPTS - 1)).  Using attempt-1 alone would let
        # the runtime cost-check abort the NO side AFTER YES already filled,
        # re-creating the unilateral-position bug this function exists to
        # prevent.  Probing the worst case ensures every retry fits the cap.
        worst_extra_ticks = (
            self._s.SEED_AGGRESSIVE_TICKS + 2 * (SEED_MAX_ATTEMPTS - 1)
        )
        yes_est_price = max(0.01, min(0.99,
            round(yes_mid + worst_extra_ticks * tick, 4)))
        no_est_price = max(0.01, min(0.99,
            round(no_mid + worst_extra_ticks * tick, 4)))

        # Matched shares: N YES + N NO (not dollar-neutral independent sizes).
        # Cap binds on the expensive side; cheap side spends less USD.
        min_needed = self._min_seed_shares(market)
        n = self._matched_seed_n(
            market, yes_mid, no_mid, yes_est_price, no_est_price, per_side_budget,
        )
        if n < min_needed:
            logger.warning(
                "WARNING | seeder | Market {} skipped — atomic seed failed "
                "(matched N={:.1f} < min={:.1f}, cap=${:.2f}, "
                "expensive≈${:.4f}). No position taken.",
                market.condition_id[:12], n, min_needed, per_side_budget,
                max(yes_est_price, no_est_price),
            )
            self._emit_seed(
                market.condition_id, 0.0, 0.0, per_side_budget,
                False, "cost_cap",
            )
            return 0.0, False, 0.0, 0.0

        yes_target = n
        no_target = n

        if self._s.DRY_RUN and self._paper is not None:
            bal_yes = self._paper.inventory_size(market.token_id_yes)
            bal_no = self._paper.inventory_size(market.token_id_no)
        else:
            bal_yes = await self._client.get_conditional_balance(market.token_id_yes)
            bal_no = await self._client.get_conditional_balance(market.token_id_no)

        # One-sided leftover at/under the unmatched cap (post-rebalance keep-50):
        # do not top-up with a fresh N/N seed (haircut). Over-cap one-sided
        # (e.g. 200/0) still hits preflight → fail. Virgin 0/0 still seeds.
        unmatched_now = abs(float(bal_yes) - float(bal_no))
        onesided_now = (
            (float(bal_yes) <= 1e-9 and float(bal_no) > 1e-9)
            or (float(bal_no) <= 1e-9 and float(bal_yes) > 1e-9)
        )
        if onesided_now and (
            market.condition_id in self._block_reseed
            or unmatched_now <= float(self._s.RESOLUTION_MAX_LOSS_USD) + 1e-12
        ):
            logger.info(
                "Seed skip {} — one-sided leftover YES={:.1f} NO={:.1f} "
                "(no re-seed haircut)",
                market.condition_id[:12], float(bal_yes), float(bal_no),
            )
            self._emit_seed(
                market.condition_id, 0.0, 0.0, per_side_budget,
                True, "rebalance_onesided_skip",
            )
            return 0.0, True, 0.0, 0.0

        # Ending sizes after buying up to N on each side (never sell-down here).
        planned_yes = max(float(bal_yes), n)
        planned_no = max(float(bal_no), n)
        planned_unmatched = abs(planned_yes - planned_no)
        # Preflight: live unmatched + extreme mid only. Do NOT honor sticky
        # max_loss from resolution_delist.json (dollar-seed false positives).
        guard = self._resolution_guard or ResolutionGuard(self._s)
        breached, max_loss, limit = guard.max_loss_check(
            market.condition_id, planned_yes, planned_no,
        )
        extreme_mid = yes_mid < 0.05 or yes_mid > 0.95
        if breached or extreme_mid:
            reason = "extreme_mid" if extreme_mid else "max_loss"
            logger.warning(
                "WARNING | seeder | Market {} skipped — preflight {} "
                "(n={:.0f}, yes_cost≈${:.2f}, no_cost≈${:.2f}, "
                "unmatched={:.0f}, max_loss=${:.2f} limit=${:.2f}). "
                "No position taken.",
                market.condition_id[:12], reason, n,
                n * yes_est_price, n * no_est_price,
                planned_unmatched, max_loss, limit,
            )
            self._emit_seed(
                market.condition_id, 0.0, 0.0, per_side_budget,
                False, f"preflight_{reason}",
            )
            return 0.0, False, 0.0, 0.0

        logger.info(
            "Seeder preflight {} — n={:.0f} yes_cost≈${:.2f} no_cost≈${:.2f} "
            "unmatched={:.0f} (limit ${:.2f})",
            market.condition_id[:12], n,
            n * yes_est_price, n * no_est_price,
            planned_unmatched, limit,
        )

        if bal_yes >= yes_target and bal_no >= no_target:
            logger.debug(
                "Seed skip {} — both sides already hold ≥ matched N "
                "(YES>={:.1f}, NO>={:.1f})",
                market.condition_id[:12], yes_target, no_target,
            )
            self._emit_seed(
                market.condition_id, 0.0, 0.0, per_side_budget, True, "already_seeded",
            )
            return 0.0, True, 0.0, 0.0

        yes_deficit = max(0.0, math.ceil(yes_target - bal_yes))
        no_deficit = max(0.0, math.ceil(no_target - bal_no))

        # Probe books for both sides BEFORE posting anything.
        yes_has_liq, _yba = await self._check_book_depth(
            market.token_id_yes, "asks", max(yes_deficit, min_needed),
        )
        no_has_liq, _nba = await self._check_book_depth(
            market.token_id_no, "asks", max(no_deficit, min_needed),
        )
        if not (yes_has_liq and no_has_liq):
            missing = []
            if not yes_has_liq:
                missing.append("YES")
            if not no_has_liq:
                missing.append("NO")
            logger.warning(
                "WARNING | seeder | Market {} skipped — atomic seed failed "
                "(orderbook empty on side(s): {}). No position taken.",
                market.condition_id[:12], "+".join(missing),
            )
            self._emit_seed(
                market.condition_id, 0.0, 0.0, per_side_budget, False, "book_empty",
            )
            return 0.0, False, 0.0, 0.0

        # ── Sequential execution (atomicity at runtime) ──
        # YES first.  If it acquires zero shares NO is skipped to keep the
        # invariant intact.  If YES partially fills, we still try NO so the
        # imbalance is at most one side's partial — better than full one-sided.
        try:
            yes_cost = await self._seed_one_side(
                market=market,
                label="YES",
                token_id=market.token_id_yes,
                token_mid=yes_mid,
                tick=tick,
                max_cost=per_side_budget,
                target=yes_target,
            )
        except Exception as exc:
            logger.error(
                "WARNING | seeder | Market {} — YES side runtime error ({}). "
                "Aborting NO side, no position taken.",
                market.condition_id[:12], exc,
            )
            self._emit_seed(
                market.condition_id, 0.0, 0.0, per_side_budget, False, "yes_runtime",
            )
            return 0.0, False, 0.0, 0.0

        if yes_cost is None:
            logger.warning(
                "WARNING | seeder | Market {} skipped — atomic seed failed "
                "(YES side runtime failure: 0 shares acquired). NO side aborted.",
                market.condition_id[:12],
            )
            self._emit_seed(
                market.condition_id, 0.0, 0.0, per_side_budget, False, "yes_empty",
            )
            return 0.0, False, 0.0, 0.0

        try:
            no_cost = await self._seed_one_side(
                market=market,
                label="NO",
                token_id=market.token_id_no,
                token_mid=no_mid,
                tick=tick,
                max_cost=per_side_budget,
                target=no_target,
            )
        except Exception as exc:
            logger.error(
                "WARNING | seeder | Market {} — NO side runtime error after YES "
                "was seeded (${:.2f}). Partial position retained, market excluded "
                "from quoting: {}",
                market.condition_id[:12], yes_cost, exc,
            )
            self._emit_seed(
                market.condition_id, yes_cost, 0.0, per_side_budget, False, "no_runtime",
            )
            return yes_cost, False, yes_cost, 0.0

        if no_cost is None:
            logger.warning(
                "WARNING | seeder | Market {} — atomic seed FAILED at runtime "
                "(YES_cost=${:.2f} acquired, NO=0 shares). Market excluded from "
                "quoting; the partial YES position will be liquidated by the next "
                "maintenance cycle.",
                market.condition_id[:12], yes_cost,
            )
            self._emit_seed(
                market.condition_id, yes_cost, 0.0, per_side_budget, False, "no_empty",
            )
            return yes_cost, False, yes_cost, 0.0

        if self._s.DRY_RUN and self._paper is not None:
            yes_px = (yes_cost / yes_deficit) if yes_deficit > 0 else 0.0
            no_px = (no_cost / no_deficit) if no_deficit > 0 else 0.0
            credited = self._paper.seed_market(
                market.condition_id,
                market.token_id_yes,
                market.token_id_no,
                yes_deficit,
                no_deficit,
                yes_px,
                no_px,
            )
            if credited is None:
                logger.warning(
                    "WARNING | seeder | Market {} skipped — paper cash short "
                    "for atomic seed (YES=${:.2f}, NO=${:.2f}).",
                    market.condition_id[:12], yes_cost, no_cost,
                )
                self._emit_seed(
                    market.condition_id, yes_cost, no_cost, per_side_budget,
                    False, "paper_cash",
                )
                return 0.0, False, 0.0, 0.0
            yes_cost, no_cost = credited

        logger.info(
            "Seeded {} — YES ${:.2f} ({:.0f} sh) + NO ${:.2f} ({:.0f} sh) "
            "| matched N={:.0f} | alloc=${:.0f}",
            market.condition_id[:12],
            yes_cost, yes_deficit, no_cost, no_deficit,
            n, market.allocated_capital,
        )
        reason = "would_seed" if self._s.DRY_RUN else "seeded"
        self._emit_seed(
            market.condition_id, yes_cost, no_cost, per_side_budget, True, reason,
        )
        return yes_cost + no_cost, True, yes_cost, no_cost

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
        target: Optional[float] = None,
    ) -> Optional[float]:
        """Ensure we hold enough tokens to post ASK at the planned size.

        Uses GTC limit orders with a timeout: post at aggressive price, wait
        for fills, cancel remaining.  Retries with escalating aggressiveness.

        Returns USD cost on success, None on failure.
        """
        if self._s.DRY_RUN and self._paper is not None:
            initial_balance = self._paper.inventory_size(token_id)
        else:
            initial_balance = await self._client.get_conditional_balance(token_id)
        if target is None:
            target = self._min_seed_shares(market)

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
            price = max(0.01, min(0.99, price))
            cost = float(deficit) * price
            logger.info(
                "[DRY RUN] Would seed {} {} — {} shares @ ${:.4f} (${:.2f})",
                market.condition_id[:12],
                label,
                deficit,
                price,
                cost,
            )
            return cost

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
                affordable = int(math.floor(effective_cap / max(aggressive_price, 0.01)))
                if affordable < 1:
                    logger.warning(
                        "Seed skip {} {} — est. ${:.2f} > cap ${:.2f}",
                        market.condition_id[:12],
                        label,
                        estimated_cost,
                        effective_cap,
                    )
                    return None
                remaining = affordable
                estimated_cost = aggressive_price * remaining

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

    async def flatten_market(
        self,
        market: MarketCandidate,
        yes_mid: float,
        inventory: Any,
    ) -> Dict[str, float]:
        """Sell all YES+NO inventory for a cid (resolution delist exit).

        DRY_RUN: paper-sell at mid, credit cash, zero paper + tracker.
        Live: aggressive GTC sells (same math; does not flip DRY_RUN).
        Returns sizes/proceeds dict for logging/tape.
        """
        cid = market.condition_id
        yes_mid = max(0.01, min(0.99, float(yes_mid)))
        no_mid = round(1.0 - yes_mid, 4)
        tick = _TICK_MAP.get(market.tick_size, 0.01)

        if self._s.DRY_RUN and self._paper is not None:
            result = self._paper.flatten_market(
                cid,
                market.token_id_yes,
                market.token_id_no,
                yes_mid,
                no_mid,
            )
        else:
            yes_sz = 0.0
            no_sz = 0.0
            if inventory is not None:
                pos = inventory.get_position(cid)
                if pos is not None:
                    yes_sz = float(pos.yes_size)
                    no_sz = float(pos.no_size)
            if not self._s.DRY_RUN:
                try:
                    if market.token_id_yes:
                        yes_sz = max(
                            yes_sz,
                            await self._client.get_conditional_balance(market.token_id_yes),
                        )
                    if market.token_id_no:
                        no_sz = max(
                            no_sz,
                            await self._client.get_conditional_balance(market.token_id_no),
                        )
                except Exception as exc:
                    logger.warning(
                        "Flatten {} balance fetch failed: {}", cid[:12], exc,
                    )
            yes_proceeds = await self._flatten_sell_side(
                market.token_id_yes, "YES", yes_sz, yes_mid, tick, cid,
            ) if yes_sz > 1e-9 and market.token_id_yes else 0.0
            no_proceeds = await self._flatten_sell_side(
                market.token_id_no, "NO", no_sz, no_mid, tick, cid,
            ) if no_sz > 1e-9 and market.token_id_no else 0.0
            result = {
                "yes_size": yes_sz,
                "no_size": no_sz,
                "yes_proceeds": yes_proceeds,
                "no_proceeds": no_proceeds,
            }

        if inventory is not None and hasattr(inventory, "flatten_position"):
            inventory.flatten_position(cid, yes_mid, no_mid)

        yes_sz = float(result.get("yes_size") or 0.0)
        no_sz = float(result.get("no_size") or 0.0)
        yes_proceeds = float(result.get("yes_proceeds") or 0.0)
        no_proceeds = float(result.get("no_proceeds") or 0.0)
        logger.info(
            "Flatten {} — sold YES={:.1f} (${:.2f}) NO={:.1f} (${:.2f}) dry_run={}",
            cid[:12], yes_sz, yes_proceeds, no_sz, no_proceeds, self._s.DRY_RUN,
        )
        try:
            if self._tape is not None:
                self._tape.skip(
                    cid,
                    "resolution_flatten",
                    yes_size=yes_sz,
                    no_size=no_sz,
                    yes_proceeds=yes_proceeds,
                    no_proceeds=no_proceeds,
                    dry_run=bool(self._s.DRY_RUN),
                )
        except Exception as exc:
            logger.warning("Tape flatten emit failed: {}", exc)
        return result

    async def rebalance_unmatched(
        self,
        market: MarketCandidate,
        yes_mid: float,
        inventory: Any,
    ) -> Dict[str, Any]:
        """Sell heavy surplus until unmatched <= RESOLUTION_MAX_LOSS_USD.

        Never wipe a 0/N book to 0/0 — leave up to ``limit`` on the heavy side.
        DRY_RUN: paper-sell at mid. Live: aggressive GTC (same ticks as flatten).
        Tapes ``rebalance_surplus`` — never ``resolution_flatten``.
        """
        cid = market.condition_id
        yes_mid = max(0.01, min(0.99, float(yes_mid)))
        no_mid = round(1.0 - yes_mid, 4)
        tick = _TICK_MAP.get(market.tick_size, 0.01)
        limit = float(self._s.RESOLUTION_MAX_LOSS_USD)

        yes_before = 0.0
        no_before = 0.0
        if inventory is not None:
            pos = inventory.get_position(cid)
            if pos is not None:
                yes_before = float(pos.yes_size)
                no_before = float(pos.no_size)

        surplus = abs(yes_before - no_before)
        sell_target = max(0.0, surplus - limit)
        heavy = "YES" if yes_before > no_before else (
            "NO" if no_before > yes_before else ""
        )
        onesided_before = min(yes_before, no_before) <= 1e-9 and surplus > limit + 1e-9

        if self._s.DRY_RUN and self._paper is not None:
            result = self._paper.rebalance_unmatched(
                cid,
                market.token_id_yes,
                market.token_id_no,
                yes_mid,
                no_mid,
                limit=limit,
            )
            sold = float(result.get("sold") or 0.0)
            heavy = str(result.get("heavy") or heavy)
            surplus = float(result.get("surplus") or surplus)
            if inventory is not None and sold > 1e-12 and heavy:
                px = yes_mid if heavy == "YES" else no_mid
                inventory.reduce_position(cid, heavy, sold, px)
        else:
            sell_tid = (
                market.token_id_yes if heavy == "YES" else market.token_id_no
            )
            sell_mid = yes_mid if heavy == "YES" else no_mid
            sold = 0.0
            proceeds = 0.0
            if heavy and sell_target > 1e-9 and sell_tid:
                if self._s.DRY_RUN:
                    sold = sell_target
                    proceeds = sell_target * sell_mid
                else:
                    try:
                        bal0 = await self._client.get_conditional_balance(sell_tid)
                    except Exception:
                        bal0 = sell_target
                    proceeds = await self._flatten_sell_side(
                        sell_tid, heavy, sell_target, sell_mid, tick, cid,
                    )
                    try:
                        bal1 = await self._client.get_conditional_balance(sell_tid)
                        sold = max(0.0, float(bal0) - float(bal1))
                    except Exception:
                        sold = proceeds / max(sell_mid, 0.01) if proceeds else 0.0
                if inventory is not None and sold > 1e-12:
                    inventory.reduce_position(cid, heavy, sold, sell_mid)
            result = {
                "heavy": heavy,
                "surplus": surplus,
                "sold": sold,
                "yes_proceeds": proceeds if heavy == "YES" else 0.0,
                "no_proceeds": proceeds if heavy == "NO" else 0.0,
            }

        yes_after = yes_before
        no_after = no_before
        if inventory is not None:
            pos = inventory.get_position(cid)
            if pos is not None:
                yes_after = float(pos.yes_size)
                no_after = float(pos.no_size)
        elif self._paper is not None:
            yes_after = float(result.get("yes_after") or 0.0)
            no_after = float(result.get("no_after") or 0.0)

        if onesided_before:
            self.note_onesided_rebalance(cid)

        logger.info(
            "Rebalance surplus {} — heavy={} surplus={:.1f} sold={:.1f} "
            "YES {:.1f}→{:.1f} NO {:.1f}→{:.1f} limit={:.0f} dry_run={}",
            cid[:12], heavy or "—", surplus, float(result.get("sold") or 0.0),
            yes_before, yes_after, no_before, no_after, limit, self._s.DRY_RUN,
        )
        try:
            if self._tape is not None:
                self._tape.skip(
                    cid,
                    "rebalance_surplus",
                    heavy=heavy,
                    surplus=surplus,
                    yes_before=yes_before,
                    no_before=no_before,
                    yes_after=yes_after,
                    no_after=no_after,
                    dry_run=bool(self._s.DRY_RUN),
                )
        except Exception as exc:
            logger.warning("Tape rebalance_surplus emit failed: {}", exc)
        return {
            "heavy": heavy,
            "surplus": surplus,
            "sold": float(result.get("sold") or 0.0),
            "yes_before": yes_before,
            "no_before": no_before,
            "yes_after": yes_after,
            "no_after": no_after,
            "yes_proceeds": float(result.get("yes_proceeds") or 0.0),
            "no_proceeds": float(result.get("no_proceeds") or 0.0),
        }

    async def _flatten_sell_side(
        self,
        token_id: str,
        label: str,
        size: float,
        mid: float,
        tick: float,
        cid: str,
    ) -> float:
        """Live GTC sell of ``size`` shares. Returns estimated USD proceeds."""
        if self._s.DRY_RUN:
            return float(size) * float(mid)
        remaining = int(math.floor(float(size)))
        if remaining < 1:
            return 0.0
        proceeds = 0.0
        for attempt in range(1, FLATTEN_MAX_ATTEMPTS + 1):
            try:
                bal = await self._client.get_conditional_balance(token_id)
            except Exception as exc:
                logger.warning(
                    "Flatten {} {} balance error: {}", cid[:12], label, exc,
                )
                break
            remaining = int(math.floor(bal))
            if remaining < 1:
                break
            discount_ticks = 1 + (attempt - 1) * 2
            sell_price = round(max(0.01, mid - discount_ticks * tick), 4)
            order_id: Optional[str] = None
            try:
                order_args = OrderArgs(
                    token_id=token_id,
                    price=sell_price,
                    size=remaining,
                    side=SELL,
                )
                resp = await self._client.create_and_post_order(
                    order_args, order_type=OrderType.GTC,
                )
                if isinstance(resp, dict):
                    order_id = resp.get("orderID", "") or resp.get("id", "")
                await asyncio.sleep(FLATTEN_TIMEOUT_SECONDS)
            except Exception as exc:
                logger.error(
                    "Flatten {} {} sell failed: {}", cid[:12], label, exc,
                )
            finally:
                if order_id:
                    try:
                        await self._client.cancel(order_id)
                    except Exception:
                        pass
            try:
                new_bal = await self._client.get_conditional_balance(token_id)
            except Exception:
                break
            filled = max(0.0, float(remaining) - float(new_bal))
            if filled > 0.01:
                proceeds += filled * sell_price
        return proceeds
