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
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

from loguru import logger
from rich import box as rich_box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich.text import Text

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
    GridRow,
    MarketBundle,
    run_grid,
)
from backtest.simulator import BotParams, DEFAULT_REBALANCE_INTERVAL_S

_console = Console(highlight=False)


DEFAULT_OUT_PATH = Path("data") / "backtest_results.csv"


# ──────────────────────────────────────────────────────────────────
# Logging — silence loguru stderr during interactive backtest
# ──────────────────────────────────────────────────────────────────

def _silence_logs(level: str = "WARNING") -> None:
    """Detach loguru's default stderr handler and reattach at WARNING+ only.

    Backtest is interactive — DEBUG/INFO logs interleave with the rich Live
    display and corrupt the layout. WARNINGs and above remain visible (and
    are routed to stderr after the Live block ends since rich captures it).
    """
    logger.remove()
    logger.add(
        sys.stderr,
        level=level.upper(),
        format=(
            "<yellow>⚠</yellow>  <level>{level}</level>  "
            "<dim>{name}:{line}</dim>  {message}"
        ),
        colorize=True,
    )


# ──────────────────────────────────────────────────────────────────
# Rich UI helpers — palette and small renderables
# ──────────────────────────────────────────────────────────────────

# Bracketed neon palette inspired by Bloomberg / hex editor aesthetics.
_C_ACCENT = "bright_cyan"
_C_DIM = "grey50"
_C_OK = "bright_green"
_C_WARN = "yellow"
_C_BAD = "bright_red"
_C_KEY = "bold bright_white"
_C_RUNNING = "bright_magenta"


def _row_style(row: GridRow) -> str:
    if row.fills_total == 0:
        return _C_WARN
    return _C_OK if row.net_pnl > 0 else _C_BAD


def _kv(key: str, value: str, key_w: int = 10) -> Text:
    """Two-tone key/value line: '  ▸ key   value'.

    ``value`` is parsed through ``Text.from_markup`` so callers can pass
    inline rich markup (e.g. ``[bold]42[/bold]``) and have it rendered.
    """
    t = Text()
    t.append("  ▸ ", style=_C_DIM)
    t.append(f"{key:<{key_w}}", style=_C_KEY)
    t.append("  ")
    t.append_text(Text.from_markup(value))
    return t


def _grid_summary(config: GridConfig) -> str:
    """Compact one-line summary of the grid dimensions."""
    o = ",".join(str(x) for x in config.quote_offset_ticks)
    v = ",".join(f"{x:.1f}" for x in config.vol_spread_multiplier_max)
    bands = " ".join(
        f"[{a:.2f},{b:.2f}]" for a, b in config.midpoint_guard_pairs
    )
    return f"offset={{{o}}} × vol_max={{{v}}} × band={bands}"


def _render_banner(
    mode: str,
    n_loaded: int,
    n_dropped: int,
    n_combos: int,
    config: GridConfig,
    rebalance_s: int,
    days: int,
    workers: int,
    out_path: Path,
    db_path: Path,
) -> Panel:
    """Top banner — title bar + key parameters laid out as a panel."""
    title = Text()
    title.append("▓▒░ ", style="bright_cyan")
    title.append("POLYMARKET LP BACKTEST", style="bold bright_white")
    title.append(" ░▒▓", style="bright_cyan")

    body = Text()
    body.append_text(_kv(
        "mode",
        f"[{_C_ACCENT}]{mode}[/{_C_ACCENT}]  "
        f"[{_C_DIM}]({n_combos} combos · {workers} workers)[/{_C_DIM}]",
    ))
    body.append("\n")
    body.append_text(_kv(
        "markets",
        f"[bold]{n_loaded}[/bold] loaded  "
        + (f"[{_C_DIM}]· {n_dropped} dropped[/{_C_DIM}]" if n_dropped else ""),
    ))
    body.append("\n")
    body.append_text(_kv("grid", _grid_summary(config)))
    body.append("\n")
    body.append_text(_kv(
        "cycle",
        f"[bold]{rebalance_s}s[/bold] rebalance · "
        f"[bold]{days}d[/bold] history",
    ))
    body.append("\n")
    body.append_text(_kv(
        "io",
        f"db=[{_C_DIM}]{db_path}[/{_C_DIM}]  "
        f"out=[{_C_DIM}]{out_path}[/{_C_DIM}]",
    ))

    return Panel(
        body,
        title=title,
        title_align="left",
        border_style=_C_ACCENT,
        padding=(0, 1),
    )


