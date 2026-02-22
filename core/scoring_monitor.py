"""
Scoring monitor — checks if our orders are earning LP rewards.

Uses the SDK's ``are_orders_scoring`` endpoint to verify that our orders
on each market meet the reward criteria (spread within rewardsMaxSpread,
size >= rewardsMinSize, two-sided).

Non-blocking: called each cycle but only runs the actual check if
SCORING_CHECK_INTERVAL_SECONDS has elapsed since the last check.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import TYPE_CHECKING, Dict, List, Set

from loguru import logger

from config.markets_filter import MarketCandidate
from config.settings import Settings
from core.client import AsyncClobClient
from utils.discord import DiscordNotifier

if TYPE_CHECKING:
    from core.order_manager import OrderManager


class ScoringMonitor:
    """Periodic scoring health check across all active markets.

    After SCORING_REMEDIATION_THRESHOLD consecutive non-scoring checks,
    the affected condition_ids are returned so the main loop can trigger
    a force cancel-and-replace to fix stale/invalid orders.
    """

    def __init__(
        self,
        client: AsyncClobClient,
        settings: Settings,
        discord: DiscordNotifier,
        order_manager: "OrderManager | None" = None,
    ) -> None:
        self._client = client
        self._s = settings
        self._discord = discord
        self._order_mgr = order_manager
        self._last_check: float = 0.0
        self._last_alert: float = 0.0
        self._consecutive_not_scoring: Dict[str, int] = defaultdict(int)
        self._last_ratio: float = 1.0
        self._remediate_cids: Set[str] = set()
        # Per-market scoring rate: tracks (checks_scoring, total_checks)
        self._scoring_history: Dict[str, List[bool]] = defaultdict(list)
        self._last_metrics_log: float = 0.0

    @property
    def last_ratio(self) -> float:
        return self._last_ratio

    async def check_if_due(self, markets: List[MarketCandidate]) -> Set[str]:
        """Run scoring check only if enough time has elapsed.

        Returns set of condition_ids that need force-replace remediation.
        """
        now = time.monotonic()
        if now - self._last_check < self._s.SCORING_CHECK_INTERVAL_SECONDS:
            return set()
        self._last_check = now
        return await self._run_check(markets)

    async def _run_check(self, markets: List[MarketCandidate]) -> Set[str]:
        """Query are_orders_scoring for each market and report results."""
        self._remediate_cids.clear()

        if not markets:
            return set()

        scoring_count = 0
        checked = 0
        threshold = self._s.SCORING_REMEDIATION_THRESHOLD

        for market in markets:
            if not market.condition_id:
                continue

            order_ids: List[str] = []
            if self._order_mgr is not None:
                order_ids = self._order_mgr.get_order_ids_for_condition(market.condition_id)
            if not order_ids:
                continue

            try:
                result = await self._client.are_orders_scoring(order_ids)
                is_scoring = bool(result) and any(result.values())
                checked += 1

                history = self._scoring_history[market.condition_id]
                history.append(is_scoring)
                if len(history) > 20:
                    self._scoring_history[market.condition_id] = history[-20:]

                if is_scoring:
                    scoring_count += 1
                    self._consecutive_not_scoring[market.condition_id] = 0
                else:
                    count = self._consecutive_not_scoring[market.condition_id] + 1
                    self._consecutive_not_scoring[market.condition_id] = count
                    if count >= threshold:
                        self._remediate_cids.add(market.condition_id)
                        if count == threshold:
                            logger.warning(
                                "Market {} not scoring for {} checks — triggering remediation",
                                market.condition_id[:12],
                                count,
                            )
            except Exception as exc:
                logger.warning("are_orders_scoring failed for {}: {}", market.condition_id[:12], exc)

        if checked == 0:
            logger.warning("Scoring check: 0 markets checked (all calls failed)")
            return set()

        ratio = scoring_count / checked
        self._last_ratio = ratio
        logger.info("Scoring: {}/{} markets ({:.0%})", scoring_count, checked, ratio)

        if self._remediate_cids:
            logger.info(
                "Scoring remediation needed for {} market(s)",
                len(self._remediate_cids),
            )

        if ratio < self._s.SCORING_ALERT_THRESHOLD:
            now = time.monotonic()
            cooldown = self._s.SKEW_ALERT_COOLDOWN_SECONDS
            if now - self._last_alert >= cooldown:
                self._last_alert = now
                await self._discord.send_alert(
                    title="Low Scoring Alert",
                    message=(
                        f"Only **{scoring_count}/{checked}** markets scoring "
                        f"(**{ratio:.0%}**) — below {self._s.SCORING_ALERT_THRESHOLD:.0%} threshold"
                    ),
                    level="warning",
                )

        # Log per-market metrics every check
        self._log_per_market_metrics()

        return set(self._remediate_cids)

    def get_scoring_rate(self, condition_id: str) -> float:
        """Return the scoring rate [0.0, 1.0] for a market based on recent history."""
        history = self._scoring_history.get(condition_id, [])
        if not history:
            return 0.0
        return sum(1 for x in history if x) / len(history)

    def get_all_scoring_rates(self) -> Dict[str, float]:
        """Return scoring rates for all tracked markets."""
        return {
            cid: self.get_scoring_rate(cid)
            for cid in self._scoring_history
        }

    def _log_per_market_metrics(self) -> None:
        """Log per-market scoring rates for observability (Batch 5a requirement)."""
        rates = self.get_all_scoring_rates()
        if not rates:
            return
        for cid, rate in sorted(rates.items(), key=lambda x: x[1]):
            logger.info(
                "Market metric: {} scoring_rate={:.0%} consecutive_not_scoring={}",
                cid[:12], rate, self._consecutive_not_scoring.get(cid, 0),
            )
