"""
backtest/run.py — CLI entry point for the P2c backtest framework.

Usage examples:
  # Quick dev iteration: 4 combos × pre-cached markets, days=30
  python -m backtest.run --quick

  # Full grid (36 combos) on all markets currently in the DB
  python -m backtest.run --all --skip-low-coverage

  # Full grid on specific condition_ids
  python -m backtest.run --condition-ids cid1,cid2,cid3 --days 30

  # Full grid on markets active during a given month (uses period filter)
  python -m backtest.run --year 2025 --month 12

The CLI is read-only against the SQLite cache (`data/backtest.db`). To
populate or refresh the cache, use:
  python -m backtest.fetcher --year 2025 --month 12 --markets 30 --days 30
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from loguru import logger

from backtest.fetcher import (
    DB_PATH,
    HistMid,
    is_low_coverage,
    list_all_markets,
    list_markets_in_period,
    load_market,
    load_mid_history,
)
from backtest.grid_search import (
    GridConfig,
    MarketBundle,
    run_grid,
)
from backtest.simulator import DEFAULT_REBALANCE_INTERVAL_S


DEFAULT_OUT_PATH = Path("data") / "backtest_results.csv"


# ──────────────────────────────────────────────────────────────────
# Market loading
# ──────────────────────────────────────────────────────────────────

def _trim_to_days(mids: List[HistMid], days: int) -> List[HistMid]:
    """Restrict a sorted-by-ts mid list to the last `days` days from its max ts.

    Trims relative to the tape's own latest sample rather than wall-clock
    `now()` so backtests over older periods are not silently empty.
    """
    if not mids or days <= 0:
        return mids
    sorted_mids = sorted(mids, key=lambda m: m.ts)
    cutoff = sorted_mids[-1].ts - (days * 86_400)
    return [m for m in sorted_mids if m.ts >= cutoff]


def load_market_bundles(
    condition_ids: List[str],
    db_path: Path = DB_PATH,
    days: Optional[int] = None,
    skip_low_coverage: bool = False,
) -> List[MarketBundle]:
    """Load (MarketCandidate, [HistMid]) tuples for the requested cids.

    Markets missing from the DB are dropped with a warning. Markets flagged
    as low_coverage are skipped iff ``skip_low_coverage`` is set.
    """
    bundles: List[MarketBundle] = []
    for cid in condition_ids:
        market = load_market(cid, db_path)
        if market is None:
            logger.warning("run: market {} not in DB, skipping", cid[:12])
            continue
        if skip_low_coverage and is_low_coverage(cid, db_path):
            logger.info("run: market {} flagged low_coverage, skipping", cid[:12])
            continue
        mids = load_mid_history(cid, "YES", db_path)
        if not mids:
            logger.warning(
                "run: market {} has no mid history, skipping", cid[:12],
            )
            continue
        if days is not None:
            mids = _trim_to_days(mids, days)
            if not mids:
                logger.warning(
                    "run: market {} has no mids in last {} days, skipping",
                    cid[:12], days,
                )
                continue
        bundles.append((market, mids))
    return bundles


# ──────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m backtest.run",
        description="Run the P2c grid search over cached market data.",
    )

    src = p.add_mutually_exclusive_group()
    src.add_argument(
        "--condition-ids",
        type=str,
        help="Comma-separated condition_ids (overrides --year/--month/--all).",
    )
    src.add_argument(
        "--year", type=int, help="Filter to markets active in {year}-{month}.",
    )
    src.add_argument(
        "--all", action="store_true",
        help="Run on every market in the DB.",
    )

    p.add_argument(
        "--month", type=int,
        help="Month (1-12); required if --year is given.",
    )
    p.add_argument(
        "--days", type=int, default=30,
        help="Restrict mid history to the last N days from each tape's "
             "latest sample (default 30).",
    )
    p.add_argument(
        "--quick", action="store_true",
        help="Use the reduced 4-combo grid for dev iteration.",
    )
    p.add_argument(
        "--skip-low-coverage", action="store_true",
        help="Exclude markets flagged with low_coverage=1.",
    )
    p.add_argument(
        "--rebalance-seconds", type=int, default=DEFAULT_REBALANCE_INTERVAL_S,
        help=f"Cycle length in seconds (default {DEFAULT_REBALANCE_INTERVAL_S}).",
    )
    p.add_argument(
        "--out", type=str, default=str(DEFAULT_OUT_PATH),
        help=f"Output CSV path (default {DEFAULT_OUT_PATH}).",
    )
    p.add_argument(
        "--db", type=str, default=str(DB_PATH),
        help=f"SQLite path (default {DB_PATH}).",
    )
    p.add_argument(
        "--limit-markets", type=int, default=None,
        help="Cap number of markets considered (after period/coverage filters).",
    )
    return p


def _resolve_condition_ids(args: argparse.Namespace) -> List[str]:
    db_path = Path(args.db)
    if args.condition_ids:
        return [c.strip() for c in args.condition_ids.split(",") if c.strip()]
    if args.year is not None:
        if args.month is None:
            raise SystemExit("--month is required when --year is given")
        return list_markets_in_period(args.year, args.month, db_path)
    if args.all:
        return list_all_markets(db_path, exclude_low_coverage=False)
    # Default fallback: all markets
    return list_all_markets(db_path, exclude_low_coverage=False)


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    db_path = Path(args.db)
    out_path = Path(args.out)

    if not db_path.exists():
        logger.error(
            "DB not found at {}. Run `python -m backtest.fetcher --help` "
            "to populate the cache.",
            db_path,
        )
        return 2

    cids = _resolve_condition_ids(args)
    if not cids:
        logger.error("No markets selected — nothing to do.")
        return 2

    if args.limit_markets is not None and args.limit_markets > 0:
        cids = cids[: args.limit_markets]

    logger.info(
        "run: loading {} markets from {} (days={}, skip_lowcov={})",
        len(cids), db_path, args.days, args.skip_low_coverage,
    )
    bundles = load_market_bundles(
        cids, db_path=db_path,
        days=args.days, skip_low_coverage=args.skip_low_coverage,
    )
    if not bundles:
        logger.error("No usable market bundles after filtering — exit.")
        return 2

    config = GridConfig.quick() if args.quick else GridConfig.full()
    logger.info(
        "run: grid mode={}, combos={}, markets={}, rebalance={}s, out={}",
        "quick" if args.quick else "full",
        config.n_combos(), len(bundles), args.rebalance_seconds, out_path,
    )

    started_at = time.time()
    rows = run_grid(
        config=config,
        market_data=bundles,
        out_csv_path=out_path,
        rebalance_interval_seconds=args.rebalance_seconds,
    )
    elapsed = time.time() - started_at
    logger.info(
        "run: done in {:.1f}s, {} rows written to {}",
        elapsed, len(rows), out_path,
    )

    # Print summary banner — top combos by realized_q_score
    if rows:
        ranked = sorted(rows, key=lambda r: r.realized_q_score, reverse=True)
        top = ranked[: min(5, len(ranked))]
        logger.info("run: top {} combos by realized_q_score:", len(top))
        for r in top:
            logger.info(
                "    offset={} vol_max={:.1f} mp=[{:.2f},{:.2f}] "
                "→ q={:.1f} pnl={:.2f} sharpe={:.2f} adverse={:.2%}",
                r.quote_offset_ticks, r.vol_spread_multiplier_max,
                r.midpoint_guard_min, r.midpoint_guard_max,
                r.realized_q_score, r.net_pnl, r.sharpe_ratio_monthly,
                r.adverse_rate,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
