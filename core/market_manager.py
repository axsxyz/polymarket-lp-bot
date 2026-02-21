"""
Market discovery, filtering, caching, and dynamic capital allocation.

Flow:
  1. Fetch all reward-eligible markets from the CLOB via
     `get_sampling_simplified_markets()` (paginated).
  2. Enrich each market with Gamma API metadata (end_date, tags, 24h volume)
     so the filter in `markets_filter.py` can apply time/tag/volume rules.
  3. Filter + rank → top N markets.
  4. Allocate capital using a weighted scheme: top-15 markets by reward_rate
     get 20-30% more capital than the rest (within per-market and global caps).
  5. Cache to `data/selected_markets.json` for crash recovery.

Refresh cadence: every 15 minutes (called by the main loop scheduler).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
from loguru import logger

from config.markets_filter import (
    MarketCandidate,
    filter_and_rank,
    parse_clob_simplified_market,
    parse_gamma_market,
)
from config.settings import Settings
from core.client import AsyncClobClient

CACHE_PATH = Path("data/selected_markets.json")
TOP_TIER_COUNT = 15
TOP_TIER_BOOST = 1.25  # top markets get 25% more capital


class MarketManager:
    """Owns the list of active markets and their capital allocations."""

    def __init__(self, client: AsyncClobClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings
        self._markets: List[MarketCandidate] = []
        self._last_refresh: Optional[datetime] = None

    @property
    def markets(self) -> List[MarketCandidate]:
        return list(self._markets)

    @property
    def market_count(self) -> int:
        return len(self._markets)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def refresh(self) -> List[MarketCandidate]:
        """Full refresh: fetch → enrich → filter → allocate → cache."""
        logger.info("Refreshing market universe …")

        clob_markets = await self._fetch_clob_sampling_markets()
        logger.info("Fetched {} reward-eligible markets from CLOB", len(clob_markets))

        enriched = await self._enrich_with_gamma(clob_markets)
        logger.info("Enriched {} markets with Gamma metadata", len(enriched))

        ranked = filter_and_rank(enriched, self._settings)
        logger.info(
            "After filtering: {} markets (target {}-{})",
            len(ranked),
            self._settings.MIN_MARKETS,
            self._settings.MAX_MARKETS,
        )

        if len(ranked) < self._settings.MIN_MARKETS:
            logger.warning(
                "Only {} markets passed filters (minimum {}). Relaxing will "
                "happen on next cycle if markets appear.",
                len(ranked),
                self._settings.MIN_MARKETS,
            )

        self._allocate_capital(ranked)
        self._markets = ranked
        self._last_refresh = datetime.now(timezone.utc)
        self._save_cache()

        return self._markets

    def get_market_by_condition(self, condition_id: str) -> Optional[MarketCandidate]:
        for m in self._markets:
            if m.condition_id == condition_id:
                return m
        return None

    # ------------------------------------------------------------------
    # CLOB fetch (paginated)
    # ------------------------------------------------------------------

    async def _fetch_clob_sampling_markets(self) -> List[MarketCandidate]:
        """Page through get_sampling_simplified_markets until exhausted."""
        all_markets: List[MarketCandidate] = []
        cursor = ""

        while True:
            try:
                resp = await self._client.get_sampling_simplified_markets(cursor)
            except Exception as exc:
                logger.error("CLOB sampling fetch failed: {}", exc)
                break

            data = resp.get("data", []) if isinstance(resp, dict) else []
            for raw in data:
                try:
                    all_markets.append(parse_clob_simplified_market(raw))
                except Exception as exc:
                    logger.debug("Skipping unparseable CLOB market: {}", exc)

            cursor = resp.get("next_cursor", "") if isinstance(resp, dict) else ""
            if not cursor or cursor == "LTE=":
                break

        return all_markets

    # ------------------------------------------------------------------
    # Gamma API enrichment
    # ------------------------------------------------------------------

    async def _enrich_with_gamma(
        self, candidates: List[MarketCandidate]
    ) -> List[MarketCandidate]:
        """
        Fetch extra metadata (end_date, tags, volume) from Gamma API and
        merge into existing candidates keyed by condition_id.
        """
        gamma_lookup = await self._fetch_gamma_markets()

        for m in candidates:
            gamma = gamma_lookup.get(m.condition_id)
            if gamma is None:
                continue
            if gamma.end_date is not None:
                m.end_date = gamma.end_date
            if gamma.tags:
                m.tags = gamma.tags
            if gamma.volume_24h > 0:
                m.volume_24h = gamma.volume_24h
            if gamma.question:
                m.question = gamma.question
            if gamma.tick_size:
                m.tick_size = gamma.tick_size

        return candidates

    async def _fetch_gamma_markets(self) -> Dict[str, MarketCandidate]:
        """Fetch Gamma /markets pages, parse each item, build a condition_id lookup."""
        lookup: Dict[str, MarketCandidate] = {}
        base = self._settings.GAMMA_API_URL.rstrip("/")
        offset = 0
        limit = 200
        total_items = 0
        parse_failures = 0

        try:
            async with aiohttp.ClientSession() as session:
                while True:
                    url = f"{base}/markets?active=true&closed=false&limit={limit}&offset={offset}"
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=40)) as resp:
                        if resp.status != 200:
                            logger.warning("Gamma API returned status {}", resp.status)
                            break
                        data = await resp.json()

                    items: list = data if isinstance(data, list) else (data.get("data", data) or [])
                    if not isinstance(items, list) or not items:
                        if offset == 0:
                            logger.warning("Gamma API returned empty list on first page")
                        break

                    page_added = 0
                    for raw in items:
                        total_items += 1
                        try:
                            mc = parse_gamma_market(raw)
                            if mc.condition_id:
                                lookup[mc.condition_id] = mc
                                page_added += 1
                        except Exception:
                            parse_failures += 1

                    if offset == 0 and page_added == 0 and items:
                        logger.debug(
                            "Gamma first page had {} items but none parsed successfully",
                            len(items),
                        )

                    if len(items) < limit:
                        break
                    offset += limit

            logger.info(
                "Gamma lookup built with {} entries (fetched {}, parse failures {})",
                len(lookup),
                total_items,
                parse_failures,
            )

        except Exception as exc:
            logger.error("Gamma API fetch completely failed: {}", exc)

        return lookup

    # ------------------------------------------------------------------
    # Capital allocation
    # ------------------------------------------------------------------

    def _allocate_capital(self, markets: List[MarketCandidate]) -> None:
        """
        Weighted capital distribution:
        - Top TOP_TIER_COUNT markets by score get TOP_TIER_BOOST weight
        - Remaining markets get weight 1.0
        - Each market is capped at MAX_CAPITAL_PER_MARKET
        - Total never exceeds TOTAL_CAPITAL
        """
        if not markets:
            return

        n = len(markets)
        weights = [
            TOP_TIER_BOOST if i < TOP_TIER_COUNT else 1.0
            for i in range(n)
        ]
        total_weight = sum(weights)
        budget = self._settings.TOTAL_CAPITAL

        for i, m in enumerate(markets):
            raw_alloc = (weights[i] / total_weight) * budget
            m.allocated_capital = min(
                max(raw_alloc, self._settings.MIN_CAPITAL_PER_MARKET),
                self._settings.MAX_CAPITAL_PER_MARKET,
            )

        # Ensure we don't exceed global budget
        total_allocated = sum(m.allocated_capital for m in markets)
        if total_allocated > budget:
            scale = budget / total_allocated
            for m in markets:
                m.allocated_capital *= scale

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    def _save_cache(self) -> None:
        try:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload = [
                {
                    "condition_id": m.condition_id,
                    "question": m.question,
                    "token_id_yes": m.token_id_yes,
                    "token_id_no": m.token_id_no,
                    "max_spread": m.max_spread,
                    "min_size": m.min_size,
                    "reward_rate_daily": m.reward_rate_daily,
                    "allocated_capital": round(m.allocated_capital, 2),
                    "score": round(m.score, 6),
                    "midpoint": m.midpoint,
                    "end_date": m.end_date.isoformat() if m.end_date else None,
                    "tags": m.tags,
                    "neg_risk": m.neg_risk,
                    "tick_size": m.tick_size,
                }
                for m in self._markets
            ]
            CACHE_PATH.write_text(json.dumps(payload, indent=2))
            logger.debug("Cached {} markets to {}", len(payload), CACHE_PATH)
        except Exception as exc:
            logger.warning("Failed to write market cache: {}", exc)

    def load_cache(self) -> bool:
        """Load markets from cache file. Returns True if successful."""
        if not CACHE_PATH.exists():
            return False
        try:
            data = json.loads(CACHE_PATH.read_text())
            markets = []
            for raw in data:
                end_date = None
                if raw.get("end_date"):
                    try:
                        end_date = datetime.fromisoformat(raw["end_date"])
                        if end_date is not None and end_date.tzinfo is None:
                            end_date = end_date.replace(tzinfo=timezone.utc)
                    except (ValueError, TypeError):
                        pass
                markets.append(
                    MarketCandidate(
                        condition_id=raw["condition_id"],
                        question=raw.get("question", ""),
                        token_id_yes=raw.get("token_id_yes", ""),
                        token_id_no=raw.get("token_id_no", ""),
                        max_spread=raw.get("max_spread", 0),
                        min_size=raw.get("min_size", 0),
                        reward_rate_daily=raw.get("reward_rate_daily", 0),
                        allocated_capital=raw.get("allocated_capital", 0),
                        score=raw.get("score", 0),
                        midpoint=raw.get("midpoint", 0.5),
                        end_date=end_date,
                        tags=raw.get("tags", []),
                        neg_risk=raw.get("neg_risk", False),
                        tick_size=raw.get("tick_size", "0.01"),
                    )
                )
            self._markets = markets
            logger.info("Loaded {} markets from cache", len(markets))
            return True
        except Exception as exc:
            logger.warning("Failed to load market cache: {}", exc)
            return False
