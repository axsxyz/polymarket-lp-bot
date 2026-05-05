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

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple, NamedTuple

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
    def net_exposure_usd(self) -> float:
        """
        Approximate USD exposure of the imbalance.
        If we hold 100 YES at avg cost 0.50 and 80 NO at 0.50,
        net exposure ≈ (100 - 80) * 0.50 = $10 directional.
        """
        if self.yes_size == 0 and self.no_size == 0:
            return 0.0
        avg_cost = 0.50
        if self.yes_size > 0 and self.yes_cost > 0:
            avg_cost = self.yes_cost / self.yes_size
        return abs(self.yes_size - self.no_size) * avg_cost

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

    @property
    def positions(self) -> Dict[str, MarketPosition]:
        return dict(self._positions)

    def get_position(self, condition_id: str) -> Optional[MarketPosition]:
        return self._positions.get(condition_id)

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

            self._total_fill_count += 1

            # Try to set missing token IDs from the global token map
            if not pos.yes_token_id or not pos.no_token_id:
                mapping = self._token_map.get(f.token_id)
                if mapping and mapping[0] == f.condition_id:
                    if mapping[1] == "yes" and not pos.yes_token_id:
                        pos.yes_token_id = f.token_id
                    elif mapping[1] == "no" and not pos.no_token_id:
                        pos.no_token_id = f.token_id

            if pos.yes_token_id and f.token_id == pos.yes_token_id:
                is_yes = True
            elif pos.no_token_id and f.token_id == pos.no_token_id:
                is_yes = False
            elif not pos.yes_token_id and not pos.no_token_id:
                logger.warning(
                    "Fill for {} with unknown token IDs — skipping (token: {})",
                    f.condition_id[:12], f.token_id[:12],
                )
                continue
            elif pos.yes_token_id:
                is_yes = False
            else:
                is_yes = True

            if f.side == "BID":
                if is_yes:
                    pos.yes_size += f.filled_size
                    pos.yes_cost += f.filled_size * f.price
                else:
                    pos.no_size += f.filled_size
                    pos.no_cost += f.filled_size * f.price
            elif f.side == "ASK":
                if is_yes:
                    avg_cost = (pos.yes_cost / pos.yes_size) if pos.yes_size > 0 else f.price
                    pnl = (f.price - avg_cost) * f.filled_size
                    self._realised_pnl += pnl
                    pos.yes_size = max(0, pos.yes_size - f.filled_size)
                    pos.yes_cost = max(0, pos.yes_cost - f.filled_size * avg_cost)
                else:
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
        return sum(p.net_exposure_usd for p in positions)

    def is_global_exposure_breached(self, active_cids: Optional[set] = None) -> bool:
        limit = self._s.GLOBAL_MAX_EXPOSURE_PCT * self._s.TOTAL_CAPITAL
        return self.global_exposure_usd(active_cids) > limit

    def gross_exposure_usd(
        self, open_order_notional: float = 0.0, active_cids: Optional[set] = None,
    ) -> float:
        """Total gross exposure: filled positions (mark-to-mid) + open order notional."""
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
        self, open_order_notional: float = 0.0, active_cids: Optional[set] = None,
    ) -> bool:
        limit = self._s.GLOBAL_MAX_GROSS_EXPOSURE_PCT * self._s.TOTAL_CAPITAL
        return self.gross_exposure_usd(open_order_notional, active_cids) > limit

    # ------------------------------------------------------------------
    # Daily drawdown circuit breaker
    # ------------------------------------------------------------------

    def check_daily_drawdown(self) -> bool:
        """
        Evaluate the daily drawdown circuit breaker.

        - At UTC midnight rollover the PnL anchor is re-snapshotted and the
          pause flag cleared (the new day starts fresh).
        - Triggers pause when realised_pnl_today < -(LIMIT_PCT × TOTAL_CAPITAL).

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
            self._drawdown_paused = False

        if self._drawdown_paused:
            return True

        limit = self._s.DAILY_DRAWDOWN_LIMIT_PCT * self._s.TOTAL_CAPITAL
        if limit <= 0:
            return False

        if self.realised_pnl_today < -limit:
            self._drawdown_paused = True
            logger.warning(
                "Daily drawdown breached: realised PnL today=${:.2f} < -${:.2f} "
                "({:.1%} of ${:.0f}) — pausing quoting until UTC midnight",
                self.realised_pnl_today, limit,
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
