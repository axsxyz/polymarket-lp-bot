"""
Toxicity analyzer — adverse-selection defense (P2b).

Two complementary signals per market, both maintained in memory and
evaluated once per cold cycle (no API calls, no persistence):

  1. VPIN (Volume-synchronised Probability of Informed Trading, simplified):
     buy/sell imbalance over a rolling window of bot fills.
       - imb >= VPIN_THRESHOLD          → linear-ramp spread multiplier
       - imb >= VPIN_PAUSE_THRESHOLD    → cancel quotes + pause
                                          (TOXICITY_PAUSE_SECONDS)

  2. Fill-vs-mid scoring:
     for each bot fill, evaluate the adverse mid drift at +5s / +30s /
     +5min. Drift is computed in ticks, weighted 0.2 / 0.3 / 0.5 across
     the three horizons, clamped to [0, +∞) (we measure pain, not
     pleasure — favourable mid moves don't deflate the score).

The two signals combine via max() into a single multiplier in the
range [1.0, TOXICITY_SPREAD_MULTIPLIER_MAX]. The Quoter multiplies this
on top of QUOTE_OFFSET_TICKS in `_compute_offset`. Final offset stays
bounded by max_spread * 0.499 (existing structural clamp).

Replaces the legacy `_fill_rates` blunt instrument that widened on
coarse fills/hour bins (true/false buckets, no directional info).

Cold path only — hot path reads `is_toxic_paused()` for symmetry with
midpoint guard (Quoter.is_midpoint_paused) and spike pause
(Quoter.is_spike_paused).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Deque, Dict, List, Optional, Tuple

from loguru import logger

from config.settings import Settings

if TYPE_CHECKING:
    from core.order_manager import FillEvent


# ──────────────────────────────────────────────────────────────────────
# Module constants — fixed by design (not env-tunable)
# ──────────────────────────────────────────────────────────────────────

# Three horizons after each fill at which the mid is sampled to detect
# adverse drift. Weights tilt toward the mid horizon — instantaneous
# noise (5s) is unreliable; 5min captures genuine information flow.
_HORIZONS_S: Tuple[float, ...] = (5.0, 30.0, 300.0)
_HORIZON_WEIGHTS: Tuple[float, ...] = (0.2, 0.3, 0.5)

# Mid-history maxlen — same safety bound as Quoter._price_history.
# Pruning is by maxlen only; horizons cap at 5min so we never need older
# samples than that. Maxlen is a hard ceiling against bursty WS streams.
_MID_HISTORY_MAXLEN = 3000

# Tolerance window for matching a mid sample to a horizon's target_ts.
# Mid samples are sparse on quiet markets (~1/cycle when WS misses);
# a few seconds of slack avoids dropping signal needlessly.
_MID_LOOKUP_TOLERANCE_S = 2.0

# Toxicity score → multiplier ramp denominator. 0 ticks → 1.0;
# >=  this many adverse ticks → MAX. Five ticks of mean adverse drift
# over the rolling window = systematic pick-off, deserves max widening.
_TOX_SCORE_DENOMINATOR_TICKS = 5.0

# VPIN below this many fills is too noisy to trust (an all-BID streak
# of 4 fills is normal market chop, not informed flow).
_VPIN_MIN_FILLS = 10

# Defensive cap on the per-market pending-fills list. Cold-cycle pruning
# normally keeps it small; this prevents pathological growth if
# evaluate() is somehow not called for a particular market for a long
# stretch (e.g. cid removed from selection mid-flight).
_PENDING_FILLS_HARD_CAP = 500

# Maxlen safety bound on the rolling toxicity samples deque per market.
# Time-based pruning in evaluate() is the actual mechanism; this only
# catches degenerate cases where evaluate() lags fills.
_TOX_SAMPLES_MAXLEN = 1000


# ──────────────────────────────────────────────────────────────────────
# Internal data
# ──────────────────────────────────────────────────────────────────────

@dataclass
class _PendingFill:
    """A bot fill awaiting horizon evaluation.

    `horizon_scores` maps horizon (in seconds) to:
      - None    → not yet evaluated (target_ts in the future)
      - float   → evaluated (adverse drift in ticks, clamped >= 0)
    """
    cid: str
    side: str  # "BID" (bot bought) or "ASK" (bot sold)
    price: float
    posted_at: float  # monotonic
    horizon_scores: Dict[float, Optional[float]] = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────
# Public class
# ──────────────────────────────────────────────────────────────────────

class ToxicityAnalyzer:
    """Per-market VPIN + fill-vs-mid adverse-selection scoring.

    Lifecycle (cold cycle, once per market per cycle):
      1. main.py → record_mid(cid, mid)        # for every market with a mid
      2. main.py → record_fill(fill_event)     # for every drained fill
      3. main.py → evaluate(cid, tick)         # processes pending, may set pause
      4. quoter  → get_spread_multiplier(...)  # consumed in _compute_offset
      5. main.py → is_toxic_paused(cid)        # add to paused_set on transition

    The hot path only reads `is_toxic_paused` (cheap dict lookup) for
    symmetry with the existing midpoint-guard / spike-pause checks.
    """

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        # VPIN: rolling sides window per market — append-only, maxlen-bounded.
        self._fill_sides: Dict[str, Deque[str]] = {}
        # Mid history per market, used by `_lookup_mid_at` for fill-vs-mid.
        self._mid_history: Dict[str, Deque[Tuple[float, float]]] = {}
        # Fills waiting on horizon evaluation; list (not deque) because we
        # filter mid-iteration via list comprehension in `evaluate`.
        self._pending_fills: Dict[str, List[_PendingFill]] = {}
        # Finalised per-fill scores in ticks, time-pruned in `evaluate`.
        self._toxicity_samples: Dict[str, Deque[Tuple[float, float]]] = {}
        # VPIN-pause deadlines (monotonic). Auto-clear on read.
        self._paused_until: Dict[str, float] = {}

    # ── Ingestion ──────────────────────────────────────────────────

    def record_mid(self, cid: str, mid: float) -> None:
        """Append a (monotonic_ts, mid) sample for a market.

        Out-of-band values (mid <= 0 or >= 1) are dropped — consistent
        with `Quoter.record_mid` so the same input feed can drive both.
        Idempotent on duplicate timestamps (deque allows duplicates).
        """
        if mid <= 0.0 or mid >= 1.0:
            return
        history = self._mid_history.get(cid)
        if history is None:
            history = deque(maxlen=_MID_HISTORY_MAXLEN)
            self._mid_history[cid] = history
        history.append((time.monotonic(), mid))

    def record_fill(self, fill: FillEvent) -> None:
        """Register a bot fill for VPIN + pending fill-vs-mid evaluation.

        Side must be "BID" or "ASK"; other values are dropped with a
        debug log. Caller (main.py cold cycle) is responsible for
        deduplication — `FillCollector` already enforces this via
        watermark + seen_ids set.
        """
        side = fill.side
        if side not in ("BID", "ASK"):
            logger.debug("Toxicity: skipping fill with unknown side {!r}", side)
            return
        cid = fill.condition_id
        if not cid:
            return

        sides = self._fill_sides.get(cid)
        if sides is None:
            sides = deque(maxlen=self._s.VPIN_WINDOW_FILLS)
            self._fill_sides[cid] = sides
        sides.append(side)

        pending = self._pending_fills.setdefault(cid, [])
        pending.append(_PendingFill(
            cid=cid,
            side=side,
            price=float(fill.price),
            posted_at=time.monotonic(),
            horizon_scores={h: None for h in _HORIZONS_S},
        ))

        if len(pending) > _PENDING_FILLS_HARD_CAP:
            dropped = len(pending) - _PENDING_FILLS_HARD_CAP
            del pending[:dropped]
            logger.warning(
                "Toxicity: pending fills cap hit for {}, dropped {} oldest",
                cid[:12], dropped,
            )

    # ── Per-cycle evaluation (cold path) ───────────────────────────

    def evaluate(self, cid: str, tick: float) -> None:
        """Process pending horizons, push finalised fills into the
        rolling score, prune old samples, (re)check VPIN pause.

        Called once per market per cold cycle from main.py. Idempotent
        on rapid re-call (already-evaluated horizons are skipped).
        """
        now = time.monotonic()

        pending = self._pending_fills.get(cid)
        if pending:
            still_pending: List[_PendingFill] = []
            for pf in pending:
                self._evaluate_horizons(pf, now, tick)
                if self._is_finalised(pf, now):
                    self._push_score(cid, pf, now)
                else:
                    still_pending.append(pf)
            self._pending_fills[cid] = still_pending

        # Prune toxicity samples older than the rolling window.
        samples = self._toxicity_samples.get(cid)
        if samples:
            cutoff = now - self._s.TOXICITY_FILL_WINDOW_SECONDS
            while samples and samples[0][0] < cutoff:
                samples.popleft()

        # VPIN pause check. PAUSE_THRESHOLD trip clamps `_paused_until`
        # to now + TOXICITY_PAUSE_SECONDS; main.py picks the transition
        # up via the `is_toxic_paused` before/after pattern.
        sides = self._fill_sides.get(cid)
        if sides and len(sides) >= _VPIN_MIN_FILLS:
            imb = self._compute_imbalance(sides)
            if imb >= self._s.VPIN_PAUSE_THRESHOLD:
                self._paused_until[cid] = now + self._s.TOXICITY_PAUSE_SECONDS

    # ── Consumers (Quoter + hot/cold path) ─────────────────────────

    def get_spread_multiplier(self, cid: str, tick: float) -> float:
        """Return the multiplier (∈ [1.0, TOXICITY_SPREAD_MULTIPLIER_MAX])
        applied by the Quoter on top of QUOTE_OFFSET_TICKS.

        Called every quote computation (cheap — dict lookups + couple
        of arithmetic ops). Defaults to 1.0 on a market with no signal
        (cold start safe — no regression vs legacy _fill_rates code path
        when fills haven't arrived yet).

        The two signals (VPIN, fill-vs-mid) are combined via max(), not
        multiplication: each is a probabilistic estimate of toxicity on
        a different timescale, so taking the most conservative one is
        defensive and predictable.
        """
        max_mult = self._s.TOXICITY_SPREAD_MULTIPLIER_MAX

        # ── VPIN component ──
        vpin_mult = 1.0
        sides = self._fill_sides.get(cid)
        if sides and len(sides) >= _VPIN_MIN_FILLS:
            imb = self._compute_imbalance(sides)
            if imb >= self._s.VPIN_THRESHOLD:
                band = max(
                    1e-9,
                    self._s.VPIN_PAUSE_THRESHOLD - self._s.VPIN_THRESHOLD,
                )
                ratio = min(1.0, (imb - self._s.VPIN_THRESHOLD) / band)
                vpin_mult = 1.0 + ratio * (max_mult - 1.0)

        # ── Fill-vs-mid toxicity component ──
        tox_mult = 1.0
        score = self._mean_score_ticks(cid)
        if score > 0.0:
            ratio = min(1.0, score / _TOX_SCORE_DENOMINATOR_TICKS)
            tox_mult = 1.0 + ratio * (max_mult - 1.0)

        return min(max_mult, max(vpin_mult, tox_mult))

    def is_toxic_paused(self, cid: str) -> bool:
        """Cheap read; auto-clears when the pause deadline elapses.

        Called from both the hot path (in `hot_requote`, planned for
        step 6) and the cold cycle's "Step 4.7 toxicity guard".
        """
        until = self._paused_until.get(cid)
        if until is None:
            return False
        if time.monotonic() >= until:
            del self._paused_until[cid]
            return False
        return True

    def toxic_pause_ttl(self, cid: str) -> float:
        """Seconds remaining on a toxicity pause, or 0.0 if not paused.

        Mirrors `Quoter.spike_pause_ttl` for diagnostics and Discord
        alert formatting.
        """
        until = self._paused_until.get(cid)
        if until is None:
            return 0.0
        return max(0.0, until - time.monotonic())

    # ── Diagnostics (used by tests + Discord alerts in main.py) ────

    def vpin_imbalance(self, cid: str) -> float:
        """Current |buys − sells| / N over the rolling fill window.

        Returns 0.0 for unknown cid or empty window. Does *not* gate on
        `_VPIN_MIN_FILLS` — diagnostic queries get the raw number;
        threshold-based decisions live in evaluate / get_spread_multiplier.
        """
        sides = self._fill_sides.get(cid)
        if not sides:
            return 0.0
        return self._compute_imbalance(sides)

    def toxicity_score_ticks(self, cid: str) -> float:
        """Mean adverse drift (in ticks) over the rolling toxicity window.

        Returns 0.0 for unknown cid or empty window.
        """
        return self._mean_score_ticks(cid)

    # ── Internals ──────────────────────────────────────────────────

    @staticmethod
    def _compute_imbalance(sides: Deque[str]) -> float:
        """|buys − sells| / N over the window. N=0 → 0.0."""
        n = len(sides)
        if n == 0:
            return 0.0
        buys = sum(1 for s in sides if s == "BID")
        sells = n - buys
        return abs(buys - sells) / n

    def _mean_score_ticks(self, cid: str) -> float:
        samples = self._toxicity_samples.get(cid)
        if not samples:
            return 0.0
        return sum(score for _ts, score in samples) / len(samples)

    def _evaluate_horizons(
        self, pf: _PendingFill, now: float, tick: float,
    ) -> None:
        """Fill in any horizon whose target_ts has passed.

        Once a horizon is past `target_ts + 2 * TOLERANCE` with no mid
        sample available, it's marked 0.0 (give up — fail safe; missing
        data → no extra widening signal from this fill).
        """
        for horizon, score in list(pf.horizon_scores.items()):
            if score is not None:
                continue
            target_ts = pf.posted_at + horizon
            if now < target_ts:
                continue
            mid_after = self._lookup_mid_at(pf.cid, target_ts)
            if mid_after is not None:
                pf.horizon_scores[horizon] = self._adverse_ticks(
                    pf.side, pf.price, mid_after, tick,
                )
            elif now > target_ts + _MID_LOOKUP_TOLERANCE_S * 2:
                pf.horizon_scores[horizon] = 0.0

    def _is_finalised(self, pf: _PendingFill, now: float) -> bool:
        """Pending fill is finalised when all horizons are evaluated, OR
        when its age exceeds the rolling toxicity window (defensive
        cleanup so the pending list never grows unbounded)."""
        if all(s is not None for s in pf.horizon_scores.values()):
            return True
        return (now - pf.posted_at) >= self._s.TOXICITY_FILL_WINDOW_SECONDS

    def _push_score(
        self, cid: str, pf: _PendingFill, now: float,
    ) -> None:
        """Combine evaluated horizons into a weighted score and push to
        the rolling toxicity samples. Skips horizons that gave up
        without a sample (None) — only weights what was actually seen."""
        total_weight = 0.0
        total_score = 0.0
        for horizon, weight in zip(_HORIZONS_S, _HORIZON_WEIGHTS):
            score = pf.horizon_scores.get(horizon)
            if score is None:
                continue
            total_weight += weight
            total_score += weight * score
        if total_weight <= 0.0:
            return
        final = total_score / total_weight

        samples = self._toxicity_samples.get(cid)
        if samples is None:
            samples = deque(maxlen=_TOX_SAMPLES_MAXLEN)
            self._toxicity_samples[cid] = samples
        samples.append((now, final))

    def _lookup_mid_at(
        self, cid: str, target_ts: float,
    ) -> Optional[float]:
        """Return the mid sample closest to `target_ts` within tolerance,
        or None.

        History is append-only ascending in monotonic time, so the loop
        early-breaks once `ts > upper`. Effective complexity is
        O(samples_in_tolerance_window), not O(history).
        """
        history = self._mid_history.get(cid)
        if not history:
            return None
        lower = target_ts - _MID_LOOKUP_TOLERANCE_S
        upper = target_ts + _MID_LOOKUP_TOLERANCE_S
        best_mid: Optional[float] = None
        best_dt = float("inf")
        for ts, mid in history:
            if ts < lower:
                continue
            if ts > upper:
                break
            dt = abs(ts - target_ts)
            if dt < best_dt:
                best_dt = dt
                best_mid = mid
        return best_mid

    @staticmethod
    def _adverse_ticks(
        side: str, price: float, mid_after: float, tick: float,
    ) -> float:
        """Adverse mid-drift after a bot fill, in ticks.

        - BID fill (bot bought @ price): adverse if mid_after < price
          → raw = (price - mid_after) / tick
        - ASK fill (bot sold @ price):   adverse if mid_after > price
          → raw = (mid_after - price) / tick

        Clamped to [0, +∞) so favourable outcomes don't deflate the
        rolling toxicity score (we measure pain, not pleasure).
        """
        if tick <= 0:
            return 0.0
        if side == "BID":
            raw = (price - mid_after) / tick
        else:
            raw = (mid_after - price) / tick
        return max(0.0, raw)
