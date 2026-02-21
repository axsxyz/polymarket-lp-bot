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
from typing import Dict, List

from loguru import logger

from config.markets_filter import MarketCandidate
from config.settings import Settings
from core.client import AsyncClobClient
from utils.discord import DiscordNotifier


class ScoringMonitor:
    """Periodic scoring health check across all active markets."""

    def __init__(
        self,
        client: AsyncClobClient,
        settings: Settings,
        discord: DiscordNotifier,
    ) -> None:
        self._client = client
        self._s = settings
        self._discord = discord
        self._last_check: float = 0.0
        self._last_alert: float = 0.0
        self._consecutive_not_scoring: Dict[str, int] = defaultdict(int)
        self._last_ratio: float = 1.0

    @property
    def last_ratio(self) -> float:
        return self._last_ratio

    async def check_if_due(self, markets: List[MarketCandidate]) -> None:
        """Run scoring check only if enough time has elapsed."""
        now = time.monotonic()
        if now - self._last_check < self._s.SCORING_CHECK_INTERVAL_SECONDS:
            return
        self._last_check = now
        await self._run_check(markets)

    async def _run_check(self, markets: List[MarketCandidate]) -> None:
        """Query are_orders_scoring for each market and report results."""
        if not markets:
            return

        scoring_count = 0
        checked = 0

        for market in markets:
            if not market.condition_id:
                continue
            try:
                is_scoring = await self._client.are_orders_scoring(
                    market.condition_id,
                )
                checked += 1
                if is_scoring:
                    scoring_count += 1
                    self._consecutive_not_scoring[market.condition_id] = 0
                else:
                    count = self._consecutive_not_scoring[market.condition_id] + 1
                    self._consecutive_not_scoring[market.condition_id] = count
                    if count >= 3:
                        logger.warning(
                            "Market {} not scoring for {} consecutive checks",
                            market.condition_id[:12],
                            count,
                        )
            except Exception as exc:
                logger.debug("are_orders_scoring failed for {}: {}", market.condition_id[:12], exc)

        if checked == 0:
            logger.warning("Scoring check: 0 markets checked (all calls failed)")
            return

        ratio = scoring_count / checked
        self._last_ratio = ratio
        logger.info("Scoring: {}/{} markets ({:.0%})", scoring_count, checked, ratio)

        if ratio < self._s.SCORING_ALERT_THRESHOLD:
            now = time.monotonic()
            cooldown = self._s.SKEW_ALERT_COOLDOWN_SECONDS  # reuse the same cooldown
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