# ──────────────────────────────────────────────────────────────────
# Live combo table
# ──────────────────────────────────────────────────────────────────

# Hacker-grade compact layout: 10 columns (incl. status glyph). All widths
# below are MINIMUMS — the table is set to expand=True so on wider terminals
# extra space is redistributed; on narrow ones (≤80) numeric columns are
# wide enough for typical values without truncation.
_TABLE_COLS = (
    ("",        "left",  1),    # status glyph (●/⠿/○) — colour carries the rest
    ("#",       "right", 3),
    ("off",     "right", 3),
    ("vol",     "right", 3),
    ("band",    "center", 11),
    ("fills",   "right", 5),
    ("q",       "right", 9),    # fits "99,999.99"
    ("pnl",     "right", 8),    # fits "+9,999.99"
    ("sharpe",  "right", 7),    # fits "+99.99"
    ("adv",     "right", 4),
)


def _make_combo_table(title: str) -> Table:
    table = Table(
        title=title,
        title_style=f"bold {_C_ACCENT}",
        box=rich_box.ROUNDED,
        border_style=_C_DIM,
        header_style="bold bright_white",
        expand=True,
        pad_edge=False,
        padding=(0, 1),
        show_lines=False,
    )
    for name, justify, width in _TABLE_COLS:
        table.add_column(name, justify=justify, min_width=width, no_wrap=True)
    return table


def _band_text(params: BotParams) -> str:
    return f"[{params.midpoint_guard_min:.2f},{params.midpoint_guard_max:.2f}]"


def _build_live_table(
    title: str,
    all_params: List[BotParams],
    rows: List[Optional[GridRow]],
    in_flight: bool,
) -> Table:
    """Render the live combos table.

    Pre-allocates one row per combo so the table height is stable across
    Live refreshes — avoids the height-mismatch redraw issue rich.Live has
    when its renderable grows row-by-row.
    """
    table = _make_combo_table(title)
    for idx, params in enumerate(all_params):
        row = rows[idx]
        if row is not None:
            style = _row_style(row)
            glyph = Text("●", style=style)
            table.add_row(
                glyph,
                str(idx + 1),
                str(row.quote_offset_ticks),
                f"{row.vol_spread_multiplier_max:.1f}",
                _band_text(params),
                f"{row.fills_total:,}",
                f"{row.realized_q_score:,.2f}",
                f"{row.net_pnl:+.2f}",
                f"{row.sharpe_ratio_monthly:+.2f}",
                f"{row.adverse_rate:.0%}",
                style=style,
            )
        else:
            glyph = (
                Text("⠿", style=_C_RUNNING)
                if in_flight
                else Text("○", style=_C_DIM)
            )
            table.add_row(
                glyph,
                str(idx + 1),
                str(params.quote_offset_ticks),
                f"{params.vol_spread_multiplier_max:.1f}",
                _band_text(params),
                "—",
                "—",
                "—",
                "—",
                "—",
                style=_C_DIM,
            )
    return table


# ──────────────────────────────────────────────────────────────────
# Final summary
# ──────────────────────────────────────────────────────────────────

