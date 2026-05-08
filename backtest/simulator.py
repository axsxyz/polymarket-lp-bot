"""
backtest/simulator.py — Replay simulator on /prices-history mid tape.

Standalone: NO imports from main.py, NO live CLOB connections.

═══════════════════════════════════════════════════════════════════════
FILL MODEL — naive maker, top-of-book queue assumption
═══════════════════════════════════════════════════════════════════════

For each cycle [t, t + REBALANCE_INTERVAL_SECONDS):
  1. mid_at_post = most recent YES mid sample at-or-before t.
  2. Quoter.compute(market, mid_at_post, ...) → YES + NO quotes.
  3. Scan YES mid samples within [t, t+interval):
       BID YES filled iff min(yes_mid in window) <= bot_yes_bid_price
       ASK YES filled iff max(yes_mid in window) >= bot_yes_ask_price
       NO mid = (1 - yes_mid); same logic on bot_no_bid / bot_no_ask.
  4. fill_price = bot's quote price (full price improvement assumed).
  5. fill_size  = full quoted size (no queue position model).

Caveat: optimistic vs reality — top-of-book queueing and partial fills
are not modelled. Use results for relative grid-search ranking, NOT for
absolute PnL prediction.

═══════════════════════════════════════════════════════════════════════
LIVE BOT FIDELITY — what is replayed faithfully vs degraded
═══════════════════════════════════════════════════════════════════════

FAITHFUL:
  - Quote computation (offsets, sizes, skew, clamps): identical to live
    `Quoter.compute()` call — same code path, same arithmetic.
  - VPIN signal: ToxicityAnalyzer.record_fill is fed simulated fills with
    BID/ASK from the bot perspective. `_compute_imbalance` is a pure
    deque-count — no time dependency. VPIN_THRESHOLD and
    VPIN_PAUSE_THRESHOLD behave identically to live.
  - Midpoint guard: `evaluate_midpoint_guard` is cycle-counter based,
    no real-time clock — faithful 1:1.

DEGRADED (caveats apply when interpreting CSV):
  - Fill-vs-mid toxicity component (5s/30s/300s adverse-drift horizons):
    ToxicityAnalyzer uses `time.monotonic()`. During fast simulation the
    monotonic clock barely advances → horizons NEVER fire → toxicity
    component contributes neutral 1.0. Net effect: spread multiplier in
    backtest reflects VPIN component only.
  - Vol multiplier: Quoter._samples_in_window filters by monotonic time.
    During simulation all recorded mids look "very recent" so the window
    collapses to "all samples in deque" (maxlen=3000). Vol is still
    computed but no longer literally tied to VOL_WINDOW_SECONDS.
  - Spike detection: NOT triggered in backtest (would persist forever
    once set, since monotonic doesn't advance with sim time).

═══════════════════════════════════════════════════════════════════════
DETERMINISM
═══════════════════════════════════════════════════════════════════════
Same input (market + yes_mid_samples + params + base_settings + interval)
→ identical SimResult. Tests verify this property.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import List, Optional

from loguru import logger

from config.markets_filter import MarketCandidate
from config.settings import Settings
from core.order_manager import FillEvent
from core.quoter import Quoter
from core.toxicity_analyzer import ToxicityAnalyzer

from backtest.fetcher import HistMid

# Replicated from quoter._TICK_MAP (private) — kept in sync if ever changed.
_TICK_DOLLARS_MAP = {
    "0.1": 0.1, "0.01": 0.01, "0.001": 0.001, "0.0001": 0.0001,
}

DEFAULT_REBALANCE_INTERVAL_S = 45
HORIZON_T1MIN_S = 60
HORIZON_T5MIN_S = 300

# Tolerance in seconds for matching a mid sample to a horizon target. With
# /prices-history at 1m granularity the closest available sample may be up to
# ~30s away, so half-interval slack is the natural floor. We use slightly
# more for the longer horizon to absorb fidelity jitter.
HORIZON_T1MIN_TOLERANCE_S = 30
HORIZON_T5MIN_TOLERANCE_S = 60

# Position size used for `Quoter.compute()`: a high constant so the simulator
# never hits the inventory-cap branch that zeroes out ASK size when flat.
_INF_POSITION = 1e9


# ──────────────────────────────────────────────────────────────────
# Public dataclasses
# ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BotParams:
    """Parameters varied by the grid search."""

    quote_offset_ticks: int
    vol_spread_multiplier_max: float
    vpin_threshold: float
    vpin_pause_threshold: float          # auto = min(1.0, vpin_threshold + 0.15)
    midpoint_guard_min: float
    midpoint_guard_max: float


@dataclass(frozen=True)
class SimulatedFill:
    """One simulated fill event with full context for downstream metrics."""

    cycle_idx: int
    ts: int                              # unix seconds, fill timestamp
    side: str                            # "BID" | "ASK" (bot perspective)
    token_side: str                      # "YES" | "NO"
    fill_price: float
    fill_size: float
    mid_at_post: float                   # mid (in token-side scale) at quote post
    spread_used: float                   # |fill_price - mid_at_post|
    mid_t1min: Optional[float]           # mid in token-side scale at fill_ts + 60s
    mid_t5min: Optional[float]           # mid in token-side scale at fill_ts + 300s
    max_spread: float                    # market.max_spread snapshot for q-score


@dataclass
class SimResult:
    """Aggregate output of `simulate_market`."""

    condition_id: str
    params: BotParams
    fills: List[SimulatedFill] = field(default_factory=list)
    cycles_total: int = 0
    cycles_paused_midpoint: int = 0
    cycles_paused_toxicity: int = 0
    duration_seconds: int = 0


# ──────────────────────────────────────────────────────────────────
# Settings construction
# ──────────────────────────────────────────────────────────────────

def build_settings_for_params(
    params: BotParams, base: Optional[Settings] = None,
) -> Settings:
    """Construct a Settings instance with backtest-overridden fields.

    Bypasses .env (`_env_file=None`) so backtest is isolated from live
    config — DRY_RUN, PRIVATE_KEY, capital, etc. all come from defaults
    or `base`.
    """
    overrides = dict(
        QUOTE_OFFSET_TICKS=params.quote_offset_ticks,
        VOL_SPREAD_MULTIPLIER_MAX=params.vol_spread_multiplier_max,
        VPIN_THRESHOLD=params.vpin_threshold,
        VPIN_PAUSE_THRESHOLD=params.vpin_pause_threshold,
        MIDPOINT_GUARD_MIN=params.midpoint_guard_min,
        MIDPOINT_GUARD_MAX=params.midpoint_guard_max,
    )
    if base is not None:
        return base.model_copy(update=overrides)
    return Settings(
        _env_file=None,
        PRIVATE_KEY="0x" + "ab" * 32,
        **overrides,
    )


# ──────────────────────────────────────────────────────────────────
# Internal helpers — bisect-based mid lookup
# ──────────────────────────────────────────────────────────────────

def _mid_at_or_before(
    sample_ts: List[int], sample_mid: List[float], target_ts: int,
) -> Optional[float]:
    """Return the most recent mid with ts <= target_ts, or None."""
    idx = bisect.bisect_right(sample_ts, target_ts) - 1
    if idx < 0:
        return None
    return sample_mid[idx]


def _lookahead_mid(
    sample_ts: List[int], sample_mid: List[float],
    target_ts: int, tolerance_s: int,
) -> Optional[float]:
    """Return the mid sample CLOSEST in time to ``target_ts`` within
    ±``tolerance_s`` seconds, or None if no sample exists in that window.

    Used to evaluate mid_t1min / mid_t5min after a fill. Symmetric tolerance
    enforces both upper and lower bounds: samples too close to the fill
    (the at-post mid) are excluded, and samples too far in the future
    (past the horizon) are also excluded (user spec: cap [t, t+300s]).

    With /prices-history at 1m granularity, a half-interval tolerance is
    the natural floor; HORIZON_T1MIN_TOLERANCE_S=30 and
    HORIZON_T5MIN_TOLERANCE_S=60 give 1-sample slack each.
    """
    n = len(sample_ts)
    if n == 0:
        return None
    idx = bisect.bisect_left(sample_ts, target_ts)
    candidates: List[tuple] = []  # (|ts - target|, mid)
    if idx > 0:
        before_dist = target_ts - sample_ts[idx - 1]
        if 0 <= before_dist <= tolerance_s:
            candidates.append((before_dist, sample_mid[idx - 1]))
    if idx < n:
        after_dist = sample_ts[idx] - target_ts
        if 0 <= after_dist <= tolerance_s:
            candidates.append((after_dist, sample_mid[idx]))
    if not candidates:
        return None
    return min(candidates, key=lambda x: x[0])[1]


def _earliest_match_ts(
    window_ts: List[int], window_mid: List[float],
    predicate,
) -> Optional[int]:
    """Return ts of first sample in window where predicate(mid) is True."""
    for i, m in enumerate(window_mid):
        if predicate(m):
            return window_ts[i]
    return None


# ──────────────────────────────────────────────────────────────────
# Internal helpers — fill construction
# ──────────────────────────────────────────────────────────────────

def _build_fill(
    cycle_idx: int, fill_ts: int, side: str, token_side: str,
    fill_price: float, fill_size: float,
    mid_at_post_yes: float, max_spread: float,
    sample_ts: List[int], sample_mid: List[float],
) -> SimulatedFill:
    """Construct a SimulatedFill with horizon lookups in the proper scale.

    `mid_at_post_yes` is the YES-scale mid at quote post; for NO fills we
    convert it (and the looked-up horizon mids) to the NO scale via
    `1 - yes_mid` so `mid_at_post`, `mid_t1min`, `mid_t5min` are all
    expressed in the same scale as `fill_price`.
    """
    mid_t1_yes = _lookahead_mid(
        sample_ts, sample_mid,
        target_ts=fill_ts + HORIZON_T1MIN_S,
        tolerance_s=HORIZON_T1MIN_TOLERANCE_S,
    )
    mid_t5_yes = _lookahead_mid(
        sample_ts, sample_mid,
        target_ts=fill_ts + HORIZON_T5MIN_S,
        tolerance_s=HORIZON_T5MIN_TOLERANCE_S,
    )

    if token_side == "NO":
        mid_at_post = 1.0 - mid_at_post_yes
        mid_t1 = (1.0 - mid_t1_yes) if mid_t1_yes is not None else None
        mid_t5 = (1.0 - mid_t5_yes) if mid_t5_yes is not None else None
    else:
        mid_at_post = mid_at_post_yes
        mid_t1 = mid_t1_yes
        mid_t5 = mid_t5_yes

    return SimulatedFill(
        cycle_idx=cycle_idx,
        ts=fill_ts,
        side=side,
        token_side=token_side,
        fill_price=fill_price,
        fill_size=fill_size,
        mid_at_post=mid_at_post,
        spread_used=abs(fill_price - mid_at_post),
        mid_t1min=mid_t1,
        mid_t5min=mid_t5,
        max_spread=max_spread,
    )


# ──────────────────────────────────────────────────────────────────
# Public entry
# ──────────────────────────────────────────────────────────────────

def simulate_market(
    market: MarketCandidate,
    yes_mid_samples: List[HistMid],
    params: BotParams,
    base_settings: Optional[Settings] = None,
    rebalance_interval_seconds: int = DEFAULT_REBALANCE_INTERVAL_S,
    quiet: bool = True,
) -> SimResult:
    """Replay the bot quoting logic on a YES-mid tape and return SimResult.

    Args:
      market: Market metadata (max_spread, min_size, tick_size, token_ids).
      yes_mid_samples: HistMid list for the YES token. Sorted by `simulate_market`
        if not already.
      params: Grid-search parameters (override Settings fields).
      base_settings: Optional base Settings; defaults to a clean instance
        bypassing .env.
      rebalance_interval_seconds: Cycle length (default 45, matches live).
      quiet: When True (default) silence Quoter / ToxicityAnalyzer info logs
        for the duration. The disable is module-scoped via loguru.disable.

    Returns:
      SimResult with fills, cycle counters, duration.
    """
    if not yes_mid_samples:
        return SimResult(condition_id=market.condition_id, params=params)

    # Normalize: sort + extract parallel arrays for bisect.
    samples = sorted(yes_mid_samples, key=lambda s: s.ts)
    sample_ts: List[int] = [s.ts for s in samples]
    sample_mid: List[float] = [s.mid for s in samples]

    settings = build_settings_for_params(params, base_settings)
    tick = _TICK_DOLLARS_MAP.get(market.tick_size, 0.01)

    # Markets loaded from SQLite do not carry an allocated_capital (the live
    # bot's MarketManager assigns this). Derive a default from settings so
    # the Quoter doesn't zero out all quote sizes due to half_capital==0.
    if market.allocated_capital <= 0:
        derived = settings.TOTAL_CAPITAL / max(settings.MAX_MARKETS, 1)
        derived = max(
            settings.MIN_CAPITAL_PER_MARKET,
            min(derived, settings.MAX_CAPITAL_PER_MARKET),
        )
        market = replace(market, allocated_capital=derived)

    quoter = Quoter(settings)
    tox = ToxicityAnalyzer(settings)
    quoter.attach_toxicity_analyzer(tox)

    if quiet:
        logger.disable("core.quoter")
        logger.disable("core.toxicity_analyzer")

    fills: List[SimulatedFill] = []
    cycles_total = 0
    cycles_paused_midpoint = 0
    cycles_paused_toxicity = 0

    try:
        t = sample_ts[0]
        t_end = sample_ts[-1]
        cycle_idx = 0

        while t < t_end:
            cycles_total += 1
            cycle_idx += 1

            mid_now = _mid_at_or_before(sample_ts, sample_mid, t)
            if mid_now is None:
                t += rebalance_interval_seconds
                continue

            # Feed both engines with the latest mid (no-op for in-band only).
            quoter.record_mid(market.condition_id, mid_now)
            tox.record_mid(market.condition_id, mid_now)

            # Midpoint guard — cycle-counter based, faithful in backtest.
            mp_paused, _ = quoter.evaluate_midpoint_guard(
                market.condition_id, mid_now,
            )
            if mp_paused:
                cycles_paused_midpoint += 1
                t += rebalance_interval_seconds
                continue

            # Toxicity guard — VPIN component is faithful; fill-vs-mid is
            # neutral in backtest (see module docstring). is_toxic_paused
            # reads `_paused_until` which evaluate() may set on VPIN trip.
            if tox.is_toxic_paused(market.condition_id):
                cycles_paused_toxicity += 1
                t += rebalance_interval_seconds
                continue

            quotes = quoter.compute(
                market, mid_now,
                yes_position=_INF_POSITION, no_position=_INF_POSITION,
            )

            window_lo = bisect.bisect_left(sample_ts, t)
            window_hi = bisect.bisect_left(
                sample_ts, t + rebalance_interval_seconds,
            )
            window_ts = sample_ts[window_lo:window_hi]
            window_mid = sample_mid[window_lo:window_hi]
            t += rebalance_interval_seconds

            if not window_ts or quotes is None:
                continue
            if quotes.yes_quote is None and quotes.no_quote is None:
                continue

            cycle_fills = _detect_fills_in_window(
                cycle_idx=cycle_idx,
                window_ts=window_ts, window_mid=window_mid,
                yes_quote=quotes.yes_quote, no_quote=quotes.no_quote,
                mid_at_post_yes=mid_now, market=market,
                sample_ts=sample_ts, sample_mid=sample_mid,
            )

            for f in cycle_fills:
                token_id = (
                    market.token_id_yes if f.token_side == "YES"
                    else market.token_id_no
                )
                tox.record_fill(FillEvent(
                    token_id=token_id,
                    condition_id=market.condition_id,
                    side=f.side,
                    price=f.fill_price,
                    filled_size=f.fill_size,
                    timestamp=datetime.now(timezone.utc),
                ))
            if cycle_fills:
                tox.evaluate(market.condition_id, tick)

            fills.extend(cycle_fills)

        return SimResult(
            condition_id=market.condition_id,
            params=params,
            fills=fills,
            cycles_total=cycles_total,
            cycles_paused_midpoint=cycles_paused_midpoint,
            cycles_paused_toxicity=cycles_paused_toxicity,
            duration_seconds=t_end - sample_ts[0],
        )
    finally:
        if quiet:
            logger.enable("core.quoter")
            logger.enable("core.toxicity_analyzer")


# ──────────────────────────────────────────────────────────────────
# Internal — fill detection in a single cycle window
# ──────────────────────────────────────────────────────────────────

def _detect_fills_in_window(
    cycle_idx: int,
    window_ts: List[int],
    window_mid: List[float],          # YES-side mids only
    yes_quote,
    no_quote,
    mid_at_post_yes: float,
    market: MarketCandidate,
    sample_ts: List[int],
    sample_mid: List[float],
) -> List[SimulatedFill]:
    """Apply the naive maker fill rule on YES + NO quotes for one cycle.

    Both YES and NO quotes can fire in the same cycle if mid range straddles
    both sides — the model assumes both orders rest on the book throughout
    the window. Documented optimism in module docstring.
    """
    out: List[SimulatedFill] = []
    if not window_mid:
        return out

    min_yes = min(window_mid)
    max_yes = max(window_mid)

    # ── YES BID: filled when YES mid drops to/below bot's bid ──
    if yes_quote and yes_quote.bid_size > 0 and min_yes <= yes_quote.bid_price:
        ts = _earliest_match_ts(
            window_ts, window_mid, lambda m: m <= yes_quote.bid_price,
        )
        if ts is not None:
            out.append(_build_fill(
                cycle_idx, ts, "BID", "YES",
                yes_quote.bid_price, yes_quote.bid_size,
                mid_at_post_yes=mid_at_post_yes,
                max_spread=market.max_spread,
                sample_ts=sample_ts, sample_mid=sample_mid,
            ))

    # ── YES ASK: filled when YES mid rises to/above bot's ask ──
    if yes_quote and yes_quote.ask_size > 0 and max_yes >= yes_quote.ask_price:
        ts = _earliest_match_ts(
            window_ts, window_mid, lambda m: m >= yes_quote.ask_price,
        )
        if ts is not None:
            out.append(_build_fill(
                cycle_idx, ts, "ASK", "YES",
                yes_quote.ask_price, yes_quote.ask_size,
                mid_at_post_yes=mid_at_post_yes,
                max_spread=market.max_spread,
                sample_ts=sample_ts, sample_mid=sample_mid,
            ))

    # ── NO BID: NO mid <= bot's NO bid ⇔ yes_mid >= 1 - bot_no_bid_price ──
    if no_quote and no_quote.bid_size > 0:
        threshold_yes = 1.0 - no_quote.bid_price
        if max_yes >= threshold_yes:
            ts = _earliest_match_ts(
                window_ts, window_mid, lambda m: m >= threshold_yes,
            )
            if ts is not None:
                out.append(_build_fill(
                    cycle_idx, ts, "BID", "NO",
                    no_quote.bid_price, no_quote.bid_size,
                    mid_at_post_yes=mid_at_post_yes,
                    max_spread=market.max_spread,
                    sample_ts=sample_ts, sample_mid=sample_mid,
                ))

    # ── NO ASK: NO mid >= bot's NO ask ⇔ yes_mid <= 1 - bot_no_ask_price ──
    if no_quote and no_quote.ask_size > 0:
        threshold_yes = 1.0 - no_quote.ask_price
        if min_yes <= threshold_yes:
            ts = _earliest_match_ts(
                window_ts, window_mid, lambda m: m <= threshold_yes,
            )
            if ts is not None:
                out.append(_build_fill(
                    cycle_idx, ts, "ASK", "NO",
                    no_quote.ask_price, no_quote.ask_size,
                    mid_at_post_yes=mid_at_post_yes,
                    max_spread=market.max_spread,
                    sample_ts=sample_ts, sample_mid=sample_mid,
                ))

    return out
