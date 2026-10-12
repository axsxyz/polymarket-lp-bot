"""
Daily rewards tracker — fetches LP reward payouts and generates summaries.

Polymarket distributes liquidity rewards daily at midnight UTC.  We query
both:
  1. Authenticated user-specific data via the CLOB SDK (get_trades, etc.)
     to compute our realised P&L from fills.
  2. Public Gamma API endpoints for per-market reward allocations so we
     can estimate our share.

The tracker maintains a rolling history of daily snapshots so the Discord
summary can show day-over-day and cumulative trends.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
from loguru import logger

from config.settings import Settings
from core.client import AsyncClobClient
from core.inventory import InventoryTracker

REWARDS_HISTORY_PATH = Path("data/rewards_history.json")


AIRDROP_METRICS_PATH = Path("data/airdrop_metrics.json")


@dataclass
class DailySnapshot:
    """One day's reward and P&L data."""

    date: str  # YYYY-MM-DD
    estimated_rewards_usd: float = 0.0
    realised_pnl_usd: float = 0.0
    total_fills: int = 0
    markets_active: int = 0
    global_exposure_usd: float = 0.0  # net unmatched |yes−no|×mid
    gross_exposure_usd: float = 0.0  # inventory mark + resting BID notional
    per_market: Dict[str, float] = field(default_factory=dict)
    # Airdrop metrics (P5-4)
    total_volume_usd: float = 0.0
    distinct_markets_scored: int = 0
    scoring_uptime_pct: float = 0.0
    total_orders_posted: int = 0
    consecutive_active_days: int = 0
    total_filled_usd: float = 0.0
    dry_run: bool = False


