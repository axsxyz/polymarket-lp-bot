#!/usr/bin/env python3
"""
Polymarket LP Farmer Bot — main entry point.

Orchestrates the full lifecycle:

  1. Initialise settings, logging, CLOB client, Discord notifier.
  2. Discover and filter reward-eligible markets.
  3. Run the quoting loop (cancel old quotes → compute new quotes → post).
  4. Background tasks: market refresh (15 min), periodic cleanup (4 h),
     daily rewards snapshot (midnight UTC).
  5. Graceful shutdown on SIGINT / SIGTERM (cancel all orders first).

Run with:
    python main.py
"""

from __future__ import annotations

import asyncio
import signal
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from loguru import logger

from config.settings import Settings
from core.client import AsyncClobClient
from core.inventory import InventoryTracker
from core.market_manager import MarketManager
from core.order_manager import OrderManager
from core.fill_collector import FillCollector
from core.quoter import Quoter, TwoSidedQuotes
from core.rewards_tracker import RewardsTracker
from core.scoring_monitor import ScoringMonitor
from utils.discord import DiscordNotifier
from utils.helpers import seconds_until_midnight_utc
from utils.logging import setup_logging


# ──────────────────────────────────────────────────────────────────────
# Quoting cycle — helpers
# ──────────────────────────────────────────────────────────────────────


def _select_cycle_markets(
    all_markets: list,
    cycle_index: int,
    max_per_cycle: int,
    always_include_top: int = 15,
) -> list:
    """
    Select markets for this cycle with round-robin for lower-priority ones.
    Top markets (by score) are always included; remaining rotate across cycles.
    """
    if len(all_markets) <= max_per_cycle:
        return list(all_markets)

    top = all_markets[:always_include_top]
    remaining = all_markets[always_include_top:]

    if not remaining:
        return list(top[:max_per_cycle])

    slots = max(1, max_per_cycle - len(top))
    start = (cycle_index * slots) % len(remaining)
    batch = []
    for i in range(slots):
        idx = (start + i) % len(remaining)
        batch.append(remaining[idx])

    return list(top) + batch


async def _batch_fetch_midpoints(
    client: AsyncClobClient,
    markets: list,
) -> dict:
    """
    Fetch midpoints in a single batch call. Falls back to individual calls
    if the batch endpoint is unavailable or fails.
    """
    token_ids = [m.token_id_yes for m in markets if m.token_id_yes]
    result: dict = {}
    if not token_ids:
        return result

    try:
        from py_clob_client.clob_types import BookParams
        params = [BookParams(token_id=tid) for tid in token_ids]
        batch_resp = await client.get_midpoints(params)

        if isinstance(batch_resp, list):
            if batch_resp:
                logger.info("Midpoint response type=list sample[0]={} type={}", batch_resp[0], type(batch_resp[0]))
            for i, tid in enumerate(token_ids):
                if i >= len(batch_resp):
                    break
                mid = _parse_midpoint_value(batch_resp[i])
                if mid is not None:
                    result[tid] = mid
        elif isinstance(batch_resp, dict):
            if batch_resp:
                sample_key = next(iter(batch_resp))
                logger.info(
                    "Midpoint response type=dict sample: key={}… val={} type={}",
                    sample_key[:20], batch_resp[sample_key], type(batch_resp[sample_key]),
                )
            for tid in token_ids:
                mid = _parse_midpoint_value(batch_resp.get(tid))
                if mid is not None:
                    result[tid] = mid

        if result:
            logger.debug("Batch midpoints: {}/{} fetched", len(result), len(token_ids))
            return result
    except Exception as exc:
        logger.debug("Batch midpoint fetch unavailable ({}), using individual calls", exc)

    for market in markets:
        tid = market.token_id_yes
        if not tid or tid in result:
            continue
        try:
            resp = await client.get_midpoint(tid)
            mid = _parse_midpoint_value(resp)
            if mid is not None:
                result[tid] = mid
        except Exception:
            pass

    return result


def _parse_midpoint_value(val) -> float | None:
    """Extract a float midpoint from various response formats."""
    try:
        if isinstance(val, dict):
            raw = val.get("mid", 0)
        elif val is not None:
            raw = val
        else:
            return None
        mid = float(raw)
        if 0.01 <= mid <= 0.99:
            return mid
    except (TypeError, ValueError):
        pass
    return None


# ──────────────────────────────────────────────────────────────────────
# Quoting cycle
# ──────────────────────────────────────────────────────────────────────


