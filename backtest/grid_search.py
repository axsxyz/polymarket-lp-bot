"""
backtest/grid_search.py — Grid search over bot params.

Iterates over a parameter grid, replays simulator+metrics on each combo for
each market, aggregates portfolio-level metrics, writes one CSV row per combo.

═══════════════════════════════════════════════════════════════════════
GRID DIMENSIONS (full)
═══════════════════════════════════════════════════════════════════════
  QUOTE_OFFSET_TICKS:       [1, 2, 3, 4]
  VOL_SPREAD_MULTIPLIER_MAX: [2.0, 3.0, 4.0]
  MIDPOINT_GUARD_PAIRS:     [(0.15,0.85), (0.18,0.82), (0.20,0.80)]
Total: 4 × 3 × 3 = 36 combos.

VPIN_THRESHOLD was DROPPED from the grid because the naive maker fill model
on /prices-history mid tape produces structurally PAIRED BID+ASK fills
(YES BID and NO ASK share the same threshold in YES-space → imbalance≈0).
VPIN dimension was therefore degenerate. See `test_simulator.TestVpinSymmetry`.
The fixed values used in all combos:
  VPIN_THRESHOLD       = Settings default (0.7)
  VPIN_PAUSE_THRESHOLD = min(1.0, VPIN_THRESHOLD + 0.15) → 0.85

═══════════════════════════════════════════════════════════════════════
QUICK MODE (--quick): 2 × 2 × 1 = 4 combos. Use for sub-30s dev iteration.
═══════════════════════════════════════════════════════════════════════

═══════════════════════════════════════════════════════════════════════
PARALLELIZATION (sequential MVP, future-proof)
═══════════════════════════════════════════════════════════════════════
`simulate_combo(params, market_data, base_settings, rebalance_s)` is the
isolated unit of work — fresh Quoter + ToxicityAnalyzer instances, no
cross-combo shared state. A future optimization can pool combos onto
ProcessPoolExecutor without code changes inside this module; the only
shared resource is the SQLite DB which is read once into memory before
the grid loop in `run.py`.
"""

from __future__ import annotations

import csv
import itertools
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from loguru import logger

from config.markets_filter import MarketCandidate
from config.settings import Settings

from backtest.fetcher import HistMid
from backtest.metrics import (
    AggregateMetrics,
    Metrics,
    aggregate_metrics,
    compute_metrics,
)
from backtest.simulator import (
    BotParams,
    DEFAULT_REBALANCE_INTERVAL_S,
    simulate_market,
)


# ──────────────────────────────────────────────────────────────────
# Grid configuration
# ──────────────────────────────────────────────────────────────────

# Live-bot defaults for VPIN params — fixed in all combos.
DEFAULT_VPIN_THRESHOLD = 0.7
DEFAULT_VPIN_PAUSE_THRESHOLD = 0.85


@dataclass(frozen=True)
class GridConfig:
    quote_offset_ticks: Tuple[int, ...]
    vol_spread_multiplier_max: Tuple[float, ...]
    midpoint_guard_pairs: Tuple[Tuple[float, float], ...]
    vpin_threshold: float = DEFAULT_VPIN_THRESHOLD
    vpin_pause_threshold: float = DEFAULT_VPIN_PAUSE_THRESHOLD

    @classmethod
    def full(cls) -> "GridConfig":
        return cls(
            quote_offset_ticks=(1, 2, 3, 4),
            vol_spread_multiplier_max=(2.0, 3.0, 4.0),
            midpoint_guard_pairs=((0.15, 0.85), (0.18, 0.82), (0.20, 0.80)),
        )

    @classmethod
    def quick(cls) -> "GridConfig":
        return cls(
            quote_offset_ticks=(2, 3),
            vol_spread_multiplier_max=(2.0, 3.0),
            midpoint_guard_pairs=((0.18, 0.82),),
        )

    def n_combos(self) -> int:
        return (
            len(self.quote_offset_ticks)
            * len(self.vol_spread_multiplier_max)
            * len(self.midpoint_guard_pairs)
        )

    def combos(self) -> Iterable[BotParams]:
        for offset, vol_max, (mp_min, mp_max) in itertools.product(
            self.quote_offset_ticks,
            self.vol_spread_multiplier_max,
            self.midpoint_guard_pairs,
        ):
            yield BotParams(
                quote_offset_ticks=offset,
                vol_spread_multiplier_max=vol_max,
                vpin_threshold=self.vpin_threshold,
                vpin_pause_threshold=self.vpin_pause_threshold,
                midpoint_guard_min=mp_min,
                midpoint_guard_max=mp_max,
            )


@dataclass(frozen=True)
class GridRow:
    """One row of the output CSV — params + portfolio metrics."""

    quote_offset_ticks: int
    vol_spread_multiplier_max: float
    midpoint_guard_min: float
    midpoint_guard_max: float
    vpin_threshold: float
    n_markets: int
    fills_total: int
    realized_q_score: float
    theoretical_max_q_score: float
    capture_rate: float
    adverse_rate: float
    net_pnl: float
    sharpe_ratio_monthly: float
    sharpe_n_days: int


