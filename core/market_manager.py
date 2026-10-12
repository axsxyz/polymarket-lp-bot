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
from enum import Enum
from pathlib import Path
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple

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

if TYPE_CHECKING:
    from utils.discord import DiscordNotifier

CACHE_PATH = Path("data/selected_markets.json")
TOP_TIER_COUNT = 15
TOP_TIER_BOOST = 1.25  # top markets get 25% more capital

# Gamma join: CLOB condition_ids ARE the fetch (no universe keyset/offset walk).
# Chunk size = Gamma page cap. Repeated ``condition_ids=`` query params (comma
# returns 0 — probed 2026-08-29).
GAMMA_PAGE_LIMIT = 100
GAMMA_CHUNK_SLEEP_S = 0.2  # mid of 0.15–0.3s between chunk requests
GAMMA_CHUNK_CONCURRENCY = 2
GAMMA_PAGE_RETRIES = 3
GAMMA_PAGE_RETRY_SLEEP_S = 0.8
# On deterministic 422 for a fat chunk, re-request at these sizes (in order).
GAMMA_422_SPLIT_SIZES = (50, 20, 1)


class MarketState(str, Enum):
    """Per-market operational state (Issue 4).

    Distinguishes WHY a market is or isn't producing scoring quotes so the
    cycle log, Discord alerts, and dynamic-realloc can react appropriately:

      - SEED_FAILED: never entered, no position acquired (atomic seed
        aborted). Capital is freely reallocated AWAY from this market.
      - ONE_SIDED:   actively quoting but only one side of the book
        (e.g. skew hard-pause). Excluded from Q-score and from realloc
        donations — the bot is mid-rebalance, do not pull its capital.
      - SCORING:     two-sided quotes posted, scoring confirmed.
        Realloc may BOOST allocation toward these markets.
      - INACTIVE:    selected but didn't post quotes this cycle (errors,
        midpoint-guard pause, spike pause, exposure gate, etc.). Capital
        can be reclaimed for re-allocation to scoring markets.
      - UNKNOWN:     default state before first cycle classification.
    """

    UNKNOWN = "UNKNOWN"
    SEED_FAILED = "SEED_FAILED"
    ONE_SIDED = "ONE_SIDED"
    SCORING = "SCORING"
    INACTIVE = "INACTIVE"