async def run_quoting_cycle(
    market_mgr: MarketManager,
    client: AsyncClobClient,
    quoter: Quoter,
    order_mgr: OrderManager,
    inventory: InventoryTracker,
    fill_collector: FillCollector,
    discord: DiscordNotifier,
    scoring_monitor: ScoringMonitor,
    settings: Settings,
    cycle_index: int = 0,
) -> Optional[Tuple[int, float]]:
    """
    One full cycle: collect fills, fetch midpoints, iterate active markets,
    evaluate skew, compute fresh quotes, post them.

    Fill collection and midpoint fetching are done once per cycle (not per-market)
    to minimize API calls and prevent double-counting.
    """
    markets = market_mgr.markets
    if not markets:
        logger.warning("No active markets — skipping cycle")
        return

    cycle_start = asyncio.get_event_loop().time()

    # ── Step 1: Collect fills once per cycle (P0-1b / P0-2) ─────
    await fill_collector.poll()
    new_fills = fill_collector.drain()
    if new_fills:
        inventory.record_fills(new_fills)
        for f in new_fills:
            if f.filled_size * f.price > 50:
                await discord.send_fill(f.condition_id, f.side, f.price, f.filled_size)
        logger.info("Recorded {} fills into inventory", len(new_fills))

    # ── Step 2: Select markets for this cycle (round-robin) ──────
    markets_this_cycle = _select_cycle_markets(
        markets, cycle_index, settings.MAX_MARKETS_PER_CYCLE,
    )

    # ── Step 3: Batch fetch midpoints (P0-4 / P3-1) ─────────────
    midpoints = await _batch_fetch_midpoints(client, markets_this_cycle)
    inventory.update_midpoints(midpoints)

    # ── Step 4: Check global exposure guard (net + gross) ─────────
    gross_breached = inventory.is_gross_exposure_breached(order_mgr.open_order_notional)
    net_breached = inventory.is_global_exposure_breached()
    if net_breached or gross_breached:
        paused = inventory.most_imbalanced_markets(n=5)
        exposure_type = "gross" if gross_breached else "net"
        exposure_val = (
            inventory.gross_exposure_usd(order_mgr.open_order_notional)
            if gross_breached else inventory.global_exposure_usd()
        )
        logger.warning(
            "Global {} exposure breached (${:.0f}) — pausing {} markets",
            exposure_type, exposure_val, len(paused),
        )
        await discord.send_alert(
            title="Global Exposure Limit",
            message=f"{exposure_type.title()} exposure ${exposure_val:.0f} exceeds limit. Pausing top-5 skewed markets.",
            level="warning",
        )
        paused_set = set(paused)
    else:
        paused_set = set()

    # ── Step 5: Per-market quoting ───────────────────────────────
    if markets_this_cycle:
        m0 = markets_this_cycle[0]
        mid0 = midpoints.get(m0.token_id_yes, m0.midpoint)
        logger.info(
            "Sample market: cid={}… max_spread={} min_size={} gamma_mid={} live_mid={} alloc=${}",
            m0.condition_id[:12], m0.max_spread, m0.min_size, m0.midpoint, mid0, m0.allocated_capital,
        )

    cycle_posted = 0
    cycle_errors = 0

    for market in markets_this_cycle:
        if market.condition_id in paused_set:
            continue

        try:
            inventory.ensure_position(
                market.condition_id,
                yes_token_id=market.token_id_yes,
                no_token_id=market.token_id_no,
            )

            skew_action = inventory.evaluate(market.condition_id)

            if skew_action.should_alert:
                await discord.send_skew_warning(
                    market.condition_id,
                    skew_action.skew_ratio,
                    skew_action.heavy_side,
                )

            if skew_action.pause_triggered:
                await discord.send_skew_pause(
                    market.condition_id,
                    skew_action.skew_ratio,
                    skew_action.heavy_side,
                    settings.SKEW_PAUSE_DURATION_SECONDS,
                )

            live_mid = midpoints.get(market.token_id_yes, market.midpoint)

            quotes = quoter.compute(
                market=market,
                live_mid=live_mid,
                skew_ratio=skew_action.skew_ratio,
                heavy_side=skew_action.heavy_side,
                paused_side=skew_action.paused_side,
            )

            posted = await order_mgr.cancel_and_replace(quotes)
            cycle_posted += len(posted)

        except Exception as exc:
            cycle_errors += 1
            logger.error("Cycle error for {}: {}", market.condition_id[:12], exc)

    # ── Step 6: Scoring check (non-blocking, skipped if not due) ──
    await scoring_monitor.check_if_due(markets_this_cycle)

    cycle_volume = sum(f.price * f.filled_size for f in new_fills) if new_fills else 0.0

    elapsed = asyncio.get_event_loop().time() - cycle_start
    logger.info(
        "Cycle #{} complete: {}/{} markets, {} orders posted, {} errors, {:.1f}s",
        cycle_index,
        len(markets_this_cycle),
        len(markets),
        cycle_posted,
        cycle_errors,
        elapsed,
    )
    return cycle_posted, cycle_volume