CSV_COLUMNS = [
    "quote_offset_ticks",
    "vol_spread_multiplier_max",
    "midpoint_guard_min",
    "midpoint_guard_max",
    "vpin_threshold",
    "n_markets",
    "fills_total",
    "realized_q_score",
    "theoretical_max_q_score",
    "capture_rate",
    "adverse_rate",
    "net_pnl",
    "sharpe_ratio_monthly",
    "sharpe_n_days",
]


# ──────────────────────────────────────────────────────────────────
# Simulation orchestration — isolated unit of work
# ──────────────────────────────────────────────────────────────────

MarketBundle = Tuple[MarketCandidate, List[HistMid]]


def simulate_combo(
    params: BotParams,
    market_data: List[MarketBundle],
    base_settings: Optional[Settings] = None,
    rebalance_interval_seconds: int = DEFAULT_REBALANCE_INTERVAL_S,
) -> AggregateMetrics:
    """Run the full sim+metrics pipeline for one parameter combo across markets.

    Independent of all other combos — safe to dispatch to a worker process.
    """
    per_market: List[Metrics] = []
    for market, mids in market_data:
        if not mids:
            continue
        result = simulate_market(
            market, mids, params,
            base_settings=base_settings,
            rebalance_interval_seconds=rebalance_interval_seconds,
            quiet=True,
        )
        per_market.append(compute_metrics(result, market, base_settings))
    return aggregate_metrics(per_market)


def aggregate_to_row(params: BotParams, agg: AggregateMetrics) -> GridRow:
    return GridRow(
        quote_offset_ticks=params.quote_offset_ticks,
        vol_spread_multiplier_max=params.vol_spread_multiplier_max,
        midpoint_guard_min=params.midpoint_guard_min,
        midpoint_guard_max=params.midpoint_guard_max,
        vpin_threshold=params.vpin_threshold,
        n_markets=agg.n_markets,
        fills_total=agg.fills_total,
        realized_q_score=agg.realized_q_score,
        theoretical_max_q_score=agg.theoretical_max_q_score,
        capture_rate=agg.capture_rate,
        adverse_rate=agg.adverse_rate,
        net_pnl=agg.net_pnl,
        sharpe_ratio_monthly=agg.sharpe_ratio_monthly,
        sharpe_n_days=agg.sharpe_n_days,
    )


# ──────────────────────────────────────────────────────────────────
# CSV output — incremental append-mode for crash safety
# ──────────────────────────────────────────────────────────────────

def write_csv_header(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_COLUMNS)


def append_csv_row(path: Path, row: GridRow) -> None:
    with path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            row.quote_offset_ticks,
            row.vol_spread_multiplier_max,
            row.midpoint_guard_min,
            row.midpoint_guard_max,
            row.vpin_threshold,
            row.n_markets,
            row.fills_total,
            f"{row.realized_q_score:.6f}",
            f"{row.theoretical_max_q_score:.6f}",
            f"{row.capture_rate:.6f}",
            f"{row.adverse_rate:.6f}",
            f"{row.net_pnl:.6f}",
            f"{row.sharpe_ratio_monthly:.6f}",
            row.sharpe_n_days,
        ])


# ──────────────────────────────────────────────────────────────────
# Top-level driver
# ──────────────────────────────────────────────────────────────────

def run_grid(
    config: GridConfig,
    market_data: List[MarketBundle],
    out_csv_path: Path,
    base_settings: Optional[Settings] = None,
    rebalance_interval_seconds: int = DEFAULT_REBALANCE_INTERVAL_S,
) -> List[GridRow]:
    """Run the grid sequentially, write one CSV row per combo as we go.

    Crash safety: header is written first, each combo's row is flushed before
    the next combo begins. Partial runs are recoverable manually.
    """
    if not market_data:
        logger.warning("Grid: empty market_data, nothing to simulate")
        write_csv_header(out_csv_path)
        return []

    n_combos = config.n_combos()
    logger.info(
        "Grid: starting {} combos × {} markets, output={}",
        n_combos, len(market_data), out_csv_path,
    )

    write_csv_header(out_csv_path)
    rows: List[GridRow] = []

    for idx, params in enumerate(config.combos(), start=1):
        agg = simulate_combo(
            params, market_data, base_settings, rebalance_interval_seconds,
        )
        row = aggregate_to_row(params, agg)
        append_csv_row(out_csv_path, row)
        rows.append(row)
        logger.info(
            "Grid [{}/{}] offset={} vol_max={:.1f} mp=[{:.2f},{:.2f}] "
            "→ fills={} q={:.1f} pnl={:.2f} sharpe={:.2f}",
            idx, n_combos,
            params.quote_offset_ticks, params.vol_spread_multiplier_max,
            params.midpoint_guard_min, params.midpoint_guard_max,
            agg.fills_total, agg.realized_q_score,
            agg.net_pnl, agg.sharpe_ratio_monthly,
        )

    logger.info("Grid: done, {} rows written to {}", len(rows), out_csv_path)
    return rows
