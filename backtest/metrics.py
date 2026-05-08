"""
backtest/metrics.py — Q-score, PnL, Sharpe ratio, adverse selection.

Pure-functional module — no IO, no random, no state. All metrics are
deterministic given a SimResult + MarketCandidate context.

═══════════════════════════════════════════════════════════════════════
DEFINITIONS
═══════════════════════════════════════════════════════════════════════

Q-score (Polymarket quadratic scoring):
  Per fill: q = ((v - s) / v)^2 × size, with v = market.max_spread (dollars),
  s = abs(fill_price - mid_at_post). Zero if s >= v.

  Realized:    sum over all fills of q.
  Theoretical: per-market ceiling assuming the bot quoted at minimum spread
               (= 1 tick) on both YES and NO at min_size for every cycle
               where it was not paused. Per user spec:
                 q_theo_max = sum over {YES, NO} of min(Q_bid_max, Q_ask_max)
                            = 2 × ((v - tick) / v)^2 × min_size  (symmetric)
               Multiplied by n_quoting_cycles (cycles_total - cycles_paused).
  Capture rate = realized / theoretical, may exceed 1.0 because the bot
                 quotes at SIZE_MULTIPLIER × min_size (default 2 ×).

Adverse selection rate:
  Per fill with mid_t5min populated, adverse iff:
    BID side: mid_t5min < fill_price (we bought; market kept falling)
    ASK side: mid_t5min > fill_price (we sold; market kept rising)
  Rate = adverse_count / fills_with_t5min, in [0, 1].

Net PnL (close-at-mid_t5min model):
  Per fill with mid_t5min populated:
    BID side: pnl = (mid_t5min - fill_price) × fill_size
    ASK side: pnl = (fill_price - mid_t5min) × fill_size
  Excludes fills where t5min is None (tape ended before horizon). This
  estimates the per-fill P&L as if the bot closed each position at the
  mid 5 minutes after the fill — a proxy for short-horizon adverse drift.

Daily PnLs:
  Same close-at-mid_t5min model, summed by UTC date of fill_ts. Used by
  `aggregate_metrics` to compute portfolio-level Sharpe (sum daily PnLs
  across markets BEFORE recomputing Sharpe — naively averaging per-market
  Sharpes is statistically incorrect).

Sharpe (portfolio, monthly-annualised):
  Aggregated daily series:
    sharpe = mean(daily_pnl) / stdev(daily_pnl) × sqrt(30)
  Requires >= 2 distinct days with non-zero stdev, else 0.0.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from config.markets_filter import MarketCandidate
from config.settings import Settings

from backtest.simulator import (
    SimResult,
    SimulatedFill,
    _TICK_DOLLARS_MAP,
    build_settings_for_params,
)

# Annualisation factor for monthly Sharpe — sqrt of trading days per month.
_SHARPE_ANNUALISATION_FACTOR = math.sqrt(30.0)


# ──────────────────────────────────────────────────────────────────
# Public dataclasses
# ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Metrics:
    """Per-market metrics from one simulation run."""

    condition_id: str
    fills_total: int
    fills_with_t5min: int
    realized_q_score: float
    theoretical_max_q_score: float
    capture_rate: float                  # realized / theoretical (>=0, may > 1)
    adverse_rate: float                  # in [0, 1]
    net_pnl: float
    daily_pnls: Dict[str, float] = field(default_factory=dict)  # YYYY-MM-DD → PnL
    cycles_total: int = 0
    cycles_paused: int = 0


@dataclass(frozen=True)
class AggregateMetrics:
    """Portfolio-level metrics across markets for one parameter combo."""

    n_markets: int
    fills_total: int
    realized_q_score: float
    theoretical_max_q_score: float
    capture_rate: float
    adverse_rate: float
    net_pnl: float
    sharpe_ratio_monthly: float
    sharpe_n_days: int


# ──────────────────────────────────────────────────────────────────
# Per-fill helpers
# ──────────────────────────────────────────────────────────────────

def compute_q_score_fill(fill: SimulatedFill) -> float:
    """Q-score for a single fill. Zero if spread >= max_spread or size <= 0."""
    v = fill.max_spread
    s = fill.spread_used
    if v <= 0 or fill.fill_size <= 0:
        return 0.0
    if s >= v:
        return 0.0
    return ((v - s) / v) ** 2 * fill.fill_size


def compute_pnl_fill(fill: SimulatedFill) -> Optional[float]:
    """Signed PnL using the close-at-mid_t5min model. None if no t5min data."""
    if fill.mid_t5min is None or fill.fill_size <= 0:
        return None
    delta = fill.mid_t5min - fill.fill_price
    if fill.side == "BID":
        return delta * fill.fill_size
    if fill.side == "ASK":
        return -delta * fill.fill_size
    return None


def is_adverse(fill: SimulatedFill) -> Optional[bool]:
    """True iff mid_t5min moved against the position. None if no t5min data."""
    pnl = compute_pnl_fill(fill)
    if pnl is None:
        return None
    return pnl < 0.0


# ──────────────────────────────────────────────────────────────────
# Theoretical max
# ──────────────────────────────────────────────────────────────────

def compute_theoretical_q_max(
    max_spread: float, tick_dollars: float, min_size: float, n_quoting_cycles: int,
) -> float:
    """Per-market ceiling: 2 × ((v - tick) / v)^2 × min_size × cycles.

    Symmetric YES + NO sides at the tightest legal spread (1 tick), at the
    minimum eligible size. Used as a normalisation reference for capture
    rate; not a literal upper bound on realised Q-score (the bot may quote
    larger than min_size).
    """
    if max_spread <= 0 or n_quoting_cycles <= 0 or min_size <= 0:
        return 0.0
    s_min = min(tick_dollars, max_spread)
    if s_min >= max_spread:
        return 0.0
    per_side = ((max_spread - s_min) / max_spread) ** 2 * min_size
    return 2.0 * per_side * n_quoting_cycles


# ──────────────────────────────────────────────────────────────────
# Per-market metrics
# ──────────────────────────────────────────────────────────────────

def compute_metrics(
    result: SimResult,
    market: MarketCandidate,
    base_settings: Optional[Settings] = None,
) -> Metrics:
    """Build the Metrics record for one SimResult.

    `base_settings` mirrors `simulate_market`'s argument (only used if your
    Settings tweaks affect tick interpretation, which they don't today —
    kept for symmetry).
    """
    tick = _TICK_DOLLARS_MAP.get(market.tick_size, 0.01)

    # Realized Q-score
    realized = sum(compute_q_score_fill(f) for f in result.fills)

    # Theoretical max — per user spec, uses min_size (not bot quote size)
    n_paused = result.cycles_paused_midpoint + result.cycles_paused_toxicity
    n_quoting = max(0, result.cycles_total - n_paused)
    theo_max = compute_theoretical_q_max(
        market.max_spread, tick, market.min_size, n_quoting,
    )
    capture = (realized / theo_max) if theo_max > 0 else 0.0

    # Adverse + PnL — restricted to fills with t5min populated
    fills_with_t5 = [f for f in result.fills if f.mid_t5min is not None]
    n_with_t5 = len(fills_with_t5)
    adverse_count = sum(1 for f in fills_with_t5 if is_adverse(f))
    adverse_rate = (adverse_count / n_with_t5) if n_with_t5 > 0 else 0.0

    pnls_per_fill = [compute_pnl_fill(f) for f in fills_with_t5]
    net_pnl = sum(p for p in pnls_per_fill if p is not None)

    # Daily PnLs by UTC date
    daily: Dict[str, float] = {}
    for f, p in zip(fills_with_t5, pnls_per_fill):
        if p is None:
            continue
        date_str = datetime.fromtimestamp(
            f.ts, tz=timezone.utc,
        ).strftime("%Y-%m-%d")
        daily[date_str] = daily.get(date_str, 0.0) + p

    # base_settings parameter currently unused but reserved for future use.
    _ = base_settings

    return Metrics(
        condition_id=result.condition_id,
        fills_total=len(result.fills),
        fills_with_t5min=n_with_t5,
        realized_q_score=realized,
        theoretical_max_q_score=theo_max,
        capture_rate=capture,
        adverse_rate=adverse_rate,
        net_pnl=net_pnl,
        daily_pnls=daily,
        cycles_total=result.cycles_total,
        cycles_paused=n_paused,
    )


# ──────────────────────────────────────────────────────────────────
# Portfolio-level aggregation
# ──────────────────────────────────────────────────────────────────

def aggregate_metrics(per_market: List[Metrics]) -> AggregateMetrics:
    """Aggregate per-market Metrics into one portfolio-level row.

    Sharpe is recomputed at the portfolio level by SUMMING daily PnLs across
    markets per UTC date BEFORE the mean/stdev step — naively averaging
    per-market Sharpes would be statistically incorrect.

    Adverse rate is weighted by `fills_with_t5min` to give larger fill
    counts proportional voice in the average.
    """
    if not per_market:
        return AggregateMetrics(
            n_markets=0, fills_total=0,
            realized_q_score=0.0, theoretical_max_q_score=0.0,
            capture_rate=0.0, adverse_rate=0.0, net_pnl=0.0,
            sharpe_ratio_monthly=0.0, sharpe_n_days=0,
        )

    fills_total = sum(m.fills_total for m in per_market)
    realized = sum(m.realized_q_score for m in per_market)
    theo_max = sum(m.theoretical_max_q_score for m in per_market)
    capture = (realized / theo_max) if theo_max > 0 else 0.0

    total_with_t5 = sum(m.fills_with_t5min for m in per_market)
    if total_with_t5 > 0:
        weighted = sum(m.adverse_rate * m.fills_with_t5min for m in per_market)
        adverse_rate = weighted / total_with_t5
    else:
        adverse_rate = 0.0

    net_pnl = sum(m.net_pnl for m in per_market)

    # Portfolio Sharpe: sum daily PnLs ACROSS markets per date, then compute
    portfolio_daily: Dict[str, float] = {}
    for m in per_market:
        for date_str, pnl in m.daily_pnls.items():
            portfolio_daily[date_str] = portfolio_daily.get(date_str, 0.0) + pnl

    daily_returns = list(portfolio_daily.values())
    n_days = len(daily_returns)
    if n_days >= 2:
        sigma = statistics.stdev(daily_returns)
        if sigma > 0:
            mu = statistics.mean(daily_returns)
            sharpe = (mu / sigma) * _SHARPE_ANNUALISATION_FACTOR
        else:
            sharpe = 0.0
    else:
        sharpe = 0.0

    return AggregateMetrics(
        n_markets=len(per_market),
        fills_total=fills_total,
        realized_q_score=realized,
        theoretical_max_q_score=theo_max,
        capture_rate=capture,
        adverse_rate=adverse_rate,
        net_pnl=net_pnl,
        sharpe_ratio_monthly=sharpe,
        sharpe_n_days=n_days,
    )


# Re-exported for tests / external callers (avoid cross-module import gymnastics).
__all__ = [
    "Metrics",
    "AggregateMetrics",
    "compute_q_score_fill",
    "compute_pnl_fill",
    "is_adverse",
    "compute_theoretical_q_max",
    "compute_metrics",
    "aggregate_metrics",
    "build_settings_for_params",
]