# ──────────────────────────────────────────────────────────────────────
# Background tasks
# ──────────────────────────────────────────────────────────────────────


async def market_refresh_task(market_mgr: MarketManager, interval: int = 900) -> None:
    """Refresh market universe every 15 minutes."""
    while True:
        await asyncio.sleep(interval)
        try:
            await market_mgr.refresh()
        except Exception as exc:
            logger.error("Market refresh failed: {}", exc)


async def stale_order_cleanup_task(order_mgr: OrderManager, interval: int = 120) -> None:
    """Cancel stale orders every 2 min — rolling refresh, no blanket cancel_all."""
    while True:
        await asyncio.sleep(interval)
        try:
            await order_mgr.cancel_stale_orders()
        except Exception as exc:
            logger.error("Stale order cleanup failed: {}", exc)


async def daily_rewards_task(
    rewards: RewardsTracker,
    market_mgr: MarketManager,
    discord: DiscordNotifier,
    scoring_monitor: ScoringMonitor,
) -> None:
    """Collect and report daily rewards shortly after midnight UTC."""
    while True:
        wait = seconds_until_midnight_utc() + 60  # 1 min after midnight
        logger.info("Next rewards snapshot in {:.0f}s", wait)
        await asyncio.sleep(wait)
        try:
            scored_count = int(scoring_monitor.last_ratio * market_mgr.market_count)
            snapshot = await rewards.collect_daily_snapshot(
                active_market_count=market_mgr.market_count,
                scored_market_count=scored_count,
                scoring_pct=scoring_monitor.last_ratio,
            )
            summary = rewards.format_summary(snapshot)
            await discord.send_daily_summary(summary)
        except Exception as exc:
            logger.error("Daily rewards task failed: {}", exc)


async def reconciliation_task(
    client: "AsyncClobClient",
    inventory: InventoryTracker,
    discord: DiscordNotifier,
    interval: int = 1800,
) -> None:
    """Reconcile local inventory against exchange every 30 minutes."""
    while True:
        await asyncio.sleep(interval)
        try:
            balances = await client.get_balance_allowance()
            if not balances:
                logger.debug("No balance data for reconciliation — skipping")
                continue

            exchange_pos = _parse_exchange_positions(balances, inventory)
            if not exchange_pos:
                continue

            corrected = inventory.reconcile(exchange_pos)
            if corrected > 0:
                logger.info("Reconciliation corrected {} position(s)", corrected)
                if corrected >= 3:
                    await discord.send_alert(
                        title="Inventory Reconciled",
                        message=f"Corrected **{corrected}** position(s) against exchange balances",
                        level="warning",
                    )
        except Exception as exc:
            logger.warning("Reconciliation failed: {}", exc)


def _parse_exchange_positions(
    balances: Any, inventory: InventoryTracker,
) -> Dict[str, Dict[str, float]]:
    """Parse balance API response into {condition_id: {"yes": size, "no": size}}.

    Handles multiple possible response shapes from the SDK.
    """
    result: Dict[str, Dict[str, float]] = {}

    if isinstance(balances, dict):
        items = balances.get("data", []) or balances.get("positions", [])
    elif isinstance(balances, list):
        items = balances
    else:
        return result

    token_to_market = {}
    for cid, pos in inventory.positions.items():
        if pos.yes_token_id:
            token_to_market[pos.yes_token_id] = (cid, "yes")
        if pos.no_token_id:
            token_to_market[pos.no_token_id] = (cid, "no")

    for item in items:
        if not isinstance(item, dict):
            continue
        asset_id = str(item.get("asset_id", "") or item.get("token_id", ""))
        try:
            balance = float(item.get("balance", 0) or item.get("size", 0) or 0)
        except (TypeError, ValueError):
            continue

        mapping = token_to_market.get(asset_id)
        if mapping:
            cid, side = mapping
            if cid not in result:
                result[cid] = {"yes": 0.0, "no": 0.0}
            result[cid][side] = balance

    return result


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────