class RewardsTracker:
    """
    Collects reward estimates and generates daily summaries.

    Usage::

        tracker = RewardsTracker(client, settings, inventory)
        snapshot = await tracker.collect_daily_snapshot(active_market_count)
        await discord.send_daily_summary(snapshot)
    """

    def __init__(
        self,
        client: AsyncClobClient,
        settings: Settings,
        inventory: InventoryTracker,
    ) -> None:
        self._client = client
        self._s = settings
        self._inventory = inventory
        self._history: List[DailySnapshot] = []
        self._consecutive_active_days: int = 0
        self._daily_volume_usd: float = 0.0
        self._daily_filled_usd: float = 0.0
        self._daily_orders_posted: int = 0
        self._cumulative_quoted_notional: float = 0.0
        self._cumulative_filled_usd: float = 0.0
        self._load_history()
        self._load_airdrop_metrics()

    # ------------------------------------------------------------------
    # Daily snapshot
    # ------------------------------------------------------------------

    def record_cycle_stats(
        self, volume_usd: float, orders_posted: int, filled_usd: float = 0.0
    ) -> None:
        """Accumulate per-cycle metrics for daily airdrop summary.

        volume_usd  — quoted notional (price × size per successful POST /order)
        filled_usd  — fill notional  (price × filled_size from actual trades)
        """
        self._daily_volume_usd += volume_usd
        self._daily_filled_usd += filled_usd
        self._daily_orders_posted += orders_posted
        self._cumulative_quoted_notional += volume_usd
        self._cumulative_filled_usd += filled_usd
        self._save_airdrop_metrics()

    async def collect_daily_snapshot(
        self,
        active_market_count: int = 0,
        scored_market_count: int = 0,
        scoring_pct: float = 0.0,
        markets: Optional[List[Any]] = None,
        open_order_notional: float = 0.0,
    ) -> DailySnapshot:
        """
        Build today's snapshot by combining:
          - Reward pool from selected markets' reward_rate_daily
          - Realised P&L from inventory cost-basis tracking
          - Net + gross exposure
          - Airdrop-relevant metrics
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        market_list = list(markets or [])

        pnl = self._inventory.realised_pnl
        fill_count = self._inventory.total_fill_count

        per_market_rewards = self._pool_from_markets(market_list)
        if not per_market_rewards:
            # Fallback only when no selected markets passed / all zero rates
            per_market_rewards = await self._fetch_reward_allocations()
        total_rewards = sum(per_market_rewards.values())

        active_cids = {
            str(getattr(m, "condition_id", "") or "")
            for m in market_list
            if getattr(m, "condition_id", None)
        } or None
        net_exp = self._inventory.global_exposure_usd(active_cids)
        gross_exp = self._inventory.gross_exposure_usd(
            open_order_notional, active_cids,
        )

        self._consecutive_active_days += 1
        n_markets = len(market_list) if market_list else active_market_count

        snapshot = DailySnapshot(
            date=today,
            estimated_rewards_usd=round(total_rewards, 4),
            realised_pnl_usd=round(pnl, 4),
            total_fills=fill_count,
            markets_active=n_markets,
            global_exposure_usd=round(net_exp, 2),
            gross_exposure_usd=round(gross_exp, 2),
            per_market=per_market_rewards,
            total_volume_usd=round(self._daily_volume_usd, 2),
            total_filled_usd=round(self._daily_filled_usd, 2),
            distinct_markets_scored=scored_market_count,
            scoring_uptime_pct=round(scoring_pct, 2),
            total_orders_posted=self._daily_orders_posted,
            consecutive_active_days=self._consecutive_active_days,
            dry_run=bool(self._s.DRY_RUN),
        )

        self._history.append(snapshot)
        self._save_history()
        self._save_airdrop_metrics()

        self._daily_volume_usd = 0.0
        self._daily_filled_usd = 0.0
        self._daily_orders_posted = 0

        logger.info(
            "Daily snapshot {}: pool≈${:.2f}  fills={}  pnl=${:.2f}  "
            "net=${:.2f} gross=${:.2f}  vol=${:.0f}  days={}  dry={}",
            today, total_rewards, fill_count, pnl,
            snapshot.global_exposure_usd, snapshot.gross_exposure_usd,
            snapshot.total_volume_usd, self._consecutive_active_days,
            snapshot.dry_run,
        )

        return snapshot

    @staticmethod
    def _pool_from_markets(markets: List[Any]) -> Dict[str, float]:
        """Sum reward_rate_daily already on selected MarketCandidate rows."""
        per_market: Dict[str, float] = {}
        for m in markets:
            cid = str(getattr(m, "condition_id", "") or "")
            daily = float(getattr(m, "reward_rate_daily", 0.0) or 0.0)
            if cid and daily > 0:
                per_market[cid] = daily
        return per_market

    # ------------------------------------------------------------------
    # Gamma reward allocations
    # ------------------------------------------------------------------

    async def _fetch_reward_allocations(self) -> Dict[str, float]:
        """
        Fallback Gamma scrape when no selected markets were passed.

        Parses both legacy ``rewards.rates`` and live ``clobRewards``.
        Prefer ``_pool_from_markets`` for the daily Discord report.
        """
        per_market: Dict[str, float] = {}
        base = self._s.GAMMA_API_URL.rstrip("/")

        try:
            async with aiohttp.ClientSession() as session:
                url = f"{base}/markets?active=true&closed=false&limit=100"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        logger.warning("Gamma rewards fetch returned {}", resp.status)
                        return per_market
                    data = await resp.json()

                items = data if isinstance(data, list) else data.get("data", [])
                for raw in items:
                    cid = raw.get("conditionId") or raw.get("condition_id") or ""
                    daily = 0.0
                    rewards = raw.get("rewards", {}) or {}
                    rates = rewards.get("rates", []) or []
                    for r in rates:
                        daily += float(r.get("rewards_daily_rate", 0) or 0)
                    if daily <= 0:
                        for cr in (raw.get("clobRewards") or []):
                            if isinstance(cr, dict):
                                daily += float(
                                    cr.get("rewardsDailyRate", 0)
                                    or cr.get("rewards_daily_rate", 0)
                                    or 0
                                )
                    if cid and daily > 0:
                        per_market[str(cid)] = daily

        except Exception as exc:
            logger.warning("Gamma reward allocation fetch failed: {}", exc)

        return per_market

    # ------------------------------------------------------------------
    # History persistence
    # ------------------------------------------------------------------

    def _save_history(self) -> None:
        try:
            REWARDS_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload = [
                {
                    "date": s.date,
                    "estimated_rewards_usd": s.estimated_rewards_usd,
                    "realised_pnl_usd": s.realised_pnl_usd,
                    "total_fills": s.total_fills,
                    "markets_active": s.markets_active,
                    "global_exposure_usd": s.global_exposure_usd,
                    "gross_exposure_usd": s.gross_exposure_usd,
                    "total_volume_usd": s.total_volume_usd,
                    "total_filled_usd": s.total_filled_usd,
                    "distinct_markets_scored": s.distinct_markets_scored,
                    "scoring_uptime_pct": s.scoring_uptime_pct,
                    "total_orders_posted": s.total_orders_posted,
                    "consecutive_active_days": s.consecutive_active_days,
                    "dry_run": s.dry_run,
                }
                for s in self._history[-90:]  # keep 90 days
            ]
            REWARDS_HISTORY_PATH.write_text(json.dumps(payload, indent=2))
        except Exception as exc:
            logger.warning("Failed to save rewards history: {}", exc)

    def _load_history(self) -> None:
        if not REWARDS_HISTORY_PATH.exists():
            return
        try:
            data = json.loads(REWARDS_HISTORY_PATH.read_text())
            for raw in data:
                self._history.append(
                    DailySnapshot(
                        date=raw["date"],
                        estimated_rewards_usd=raw.get("estimated_rewards_usd", 0),
                        realised_pnl_usd=raw.get("realised_pnl_usd", 0),
                        total_fills=raw.get("total_fills", 0),
                        markets_active=raw.get("markets_active", 0),
                        global_exposure_usd=raw.get("global_exposure_usd", 0),
                        gross_exposure_usd=raw.get("gross_exposure_usd", 0),
                        total_volume_usd=raw.get("total_volume_usd", 0),
                        total_filled_usd=raw.get("total_filled_usd", 0.0),
                        distinct_markets_scored=raw.get("distinct_markets_scored", 0),
                        scoring_uptime_pct=raw.get("scoring_uptime_pct", 0),
                        total_orders_posted=raw.get("total_orders_posted", 0),
                        consecutive_active_days=raw.get("consecutive_active_days", 0),
                        dry_run=bool(raw.get("dry_run", False)),
                    )
                )
        except Exception as exc:
            logger.warning("Failed to load rewards history: {}", exc)

    # ------------------------------------------------------------------
    # Reporting helpers
    # ------------------------------------------------------------------

    @property
    def latest_snapshot(self) -> Optional[DailySnapshot]:
        return self._history[-1] if self._history else None

    @property
    def cumulative_rewards(self) -> float:
        return sum(s.estimated_rewards_usd for s in self._history)

    def estimate_our_share_usd(
        self,
        markets: Optional[List[Any]] = None,
        *,
        session_started_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Operator ESTIM of our LP reward share — never add to realised PnL.

        Prefer today's ``DailySnapshot.estimated_rewards_usd × LP_ESTIMATED_SHARE``.
        When history is empty (typical DRY_RUN before midnight snapshot)::

            sum(reward_rate_daily) × share × tightness_factor × (elapsed/86400)

        Label always ``ESTIM, not CLOB Q-score``.
        """
        share = float(self._s.LP_ESTIMATED_SHARE)
        note = "ESTIM, not CLOB Q-score"
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        snap = self.latest_snapshot
        if (
            snap is not None
            and snap.date == today
            and float(snap.estimated_rewards_usd or 0.0) > 0
        ):
            return {
                "est_rewards_usd": round(float(snap.estimated_rewards_usd) * share, 4),
                "est_share": share,
                "est_note": note,
            }

        pool_day = sum(
            float(getattr(m, "reward_rate_daily", 0.0) or 0.0)
            for m in (markets or [])
        )
        tightness = float(self._s.tightness_factor)
        frac = 0.0
        if session_started_at:
            try:
                raw = session_started_at.replace("Z", "+00:00")
                started = datetime.fromisoformat(raw)
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(timezone.utc) - started).total_seconds()
                frac = max(0.0, min(1.0, elapsed / 86400.0))
            except Exception:
                frac = 0.0
        usd = round(pool_day * share * tightness * frac, 4)
        return {
            "est_rewards_usd": usd,
            "est_share": share,
            "est_note": note,
        }

    # ------------------------------------------------------------------
    # Airdrop metrics persistence (P5-4)
    # ------------------------------------------------------------------

    def _save_airdrop_metrics(self) -> None:
        try:
            AIRDROP_METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "consecutive_active_days": self._consecutive_active_days,
                "total_quoted_notional": self._cumulative_quoted_notional,
                "total_filled_usd": self._cumulative_filled_usd,
            }
            AIRDROP_METRICS_PATH.write_text(json.dumps(payload, indent=2))
        except Exception as exc:
            logger.warning("Failed to save airdrop metrics: {}", exc)

    def _load_airdrop_metrics(self) -> None:
        if not AIRDROP_METRICS_PATH.exists():
            return
        try:
            data = json.loads(AIRDROP_METRICS_PATH.read_text())
            self._consecutive_active_days = data.get("consecutive_active_days", 0)
            self._cumulative_quoted_notional = data.get("total_quoted_notional", 0.0)
            self._cumulative_filled_usd = data.get("total_filled_usd", 0.0)
        except Exception as exc:
            logger.warning("Failed to load airdrop metrics: {}", exc)

    # ------------------------------------------------------------------
    # Reporting helpers
    # ------------------------------------------------------------------

    def format_summary(
        self, snapshot: DailySnapshot, q_score_ratio: Optional[float] = None
    ) -> str:
        """Build a human-readable summary string for Discord."""
        share = (
            q_score_ratio
            if (q_score_ratio is not None and q_score_ratio > 0)
            else self._s.LP_ESTIMATED_SHARE
        )
        estimated_share = snapshot.estimated_rewards_usd * share
        run = "DRY_RUN (paper)" if snapshot.dry_run else "LIVE"
        lines = [
            f"**Daily LP Summary — {snapshot.date}** · {run}",
            "",
            f"Selected markets pool $/day: **${snapshot.estimated_rewards_usd:.2f}**",
            f"Est. our share (~{share:.0%} × pool): **${estimated_share:.2f}**",
            f"  _(ESTIM — not CLOB Q-score; paper fills ≠ live rewards)_"
            if snapshot.dry_run
            else "  _(ESTIM — not CLOB Q-score)_",
            f"Realised PnL (cost-basis): **${snapshot.realised_pnl_usd:.2f}**",
            f"Total fills: {snapshot.total_fills}",
            f"Active markets: {snapshot.markets_active}",
            f"Net exposure (unmatched YES/NO): **${snapshot.global_exposure_usd:.2f}**",
            f"Gross deployed (inv + BIDs): **${snapshot.gross_exposure_usd:.2f}**",
            f"Cumulative selected pool (hist): ${self.cumulative_rewards:.2f}",
            "",
            "**Airdrop Metrics**",
            f"Volume quoted today: ${snapshot.total_volume_usd:,.0f}",
            f"Volume filled today: ${snapshot.total_filled_usd:,.0f}",
            f"Markets scored: {snapshot.distinct_markets_scored}",
            f"Scoring uptime: {snapshot.scoring_uptime_pct:.0%}",
            f"Orders posted: {snapshot.total_orders_posted}",
            f"Consecutive active days: {snapshot.consecutive_active_days}",
        ]
        return "\n".join(lines)