def _ranked_summary_panel(rows: List[GridRow]) -> Panel:
    table = Table(
        box=rich_box.SIMPLE_HEAVY,
        border_style=_C_DIM,
        header_style="bold bright_white",
        show_lines=False,
        pad_edge=False,
        padding=(0, 1),
        expand=True,
    )
    cols = (
        ("rank",   "right",  4),
        ("off",    "right",  3),
        ("vol",    "right",  3),
        ("band",   "center", 11),
        ("fills",  "right",  5),
        ("q",      "right",  9),
        ("pnl",    "right",  8),
        ("sharpe", "right",  7),
        ("cap",    "right",  5),
        ("adv",    "right",  4),
    )
    for name, justify, width in cols:
        table.add_column(name, justify=justify, min_width=width, no_wrap=True)

    ranked = sorted(rows, key=lambda r: r.realized_q_score, reverse=True)
    for rank, r in enumerate(ranked, start=1):
        rank_label = f"★{rank}" if rank == 1 else str(rank)
        table.add_row(
            rank_label,
            str(r.quote_offset_ticks),
            f"{r.vol_spread_multiplier_max:.1f}",
            f"[{r.midpoint_guard_min:.2f},{r.midpoint_guard_max:.2f}]",
            f"{r.fills_total:,}",
            f"{r.realized_q_score:,.2f}",
            f"{r.net_pnl:+.2f}",
            f"{r.sharpe_ratio_monthly:+.2f}",
            f"{r.capture_rate:.1%}",
            f"{r.adverse_rate:.0%}",
            style=_row_style(r),
        )

    title = Text("◇ Top combos by Q-score", style=f"bold {_C_ACCENT}")
    return Panel(
        table, title=title, title_align="left",
        border_style=_C_ACCENT, padding=(0, 1),
    )


def _stats_footer(
    rows: List[GridRow], elapsed: float, out_path: Path,
) -> Panel:
    if not rows:
        body = Text("no rows produced", style=_C_WARN)
        return Panel(body, border_style=_C_DIM, padding=(0, 1))

    total_fills = sum(r.fills_total for r in rows)
    sum_pnl = sum(r.net_pnl for r in rows)
    best = max(rows, key=lambda r: r.realized_q_score)
    worst = min(rows, key=lambda r: r.realized_q_score)
    avg_capture = sum(r.capture_rate for r in rows) / len(rows)

    pnl_style = _C_OK if sum_pnl > 0 else (_C_WARN if sum_pnl == 0 else _C_BAD)

    def _combo_tag(r: GridRow) -> str:
        return (
            f"off={r.quote_offset_ticks},vol={r.vol_spread_multiplier_max:.1f}"
        )

    # Single-column "log line" style stays readable at any width — no
    # awkward two-column wrapping when the terminal is narrow.
    lines = [
        _kv("fills", f"[bold]{total_fills:,}[/bold] over {len(rows)} combos"),
        _kv(
            "pnl Σ",
            f"[{pnl_style}]{sum_pnl:+,.2f}[/{pnl_style}]  "
            f"[{_C_DIM}]capture(avg)={avg_capture:.1%}[/{_C_DIM}]",
        ),
        _kv(
            "best",
            f"[{_C_OK}]Q={best.realized_q_score:,.2f}[/{_C_OK}] "
            f"[{_C_DIM}]({_combo_tag(best)})[/{_C_DIM}]",
        ),
        _kv(
            "worst",
            f"[{_C_BAD}]Q={worst.realized_q_score:,.2f}[/{_C_BAD}] "
            f"[{_C_DIM}]({_combo_tag(worst)})[/{_C_DIM}]",
        ),
        _kv("runtime", f"[bold]{elapsed:.2f}s[/bold]"),
        _kv("csv", f"[{_C_DIM}]{out_path}[/{_C_DIM}]"),
    ]

    return Panel(Group(*lines), border_style=_C_DIM, padding=(0, 1))


# ──────────────────────────────────────────────────────────────────
# Progress bar
# ──────────────────────────────────────────────────────────────────