async def main() -> None:
    settings = Settings()
    setup_logging(settings.LOG_LEVEL)

    logger.info("=" * 60)
    logger.info("Polymarket LP Farmer Bot")
    logger.info("Mode: {}  DRY_RUN: {}  Capital: ${:,.0f}", settings.MODE.value, settings.DRY_RUN, settings.TOTAL_CAPITAL)
    logger.info("Tightness: {:.2f}  Size mult: {:.1f}x  Interval: {}s", settings.tightness_factor, settings.SIZE_MULTIPLIER, settings.REBALANCE_INTERVAL_SECONDS)
    logger.info("API rate: {:.1f} req/s  Burst: {:.0f}  Max markets/cycle: {}", settings.API_RATE_LIMIT_RPS, settings.API_RATE_LIMIT_BURST, settings.MAX_MARKETS_PER_CYCLE)
    logger.info("=" * 60)

    # ── Init components ──────────────────────────────────────────
    discord = DiscordNotifier(settings)

    client = AsyncClobClient(settings)
    try:
        await client.initialize()
    except Exception as exc:
        logger.critical("Failed to initialise CLOB client: {}", exc)
        await discord.send_error(f"Failed to start: {exc}")
        sys.exit(1)

    # Verify connectivity
    try:
        ok = await client.get_ok()
        logger.info("CLOB health check: {}", ok)
    except Exception as exc:
        logger.critical("CLOB unreachable: {}", exc)
        await discord.send_error(f"CLOB unreachable: {exc}")
        sys.exit(1)

    market_mgr = MarketManager(client, settings)
    quoter = Quoter(settings)
    order_mgr = OrderManager(client, settings)
    inventory = InventoryTracker(settings)
    fill_collector = FillCollector(client)
    scoring_monitor = ScoringMonitor(client, settings, discord)
    rewards = RewardsTracker(client, settings, inventory)

    # ── Load markets ─────────────────────────────────────────────
    if not market_mgr.load_cache():
        logger.info("No cache found — doing initial market fetch")

    await market_mgr.refresh()

    if market_mgr.market_count == 0:
        logger.critical("No markets available after filtering — exiting")
        await discord.send_error("No markets passed filters. Check config.")
        sys.exit(1)

    logger.info("Active markets: {}", market_mgr.market_count)
    await discord.send_startup(
        market_mgr.market_count,
        settings.TOTAL_CAPITAL,
        settings.MODE.value,
    )

    # ── Graceful shutdown ────────────────────────────────────────
    shutdown_event = asyncio.Event()

    def _signal_handler(sig, frame):
        logger.warning("Received signal {} — initiating shutdown", sig)
        shutdown_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # ── Launch background tasks ──────────────────────────────────
    bg_tasks = [
        asyncio.create_task(market_refresh_task(market_mgr)),
        asyncio.create_task(stale_order_cleanup_task(order_mgr)),
        asyncio.create_task(daily_rewards_task(rewards, market_mgr, discord, scoring_monitor)),
        asyncio.create_task(reconciliation_task(client, inventory, discord)),
    ]

    # ── Main quoting loop ────────────────────────────────────────
    logger.info("Entering main quoting loop (interval={}s)", settings.REBALANCE_INTERVAL_SECONDS)

    cycle_index = 0
    try:
        while not shutdown_event.is_set():
            cycle_start = asyncio.get_event_loop().time()

            try:
                result = await run_quoting_cycle(
                    market_mgr=market_mgr,
                    client=client,
                    quoter=quoter,
                    order_mgr=order_mgr,
                    inventory=inventory,
                    fill_collector=fill_collector,
                    discord=discord,
                    scoring_monitor=scoring_monitor,
                    settings=settings,
                    cycle_index=cycle_index,
                )
                if result:
                    posted, volume = result
                    rewards.record_cycle_stats(volume_usd=volume, orders_posted=posted)
            except Exception as exc:
                logger.error("Unhandled cycle error: {}", exc)
                await discord.send_error(f"Cycle error: {exc}")

            cycle_index += 1
            elapsed = asyncio.get_event_loop().time() - cycle_start
            sleep_time = max(0, settings.REBALANCE_INTERVAL_SECONDS - elapsed)
            logger.info("Cycle took {:.1f}s — sleeping {:.1f}s", elapsed, sleep_time)

            try:
                await asyncio.wait_for(
                    shutdown_event.wait(),
                    timeout=sleep_time,
                )
            except asyncio.TimeoutError:
                pass

    finally:
        # ── Shutdown sequence ────────────────────────────────────
        logger.info("Shutting down — cancelling all open orders …")
        await order_mgr.cancel_all_orders()

        for task in bg_tasks:
            task.cancel()
        await asyncio.gather(*bg_tasks, return_exceptions=True)

        await discord.send_shutdown("graceful")
        await discord.close()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