class MarketManager:
    """Owns the list of active markets and their capital allocations."""

    def __init__(
        self,
        client: AsyncClobClient,
        settings: Settings,
        discord: Optional["DiscordNotifier"] = None,
    ) -> None:
        self._client = client
        self._settings = settings
        self._discord = discord
        self._markets: List[MarketCandidate] = []
        self._token_index: Dict[str, MarketCandidate] = {}
        self._last_refresh: Optional[datetime] = None
        # Per-market operational state (Issue 4) — survives across cycles
        # so realloc can read it without recomputing from scratch.
        self._states: Dict[str, MarketState] = {}
        self._sticky_inactive: set[str] = set()
        self.last_went_in: List[MarketCandidate] = []
        self.last_went_out: List[MarketCandidate] = []
        self._tape: Any = None
        self._digest: Any = None
        self._last_gamma_status: Any = None
        self._last_gamma_pages: int = 0
        self._last_clob_pages: int = 0

    def set_tape(self, tape: Any) -> None:
        self._tape = tape

    def set_digest(self, digest: Any) -> None:
        self._digest = digest

    @property
    def markets(self) -> List[MarketCandidate]:
        return list(self._markets)

    @property
    def market_count(self) -> int:
        return len(self._markets)

    # ------------------------------------------------------------------
    # Market state (Issue 4)
    # ------------------------------------------------------------------

    def set_state(self, condition_id: str, state: MarketState, reason: str = "") -> None:
        """Record the latest operational state for a market.

        SEED_FAILED is sticky: once set by the seeder it remains until the
        next ``refresh()`` rebuilds the universe (the unseeded cid leaves
        the candidate list).  Resolution-delisted INACTIVE is also sticky
        (``mark_resolution_inactive``) so refresh cannot re-arm a delist.
        """
        prev = self._states.get(condition_id, MarketState.UNKNOWN)
        if (
            self._states.get(condition_id) == MarketState.SEED_FAILED
            and state != MarketState.SEED_FAILED
        ):
            return
        if (
            condition_id in getattr(self, "_sticky_inactive", set())
            and state != MarketState.INACTIVE
        ):
            return
        self._states[condition_id] = state
        if prev != state:
            self._emit_state(condition_id, prev.value, state.value, reason)

    def _emit_state(self, cid: str, from_state: str, to_state: str, reason: str) -> None:
        tape = getattr(self, "_tape", None)
        if tape is None:
            return
        try:
            tape.state(cid=cid, from_state=from_state, to_state=to_state, reason=reason)
        except Exception as exc:
            logger.warning("Tape state emit failed: {}", exc)

    def mark_resolution_inactive(self, condition_id: str) -> None:
        """INACTIVE that survives Step 7 reclassification and refresh."""
        if not hasattr(self, "_sticky_inactive"):
            self._sticky_inactive = set()
        prev = self._states.get(condition_id, MarketState.UNKNOWN)
        self._sticky_inactive.add(condition_id)
        self._states[condition_id] = MarketState.INACTIVE
        if prev != MarketState.INACTIVE:
            self._emit_state(
                condition_id, prev.value, MarketState.INACTIVE.value, "resolution",
            )

    def get_state(self, condition_id: str) -> MarketState:
        return self._states.get(condition_id, MarketState.UNKNOWN)

    def get_states_snapshot(self) -> Dict[str, MarketState]:
        """Read-only copy of the current state map (for logging/realloc)."""
        return dict(self._states)

    def cids_in_state(self, state: MarketState) -> List[str]:
        return [cid for cid, st in self._states.items() if st == state]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def refresh(self) -> List[MarketCandidate]:
        """Full refresh: fetch → enrich → filter → allocate → cache."""
        logger.info("Refreshing market universe …")

        # Snapshot previous selection for diff (boot: empty unless cache loaded)
        prev_selection: Dict[str, MarketCandidate] = {
            m.condition_id: m for m in self._markets
        }

        clob_markets = await self._fetch_clob_sampling_markets()
        logger.info("Fetched {} reward-eligible markets from CLOB", len(clob_markets))

        enriched = await self._enrich_with_gamma(clob_markets)
        logger.info("Enriched {} markets with Gamma metadata", len(enriched))

        filter_stats: Dict[str, Any] = {}
        ranked = filter_and_rank(enriched, self._settings, stats_out=filter_stats)
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
        self._rebuild_token_index()
        self._last_refresh = datetime.now(timezone.utc)
        self._save_cache()
        self._emit_universe_filter(len(clob_markets), ranked, filter_stats)
        self._log_capital_sizing(ranked, filter_stats)

        self._log_selection_details(prev_selection)

        # Sync CONDITIONAL (ERC1155) per token so the CLOB accepts ASK orders
        token_ids = list(
            {m.token_id_yes for m in ranked if m.token_id_yes}
            | {m.token_id_no for m in ranked if m.token_id_no}
        )
        await self._client.sync_conditional_allowances(token_ids)

        return self._markets

    def _emit_universe_filter(
        self,
        fetched: int,
        ranked: List[MarketCandidate],
        stats: Dict[str, Any],
    ) -> None:
        selected_rows: List[Dict[str, Any]] = []
        for i, m in enumerate(ranked, start=1):
            selected_rows.append({
                "cid": m.condition_id,
                "rank": i,
                "score": m.score,
                "question": m.question or "",
                "mid": m.midpoint,
                "spread": m.max_spread,
                "reward": m.reward_rate_daily,
                "min_size": m.min_size,
                "alloc": m.allocated_capital,
                "tags": list(m.tags[:6]) if m.tags else [],
            })
        rejection_counts = dict(stats.get("rejection_counts") or {})
        samples = dict(stats.get("samples") or {})
        ranked_n = int(stats.get("passed_count") or len(ranked))
        gamma_matched = int(getattr(self, "_last_gamma_matched", 0) or 0)
        gamma_status = getattr(self, "_last_gamma_status", None)
        pages = int(getattr(self, "_last_gamma_pages", 0) or 0)
        try:
            if getattr(self, "_tape", None) is not None:
                self._tape.universe(
                    fetched=fetched,
                    gamma_matched=gamma_matched,
                    gamma_status=gamma_status,
                    pages=pages,
                )
                self._tape.filter(
                    rejection_counts=rejection_counts,
                    samples=samples,
                    selected=selected_rows,
                    ranked=ranked_n,
                )
                limit = float(self._settings.RESOLUTION_MAX_LOSS_USD)
                for m in ranked:
                    if float(m.min_size) > limit + 1e-12:
                        self._tape.skip(
                            m.condition_id,
                            "min_size_gt_max_loss",
                            min_size=m.min_size,
                            limit=limit,
                        )
            if getattr(self, "_digest", None) is not None:
                self._digest.set_funnel(
                    fetched=fetched,
                    gamma_matched=gamma_matched,
                    gamma_status=gamma_status,
                    pages=pages,
                    rejection_counts=rejection_counts,
                    samples=samples,
                    selected=selected_rows,
                    ranked=ranked_n,
                )
        except Exception as exc:
            logger.warning("Tape universe/filter emit failed: {}", exc)

    def _log_selection_details(
        self, prev_selection: Dict[str, MarketCandidate]
    ) -> None:
        """Log each selected market in detail and emit IN/OUT diff vs the
        previous selection.

        - Always logs the full selected list as a clean fixed-width table
          (Issue 6) so the daily logs show selections at a glance instead
          of long key-value lines.
        - Computes IN/OUT diff against `prev_selection` (empty on cold boot).
        - Sends a Discord 'info' alert when the selection actually changed
          (skipped when there's no prior state, to avoid spam at startup).
        """
        new_cids = {m.condition_id for m in self._markets}

        logger.info("Selected markets ({}):", len(self._markets))
        # Header — widths chosen to keep typical market lines under 120 cols.
        header = (
            f"  {'Question':<40}  {'cid':<10}  "
            f"{'mid':>6}  {'spread':>7}  {'alloc':>8}  {'reward/day':>10}"
        )
        sep = "  " + "─" * (len(header) - 2)
        logger.info(header)
        logger.info(sep)
        for m in self._markets:
            q = (m.question or m.condition_id[:16])
            q = (q[:37] + "…") if len(q) > 40 else q
            logger.info(
                "  {:<40}  {:<10}  {:>6.3f}  {:>7.4f}  {:>7.2f}$  {:>9.2f}$",
                q, m.condition_id[:10], m.midpoint, m.max_spread,
                m.allocated_capital, m.reward_rate_daily,
            )
        logger.info(sep)

        if not prev_selection:
            return  # No prior state — skip diff and Discord notification

        went_in = [m for m in self._markets if m.condition_id not in prev_selection]
        went_out = [
            prev_selection[cid]
            for cid in prev_selection
            if cid not in new_cids
        ]
        self.last_went_in = list(went_in)
        self.last_went_out = list(went_out)

        for m in went_in:
            q = (m.question or "")[:50]
            logger.info("Market IN:  {} ({})", q, m.condition_id[:12])
        for m in went_out:
            q = (m.question or "")[:50]
            logger.info("Market OUT: {} ({})", q, m.condition_id[:12])

        if (went_in or went_out) and self._discord is not None:
            in_lines = [
                f"• {(m.question or '')[:60]} (`{m.condition_id[:10]}…`)"
                for m in went_in
            ]
            out_lines = [
                f"• {(m.question or '')[:60]} (`{m.condition_id[:10]}…`)"
                for m in went_out
            ]
            fields: List[Dict[str, str]] = []
            if in_lines:
                fields.append({
                    "name": f"IN ({len(in_lines)})",
                    "value": "\n".join(in_lines[:10]),
                    "inline": False,
                })
            if out_lines:
                fields.append({
                    "name": f"OUT ({len(out_lines)})",
                    "value": "\n".join(out_lines[:10]),
                    "inline": False,
                })
            asyncio.create_task(self._discord.send_alert(
                title="Market selection changed",
                message=(
                    f"Active markets: {len(self._markets)} "
                    f"(was {len(prev_selection)})"
                ),
                level="info",
                fields=fields,
            ))

    def get_market_by_condition(self, condition_id: str) -> Optional[MarketCandidate]:
        for m in self._markets:
            if m.condition_id == condition_id:
                return m
        return None

    def get_market_by_token(self, token_id: str) -> Optional[MarketCandidate]:
        """O(1) lookup by token_id (YES or NO). Rebuilt on each refresh."""
        return self._token_index.get(token_id)

    # ------------------------------------------------------------------
    # CLOB fetch (paginated)
    # ------------------------------------------------------------------

    async def _fetch_clob_sampling_markets(self) -> List[MarketCandidate]:
        """Page through get_sampling_simplified_markets until exhausted."""
        all_markets: List[MarketCandidate] = []
        cursor = ""
        pages = 0

        while True:
            try:
                resp = await self._client.get_sampling_simplified_markets(cursor)
            except Exception as exc:
                logger.error("CLOB sampling fetch failed: {}", exc)
                break

            pages += 1
            data = resp.get("data", []) if isinstance(resp, dict) else []
            for raw in data:
                try:
                    all_markets.append(parse_clob_simplified_market(raw))
                except Exception as exc:
                    logger.debug("Skipping unparseable CLOB market: {}", exc)

            cursor = resp.get("next_cursor", "") if isinstance(resp, dict) else ""
            if not cursor or cursor == "LTE=":
                break

        self._last_clob_pages = pages
        return all_markets

    # ------------------------------------------------------------------
    # Gamma API enrichment
    # ------------------------------------------------------------------

    async def _enrich_with_gamma(
        self, candidates: List[MarketCandidate]
    ) -> List[MarketCandidate]:
        """Join CLOB candidates to Gamma rows by condition_id (chunked GET).

        No keyset/offset universe walk. Leftover cids → gamma_miss unless
        CLOB already supplied end_date (then not a miss).
        """
        t0 = time.monotonic()
        cids = list(dict.fromkeys(
            m.condition_id for m in candidates if m.condition_id
        ))
        gamma_lookup, stats = await self._fetch_gamma_by_condition_ids(cids)

        enriched_count = 0
        for m in candidates:
            gamma = gamma_lookup.get(m.condition_id)
            if gamma is None:
                if not m.has_end_date:
                    m.gamma_miss = True
                continue
            enriched_count += 1
            m.gamma_miss = False
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

        # CLOB end_date fills: not a gamma_miss when date already present.
        for m in candidates:
            if m.has_end_date:
                m.gamma_miss = False

        still_miss = sum(
            1 for m in candidates if m.gamma_miss and not m.has_end_date
        )
        no_date = sum(
            1 for m in candidates
            if (not m.has_end_date) and (not m.gamma_miss)
        )
        elapsed = time.monotonic() - t0
        logger.info(
            "Gamma join: chunks={} hits={}/{} misses={} splits_422={} "
            "elapsed={:.1f}s gamma_miss={} no_end_date={}",
            stats.get("chunks", 0),
            enriched_count,
            len(candidates),
            stats.get("misses", max(0, len(cids) - enriched_count)),
            stats.get("splits_422", 0),
            elapsed,
            still_miss,
            no_date,
        )
        if still_miss or no_date:
            logger.warning(
                "Gamma enrichment leftovers: gamma_miss={} no_end_date={}",
                still_miss, no_date,
            )
        self._last_gamma_matched = enriched_count
        return candidates

    @staticmethod
    def _gamma_chunk_params(condition_ids: Sequence[str]) -> List[Tuple[str, str]]:
        """Build repeated ``condition_ids`` query params (not comma-joined)."""
        params: List[Tuple[str, str]] = [
            ("condition_ids", cid) for cid in condition_ids
        ]
        params.append(("limit", str(min(GAMMA_PAGE_LIMIT, max(len(condition_ids), 1)))))
        return params

    @staticmethod
    def _parse_gamma_list_payload(data: Any) -> list:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            items = data.get("markets") or data.get("data") or []
            return items if isinstance(items, list) else []
        return []

    async def _gamma_get_chunk(
        self,
        session: aiohttp.ClientSession,
        base: str,
        condition_ids: Sequence[str],
    ) -> Tuple[int, Any, str]:
        """One GET /markets?condition_ids=… Return (status, json_or_None, body_snip)."""
        url = f"{base}/markets"
        params = self._gamma_chunk_params(condition_ids)
        try:
            async with session.get(
                url,
                params=params,
                timeout=aiohttp.ClientTimeout(total=40),
            ) as resp:
                status = resp.status
                if status == 200:
                    return status, await resp.json(), ""
                body = (await resp.text())[:240]
                return status, None, body
        except Exception as exc:
            return 0, None, repr(exc)[:240]

    async def _fetch_gamma_chunk_recursive(
        self,
        session: aiohttp.ClientSession,
        base: str,
        condition_ids: List[str],
        stats: Dict[str, int],
        sem: asyncio.Semaphore,
    ) -> Dict[str, MarketCandidate]:
        """Fetch one chunk; on 422 split to 50/20/1 without aborting siblings."""
        if not condition_ids:
            return {}

        lookup: Dict[str, MarketCandidate] = {}
        data = None
        last_status: Any = None

        async with sem:
            for attempt in range(1, GAMMA_PAGE_RETRIES + 1):
                status, payload, body = await self._gamma_get_chunk(
                    session, base, condition_ids,
                )
                last_status = status
                self._last_gamma_status = status

                if status == 200:
                    data = payload
                    break

                if status == 422:
                    # Deterministic — split; do not burn retries on fat chunks.
                    break

                if status in (429, 500, 502, 503, 504) or status == 0:
                    logger.warning(
                        "Gamma join chunk n={} status {} attempt {}/{} body={!r}",
                        len(condition_ids), status, attempt, GAMMA_PAGE_RETRIES,
                        body,
                    )
                    if attempt < GAMMA_PAGE_RETRIES:
                        await asyncio.sleep(GAMMA_PAGE_RETRY_SLEEP_S * attempt)
                    continue

                logger.warning(
                    "Gamma join chunk n={} status {} body={!r} — skipping chunk",
                    len(condition_ids), status, body,
                )
                break

            await asyncio.sleep(GAMMA_CHUNK_SLEEP_S)

        if last_status == 422:
            next_size = next(
                (s for s in GAMMA_422_SPLIT_SIZES if s < len(condition_ids)),
                None,
            )
            if next_size is None:
                logger.warning(
                    "Gamma join 422 on single cid {} — giving up on this id",
                    condition_ids[0][:16],
                )
                return {}
            stats["splits_422"] = stats.get("splits_422", 0) + 1
            logger.warning(
                "Gamma join 422 on chunk n={} — splitting to {}",
                len(condition_ids), next_size,
            )
            for i in range(0, len(condition_ids), next_size):
                sub = await self._fetch_gamma_chunk_recursive(
                    session, base, condition_ids[i:i + next_size], stats, sem,
                )
                lookup.update(sub)
            return lookup

        if data is None:
            return {}

        items = self._parse_gamma_list_payload(data)
        for raw in items:
            try:
                mc = parse_gamma_market(raw)
                if mc.condition_id:
                    lookup[mc.condition_id] = mc
            except Exception:
                stats["parse_failures"] = stats.get("parse_failures", 0) + 1
        return lookup

    async def _fetch_gamma_by_condition_ids(
        self, condition_ids: Sequence[str],
    ) -> Tuple[Dict[str, MarketCandidate], Dict[str, int]]:
        """Batch-fetch Gamma markets for the given CLOB condition_ids.

        Chunks of ``GAMMA_PAGE_LIMIT`` with concurrency ≤2. Never walks
        ``/markets/keyset`` or ``offset=``.
        """
        # Preserve order, drop empties / dupes.
        cids = list(dict.fromkeys(c for c in condition_ids if c))
        stats: Dict[str, int] = {
            "chunks": 0,
            "hits": 0,
            "misses": 0,
            "splits_422": 0,
            "parse_failures": 0,
        }
        if not cids:
            self._last_gamma_pages = 0
            self._last_gamma_status = None
            return {}, stats

        chunks: List[List[str]] = [
            cids[i:i + GAMMA_PAGE_LIMIT]
            for i in range(0, len(cids), GAMMA_PAGE_LIMIT)
        ]
        stats["chunks"] = len(chunks)
        base = self._settings.GAMMA_API_URL.rstrip("/")
        lookup: Dict[str, MarketCandidate] = {}
        sem = asyncio.Semaphore(GAMMA_CHUNK_CONCURRENCY)

        try:
            async with aiohttp.ClientSession() as session:
                async def _one(chunk: List[str]) -> Dict[str, MarketCandidate]:
                    return await self._fetch_gamma_chunk_recursive(
                        session, base, chunk, stats, sem,
                    )

                results = await asyncio.gather(
                    *[_one(ch) for ch in chunks],
                    return_exceptions=True,
                )
                for res in results:
                    if isinstance(res, BaseException):
                        logger.warning("Gamma join chunk failed: {!r}", res)
                        continue
                    lookup.update(res)
        except Exception as exc:
            logger.error("Gamma condition_ids join completely failed: {}", exc)
            self._last_gamma_status = "error"

        stats["hits"] = len(lookup)
        stats["misses"] = max(0, len(cids) - len(lookup))
        self._last_gamma_pages = stats["chunks"]
        return lookup, stats

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
        - If TOTAL/N < MIN_CAPITAL_PER_MARKET, truncate N to floor(TOTAL/MIN)
          instead of silently flooring every book at MIN and over-committing
        """
        if not markets:
            return

        budget = self._settings.TOTAL_CAPITAL
        min_cap = self._settings.MIN_CAPITAL_PER_MARKET
        max_cap = self._settings.MAX_CAPITAL_PER_MARKET
        ranked_n = len(markets)

        # Cap N so raw TOTAL/N stays ≥ MIN (MAX_MARKETS already applied upstream).
        if min_cap > 0:
            max_affordable = max(1, int(budget // min_cap))
            if len(markets) > max_affordable:
                logger.warning(
                    "Capping n={} → {} (ranked={}, floor(TOTAL/MIN)={}): "
                    "TOTAL/N would be below MIN_CAPITAL_PER_MARKET=${:.0f}",
                    len(markets), max_affordable, ranked_n, max_affordable, min_cap,
                )
                del markets[max_affordable:]

        n = len(markets)
        weights = [
            TOP_TIER_BOOST if i < TOP_TIER_COUNT else 1.0
            for i in range(n)
        ]
        total_weight = sum(weights)

        for i, m in enumerate(markets):
            raw_alloc = (weights[i] / total_weight) * budget
            m.allocated_capital = min(
                max(raw_alloc, min_cap),
                max_cap,
            )

        # Ensure we don't exceed global budget
        total_allocated = sum(m.allocated_capital for m in markets)
        if total_allocated > budget:
            scale = budget / total_allocated
            for m in markets:
                m.allocated_capital *= scale

    def _log_capital_sizing(
        self,
        markets: List[MarketCandidate],
        filter_stats: Dict[str, Any],
    ) -> None:
        """Refresh summary: n, ranked, alloc median, seed budget, cash reserve."""
        import statistics

        n = len(markets)
        ranked = int(filter_stats.get("passed_count") or n)
        allocs = [m.allocated_capital for m in markets]
        alloc_med = float(statistics.median(allocs)) if allocs else 0.0
        seed_budget = self._settings.TOTAL_CAPITAL * self._settings.SEED_BUDGET_PCT
        cash_reserve = self._settings.TOTAL_CAPITAL - seed_budget
        logger.info(
            "Capital sizing: n={} ranked={} alloc_med=${:.0f} "
            "seed_budget=${:.0f} cash_reserve=${:.0f}",
            n, ranked, alloc_med, seed_budget, cash_reserve,
        )

    # ------------------------------------------------------------------
    # Performance-based market scoring
    # ------------------------------------------------------------------

    def apply_performance_multipliers(self, scoring_rates: dict[str, float]) -> None:
        """Adjust rank scores based on live performance.

        Markets with 0% scoring for extended periods get deprioritized.
        Markets consistently scoring get a small boost.
        """
        if not scoring_rates or not self._markets:
            return

        for m in self._markets:
            rate = scoring_rates.get(m.condition_id)
            if rate is None:
                continue
            if rate == 0.0:
                m.score *= 0.1  # heavily deprioritize
            elif rate < 0.3:
                m.score *= 0.5
            elif rate > 0.8:
                m.score *= 1.1  # small boost for consistent scorers

        self._markets.sort(key=lambda m: m.score, reverse=True)

    # ------------------------------------------------------------------
    # Dynamic capital reallocation
    # ------------------------------------------------------------------

    def reallocate_capital(self, scoring_rates: dict[str, float]) -> int:
        """Shift capital toward SCORING markets, away from SEED_FAILED / INACTIVE.

        Returns number of markets whose allocation changed by > 5%.
        Caps total shift at DYNAMIC_REALLOC_MAX_SHIFT_PCT per cycle.

        Donor selection (Issue 4):
          - SEED_FAILED: pulled from — capital was never deployed there.
          - INACTIVE:   pulled from — selected but not currently quoting.
          - ONE_SIDED:  PRESERVED — the bot is actively rebalancing on one
                        side and needs its capital to recover; pulling
                        capital here would freeze the recovery.
          - SCORING:    BOOSTED — proven-effective markets receive the
                        redistributed capital.
        """
        if not self._markets:
            return 0

        max_shift = self._settings.DYNAMIC_REALLOC_MAX_SHIFT_PCT
        budget = self._settings.TOTAL_CAPITAL
        adjusted = 0

        # Recipients: SCORING markets (preferred) or, if none classified
        # yet, fall back to scoring_rates >= 0.5 to keep the realloc
        # functional during the first few cycles.
        scoring_set: set[str] = {
            cid for cid, st in self._states.items()
            if st == MarketState.SCORING
        }
        if not scoring_set and scoring_rates:
            scoring_set = {
                cid for cid, rate in scoring_rates.items() if rate >= 0.5
            }
        scoring_markets = [m for m in self._markets if m.condition_id in scoring_set]

        # Donors: SEED_FAILED ∪ INACTIVE.  ONE_SIDED is intentionally
        # excluded so we don't strip capital from a market that's
        # mid-rebalance.
        donor_states = {MarketState.SEED_FAILED, MarketState.INACTIVE}
        donors = [
            m for m in self._markets
            if self._states.get(m.condition_id) in donor_states
        ]

        if not scoring_markets or not donors:
            return 0

        # Calculate how much to redistribute (cap at MAX_SHIFT_PCT of both
        # the donor pool and the total budget).
        total_from_donors = sum(m.allocated_capital for m in donors)
        shift_amount = min(total_from_donors * max_shift, budget * max_shift)
        if shift_amount <= 0:
            return 0

        per_donor_cut = shift_amount / len(donors)
        per_scoring_boost = shift_amount / len(scoring_markets)

        for m in donors:
            old = m.allocated_capital
            m.allocated_capital = max(
                self._settings.MIN_CAPITAL_PER_MARKET,
                m.allocated_capital - per_donor_cut,
            )
            if abs(m.allocated_capital - old) / max(old, 1) > 0.05:
                adjusted += 1

        for m in scoring_markets:
            old = m.allocated_capital
            m.allocated_capital = min(
                self._settings.MAX_CAPITAL_PER_MARKET,
                m.allocated_capital + per_scoring_boost,
            )
            if abs(m.allocated_capital - old) / max(old, 1) > 0.05:
                adjusted += 1

        # Clamp total to budget
        total = sum(m.allocated_capital for m in self._markets)
        if total > budget:
            scale = budget / total
            for m in self._markets:
                m.allocated_capital *= scale

        if adjusted:
            seed_failed_donors = [
                m for m in donors
                if self._states.get(m.condition_id) == MarketState.SEED_FAILED
            ]
            inactive_donors = [
                m for m in donors
                if self._states.get(m.condition_id) == MarketState.INACTIVE
            ]
            donor_summary_parts = []
            if seed_failed_donors:
                donor_summary_parts.append(
                    f"SEED_FAILED: {', '.join(m.condition_id[:10] for m in seed_failed_donors[:3])}"
                    + (f" +{len(seed_failed_donors) - 3} more" if len(seed_failed_donors) > 3 else "")
                )
            if inactive_donors:
                donor_summary_parts.append(
                    f"INACTIVE: {', '.join(m.condition_id[:10] for m in inactive_donors[:3])}"
                    + (f" +{len(inactive_donors) - 3} more" if len(inactive_donors) > 3 else "")
                )
            logger.info(
                "Dynamic realloc: shifted ${:.0f} from [{}] to [SCORING: {} markets] ({} adjusted)",
                shift_amount,
                " | ".join(donor_summary_parts) if donor_summary_parts else "n/a",
                len(scoring_markets), adjusted,
            )

        return adjusted

    # ------------------------------------------------------------------
    # Token index
    # ------------------------------------------------------------------

    def _rebuild_token_index(self) -> None:
        """Rebuild {token_id -> MarketCandidate} for O(1) lookups."""
        self._token_index = {}
        for m in self._markets:
            if m.token_id_yes:
                self._token_index[m.token_id_yes] = m
            if m.token_id_no:
                self._token_index[m.token_id_no] = m

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
            self._rebuild_token_index()
            logger.info("Loaded {} markets from cache", len(markets))
            return True
        except Exception as exc:
            logger.warning("Failed to load market cache: {}", exc)
            return False
