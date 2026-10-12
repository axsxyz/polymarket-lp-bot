"""
Inventory & skew management — the capital-protection layer.

We are NEVER directional.  When fills create net YES or NO exposure we must
detect and correct it before the position grows large enough to hurt us.

Skew ratio  = |yes_pos - no_pos| / max(yes_pos + no_pos, 1)
              0.0 = perfectly balanced   1.0 = fully one-sided

Three escalation tiers (thresholds configurable in Settings):

  1. SKEW_ALERT_THRESHOLD   (default 0.25)  → Discord warning
  2. SKEW_ACTION_THRESHOLD  (default 0.30)  → widen heavy side / tighten light side
  3. SKEW_PAUSE_THRESHOLD   (default 0.45)  → pause quoting on heavy side for N minutes

Global guard: total net directional exposure across ALL markets must stay
below GLOBAL_MAX_EXPOSURE_PCT * TOTAL_CAPITAL.  If breached the most-
imbalanced markets are paused until exposure drops.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, NamedTuple

from loguru import logger

from config.settings import Settings
from core.order_manager import FillEvent


class _PauseState(NamedTuple):
    until: datetime
    side: str


@dataclass
class MarketPosition:
    """Net token holdings for one market (YES and NO sides)."""

    condition_id: str
    yes_token_id: str = ""
    no_token_id: str = ""
    yes_size: float = 0.0
    no_size: float = 0.0
    yes_cost: float = 0.0
    no_cost: float = 0.0

    @property
    def skew_ratio(self) -> float:
        total = self.yes_size + self.no_size
        if total <= 0:
            return 0.0
        return abs(self.yes_size - self.no_size) / total

    @property
    def heavy_side(self) -> str:
        if self.yes_size > self.no_size:
            return "YES"
        elif self.no_size > self.yes_size:
            return "NO"
        return ""

    @property
    @property
    def net_exposure_usd(self) -> float:
        """Unmatched size × $0.50 when no live mid is available."""
        if self.yes_size == 0 and self.no_size == 0:
            return 0.0
        return abs(self.yes_size - self.no_size) * 0.50

    def gross_exposure_at_mid(self, yes_mid: float = 0.5) -> float:
        """Gross position value using mark-to-mid pricing (primary valuation)."""
        no_mid = max(0.01, 1.0 - yes_mid)
        return self.yes_size * yes_mid + self.no_size * no_mid


@dataclass
class SkewAction:
    """Instructions for the quoter based on current inventory state."""

    condition_id: str
    skew_ratio: float
    heavy_side: str
    should_alert: bool = False
    should_adjust_quotes: bool = False
    paused_side: str = ""  # token side to stop quoting entirely (hard pause >= 0.70)
    recovery_side: str = ""  # token side in recovery mode (reduced quoting, 0.45-0.70)
    pause_triggered: bool = False  # True only on transition to paused


INVENTORY_PATH = Path("data/inventory_state.json")
_SIZE_DUST = 0.5


class InventoryTracker:
    """
    Tracks per-market positions and produces SkewActions each cycle.

    Positions are updated incrementally via `record_fills()` and can be
    reconciled periodically against the exchange's view.
    """

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._positions: Dict[str, MarketPosition] = {}
        self._pause_until: Dict[str, _PauseState] = {}
        self._last_alert_time: Dict[str, datetime] = {}
        self._midpoints: Dict[str, float] = {}  # token_id → mid price
        self._token_map: Dict[str, tuple] = {}  # token_id → (condition_id, "yes"|"no")
        self._realised_pnl: float = 0.0
        self._total_fill_count: int = 0
        # Daily drawdown tracking (anchor + flag, reset at UTC midnight)
        self._realised_pnl_day_anchor: float = 0.0
        self._current_day_utc: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._drawdown_paused: bool = False
        self._mtm_equity_day_anchor: Optional[float] = None
        self._persist_path = INVENTORY_PATH
        self._tape: Optional[Any] = None

    @property
    def positions(self) -> Dict[str, MarketPosition]:
        return dict(self._positions)

    def get_position(self, condition_id: str) -> Optional[MarketPosition]:
        return self._positions.get(condition_id)

    def set_tape(self, tape: Any) -> None:
        """Optional LocalTape for cost_basis_missing / diagnostics."""
        self._tape = tape

    def apply_seed_costs_once(
        self,
        condition_id: str,
        yes_cost: float = 0.0,
        no_cost: float = 0.0,
    ) -> None:
        """Write seed USD cost basis only when currently unset (≤ 0).

        Never re-assign original seed USD after fills/rebalance have moved cost.
        """
        pos = self._positions.get(condition_id)
        if pos is None:
            return
        yc = float(yes_cost or 0.0)
        nc = float(no_cost or 0.0)
        if yc > 0 and pos.yes_cost <= 0:
            pos.yes_cost = yc
        if nc > 0 and pos.no_cost <= 0:
            pos.no_cost = nc

    def add_seed_costs(
        self,
        condition_id: str,
        yes_cost: float = 0.0,
        no_cost: float = 0.0,
    ) -> None:
        """Add top-up seed USD to existing cost basis. Never replace."""
        pos = self._positions.get(condition_id)
        if pos is None:
            return
        yc = float(yes_cost or 0.0)
        nc = float(no_cost or 0.0)
        if yc > 0:
            pos.yes_cost += yc
        if nc > 0:
            pos.no_cost += nc

    def absorb_seed_acquisition(
        self,
        condition_id: str,
        *,
        prev_yes_size: float,
        prev_no_size: float,
        yes_cost: float = 0.0,
        no_cost: float = 0.0,
    ) -> None:
        """After sizes were updated from paper/chain: write-once or add top-up.

        - cost unset + seeder USD → ``apply_seed_costs_once``
        - cost set + size grew + seeder USD → ``add_seed_costs`` (never reset)
        - size grew + zero seeder USD → tape ``seed_cost_gap``, init delta at mid
        - size unchanged → no-op (stale original seed USD must not reset/add)
        """
        pos = self._positions.get(condition_id)
        if pos is None:
            return
        yc = float(yes_cost or 0.0)
        nc = float(no_cost or 0.0)
        eps = 1e-9
        delta_yes = float(pos.yes_size) - float(prev_yes_size)
        delta_no = float(pos.no_size) - float(prev_no_size)

        if yc > 0 and pos.yes_cost <= 0:
            pos.yes_cost = yc
        elif yc > 0 and delta_yes > eps and pos.yes_cost > 0:
            pos.yes_cost += yc
        elif delta_yes > eps and yc <= 0:
            self._cover_seed_cost_gap(pos, is_yes=True, delta=delta_yes)

        if nc > 0 and pos.no_cost <= 0:
            pos.no_cost = nc
        elif nc > 0 and delta_no > eps and pos.no_cost > 0:
            pos.no_cost += nc
        elif delta_no > eps and nc <= 0:
            self._cover_seed_cost_gap(pos, is_yes=False, delta=delta_no)

    def _cover_seed_cost_gap(
        self,
        pos: MarketPosition,
        *,
        is_yes: bool,
        delta: float,
    ) -> None:
        """Size grew without seeder USD — init delta at mid (realised +0)."""
        if delta <= 1e-12:
            return
        mid = self._side_mid(pos, is_yes)
        add_usd = delta * mid
        side = "YES" if is_yes else "NO"
        if is_yes:
            pos.yes_cost += add_usd
        else:
            pos.no_cost += add_usd
        logger.warning(
            "seed_cost_gap {} {} delta={:.4f} → init @ mid={:.4f} (+${:.4f})",
            pos.condition_id[:12], side, delta, mid, add_usd,
        )
        if self._tape is not None:
            try:
                self._tape.skip(
                    pos.condition_id,
                    "seed_cost_gap",
                    side=side,
                    delta=delta,
                    mid=mid,
                    cost_added=add_usd,
                )
            except Exception as exc:
                logger.debug("tape seed_cost_gap failed: {}", exc)

    def _side_mid(self, pos: MarketPosition, is_yes: bool) -> float:
        """Best-effort mid for missing-basis init (token mid → complement → 0.5)."""
        if is_yes:
            if pos.yes_token_id and pos.yes_token_id in self._midpoints:
                return float(self._midpoints[pos.yes_token_id])
            if pos.no_token_id and pos.no_token_id in self._midpoints:
                return max(0.01, 1.0 - float(self._midpoints[pos.no_token_id]))
            return 0.5
        if pos.no_token_id and pos.no_token_id in self._midpoints:
            return float(self._midpoints[pos.no_token_id])
        if pos.yes_token_id and pos.yes_token_id in self._midpoints:
            return max(0.01, 1.0 - float(self._midpoints[pos.yes_token_id]))
        return 0.5

    def _ensure_cost_basis(self, pos: MarketPosition, is_yes: bool) -> None:
        """If size>0 and cost==0, init cost=size×mid once. Realised unchanged."""
        if is_yes:
            if pos.yes_size <= 1e-12 or pos.yes_cost > 0:
                return
            mid = self._side_mid(pos, True)
            pos.yes_cost = pos.yes_size * mid
            logger.warning(
                "cost_basis_missing {} YES size={:.4f} → init @ mid={:.4f} (realised+0)",
                pos.condition_id[:12], pos.yes_size, mid,
            )
            if self._tape is not None:
                try:
                    self._tape.skip(
                        pos.condition_id,
                        "cost_basis_missing",
                        side="YES",
                        size=pos.yes_size,
                        mid=mid,
                        cost=pos.yes_cost,
                    )
                except Exception as exc:
                    logger.debug("tape cost_basis_missing failed: {}", exc)
            return
        if pos.no_size <= 1e-12 or pos.no_cost > 0:
            return
        mid = self._side_mid(pos, False)
        pos.no_cost = pos.no_size * mid
        logger.warning(
            "cost_basis_missing {} NO size={:.4f} → init @ mid={:.4f} (realised+0)",
            pos.condition_id[:12], pos.no_size, mid,
        )
        if self._tape is not None:
            try:
                self._tape.skip(
                    pos.condition_id,
                    "cost_basis_missing",
                    side="NO",
                    size=pos.no_size,
                    mid=mid,
                    cost=pos.no_cost,
                )
            except Exception as exc:
                logger.debug("tape cost_basis_missing failed: {}", exc)

    # ------------------------------------------------------------------
    # Position tracking
    # ------------------------------------------------------------------

    def ensure_position(
        self, condition_id: str, yes_token_id: str = "", no_token_id: str = ""
    ) -> MarketPosition:
        """Get or create a position record for a market."""
        if condition_id not in self._positions:
            self._positions[condition_id] = MarketPosition(
                condition_id=condition_id,
                yes_token_id=yes_token_id,
                no_token_id=no_token_id,
            )
        pos = self._positions[condition_id]
        if yes_token_id:
            pos.yes_token_id = yes_token_id
            self._token_map[yes_token_id] = (condition_id, "yes")
        if no_token_id:
            pos.no_token_id = no_token_id
            self._token_map[no_token_id] = (condition_id, "no")
        return pos

    def flatten_position(
        self,
        condition_id: str,
        yes_price: float,
        no_price: float,
    ) -> float:
        """Zero YES+NO for a market; realise PnL at given exit prices.

        Returns total USD proceeds (size × price). Idempotent on empty.
        """
        pos = self._positions.get(condition_id)
        if pos is None:
            return 0.0
        proceeds = 0.0
        if pos.yes_size > 1e-12:
            self._ensure_cost_basis(pos, True)
            avg = (pos.yes_cost / pos.yes_size) if pos.yes_size > 0 else float(yes_price)
            proceeds += pos.yes_size * float(yes_price)
            self._realised_pnl += pos.yes_size * (float(yes_price) - avg)
            pos.yes_size = 0.0
            pos.yes_cost = 0.0
        if pos.no_size > 1e-12:
            self._ensure_cost_basis(pos, False)
            avg = (pos.no_cost / pos.no_size) if pos.no_size > 0 else float(no_price)
            proceeds += pos.no_size * float(no_price)
            self._realised_pnl += pos.no_size * (float(no_price) - avg)
            pos.no_size = 0.0
            pos.no_cost = 0.0
        return proceeds

    def reduce_position(
        self,
        condition_id: str,
        side: str,
        size: float,
        price: float,
    ) -> float:
        """Sell ``size`` shares on YES or NO; realise PnL vs avg cost.

        Does not touch the other side. Size is clipped to holdings.
        Returns USD proceeds.
        """
        pos = self._positions.get(condition_id)
        if pos is None or size <= 1e-12:
            return 0.0
        px = float(price)
        want = float(size)
        key = (side or "").strip().upper()
        if key == "YES":
            held = pos.yes_size
            if held <= 1e-12:
                return 0.0
            self._ensure_cost_basis(pos, True)
            sold = min(want, held)
            avg = (pos.yes_cost / pos.yes_size) if pos.yes_size > 0 else px
            proceeds = sold * px
            self._realised_pnl += sold * (px - avg)
            pos.yes_size = max(0.0, pos.yes_size - sold)
            pos.yes_cost = max(0.0, pos.yes_cost - sold * avg)
            return proceeds
        if key == "NO":
            held = pos.no_size
            if held <= 1e-12:
                return 0.0
            self._ensure_cost_basis(pos, False)
            sold = min(want, held)
            avg = (pos.no_cost / pos.no_size) if pos.no_size > 0 else px
            proceeds = sold * px
            self._realised_pnl += sold * (px - avg)
            pos.no_size = max(0.0, pos.no_size - sold)
            pos.no_cost = max(0.0, pos.no_cost - sold * avg)
            return proceeds
        return 0.0

    def update_midpoints(self, midpoints: Dict[str, float]) -> None:
        """Update mark-to-mid prices from the latest batch fetch."""
        self._midpoints.update(midpoints)

    @property
    def realised_pnl(self) -> float:
        return self._realised_pnl

    @property
    def realised_pnl_today(self) -> float:
        """PnL realised since the start of the current UTC day."""
        return self._realised_pnl - self._realised_pnl_day_anchor

    @property
    def is_drawdown_paused(self) -> bool:
        return self._drawdown_paused

    @property
    def total_fill_count(self) -> int:
        return self._total_fill_count

    def register_token_mapping(self, token_id: str, condition_id: str, side: str) -> None:
        """Register a token → (condition_id, side) mapping for fill routing.

        Called at startup and on market refresh so fills with unknown token IDs
        can still be attributed to the correct position.
        """
        self._token_map[token_id] = (condition_id, side)

    def record_fills(self, fills: List[FillEvent]) -> None:
        """
        Update positions and track realised PnL using average-cost basis.

        Fill side semantics:
          BID filled → we BOUGHT the token  → position increases
          ASK filled → we SOLD  the token   → realise PnL vs avg cost
        """
        for f in fills:
            pos = self._positions.get(f.condition_id)
            if pos is None:
                pos = MarketPosition(condition_id=f.condition_id)
                self._positions[f.condition_id] = pos

            # Try to set missing token IDs from the global token map
            if not pos.yes_token_id or not pos.no_token_id:
                mapping = self._token_map.get(f.token_id)
                if mapping and mapping[0] == f.condition_id:
                    if mapping[1] == "yes" and not pos.yes_token_id:
                        pos.yes_token_id = f.token_id
                    elif mapping[1] == "no" and not pos.no_token_id:
                        pos.no_token_id = f.token_id

            # YES/NO attribution from token_id only — never guess.
            is_yes: Optional[bool] = None
            mapping = self._token_map.get(f.token_id)
            if mapping and mapping[0] == f.condition_id:
                is_yes = mapping[1] == "yes"
            elif pos.yes_token_id and f.token_id == pos.yes_token_id:
                is_yes = True
            elif pos.no_token_id and f.token_id == pos.no_token_id:
                is_yes = False
            else:
                logger.warning(
                    "Fill for {} unknown token {} — skip (no YES/NO guess)",
                    f.condition_id[:12],
                    (f.token_id[:12] if f.token_id else "?"),
                )
                continue

            self._total_fill_count += 1

            if f.side == "BID":
                if is_yes:
                    pos.yes_size += f.filled_size
                    pos.yes_cost += f.filled_size * f.price
                else:
                    pos.no_size += f.filled_size
                    pos.no_cost += f.filled_size * f.price
            elif f.side == "ASK":
                if is_yes:
                    self._ensure_cost_basis(pos, True)
                    avg_cost = (pos.yes_cost / pos.yes_size) if pos.yes_size > 0 else f.price
                    pnl = (f.price - avg_cost) * f.filled_size
                    self._realised_pnl += pnl
                    pos.yes_size = max(0, pos.yes_size - f.filled_size)
                    pos.yes_cost = max(0, pos.yes_cost - f.filled_size * avg_cost)
                else:
                    self._ensure_cost_basis(pos, False)
                    avg_cost = (pos.no_cost / pos.no_size) if pos.no_size > 0 else f.price
                    pnl = (f.price - avg_cost) * f.filled_size
                    self._realised_pnl += pnl
                    pos.no_size = max(0, pos.no_size - f.filled_size)
                    pos.no_cost = max(0, pos.no_cost - f.filled_size * avg_cost)

    # ------------------------------------------------------------------
    # Skew evaluation
    # ------------------------------------------------------------------

    def evaluate(self, condition_id: str) -> SkewAction:
        """Compute the SkewAction for a market based on current inventory."""
        pos = self._positions.get(condition_id)
        if pos is None:
            return SkewAction(condition_id=condition_id, skew_ratio=0.0, heavy_side="")

        ratio = pos.skew_ratio
        heavy = pos.heavy_side

        action = SkewAction(
            condition_id=condition_id,
            skew_ratio=ratio,
            heavy_side=heavy,
        )

        if ratio >= self._s.SKEW_ALERT_THRESHOLD:
            now = datetime.now(timezone.utc)
            last = self._last_alert_time.get(condition_id)
            cooldown = timedelta(seconds=self._s.SKEW_ALERT_COOLDOWN_SECONDS)
            if last is None or (now - last) >= cooldown:
                action.should_alert = True
                self._last_alert_time[condition_id] = now
                logger.info("Skew alert {} → {:.1%} (heavy: {})", condition_id[:12], ratio, heavy)
        elif condition_id in self._last_alert_time:
            del self._last_alert_time[condition_id]

        if ratio >= self._s.SKEW_ACTION_THRESHOLD:
            action.should_adjust_quotes = True

        # Two-tier skew management:
        #   SKEW_PAUSE_THRESHOLD (0.45) .. SKEW_HARD_PAUSE_THRESHOLD (0.70) → recovery mode
        #   >= SKEW_HARD_PAUSE_THRESHOLD (0.70) or timer still active       → full pause
        #
        # Recovery mode keeps reduced ASK to attract fills that reduce the position.
        # Full pause only at extreme skew where the market is dangerously one-sided.
        if ratio >= self._s.SKEW_HARD_PAUSE_THRESHOLD or self._is_paused(condition_id):
            if ratio >= self._s.SKEW_HARD_PAUSE_THRESHOLD and not self._is_paused(condition_id):
                self._set_pause(condition_id, heavy)
                action.pause_triggered = True
                logger.warning(
                    "SKEW HARD PAUSE {} → {:.1%} heavy side {} for {}s",
                    condition_id[:12], ratio, heavy, self._s.SKEW_PAUSE_DURATION_SECONDS
                )
            pause_state = self._pause_until.get(condition_id)
            action.paused_side = pause_state.side if pause_state else heavy
        elif ratio >= self._s.SKEW_PAUSE_THRESHOLD:
            action.recovery_side = heavy
            logger.debug(
                "Skew recovery mode {} → {:.1%} (heavy: {})",
                condition_id[:12], ratio, heavy,
            )

        return action

    def evaluate_all(self) -> List[SkewAction]:
        """Evaluate skew for every tracked market."""
        return [self.evaluate(cid) for cid in self._positions]

    # ------------------------------------------------------------------
    # Global exposure guard
    # ------------------------------------------------------------------

    def global_exposure_usd(self, active_cids: Optional[set] = None) -> float:
        """Total net exposure, optionally scoped to a set of active markets.

        Stale positions from non-active markets are excluded when *active_cids*
        is provided — this prevents orphaned fills from triggering the limit.
        """
        positions = self._positions.values()
        if active_cids is not None:
            positions = [p for p in positions if p.condition_id in active_cids]
        total = 0.0
        for p in positions:
            unmatched = abs(p.yes_size - p.no_size)
            mid = self._midpoints.get(p.yes_token_id, 0.5)
            total += unmatched * (mid if mid > 0 else 0.50)
        return total

    def is_global_exposure_breached(self, active_cids: Optional[set] = None) -> bool:
        limit = self._s.GLOBAL_MAX_EXPOSURE_PCT * self._s.TOTAL_CAPITAL
        return self.global_exposure_usd(active_cids) > limit

    def gross_exposure_usd(
        self, open_order_notional: float = 0.0, active_cids: Optional[set] = None,
    ) -> float:
        """Deployed gross: filled mark-to-mid + BID notional (ASK excluded)."""
        positions = self._positions.values()
        if active_cids is not None:
            positions = [p for p in positions if p.condition_id in active_cids]
        filled = sum(
            pos.gross_exposure_at_mid(
                self._midpoints.get(pos.yes_token_id, 0.5)
            )
            for pos in positions
        )
        return filled + open_order_notional

    def is_gross_exposure_breached(
        self,
        open_order_notional: float = 0.0,
        active_cids: Optional[set] = None,
        deployed_capital: Optional[float] = None,
    ) -> bool:
        """Gross exposure breach (Issue 5).

        When *deployed_capital* is provided the limit is
        ``deployed_capital × MAX_EXPOSURE_RATIO`` (a ratio of the actually
        deployed capital, per the Issue 5 spec).  Otherwise it falls back
        to the legacy ``GLOBAL_MAX_GROSS_EXPOSURE_PCT × TOTAL_CAPITAL``
        check so existing tests/callers keep working unchanged.
        """
        if deployed_capital is not None and deployed_capital > 0:
            limit = deployed_capital * self._s.MAX_EXPOSURE_RATIO
        else:
            limit = self._s.GLOBAL_MAX_GROSS_EXPOSURE_PCT * self._s.TOTAL_CAPITAL
        return self.gross_exposure_usd(open_order_notional, active_cids) > limit

    # ------------------------------------------------------------------
    # Per-market exposure (Issue 5)
    # ------------------------------------------------------------------

    def market_exposure_usd(
        self, condition_id: str, open_order_notional_for_market: float = 0.0,
    ) -> float:
        """Deployed for one market: inventory mark-to-mid + BID notional only."""
        pos = self._positions.get(condition_id)
        if pos is None:
            return open_order_notional_for_market
        yes_mid = self._midpoints.get(pos.yes_token_id, 0.5)
        return pos.gross_exposure_at_mid(yes_mid) + open_order_notional_for_market

    def inventory_mark_usd(self, condition_id: str) -> float:
        """Mark-to-mid inventory value for one market (0 if unknown)."""
        pos = self._positions.get(condition_id)
        if pos is None:
            return 0.0
        yes_mid = self._midpoints.get(pos.yes_token_id, 0.5)
        return pos.gross_exposure_at_mid(yes_mid)

    def is_market_exposure_breached(
        self,
        condition_id: str,
        allocated_capital: float,
        open_order_notional_for_market: float = 0.0,
        minsize_bid_floor_usd: float = 0.0,
    ) -> tuple[bool, float, float]:
        """Per-market exposure check (Issue 5).

        Deployed = inventory mark + BID notional (ASK excluded).
        CLOB ``min_size`` two-sided BID floor may overshoot
        ``ALLOC × MAX_MARKET_EXPOSURE_RATIO`` — that alone must not pause
        (Q-score requires two-sided BIDs; never INACTIVE for floor overshoot).

        Returns ``(breached, exposure_usd, limit_usd)``.
        """
        if allocated_capital <= 0:
            return False, 0.0, 0.0
        limit = allocated_capital * self._s.MAX_MARKET_EXPOSURE_RATIO
        exposure = self.market_exposure_usd(
            condition_id, open_order_notional_for_market,
        )
        if exposure <= limit:
            return False, exposure, limit
        # Tolerate overshoot only when resting BIDs are at/under the
        # two-sided min_size floor (preferred path — no naked ASK).
        if (
            minsize_bid_floor_usd > 0
            and open_order_notional_for_market <= minsize_bid_floor_usd + 1e-6
        ):
            return False, exposure, limit
        return True, exposure, limit

    # ------------------------------------------------------------------
    # Daily drawdown circuit breaker
    # ------------------------------------------------------------------

    def mark_to_mid_equity(self) -> float:
        """realised PnL + current inventory mark-to-mid (gross at mid)."""
        mtm = 0.0
        for pos in self._positions.values():
            yes_mid = self._midpoints.get(pos.yes_token_id, 0.5)
            mtm += pos.gross_exposure_at_mid(yes_mid)
        return self._realised_pnl + mtm

    def check_daily_drawdown(self) -> bool:
        """
        Evaluate the daily drawdown circuit breaker.

        - At UTC midnight rollover the PnL/MTM anchors are re-snapshotted
          and the pause flag cleared.
        - Triggers pause when realised_pnl_today < -limit OR
          (mark_to_mid_equity − midnight_mtm) < -limit.

        Returns True if the bot should pause new quoting for today.
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._current_day_utc:
            logger.info(
                "UTC day rollover: resetting drawdown anchor "
                "({} → {}, prior day PnL=${:.2f})",
                self._current_day_utc, today, self.realised_pnl_today,
            )
            self._current_day_utc = today
            self._realised_pnl_day_anchor = self._realised_pnl
            self._mtm_equity_day_anchor = self.mark_to_mid_equity()
            self._drawdown_paused = False

        if self._mtm_equity_day_anchor is None:
            self._mtm_equity_day_anchor = self.mark_to_mid_equity()

        if self._drawdown_paused:
            return True

        limit = self._s.DAILY_DRAWDOWN_LIMIT_PCT * self._s.TOTAL_CAPITAL
        if limit <= 0:
            return False

        mtm_dd = self.mark_to_mid_equity() - self._mtm_equity_day_anchor
        realised_breach = self.realised_pnl_today < -limit
        mtm_breach = mtm_dd < -limit
        if realised_breach or mtm_breach:
            self._drawdown_paused = True
            logger.warning(
                "Daily drawdown breached: realised today=${:.2f} mtm_dd=${:.2f} "
                "< -${:.2f} ({:.1%} of ${:.0f}) — pausing quoting until UTC midnight",
                self.realised_pnl_today, mtm_dd, limit,
                self._s.DAILY_DRAWDOWN_LIMIT_PCT, self._s.TOTAL_CAPITAL,
            )
            return True

        return False

    def most_imbalanced_markets(
        self, n: int = 5, active_cids: Optional[set] = None,
    ) -> List[str]:
        """Return condition_ids of the N most skewed active markets."""
        positions = self._positions.values()
        if active_cids is not None:
            positions = [p for p in positions if p.condition_id in active_cids]
        ranked = sorted(
            positions,
            key=lambda p: p.skew_ratio,
            reverse=True,
        )
        return [p.condition_id for p in ranked[:n]]

    def exposure_breakdown(self, active_cids: Optional[set] = None) -> str:
        """Per-position exposure breakdown for diagnostic logging."""
        lines = []
        positions = sorted(
            self._positions.values(),
            key=lambda p: p.net_exposure_usd,
            reverse=True,
        )
        for p in positions:
            if p.yes_size == 0 and p.no_size == 0:
                continue
            tag = "active" if (active_cids and p.condition_id in active_cids) else "STALE"
            lines.append(
                f"  {p.condition_id[:12]} [{tag}] YES={p.yes_size:.1f} NO={p.no_size:.1f} "
                f"skew={p.skew_ratio:.0%} net=${p.net_exposure_usd:.1f}"
            )
        return "\n".join(lines) if lines else "  (no positions)"

    # ------------------------------------------------------------------
    # Pause management
    # ------------------------------------------------------------------

    def _is_paused(self, condition_id: str) -> bool:
        state = self._pause_until.get(condition_id)
        if state is None:
            return False
        if datetime.now(timezone.utc) >= state.until:
            del self._pause_until[condition_id]
            logger.info("Pause expired for {}", condition_id[:12])
            return False
        return True

    def _set_pause(self, condition_id: str, side: str) -> None:
        duration = timedelta(seconds=self._s.SKEW_PAUSE_DURATION_SECONDS)
        self._pause_until[condition_id] = _PauseState(
            until=datetime.now(timezone.utc) + duration,
            side=side,
        )

    # ------------------------------------------------------------------
    # Reconciliation (P1-1)
    # ------------------------------------------------------------------

    def reconcile(self, exchange_positions: Dict[str, Dict[str, float]]) -> int:
        """Compare local inventory vs exchange truth; correct discrepancies > 1%.

        Args:
            exchange_positions: {condition_id: {"yes": size, "no": size}}

        Returns:
            Number of positions corrected.
        """
        corrected = 0
        for cid, sizes in exchange_positions.items():
            pos = self._positions.get(cid)
            if pos is None:
                continue

            exchange_yes = sizes.get("yes", 0.0)
            exchange_no = sizes.get("no", 0.0)

            yes_denom = max(pos.yes_size, exchange_yes, 0.01)
            no_denom = max(pos.no_size, exchange_no, 0.01)
            yes_diff_pct = abs(pos.yes_size - exchange_yes) / yes_denom
            no_diff_pct = abs(pos.no_size - exchange_no) / no_denom

            if yes_diff_pct > 0.01 or no_diff_pct > 0.01:
                corrected += 1
                old_yes, old_no = pos.yes_size, pos.no_size

                if old_yes > 0 and exchange_yes > 0:
                    pos.yes_cost *= exchange_yes / old_yes
                elif exchange_yes == 0:
                    pos.yes_cost = 0.0

                if old_no > 0 and exchange_no > 0:
                    pos.no_cost *= exchange_no / old_no
                elif exchange_no == 0:
                    pos.no_cost = 0.0

                pos.yes_size = exchange_yes
                pos.no_size = exchange_no

                max_diff = max(yes_diff_pct, no_diff_pct)
                if max_diff > 0.10:
                    logger.warning(
                        "Large reconciliation {}: YES {:.2f}→{:.2f} ({:.1%}), NO {:.2f}→{:.2f} ({:.1%})",
                        cid[:12], old_yes, exchange_yes, yes_diff_pct,
                        old_no, exchange_no, no_diff_pct,
                    )
                else:
                    logger.info(
                        "Reconciled {}: YES {:.2f}→{:.2f}, NO {:.2f}→{:.2f}",
                        cid[:12], old_yes, exchange_yes, old_no, exchange_no,
                    )

        return corrected

    # ------------------------------------------------------------------
    # Summary / reporting
    # ------------------------------------------------------------------

    def summary(self) -> Dict[str, Dict]:
        """Build a dict suitable for Discord/logging."""
        out = {}
        for cid, pos in self._positions.items():
            if pos.yes_size == 0 and pos.no_size == 0:
                continue
            out[cid] = {
                "yes_size": round(pos.yes_size, 2),
                "no_size": round(pos.no_size, 2),
                "skew": round(pos.skew_ratio, 4),
                "heavy_side": pos.heavy_side,
                "net_exposure_usd": round(pos.net_exposure_usd, 2),
                "paused": self._is_paused(cid),
            }
        out["_realised_pnl"] = round(self._realised_pnl, 4)
        out["_total_fills"] = self._total_fill_count
        return out

    def save_state(self, path: Optional[Path] = None) -> None:
        """Persist size + cost basis + realised/drawdown anchors."""
        dest = path or self._persist_path
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            positions = {}
            for cid, pos in self._positions.items():
                positions[cid] = {
                    "yes_token_id": pos.yes_token_id,
                    "no_token_id": pos.no_token_id,
                    "yes_size": pos.yes_size,
                    "no_size": pos.no_size,
                    "yes_cost": pos.yes_cost,
                    "no_cost": pos.no_cost,
                }
            dest.write_text(json.dumps({
                "realised_pnl": self._realised_pnl,
                "realised_pnl_day_anchor": self._realised_pnl_day_anchor,
                "mtm_equity_day_anchor": self._mtm_equity_day_anchor,
                "current_day_utc": self._current_day_utc,
                "positions": positions,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }))
        except Exception as exc:
            logger.warning("Failed to persist inventory state: {}", exc)

    def apply_persisted_state(self, path: Optional[Path] = None) -> None:
        """Merge disk state after chain seed.

        File costs win when sizes match within dust. Orphan cids (not in
        the current market list) are restored in full so capital is not
        dropped on the floor. Realised/drawdown anchors restored as-is.
        """
        src = path or self._persist_path
        try:
            if not src.exists():
                return
            data = json.loads(src.read_text())
        except Exception as exc:
            logger.warning("Failed to load inventory state: {}", exc)
            return
        if not isinstance(data, dict):
            return
        self._realised_pnl = float(data.get("realised_pnl", self._realised_pnl) or 0.0)
        self._realised_pnl_day_anchor = float(
            data.get("realised_pnl_day_anchor", self._realised_pnl_day_anchor) or 0.0
        )
        mtm_anchor = data.get("mtm_equity_day_anchor")
        if mtm_anchor is not None:
            try:
                self._mtm_equity_day_anchor = float(mtm_anchor)
            except (TypeError, ValueError):
                pass
        file_day = str(data.get("current_day_utc") or "")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if file_day == today:
            self._current_day_utc = file_day
        recs = data.get("positions") or {}
        if not isinstance(recs, dict):
            return
        for cid, rec in recs.items():
            if not isinstance(rec, dict):
                continue
            pos = self.ensure_position(
                cid,
                yes_token_id=str(rec.get("yes_token_id") or ""),
                no_token_id=str(rec.get("no_token_id") or ""),
            )
            file_yes = float(rec.get("yes_size") or 0.0)
            file_no = float(rec.get("no_size") or 0.0)
            file_yc = float(rec.get("yes_cost") or 0.0)
            file_nc = float(rec.get("no_cost") or 0.0)
            sizes_match = (
                abs(pos.yes_size - file_yes) <= _SIZE_DUST
                and abs(pos.no_size - file_no) <= _SIZE_DUST
            )
            if sizes_match:
                pos.yes_cost = file_yc
                pos.no_cost = file_nc
            elif pos.yes_size == 0 and pos.no_size == 0:
                pos.yes_size = file_yes
                pos.no_size = file_no
                pos.yes_cost = file_yc
                pos.no_cost = file_nc
            if rec.get("yes_token_id") and not pos.yes_token_id:
                pos.yes_token_id = str(rec["yes_token_id"])
                self._token_map[pos.yes_token_id] = (cid, "yes")
            if rec.get("no_token_id") and not pos.no_token_id:
                pos.no_token_id = str(rec["no_token_id"])
                self._token_map[pos.no_token_id] = (cid, "no")
        logger.info("Inventory state merged from {}", src)