def _make_progress() -> Progress:
    return Progress(
        SpinnerColumn(style=_C_ACCENT),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(
            bar_width=None,
            complete_style=_C_ACCENT,
            finished_style=_C_OK,
        ),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("•"),
        TimeRemainingColumn(),
        expand=True,
    )


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
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show INFO/DEBUG logs (default: WARNING+).",
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
    _silence_logs(level="DEBUG" if args.verbose else "WARNING")
    db_path = Path(args.db)
    out_path = Path(args.out)

    if not db_path.exists():
        _console.print(Panel(
            Text(
                f"DB not found at {db_path}.\n"
                "Run `python -m backtest.fetcher --help` to populate the cache.",
                style=_C_BAD,
            ),
            border_style=_C_BAD,
        ))
        return 2

    cids = _resolve_condition_ids(args)
    if not cids:
        _console.print(Panel(
            Text("No markets selected — nothing to do.", style=_C_BAD),
            border_style=_C_BAD,
        ))
        return 2

    if args.limit_markets is not None and args.limit_markets > 0:
        cids = cids[: args.limit_markets]

    # ── Phase 1 — Load market bundles (transient spinner) ─────────
    load_t0 = time.perf_counter()
    with _console.status(
        Text.assemble(
            ("⠿ ", _C_ACCENT),
            ("loading ", _C_DIM),
            (f"{len(cids)}", "bold bright_white"),
            (" markets from SQLite…", _C_DIM),
        ),
        spinner="dots",
        spinner_style=_C_ACCENT,
    ):
        bundles = load_market_bundles(
            cids, db_path=db_path,
            days=args.days, skip_low_coverage=args.skip_low_coverage,
        )
    load_elapsed = time.perf_counter() - load_t0

    if not bundles:
        _console.print(Panel(
            Text("No usable market bundles after filtering — exit.", style=_C_BAD),
            border_style=_C_BAD,
        ))
        return 2

    # ── Phase 2 — Banner ──────────────────────────────────────────
    config = GridConfig.quick() if args.quick else GridConfig.full()
    n_combos = config.n_combos()
    n_dropped = len(cids) - len(bundles)
    workers = min(os.cpu_count() or 1, n_combos)

    _console.print()
    _console.print(_render_banner(
        mode="quick" if args.quick else "full",
        n_loaded=len(bundles),
        n_dropped=n_dropped,
        n_combos=n_combos,
        config=config,
        rebalance_s=args.rebalance_seconds,
        days=args.days,
        workers=workers,
        out_path=out_path,
        db_path=db_path,
    ))
    _console.print(Text.assemble(
        ("  ✓ ", _C_OK),
        ("loaded ", _C_DIM),
        (f"{len(bundles)}", "bold bright_white"),
        (" market bundles in ", _C_DIM),
        (f"{load_elapsed*1000:.0f}ms", "bold"),
    ))
    _console.print()

    # ── Phase 3 — Live execution (table + progress bar) ───────────
    all_params: List[BotParams] = list(config.combos())
    completed_rows: List[Optional[GridRow]] = [None] * n_combos

    progress = _make_progress()
    task_id = progress.add_task(
        Text.assemble(
            ("◉ ", _C_ACCENT),
            ("simulating ", "bold"),
            (f"{n_combos} combos × {len(bundles)} markets", _C_DIM),
        ).markup,
        total=n_combos,
    )

    title = f"Combos · {n_combos} total · {workers} workers"

    def _renderable() -> Group:
        # in_flight is True while progress is incomplete; flips to False
        # so pending rows don't say "running" once a worker queue is empty.
        in_flight = progress.tasks[0].completed < n_combos
        table = _build_live_table(title, all_params, completed_rows, in_flight)
        return Group(table, Text(""), progress)

    started_at = time.perf_counter()
    with Live(
        _renderable(),
        console=_console,
        refresh_per_second=12,
        transient=False,
        vertical_overflow="visible",
    ) as live:
        def _on_done(
            completed: int, original_idx: int, total: int,
            params: BotParams, row: GridRow,
        ) -> None:
            completed_rows[original_idx] = row
            progress.advance(task_id)
            live.update(_renderable())

        rows = run_grid(
            config=config,
            market_data=bundles,
            out_csv_path=out_path,
            rebalance_interval_seconds=args.rebalance_seconds,
            on_combo_done=_on_done,
        )
        # Final refresh so the last completion paints before Live exits.
        live.update(_renderable())

    elapsed = time.perf_counter() - started_at

    # ── Phase 4 — Final ranked summary + stats footer ─────────────
    _console.print()
    _console.print(_ranked_summary_panel(rows))
    _console.print(_stats_footer(rows, elapsed, out_path))
    _console.print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
