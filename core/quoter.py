"""
Quoting engine — computes optimal two-sided bid/ask for each market.

The Polymarket LP rewards use a quadratic scoring function:

    S(v, s) = ((v - s) / v) ** 2

where v = max_spread (in dollars), s = distance from adjusted midpoint.
Tighter quotes score quadratically better, but tighter also means more
adverse-selection risk if the market moves.

Our approach:
  1. Fetch the live midpoint from the CLOB orderbook.
  2. Place bid at  mid - (max_spread * tightness_factor)
     Place ask at  mid + (max_spread * tightness_factor)
  3. Size each side at  min_size * size_multiplier, capped by the
     per-market capital allocation (split 50/50 bid vs ask).
  4. Apply inventory skew adjustments: if we're long YES, widen the
     bid (less eager to buy more YES) and tighten the ask (more eager
     to sell YES).  Vice-versa for long NO.
  5. Clamp all prices to [0.01, 0.99] and round to the market tick.

The quoter is stateless — it reads the current midpoint + inventory and
outputs a fresh Quote every cycle.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Deque, Dict, Optional, Tuple

from loguru import logger

from config.markets_filter import MarketCandidate
from config.settings import Settings

if TYPE_CHECKING:
    from core.toxicity_analyzer import ToxicityAnalyzer


@dataclass(frozen=True)
class Quote:
    """Immutable snapshot of the two-sided quote for one token book."""

    token_id: str
    bid_price: float
    bid_size: float
    ask_price: float
    ask_size: float
    mid: float
    spread_used: float  # actual offset from mid, in dollars


@dataclass(frozen=True)
class TwoSidedQuotes:
    """Paired quotes for YES and NO books of a single market."""

    condition_id: str
    yes_quote: Optional[Quote]
    no_quote: Optional[Quote]


# --------------------------------------------------------------------------
# Tick rounding
# --------------------------------------------------------------------------

_TICK_MAP = {
    "0.1": 0.1,
    "0.01": 0.01,
    "0.001": 0.001,
    "0.0001": 0.0001,
}

# P2a: rolling vol/spike detection — sized for VOL_WINDOW_SECONDS up to
# 3600s with bursts of ~3 mids/sec from WS price_change events. Acts as
# a safety bound; expiry by timestamp is the actual pruning mechanism.
_VOL_HISTORY_MAX_LEN = 3000
_VOL_MIN_SAMPLES = 5


def _round_to_tick(price: float, tick_str: str) -> float:
    tick = _TICK_MAP.get(tick_str, 0.01)
    return round(round(price / tick) * tick, 4)


def _clamp(price: float, lo: float = 0.01, hi: float = 0.99) -> float:
    return max(lo, min(hi, price))


def _pstdev_fast(samples: list[float]) -> float:
    """Population std-dev in float space — replaces statistics.pstdev.

    Uses math.fsum for compensated summation (same numerical stability as
    the stdlib's exact-Fraction path) without allocating Fraction objects.
    Benchmarks ~50-100× faster on typical window sizes of 100-3000 samples.
    """
    n = len(samples)
    if n < 2:
        return 0.0
    mean = math.fsum(samples) / n
    variance = math.fsum((x - mean) ** 2 for x in samples) / n
    return math.sqrt(variance)


# --------------------------------------------------------------------------
# Score estimator (useful for logging/monitoring)
# --------------------------------------------------------------------------

def estimate_q_score(max_spread: float, spread_from_mid: float, size: float) -> float:
    """
    Estimate the instantaneous Q contribution of a single order.
    S(v, s) = ((v - s) / v)^2 * size
    """
    if max_spread <= 0 or spread_from_mid >= max_spread:
        return 0.0
    ratio = (max_spread - spread_from_mid) / max_spread
    return (ratio ** 2) * size


# --------------------------------------------------------------------------
# Quoter
# --------------------------------------------------------------------------

class Quoter:
    """Computes optimal quotes for each market each cycle."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._scoring_rates: dict[str, float] = {}
        # Optional dependency, attached at startup. None → neutral 1.0
        # multiplier in _compute_offset (cold-start / DI-less safe).
        self._toxicity: Optional["ToxicityAnalyzer"] = None
        # Per-market midpoint-guard state (driven by cold cycle only).
        self._midpoint_breach_count: dict[str, int] = {}
        self._midpoint_paused: set[str] = set()
        # P2a: rolling mid history per market for realised-vol + spike
        # detection. Keyed by condition_id, each entry is (monotonic_ts, mid).
        self._price_history: Dict[str, Deque[Tuple[float, float]]] = {}
        self._spike_paused_until: Dict[str, float] = {}
        # Optional cap on samples returned by _samples_in_window. Set to a
        # small value (e.g. 150) by the backtest to avoid O(N) pstdev on
        # deques that are fully in-window (monotonic clock barely advances).
        # None → no cap (live bot default).
        self._vol_max_samples: Optional[int] = None

    def update_scoring_rates(self, rates: dict[str, float]) -> None:
        """Update per-market scoring rates from ScoringMonitor."""
        self._scoring_rates = dict(rates)

    def attach_toxicity_analyzer(self, analyzer: "ToxicityAnalyzer") -> None:
        """Wire the toxicity analyzer; called once at startup from main.py.

        Optional dependency — when None, _compute_offset uses a neutral
        multiplier of 1.0 so legacy code paths and tests work unchanged.
        """
        self._toxicity = analyzer

    def evaluate_midpoint_guard(
        self, condition_id: str, live_mid: float
    ) -> tuple[bool, int]:
        """Track consecutive midpoint breaches, return (is_paused, breach_count).

        Increments the per-market counter when ``live_mid`` falls outside
        [MIDPOINT_GUARD_MIN, MIDPOINT_GUARD_MAX]; resets to 0 inside the band.
        Once the counter reaches MIDPOINT_GUARD_CYCLES the market is flagged
        paused; pause clears as soon as a single in-band reading is observed
        (auto-recovery). Static MIDPOINT_MIN/MAX selection filter is untouched.
        """
        out_of_band = (
            live_mid < self._s.MIDPOINT_GUARD_MIN
            or live_mid > self._s.MIDPOINT_GUARD_MAX
        )
        if out_of_band:
            count = self._midpoint_breach_count.get(condition_id, 0) + 1
            self._midpoint_breach_count[condition_id] = count
            if count >= self._s.MIDPOINT_GUARD_CYCLES:
                self._midpoint_paused.add(condition_id)
            return (condition_id in self._midpoint_paused, count)

        self._midpoint_breach_count[condition_id] = 0
        self._midpoint_paused.discard(condition_id)
        return (False, 0)

    def is_midpoint_paused(self, condition_id: str) -> bool:
        """Cheap read-only check used by the hot WS path."""
        return condition_id in self._midpoint_paused

    # ------------------------------------------------------------------
    # P2a — Realised-volatility + spike detection
    # ------------------------------------------------------------------

    def record_mid(self, condition_id: str, mid: float) -> None:
        """Append a (monotonic_ts, mid) sample to the rolling history.

        Called by both the hot WS path and the cold cycle so the volatility
        window stays fresh whether or not WS is healthy. Old samples are
        pruned lazily on read by ``_samples_in_window``; the deque maxlen
        is a hard safety net against unbounded growth on extreme bursts.
        """
        if mid <= 0.0 or mid >= 1.0:
            return
        history = self._price_history.get(condition_id)
        if history is None:
            history = deque(maxlen=_VOL_HISTORY_MAX_LEN)
            self._price_history[condition_id] = history
        history.append((time.monotonic(), mid))

    def _samples_in_window(
        self,
        condition_id: str,
        window_seconds: float,
        max_samples: Optional[int] = None,
    ) -> list[float]:
        """Return mid values within the last *window_seconds* (newest last).

        ``max_samples`` caps the returned list to the most-recent N entries.
        Use in the backtest (default 150) to bound pstdev cost when the
        monotonic clock doesn't advance and the full deque is always in-window.
        Live usage leaves ``max_samples=None`` (no cap, identical behaviour).
        """
        history = self._price_history.get(condition_id)
        if not history:
            return []
        cutoff = time.monotonic() - window_seconds
        samples = [mid for ts, mid in history if ts >= cutoff]
        if max_samples is not None and len(samples) > max_samples:
            return samples[-max_samples:]
        return samples

    def _compute_vol_multiplier(
        self, samples: list[float], tick: float,
    ) -> float:
        """Map realised stdev (in ticks) over VOL_WINDOW_SECONDS to a
        spread multiplier in [VOL_SPREAD_MULTIPLIER_MIN, VOL_SPREAD_MULTIPLIER_MAX].

        - stdev <= 1 tick  → linearly between MIN and 1.0  (calm → tighten)
        - stdev > 1 tick   → linearly between 1.0 and MAX  (volatile → widen)
        Insufficient history → 1.0 (neutral, no regression on cold start).

        ``samples`` is the pre-fetched window from ``_samples_in_window``,
        computed once per ``compute()`` call and shared across YES + NO quotes.
        """
        if len(samples) < _VOL_MIN_SAMPLES:
            return 1.0
        stdev_dollars = _pstdev_fast(samples)
        stdev_ticks = stdev_dollars / max(tick, 1e-6)

        lo = self._s.VOL_SPREAD_MULTIPLIER_MIN
        hi = self._s.VOL_SPREAD_MULTIPLIER_MAX
        if stdev_ticks <= 1.0:
            mult = lo + (1.0 - lo) * stdev_ticks
        else:
            ratio = min((stdev_ticks - 1.0) / 4.0, 1.0)
            mult = 1.0 + ratio * (hi - 1.0)
        return max(lo, min(hi, mult))

    def evaluate_spike(
        self, condition_id: str, tick: float,
    ) -> Tuple[bool, int]:
        """Return ``(is_spike, ticks_moved)`` over the spike window.

        Uses **range** (max - min) rather than net move so that pump-and-revert
        patterns still trigger. Caller must invoke ``record_mid`` first to
        keep the history fresh — both the hot and cold path do this.
        """
        samples = self._samples_in_window(
            condition_id, self._s.SPIKE_DETECTION_WINDOW_SECONDS,
        )
        if len(samples) < 2:
            return (False, 0)
        rng = max(samples) - min(samples)
        ticks_moved = int(rng / max(tick, 1e-6))
        return (ticks_moved >= self._s.SPIKE_TICKS_THRESHOLD, ticks_moved)

    def trigger_spike_pause(self, condition_id: str) -> None:
        """Mark *condition_id* as spike-paused for SPIKE_PAUSE_SECONDS."""
        self._spike_paused_until[condition_id] = (
            time.monotonic() + self._s.SPIKE_PAUSE_SECONDS
        )

    def is_spike_paused(self, condition_id: str) -> bool:
        """Cheap read-only check; auto-clears when the deadline elapses."""
        until = self._spike_paused_until.get(condition_id)
        if until is None:
            return False
        if time.monotonic() >= until:
            del self._spike_paused_until[condition_id]
            return False
        return True

    def spike_pause_ttl(self, condition_id: str) -> float:
        """Seconds remaining on a spike pause, or 0.0 if not paused."""
        until = self._spike_paused_until.get(condition_id)
        if until is None:
            return 0.0
        return max(0.0, until - time.monotonic())

    def compute(
        self,
        market: MarketCandidate,
        live_mid: float,
        skew_ratio: float = 0.0,
        heavy_side: str = "",
        paused_side: str = "",
        recovery_side: str = "",
        yes_position: float = 0.0,
        no_position: float = 0.0,
    ) -> TwoSidedQuotes:
        """
        Build two-sided quotes for a market.

        Args:
            market: MarketCandidate with reward params and allocated capital.
            live_mid: Current midpoint for the YES token (0-1 scale).
            skew_ratio: Current inventory skew ratio [0, 1].  0 = perfectly balanced.
            heavy_side: "YES" or "NO" indicating which side is over-weight.
            paused_side: if set, we skip quoting on this token side entirely
                         (hard pause when skew >= SKEW_HARD_PAUSE_THRESHOLD).
            recovery_side: if set, reduced quoting on this side (skew 0.45-0.70).
            yes_position: Current YES token holding (used to skip sells when flat).
            no_position: Current NO token holding (used to skip sells when flat).
        """
        if not market.token_id_yes or not market.token_id_no:
            logger.debug("Skipping market %s - missing token_ids", market.condition_id[:12])
            return TwoSidedQuotes(condition_id=market.condition_id, yes_quote=None, no_quote=None)

        # Fetch vol window once; reused for both YES and NO quotes so
        # _samples_in_window is not called twice per cycle.
        vol_samples: list[float] = (
            self._samples_in_window(
                market.condition_id,
                self._s.VOL_WINDOW_SECONDS,
                max_samples=self._vol_max_samples,
            )
            if self._s.QUOTE_OFFSET_TICKS > 0 else []
        )

        yes_quote = self._build_quote(
            market=market,
            token_id=market.token_id_yes,
            mid=live_mid,
            skew_ratio=skew_ratio,
            is_heavy_side=(heavy_side == "YES"),
            is_paused=(paused_side == "YES"),
            is_recovery=(recovery_side == "YES"),
            position_size=yes_position,
            vol_samples=vol_samples,
        )

        no_mid = _clamp(1.0 - live_mid)
        no_quote = self._build_quote(
            market=market,
            token_id=market.token_id_no,
            mid=no_mid,
            skew_ratio=skew_ratio,
            is_heavy_side=(heavy_side == "NO"),
            is_paused=(paused_side == "NO"),
            is_recovery=(recovery_side == "NO"),
            position_size=no_position,
            vol_samples=vol_samples,
        )

        return TwoSidedQuotes(
            condition_id=market.condition_id,
            yes_quote=yes_quote,
            no_quote=no_quote,
        )

    def _compute_offset(
        self, market: MarketCandidate, mid: float, vol_samples: list[float] | None = None,
    ) -> float:
        """Per-market quote offset from mid in dollars.

        When QUOTE_OFFSET_TICKS > 0, computes the offset directly in dollar
        terms (ticks * tick_size), incorporating realised-vol and toxicity
        multipliers. Falls back to the legacy tightness-factor approach
        when QUOTE_OFFSET_TICKS=0.

        ``vol_samples`` is the pre-fetched volatility window passed down from
        ``compute()`` so YES and NO quotes share the same sample set.

        Returns a clamped offset: at least 1 tick, at most max_spread * 0.499.
        """
        tick = _TICK_MAP.get(market.tick_size, 0.01)

        if self._s.QUOTE_OFFSET_TICKS > 0:
            # vol_samples pre-fetched by compute() for the YES+NO shared window;
            # fall back to a fresh fetch when called directly (e.g. tests).
            _samples = (
                vol_samples if vol_samples is not None
                else self._samples_in_window(
                    market.condition_id,
                    self._s.VOL_WINDOW_SECONDS,
                    max_samples=self._vol_max_samples,
                )
            )
            vol_mult = self._compute_vol_multiplier(_samples, tick)
            tox_mult = (
                self._toxicity.get_spread_multiplier(market.condition_id, tick)
                if self._toxicity is not None else 1.0
            )
            offset = self._s.QUOTE_OFFSET_TICKS * tick * vol_mult * tox_mult
        else:
            offset = market.max_spread * self._s.tightness_factor

        return max(tick, min(offset, market.max_spread * 0.499))

    def _build_quote(
        self,
        market: MarketCandidate,
        token_id: str,
        mid: float,
        skew_ratio: float,
        is_heavy_side: bool,
        is_paused: bool,
        is_recovery: bool = False,
        position_size: float = 0.0,
        vol_samples: list[float] | None = None,
    ) -> Optional[Quote]:
        """Build bid+ask for a single token book using tick-based offsets."""
        if not token_id:
            return None
        if is_paused:
            logger.debug("Skipping {} — hard-paused due to extreme skew", token_id[:16])
            return None

        base_offset = self._compute_offset(market, mid, vol_samples or [])
        tick = _TICK_MAP.get(market.tick_size, 0.01)
        max_sp = market.max_spread
        if max_sp > 0.50:
            logger.warning(
                "max_spread={:.4f} for {} exceeds 0.50 — capping at 0.50",
                max_sp, market.condition_id[:12],
            )
            max_sp = 0.50

        bid_offset = base_offset
        ask_offset = base_offset

        if skew_ratio > 0.08:
            skew_widen = base_offset * skew_ratio * 1.4
            skew_tighten = base_offset * skew_ratio * 0.85
            if is_heavy_side:
                bid_offset = min(base_offset + skew_widen, max_sp * 0.92)
                ask_offset = max(base_offset - skew_tighten, tick)
            else:
                bid_offset = max(base_offset - skew_tighten, tick)
                ask_offset = min(base_offset + skew_widen, max_sp * 0.92)

        if is_recovery and is_heavy_side:
            bid_offset = min(bid_offset * 2.0, max_sp * 0.95)
            ask_offset = max(ask_offset * 0.5, tick)

        raw_bid = mid - bid_offset
        raw_ask = mid + ask_offset
        if raw_bid <= 0.01 or raw_ask >= 0.99:
            logger.warning(
                "Quote clamped! mid={:.4f} max_spread={:.4f} "
                "bid_offset={:.4f} ask_offset={:.4f} raw_bid={:.4f} raw_ask={:.4f}",
                mid, max_sp, bid_offset, ask_offset, raw_bid, raw_ask,
            )

        bid_price = _clamp(_round_to_tick(mid - bid_offset, market.tick_size))
        ask_price = _clamp(_round_to_tick(mid + ask_offset, market.tick_size))

        if ask_price - bid_price < tick * 1.5:
            bid_price = _clamp(mid - tick * 1.2)
            ask_price = _clamp(mid + tick * 1.2)

        half_capital = market.allocated_capital / 2.0
        base_size = market.min_size * self._s.SIZE_MULTIPLIER

        max_bid_shares = half_capital / max(bid_price, 0.01)
        max_ask_shares = half_capital / max(1.0 - ask_price, 0.01)

        bid_size = round(min(base_size, max_bid_shares), 2)
        ask_size = round(min(base_size, max_ask_shares), 2)

        if is_recovery and is_heavy_side:
            reduced = round(ask_size * self._s.SKEW_RECOVERY_ASK_SIZE_PCT, 2)
            ask_size = max(reduced, market.min_size)

        if position_size <= 0:
            ask_size = 0.0
        elif ask_size > position_size:
            ask_size = round(position_size, 2)

        if bid_size > 0 and bid_size < market.min_size:
            logger.info(
                "Skipping BID for {} — need {} shares but capital only covers {:.0f} (min_size={})",
                token_id[:12], market.min_size, max_bid_shares, market.min_size,
            )
            bid_price = 0.0
            bid_size = 0.0

        if ask_size > 0 and ask_size < market.min_size:
            logger.info(
                "Skipping ASK for {} — need {} shares but capital only covers {:.0f} (min_size={})",
                token_id[:12], market.min_size, max_ask_shares, market.min_size,
            )
            ask_price = 0.0
            ask_size = 0.0

        if bid_size == 0 and ask_size == 0:
            return None

        avg_spread = (bid_offset + ask_offset) / 2.0
        q_est = estimate_q_score(max_sp, avg_spread, (bid_size + ask_size) / 2.0)
        q_pct = ((max_sp - avg_spread) / max_sp) ** 2 * 100 if max_sp > 0 else 0

        logger.info(
            "Quote {}  bid={:.4f}x{:.1f}  ask={:.4f}x{:.1f}  offset={:.4f}  Q≈{:.2f} ({:.0f}%)  skew={:.1%}",
            token_id[:12], bid_price, bid_size, ask_price, ask_size,
            avg_spread, q_est, q_pct, skew_ratio,
        )

        return Quote(
            token_id=token_id,
            bid_price=bid_price,
            bid_size=bid_size,
            ask_price=ask_price,
            ask_size=ask_size,
            mid=mid,
            spread_used=avg_spread,
        )
