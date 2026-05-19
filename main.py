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
import collections
import random
import signal
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from loguru import logger

from config.settings import Settings
from core.client import AsyncClobClient
from core.inventory import InventoryTracker
from core.market_manager import MarketManager, MarketState
from core.order_manager import OrderManager, QuoteUpdateResult
from core.fill_collector import FillCollector
from core.quoter import Quoter, TwoSidedQuotes
from core.rewards_tracker import RewardsTracker
from core.inventory_seeder import InventorySeeder
from core.scoring_monitor import ScoringMonitor
from core.toxicity_analyzer import ToxicityAnalyzer
from core.ws_feed import WebSocketFeed
from utils.discord import DiscordNotifier
from utils.helpers import seconds_until_midnight_utc
from utils.logging import setup_logging

# Locks protecting shared state from concurrent mutation by background tasks
_market_lock = asyncio.Lock()
_inventory_lock = asyncio.Lock()

# Per-market debounce timestamps for hot requotes
_hot_requote_last: Dict[str, float] = {}

# Track whether the daily drawdown alert has been sent for the current paused state
_drawdown_alert_sent: bool = False


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
    ws_feed: Optional[WebSocketFeed] = None,
) -> dict:
    """
    Fetch midpoints — reads from the WS cache first (instant, free),
    falls back to REST batch/individual calls for any misses.
    """
    token_ids = [m.token_id_yes for m in markets if m.token_id_yes]
    result: dict = {}
    if not token_ids:
        return result

    # Phase 1: read from WS cache (instant, no API calls)
    ws_hits = 0
    if ws_feed:
        for tid in token_ids:
            mid = ws_feed.get_mid(tid)
            if mid is not None:
                result[tid] = mid
                ws_hits += 1
        if ws_hits:
            logger.debug("WS cache: {}/{} midpoints from cache", ws_hits, len(token_ids))

    # Phase 2: REST fallback for any tokens the WS didn't cover
    missing = [tid for tid in token_ids if tid not in result]
    if not missing:
        return result

    try:
        from py_clob_client_v2 import BookParams
        params = [BookParams(token_id=tid) for tid in missing]
        batch_resp = await client.get_midpoints(params)

        if isinstance(batch_resp, list):
            for i, tid in enumerate(missing):
                if i >= len(batch_resp):
                    break
                mid = _parse_midpoint_value(batch_resp[i])
                if mid is not None:
                    result[tid] = mid
        elif isinstance(batch_resp, dict):
            for tid in missing:
                mid = _parse_midpoint_value(batch_resp.get(tid))
                if mid is not None:
                    result[tid] = mid

        if len(result) > ws_hits:
            logger.debug("REST batch: {}/{} additional midpoints", len(result) - ws_hits, len(missing))
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
# Event-driven hot requote (triggered by WebSocket mid-price changes)
# ──────────────────────────────────────────────────────────────────────

# These module-level refs are set by main() once components are initialised.
_ws_feed: Optional[WebSocketFeed] = None
_hot_market_mgr: Optional[MarketManager] = None
_hot_order_mgr: Optional[OrderManager] = None
_hot_inventory: Optional["InventoryTracker"] = None
_hot_quoter: Optional[Quoter] = None
_hot_settings: Optional[Settings] = None
_hot_discord: Optional[DiscordNotifier] = None


async def hot_requote(token_id: str, new_mid: float, is_fast: bool = False) -> None:
    """Event-driven requote triggered by WS mid-price change.

    Fires for a single market when its mid has moved >= 1 tick from the
    current order center.  Lightweight: no fill collection, no skew eval,
    no exposure checks — the cold path handles all that.
    """
    if not all((_hot_market_mgr, _hot_order_mgr, _hot_inventory, _hot_quoter, _hot_settings)):
        return

    if _hot_inventory.is_drawdown_paused:
        return

    market = _hot_market_mgr.get_market_by_token(token_id)
    if not market:
        return

    if _hot_quoter.is_midpoint_paused(market.condition_id):
        return

    # P2a: feed the YES-side mid into rolling history first so the spike
    # detector and vol multiplier see the latest tick before we decide.
    yes_mid_for_history = (
        new_mid if token_id == market.token_id_yes else (1.0 - new_mid)
    )
    _hot_quoter.record_mid(market.condition_id, yes_mid_for_history)

    if _hot_quoter.is_spike_paused(market.condition_id):
        return

    tick_value = {"0.1": 0.1, "0.01": 0.01, "0.001": 0.001, "0.0001": 0.0001}.get(
        market.tick_size, 0.01,
    )
    is_spike, ticks_moved = _hot_quoter.evaluate_spike(
        market.condition_id, tick_value,
    )
    if is_spike:
        _hot_quoter.trigger_spike_pause(market.condition_id)
        logger.warning(
            "SPIKE detected cid={} ticks_moved={} (>={}) within {}s — "
            "cancelling orders, pausing {}s",
            market.condition_id[:12], ticks_moved,
            _hot_settings.SPIKE_TICKS_THRESHOLD,
            _hot_settings.SPIKE_DETECTION_WINDOW_SECONDS,
            _hot_settings.SPIKE_PAUSE_SECONDS,
        )
        asyncio.create_task(_hot_spike_react(market, ticks_moved))
        return

    now = asyncio.get_event_loop().time()
    last = _hot_requote_last.get(market.condition_id, 0)
    debounce_s = _hot_settings.HOT_REQUOTE_DEBOUNCE_MS / 1000.0
    if now - last < debounce_s:
        return
    _hot_requote_last[market.condition_id] = now

    try:
        async with _hot_order_mgr.market_lock(market.condition_id):
            pos = _hot_inventory.get_position(market.condition_id)
            skew_action = _hot_inventory.evaluate(market.condition_id)

            yes_mid = new_mid if token_id == market.token_id_yes else (1.0 - new_mid)

            quotes = _hot_quoter.compute(
                market=market,
                live_mid=yes_mid,
                skew_ratio=skew_action.skew_ratio,
                heavy_side=skew_action.heavy_side,
                paused_side=skew_action.paused_side,
                yes_position=pos.yes_size if pos else 0.0,
                no_position=pos.no_size if pos else 0.0,
            )
            result = await _hot_order_mgr.cancel_and_replace(quotes)

        if result.posted and _ws_feed:
            _ws_feed.update_order_center(market.token_id_yes, yes_mid)
            _ws_feed.update_order_center(market.token_id_no, 1.0 - yes_mid)
            # INFO only when an order is actually modified/cancelled
            # (Issue 6).  Smart-diff "kept" paths emit nothing here — the
            # underlying order_manager.cancel_and_replace() already logs
            # at DEBUG when it keeps an order within the tick threshold.
            logger.info(
                "Hot requote {} — {} orders posted (fast={})",
                market.condition_id[:12], len(result.posted), is_fast,
            )
        elif result.kept and not result.posted:
            # All sides within tick threshold; nothing modified — DEBUG only.
            logger.debug(
                "Hot requote {} — kept {} orders unchanged (fast={})",
                market.condition_id[:12], result.kept, is_fast,
            )
    except Exception as exc:
        logger.error("Hot requote failed for {}: {}", market.condition_id[:12], exc)
        try:
            ghosts = await _hot_order_mgr.reconcile_ghost_orders()
            if ghosts > 0:
                logger.warning("Hot-path ghost sweep: cancelled {} orphaned orders", ghosts)
        except Exception:
            pass


async def _hot_spike_react(market, ticks_moved: int) -> None:
    """Cancel quotes + Discord alert after a spike detection.

    Detached from hot_requote so the detection path returns immediately;
    the cancel + notify happen in the background. Cold path skips the
    market while ``is_spike_paused`` is True (set by the caller).
    """
    if not _hot_order_mgr or not _hot_settings:
        return
    try:
        async with _hot_order_mgr.market_lock(market.condition_id):
            await _hot_order_mgr.cancel_orders_for_market(market.condition_id)
    except Exception as exc:
        logger.error(
            "Spike cancel failed for {}: {}", market.condition_id[:12], exc,
        )
    if _hot_discord is not None:
        try:
            await _hot_discord.send_alert(
                title="Spike Detector — Market Paused",
                message=(
                    f"Market `{market.condition_id[:12]}…` spike: "
                    f"**{ticks_moved} ticks** moved within "
                    f"{_hot_settings.SPIKE_DETECTION_WINDOW_SECONDS}s "
                    f"(threshold {_hot_settings.SPIKE_TICKS_THRESHOLD}). "
                    f"Quotes cancelled. Pause **{_hot_settings.SPIKE_PAUSE_SECONDS}s** — "
                    f"vol multiplier handles progressive resume."
                ),
                level="warning",
            )
        except Exception as exc:
            logger.debug("Spike Discord alert failed: {}", exc)


# ──────────────────────────────────────────────────────────────────────
# Quoting cycle
# ──────────────────────────────────────────────────────────────────────


_MAX_NOTIFIED_ORDERS = 1000
_notified_orders: collections.OrderedDict = collections.OrderedDict()


async def run_quoting_cycle(
    market_mgr: MarketManager,
    client: AsyncClobClient,
    quoter: Quoter,
    order_mgr: OrderManager,
    inventory: InventoryTracker,
    fill_collector: FillCollector,
    discord: DiscordNotifier,
    scoring_monitor: ScoringMonitor,
    toxicity: ToxicityAnalyzer,
    settings: Settings,
    cycle_index: int = 0,
    skip_cids: set | None = None,
    ws_feed: Optional[WebSocketFeed] = None,
) -> Optional[Tuple[int, float, float]]:
    """
    One full cycle: collect fills, fetch midpoints, iterate active markets,
    evaluate skew, compute fresh quotes, post them.

    Fill collection and midpoint fetching are done once per cycle (not per-market)
    to minimize API calls and prevent double-counting.
    """
    async with _market_lock:
        markets = market_mgr.markets
    if not markets:
        logger.warning("No active markets — skipping cycle")
        return

    cycle_start = asyncio.get_event_loop().time()

    # ── Step 1: Collect fills once per cycle (P0-1b / P0-2) ─────
    await fill_collector.poll()
    all_fills = fill_collector.drain()
    known_cids = {m.condition_id for m in markets}
    new_fills = [f for f in all_fills if f.condition_id in known_cids]
    if len(all_fills) > len(new_fills):
        logger.debug(
            "Filtered {} fills from non-active markets (orphaned orders)",
            len(all_fills) - len(new_fills),
        )
    filled_cids: set = set()
    if new_fills:
        async with _inventory_lock:
            inventory.record_fills(new_fills)
        for f in new_fills:
            filled_cids.add(f.condition_id)
            toxicity.record_fill(f)
            asyncio.create_task(discord.send_fill(f.condition_id, f.side, f.price, f.filled_size))
        logger.info("Recorded {} fills into inventory", len(new_fills))

    # ── Step 1b: Daily drawdown circuit breaker ──────────────────
    global _drawdown_alert_sent
    if inventory.check_daily_drawdown():
        if not _drawdown_alert_sent:
            limit_usd = settings.DAILY_DRAWDOWN_LIMIT_PCT * settings.TOTAL_CAPITAL
            logger.error(
                "DAILY DRAWDOWN PAUSE: realised PnL today=${:.2f} (limit=-${:.2f}) — "
                "cancelling all orders, no new quotes until UTC midnight",
                inventory.realised_pnl_today, limit_usd,
            )
            try:
                await order_mgr.cancel_all_orders()
            except Exception as exc:
                logger.error("Drawdown pause: cancel_all_orders failed: {}", exc)
            await discord.send_alert(
                title="Daily Drawdown Circuit Breaker",
                message=(
                    f"Realised PnL today **${inventory.realised_pnl_today:.2f}** "
                    f"breached limit **-${limit_usd:.2f}** "
                    f"({settings.DAILY_DRAWDOWN_LIMIT_PCT:.0%} of ${settings.TOTAL_CAPITAL:,.0f}). "
                    f"All orders cancelled. Quoting paused until UTC midnight."
                ),
                level="critical",
            )
            _drawdown_alert_sent = True
        return None
    else:
        _drawdown_alert_sent = False

    # ── Step 2: Select markets for this cycle (round-robin) ──────
    markets_this_cycle = _select_cycle_markets(
        markets, cycle_index, settings.MAX_MARKETS_PER_CYCLE,
    )

    # ── Step 2b: Refresh USDC balance for collateral pre-checks ──
    await order_mgr.refresh_usdc_balance()

    # ── Step 3: Batch fetch midpoints (P0-4 / P3-1) ─────────────
    midpoints = await _batch_fetch_midpoints(client, markets_this_cycle, ws_feed=ws_feed)
    inventory.update_midpoints(midpoints)

    # ── Step 3a': Feed mids into quoter + toxicity rolling histories ──
    # Keeps the realised-vol window (P2a) and fill-vs-mid lookback (P2b)
    # fresh even if the WS hot path is not firing (e.g. WS down, low-
    # traffic market). No-op when mids are missing — both record_mid
    # impls drop out-of-band values internally.
    for m in markets_this_cycle:
        mid = midpoints.get(m.token_id_yes)
        if mid is not None:
            quoter.record_mid(m.condition_id, mid)
            toxicity.record_mid(m.condition_id, mid)

    # ── Step 3b: Feed scoring rates into quoter ─────────────────
    scoring_rates = scoring_monitor.get_all_scoring_rates()
    if scoring_rates:
        quoter.update_scoring_rates(scoring_rates)

    # ── Step 4: Per-market exposure gate (Issue 5) ──────────────
    # Pause individual markets that breach MAX_MARKET_EXPOSURE_RATIO
    # BEFORE the global gross check so a runaway market is contained
    # without dragging the whole bot to the global limit.
    active_cids = {m.condition_id for m in markets}
    paused_set: set[str] = set()
    skip_reasons: Dict[str, str] = {}
    for m in markets_this_cycle:
        breached, exposure_val, limit_val = inventory.is_market_exposure_breached(
            m.condition_id,
            allocated_capital=m.allocated_capital,
            open_order_notional_for_market=order_mgr.open_order_notional_for_market(m.condition_id),
        )
        if breached:
            logger.warning(
                "WARNING | exposure | Market {} — per-market limit breached "
                "(net=${:.2f}, limit=${:.2f}) — pausing quotes",
                m.condition_id[:12], exposure_val, limit_val,
            )
            paused_set.add(m.condition_id)
            skip_reasons[m.condition_id] = "exposure_per_market"
            try:
                await order_mgr.cancel_orders_for_market(m.condition_id)
            except Exception as exc:
                logger.error(
                    "Per-market exposure pause: cancel_orders_for_market({}) failed: {}",
                    m.condition_id[:12], exc,
                )

    # ── Step 4b: Global gross exposure (deployed-capital-relative) ──
    # Limit = sum(allocated_capital across active markets) × MAX_EXPOSURE_RATIO
    # This is *deployed* capital, not the static TOTAL_CAPITAL ceiling — so
    # the gate scales with what the bot actually has out on the book.
    deployed_capital = sum(m.allocated_capital for m in markets) or settings.TOTAL_CAPITAL
    gross_breached = inventory.is_gross_exposure_breached(
        order_mgr.open_order_notional, active_cids,
        deployed_capital=deployed_capital,
    )
    net_breached = inventory.is_global_exposure_breached(active_cids)
    if net_breached or gross_breached:
        exposure_type = "gross" if gross_breached else "net"
        exposure_val = (
            inventory.gross_exposure_usd(order_mgr.open_order_notional, active_cids)
            if gross_breached else inventory.global_exposure_usd(active_cids)
        )
        # Pause at most N-1 markets so at least one stays active for rebalancing
        max_pause = max(1, len(active_cids) - 1)
        paused = inventory.most_imbalanced_markets(n=max_pause, active_cids=active_cids)
        logger.warning(
            "Global {} exposure breached (${:.0f}) — pausing {}/{} markets\n{}",
            exposure_type, exposure_val, len(paused), len(active_cids),
            inventory.exposure_breakdown(active_cids),
        )
        if cycle_index % 20 == 0:
            await discord.send_alert(
                title="Global Exposure Limit",
                message=f"{exposure_type.title()} exposure ${exposure_val:.0f} exceeds limit. Pausing {len(paused)}/{len(active_cids)} markets.",
                level="warning",
            )
        for cid in paused:
            paused_set.add(cid)
            skip_reasons.setdefault(cid, "exposure_global")

        # Cancel open orders on paused markets to stop further fills
        for cid in paused:
            await order_mgr.cancel_orders_for_market(cid)

    if skip_cids:
        for cid in skip_cids:
            paused_set.add(cid)
            skip_reasons.setdefault(cid, "seed_failed")

    # ── Step 4.5: Continuous midpoint guard (per market) ─────────
    newly_paused: list[tuple[str, float]] = []
    newly_recovered: list[str] = []
    for m in markets_this_cycle:
        cid = m.condition_id
        live_mid = midpoints.get(m.token_id_yes)
        if live_mid is None:
            continue  # no fresh quote — don't penalise a stale read
        was_paused = quoter.is_midpoint_paused(cid)
        is_paused, _breach_count = quoter.evaluate_midpoint_guard(cid, live_mid)
        if is_paused:
            paused_set.add(cid)
            skip_reasons.setdefault(cid, "midpoint_guard")
            if not was_paused:
                newly_paused.append((cid, live_mid))
        elif was_paused:
            newly_recovered.append(cid)

    for cid, mid_val in newly_paused:
        logger.warning(
            "Midpoint guard tripped: cid={} live_mid={:.4f} outside [{:.2f}, {:.2f}] "
            "for {} cycles — pausing market, cancelling open orders",
            cid[:12], mid_val,
            settings.MIDPOINT_GUARD_MIN, settings.MIDPOINT_GUARD_MAX,
            settings.MIDPOINT_GUARD_CYCLES,
        )
        try:
            await order_mgr.cancel_orders_for_market(cid)
        except Exception as exc:
            logger.error(
                "Midpoint guard: cancel_orders_for_market({}) failed: {}",
                cid[:12], exc,
            )
        asyncio.create_task(discord.send_alert(
            title="Midpoint Guard — Market Paused",
            message=(
                f"Market `{cid[:12]}…` paused: live mid **{mid_val:.4f}** outside "
                f"[{settings.MIDPOINT_GUARD_MIN:.2f}, {settings.MIDPOINT_GUARD_MAX:.2f}] "
                f"for {settings.MIDPOINT_GUARD_CYCLES} consecutive cycles. "
                f"Auto-resume on first in-band reading."
            ),
            level="warning",
        ))

    for cid in newly_recovered:
        logger.info(
            "Midpoint guard cleared: cid={} — resuming quotes next cycle",
            cid[:12],
        )
        asyncio.create_task(discord.send_alert(
            title="Midpoint Guard — Market Resumed",
            message=f"Market `{cid[:12]}…` mid back in band — quoting resumed.",
            level="info",
        ))

    # ── Step 4.6: Spike-pause skip (P2a) ─────────────────────────
    # Hot path already cancelled the orders and triggered the pause;
    # cold path simply skips these markets until the deadline elapses.
    for m in markets_this_cycle:
        if quoter.is_spike_paused(m.condition_id):
            paused_set.add(m.condition_id)
            skip_reasons.setdefault(m.condition_id, "spike_paused")

    # ── Step 4.7: Toxicity guard (P2b) ───────────────────────────
    # evaluate() resolves pending fill-vs-mid horizons, refreshes the
    # rolling toxicity score, and may trip the VPIN pause when imbalance
    # is extreme. Pattern mirrors Step 4.5 midpoint guard for the entry
    # transition (cancel + Discord alert), and Step 4.6 spike pause for
    # the silent auto-recovery (deadline-based, no Discord on resume).
    tox_newly_paused: list[tuple[str, float]] = []
    for m in markets_this_cycle:
        tick = {"0.1": 0.1, "0.01": 0.01, "0.001": 0.001, "0.0001": 0.0001}.get(
            m.tick_size, 0.01
        )
        was_paused = toxicity.is_toxic_paused(m.condition_id)
        toxicity.evaluate(m.condition_id, tick)
        if toxicity.is_toxic_paused(m.condition_id):
            paused_set.add(m.condition_id)
            skip_reasons.setdefault(m.condition_id, "toxicity_paused")
            if not was_paused:
                tox_newly_paused.append(
                    (m.condition_id, toxicity.vpin_imbalance(m.condition_id))
                )

    for cid, imb in tox_newly_paused:
        logger.warning(
            "Toxicity pause: cid={} VPIN imbalance={:.2f} ≥ pause threshold {:.2f} "
            "— cancelling orders, pausing {}s",
            cid[:12], imb, settings.VPIN_PAUSE_THRESHOLD,
            settings.TOXICITY_PAUSE_SECONDS,
        )
        try:
            await order_mgr.cancel_orders_for_market(cid)
        except Exception as exc:
            logger.error(
                "Toxicity pause: cancel_orders_for_market({}) failed: {}",
                cid[:12], exc,
            )
        asyncio.create_task(discord.send_alert(
            title="Toxicity Pause — Adverse Flow Detected",
            message=(
                f"Market `{cid[:12]}…` paused: VPIN imbalance **{imb:.2f}** "
                f"≥ threshold **{settings.VPIN_PAUSE_THRESHOLD:.2f}**. "
                f"Quotes cancelled. Pause **{settings.TOXICITY_PAUSE_SECONDS}s** — "
                f"auto-resume on expiry, multiplier handles progressive widening."
            ),
            level="warning",
        ))

    # ── Step 5: Per-market quoting (parallel with semaphore) ─────
    if markets_this_cycle:
        m0 = markets_this_cycle[0]
        mid0 = midpoints.get(m0.token_id_yes, m0.midpoint)
        logger.debug(
            "Sample market: cid={}… max_spread={} min_size={} gamma_mid={} live_mid={} alloc=${}",
            m0.condition_id[:12], m0.max_spread, m0.min_size, m0.midpoint, mid0, m0.allocated_capital,
        )

    _sem = asyncio.Semaphore(4)
    # Per-market classification (Issue 3 / 4) — every entry is keyed by
    # condition_id so the cycle-summary table can be rebuilt deterministically.
    market_results: Dict[str, Dict[str, Any]] = {}

    async def _process_market(market):
        async with _sem:
            posted = kept = errors = 0
            two_sided = False
            if market.condition_id in paused_set:
                return (market.condition_id, 0, 0, 0, False)
            try:
                async with order_mgr.market_lock(market.condition_id):
                    async with _inventory_lock:
                        inventory.ensure_position(
                            market.condition_id,
                            yes_token_id=market.token_id_yes,
                            no_token_id=market.token_id_no,
                        )
                        skew_action = inventory.evaluate(market.condition_id)

                    if skew_action.should_alert:
                        asyncio.create_task(discord.send_skew_warning(
                            market.condition_id,
                            skew_action.skew_ratio,
                            skew_action.heavy_side,
                        ))

                    if skew_action.pause_triggered:
                        asyncio.create_task(discord.send_skew_pause(
                            market.condition_id,
                            skew_action.skew_ratio,
                            skew_action.heavy_side,
                            settings.SKEW_PAUSE_DURATION_SECONDS,
                        ))

                    live_mid = midpoints.get(market.token_id_yes, market.midpoint)

                    pos = inventory.get_position(market.condition_id)
                    quotes = quoter.compute(
                        market=market,
                        live_mid=live_mid,
                        skew_ratio=skew_action.skew_ratio,
                        heavy_side=skew_action.heavy_side,
                        paused_side=skew_action.paused_side,
                        recovery_side=skew_action.recovery_side,
                        yes_position=pos.yes_size if pos else 0.0,
                        no_position=pos.no_size if pos else 0.0,
                    )
                    two_sided = quotes.is_two_sided

                    force = market.condition_id in filled_cids
                    result = await order_mgr.cancel_and_replace(quotes, force_replace=force)
                    posted = len(result.posted)
                    kept = result.kept

                    # Update WS order centers so hot path knows current quote position
                    if result.posted and ws_feed:
                        ws_feed.update_order_center(market.token_id_yes, live_mid)
                        ws_feed.update_order_center(market.token_id_no, 1.0 - live_mid)

                for rec in result.posted:
                    market_key = f"{rec.condition_id}:{rec.side}:{rec.token_id}"
                    if market_key not in _notified_orders:
                        _notified_orders[market_key] = None
                        while len(_notified_orders) > _MAX_NOTIFIED_ORDERS:
                            _notified_orders.popitem(last=False)
                        asyncio.create_task(discord.send_order_posted(
                            condition_id=rec.condition_id,
                            side=rec.side,
                            token_label=rec.token_id[:12],
                            price=rec.price,
                            size=rec.size,
                            order_id=rec.order_id,
                        ))

            except Exception as exc:
                errors = 1
                logger.error("Cycle error for {}: {}", market.condition_id[:12], exc)
            return (market.condition_id, posted, kept, errors, two_sided)

    tasks = [_process_market(m) for m in markets_this_cycle]
    cycle_results = await asyncio.gather(*tasks)
    cycle_posted = 0
    cycle_kept = 0
    cycle_errors = 0
    for cid, posted, kept, errors, two_sided in cycle_results:
        cycle_posted += posted
        cycle_kept += kept
        cycle_errors += errors
        market_results[cid] = {
            "posted": posted,
            "kept": kept,
            "errors": errors,
            "two_sided": two_sided,
        }

    # ── Step 6: Scoring check + auto-remediation ─────────────────
    remediate_cids = await scoring_monitor.check_if_due(markets_this_cycle)
    if remediate_cids:
        for market in markets_this_cycle:
            if market.condition_id not in remediate_cids:
                continue
            try:
                live_mid = midpoints.get(market.token_id_yes, market.midpoint)
                pos = inventory.get_position(market.condition_id)
                skew_action = inventory.evaluate(market.condition_id)
                quotes = quoter.compute(
                    market=market,
                    live_mid=live_mid,
                    skew_ratio=skew_action.skew_ratio,
                    heavy_side=skew_action.heavy_side,
                    paused_side=skew_action.paused_side,
                    recovery_side=skew_action.recovery_side,
                    yes_position=pos.yes_size if pos else 0.0,
                    no_position=pos.no_size if pos else 0.0,
                )
                result = await order_mgr.cancel_and_replace(quotes, force_replace=True)
                if result.posted:
                    logger.info(
                        "Remediation: force-replaced {} orders for {}",
                        len(result.posted), market.condition_id[:12],
                    )
                    cycle_posted += len(result.posted)
            except Exception as exc:
                logger.error("Remediation failed for {}: {}", market.condition_id[:12], exc)

    cycle_volume = sum(f.price * f.filled_size for f in new_fills) if new_fills else 0.0

    # ── Per-market metrics logging (Batch 5a) ──────────────────
    total_allocated = sum(m.allocated_capital for m in markets_this_cycle)
    total_deployed = order_mgr.open_order_notional
    capital_utilization = total_deployed / max(total_allocated, 1.0)
    if cycle_index % 20 == 0:
        logger.info(
            "Capital utilization: ${:.0f} deployed / ${:.0f} allocated = {:.0%}",
            total_deployed, total_allocated, capital_utilization,
        )

    # ── Step 7: Classify markets + cycle summary table (Issue 3/4/6) ──
    elapsed = asyncio.get_event_loop().time() - cycle_start
    scoring_rates_now = scoring_monitor.get_all_scoring_rates()

    selected_count = len(markets_this_cycle)
    active_count = 0
    scoring_count = 0

    table_rows: list[str] = []
    skip_log_lines: list[str] = []
    for m in markets_this_cycle:
        cid = m.condition_id
        prior_state = market_mgr.get_state(cid)
        info = market_results.get(cid, {})
        two_sided = bool(info.get("two_sided"))
        posted = int(info.get("posted") or 0)
        kept = int(info.get("kept") or 0)
        errors = int(info.get("errors") or 0)

        # Determine state for this cycle. SEED_FAILED is sticky from the
        # seeder run (set_state guards against being overwritten downward).
        if prior_state == MarketState.SEED_FAILED:
            state = MarketState.SEED_FAILED
            skip_log_lines.append(
                f"  SKIP  {cid[:12]}…  reason=seed_failed"
            )
        elif cid in paused_set:
            state = MarketState.INACTIVE
            reason = skip_reasons.get(cid, "paused")
            skip_log_lines.append(f"  SKIP  {cid[:12]}…  reason={reason}")
        elif two_sided and (posted > 0 or kept > 0) and errors == 0:
            # Two-sided posted (or maintained) — eligible for scoring.
            # Confirmed scoring is asserted by scoring_monitor's last
            # observation; until we have one, mark TWO_SIDED as SCORING
            # since LP rewards accrue while the order is live & two-sided.
            scoring_rate = scoring_rates_now.get(cid)
            if scoring_rate is None or scoring_rate >= 0.5:
                state = MarketState.SCORING
                scoring_count += 1
            else:
                state = MarketState.INACTIVE
            active_count += 1
        elif (posted > 0 or kept > 0) and not two_sided:
            state = MarketState.ONE_SIDED
            active_count += 1  # actively quoting, just not two-sided
            skip_log_lines.append(f"  SKIP  {cid[:12]}…  reason=skew_one_sided")
        else:
            state = MarketState.INACTIVE
            if errors > 0:
                skip_log_lines.append(f"  SKIP  {cid[:12]}…  reason=cycle_error")

        market_mgr.set_state(cid, state)

        # Build summary table row
        pos = inventory.get_position(cid)
        skew_str = "—"
        net_str = "—"
        if pos is not None:
            ratio = pos.skew_ratio
            sign = "+" if pos.heavy_side == "NO" else ("-" if pos.heavy_side == "YES" else "")
            skew_str = f"{sign}{ratio:.0%}"
            yes_mid = midpoints.get(m.token_id_yes, m.midpoint)
            net_str = f"${pos.gross_exposure_at_mid(yes_mid):.1f}"

        live_mid = midpoints.get(m.token_id_yes, m.midpoint)
        if state in (MarketState.SEED_FAILED, MarketState.INACTIVE):
            bid_disp = ask_disp = "  ---"
        else:
            tick = {"0.1": 0.1, "0.01": 0.01, "0.001": 0.001, "0.0001": 0.0001}.get(m.tick_size, 0.01)
            offset = settings.QUOTE_OFFSET_TICKS * tick if settings.QUOTE_OFFSET_TICKS else m.max_spread * settings.tightness_factor
            bid_disp = f"{max(0.01, live_mid - offset):.3f}"
            ask_disp = f"{min(0.99, live_mid + offset):.3f}" if state != MarketState.ONE_SIDED else "  ---"

        question = (m.question or cid[:18])[:20]
        table_rows.append(
            f"│  {question:<20}  {state.value:<11}  {bid_disp:>5}  {ask_disp:>5}  "
            f"{skew_str:>6}  {net_str:>7}  │"
        )

    # Cycle summary table (Issue 6)
    title = (
        f"┌─ Cycle #{cycle_index} "
        + "─" * max(0, 80 - 13 - len(str(cycle_index)))
        + "─┐"
    )
    header_row = (
        f"│  {'Market':<20}  {'State':<11}  {'Bid':>5}  {'Ask':>5}  "
        f"{'Skew':>6}  {'Net($)':>7}  │"
    )
    bottom_summary = (
        f"│  posted={cycle_posted}  kept={cycle_kept}  errors={cycle_errors}  "
        f"elapsed={elapsed:.1f}s"
    )
    bottom_summary = bottom_summary + " " * max(0, len(title) - len(bottom_summary) - 1) + "│"
    bottom = "└" + "─" * (len(title) - 2) + "┘"

    logger.info(title)
    logger.info(header_row)
    for row in table_rows:
        logger.info(row)
    logger.info(bottom_summary)
    logger.info(bottom)

    logger.info(
        "Cycle #{} complete — selected={} active={} scoring={} | "
        "posted={} kept={} errors={} | {:.1f}s",
        cycle_index, selected_count, active_count, scoring_count,
        cycle_posted, cycle_kept, cycle_errors, elapsed,
    )
    if active_count < selected_count:
        for line in skip_log_lines:
            logger.info(line)

    fill_count = len(new_fills) if new_fills else 0
    has_news = fill_count > 0 or cycle_errors > 0 or cycle_index == 0
    if has_news:
        await discord.send_cycle_summary(
            cycle_index=cycle_index,
            markets_count=active_count,
            orders_posted=cycle_posted,
            errors=cycle_errors,
            elapsed=elapsed,
            fills=fill_count,
            scoring_pct=scoring_monitor.last_ratio,
        )

    if cycle_errors > 0:
        try:
            ghosts = await order_mgr.reconcile_ghost_orders()
            if ghosts > 0:
                logger.warning(
                    "Post-error ghost sweep: cancelled {} orphaned orders", ghosts
                )
        except Exception as exc:
            logger.error("Post-error ghost sweep failed: {}", exc)

    cycle_quoted = order_mgr.drain_quoted_notional()
    return cycle_posted, cycle_volume, cycle_quoted


# ──────────────────────────────────────────────────────────────────────
# Background tasks
# ──────────────────────────────────────────────────────────────────────


async def market_refresh_task(market_mgr: MarketManager, interval: int = 900) -> None:
    """Refresh market universe every 15 minutes. Re-subscribes WS feed on change."""
    while True:
        await asyncio.sleep(interval)
        try:
            async with _market_lock:
                await market_mgr.refresh()

            # Re-subscribe WS feed with refreshed token set
            if _ws_feed and market_mgr.markets:
                token_ids = []
                tick_sizes: Dict[str, float] = {}
                for m in market_mgr.markets:
                    tick = {"0.1": 0.1, "0.01": 0.01, "0.001": 0.001, "0.0001": 0.0001}.get(m.tick_size, 0.01)
                    for tid in (m.token_id_yes, m.token_id_no):
                        if tid:
                            token_ids.append(tid)
                            tick_sizes[tid] = tick
                await _ws_feed.subscribe(token_ids, tick_sizes)
                logger.info("WS feed re-subscribed with {} tokens after market refresh", len(token_ids))
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


async def ghost_order_reconciliation_task(order_mgr: OrderManager, interval: int = 30) -> None:
    """Detect and cancel ghost orders frequently.

    Ghost orders are orders live on the CLOB but not in local tracking,
    typically caused by cancel API failures during stale-order replacement.
    A ghost + real order on the same side can both fill on a price move,
    doubling unintended exposure — so we check every 30s.
    """
    await asyncio.sleep(30)  # initial delay — let first cycle seed orders
    while True:
        try:
            cancelled = await order_mgr.reconcile_ghost_orders()
            if cancelled > 0:
                logger.info("Ghost reconciliation cleaned up {} orphaned orders", cancelled)
        except Exception as exc:
            logger.error("Ghost order reconciliation failed: {}", exc)
        await asyncio.sleep(interval)


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


async def periodic_dashboard_task(
    market_mgr: MarketManager,
    order_mgr: OrderManager,
    inventory: InventoryTracker,
    scoring_monitor: ScoringMonitor,
    discord: DiscordNotifier,
    settings: Settings,
    interval: int = 900,
) -> None:
    """Send a periodic status dashboard to Discord every 15 minutes."""
    while True:
        await asyncio.sleep(interval)
        try:
            deployed = order_mgr.open_order_notional
            inv_summary = inventory.summary()
            skew_parts = []
            for cid, data in inv_summary.items():
                if cid.startswith("_"):
                    continue
                if isinstance(data, dict) and data.get("skew", 0) > 0.15:
                    skew_parts.append(f"{cid[:8]}={data['skew']:.0%}")
            skew_str = ", ".join(skew_parts[:5]) if skew_parts else "balanced"

            await discord.send_status_dashboard(
                capital_deployed=deployed,
                capital_total=settings.TOTAL_CAPITAL,
                scoring_pct=scoring_monitor.last_ratio,
                market_count=market_mgr.market_count,
                fill_count=inventory.total_fill_count,
                pnl=inventory.realised_pnl,
                skew_summary=skew_str,
            )
        except Exception as exc:
            logger.debug("Dashboard send failed: {}", exc)


async def dynamic_realloc_task(
    market_mgr: MarketManager,
    scoring_monitor: ScoringMonitor,
    settings: Settings,
) -> None:
    """Periodically reallocate capital based on scoring performance."""
    if not settings.DYNAMIC_REALLOC_ENABLED:
        return
    while True:
        await asyncio.sleep(settings.DYNAMIC_REALLOC_INTERVAL_SECONDS)
        try:
            rates = scoring_monitor.get_all_scoring_rates()
            if rates:
                async with _market_lock:
                    market_mgr.apply_performance_multipliers(rates)
                    adjusted = market_mgr.reallocate_capital(rates)
                if adjusted:
                    logger.info("Dynamic reallocation adjusted {} market(s)", adjusted)
        except Exception as exc:
            logger.error("Dynamic reallocation failed: {}", exc)


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

            async with _inventory_lock:
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
    logger.info("WS: {}  Offset ticks: {}  Debounce: {}ms  VPIN pause: {:.2f}  Tox max mult: {:.2f}x",
                settings.WS_ENABLED, settings.QUOTE_OFFSET_TICKS,
                settings.HOT_REQUOTE_DEBOUNCE_MS, settings.VPIN_PAUSE_THRESHOLD,
                settings.TOXICITY_SPREAD_MULTIPLIER_MAX)
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

    market_mgr = MarketManager(client, settings, discord=discord)
    quoter = Quoter(settings)
    toxicity = ToxicityAnalyzer(settings)
    quoter.attach_toxicity_analyzer(toxicity)
    order_mgr = OrderManager(client, settings)
    inventory = InventoryTracker(settings)
    fill_collector = FillCollector(client)
    scoring_monitor = ScoringMonitor(client, settings, discord, order_manager=order_mgr)
    rewards = RewardsTracker(client, settings, inventory)

    # ── Cancel orphaned orders from previous sessions ───────────
    # Orders posted by earlier runs survive restarts.  If left live they
    # generate fills the inventory tracker cannot attribute correctly,
    # creating phantom one-sided exposure that triggers the global limit.
    try:
        await order_mgr.cancel_all_orders()
        logger.info("Startup: cleared all pre-existing orders on exchange")
    except Exception as exc:
        logger.warning("Startup: failed to clear pre-existing orders: {}", exc)

    # ── Load markets ─────────────────────────────────────────────
    if not market_mgr.load_cache():
        logger.info("No cache found — doing initial market fetch")

    await market_mgr.refresh()

    if market_mgr.market_count == 0:
        logger.critical("No markets available after filtering — exiting")
        await discord.send_error("No markets passed filters. Check config.")
        sys.exit(1)

    logger.info("Active markets: {}", market_mgr.market_count)

    # ── Inventory seed (acquire minimal token positions for ASK orders) ──
    unseeded_markets: set = set()
    seed_costs: dict = {}
    if settings.SEED_ENABLED:
        seeder = InventorySeeder(client, settings, discord)
        unseeded_markets, seed_costs = await seeder.seed_all(market_mgr.markets)

        # Mark per-market state up front (Issue 4): SEED_FAILED is sticky
        # across cycles via market_mgr.set_state(); other states transition
        # freely as the bot quotes.
        for m in market_mgr.markets:
            if m.condition_id in unseeded_markets:
                market_mgr.set_state(m.condition_id, MarketState.SEED_FAILED)
            else:
                market_mgr.set_state(m.condition_id, MarketState.UNKNOWN)

        if unseeded_markets:
            logger.info(
                "{} market(s) failed seeding — atomic skip (no position, excluded from quoting)",
                len(unseeded_markets),
            )

        # Re-sync CONDITIONAL allowances so the CLOB recognises the new balances
        token_ids = []
        for m in market_mgr.markets:
            if m.condition_id in unseeded_markets:
                continue
            if m.token_id_yes:
                token_ids.append(m.token_id_yes)
            if m.token_id_no:
                token_ids.append(m.token_id_no)
        if token_ids:
            await client.sync_conditional_allowances(token_ids)

    # ── Sync on-chain balances into inventory so quoter knows position sizes ──
    for m in market_mgr.markets:
        if m.condition_id in unseeded_markets:
            continue
        pos = inventory.ensure_position(m.condition_id, m.token_id_yes, m.token_id_no)
        if m.token_id_yes:
            bal = await client.get_conditional_balance(m.token_id_yes)
            if bal > 0 and bal > pos.yes_size:
                pos.yes_size = bal
                if pos.yes_cost <= 0:
                    _yes_seed = seed_costs.get(m.condition_id, {}).get("yes", 0.0)
                    if _yes_seed > 0:
                        pos.yes_cost = _yes_seed
                    else:
                        mid = m.midpoint if m.midpoint > 0 else 0.50
                        pos.yes_cost = bal * mid
                logger.info("Inventory sync: {} YES = {:.2f}", m.condition_id[:12], bal)
        if m.token_id_no:
            bal = await client.get_conditional_balance(m.token_id_no)
            if bal > 0 and bal > pos.no_size:
                pos.no_size = bal
                if pos.no_cost <= 0:
                    _no_seed = seed_costs.get(m.condition_id, {}).get("no", 0.0)
                    if _no_seed > 0:
                        pos.no_cost = _no_seed
                    else:
                        no_mid = (1.0 - m.midpoint) if m.midpoint > 0 else 0.50
                        pos.no_cost = bal * no_mid
                logger.info("Inventory sync: {} NO = {:.2f}", m.condition_id[:12], bal)

    # Advance fill collector watermark past any seed trades so they
    # don't get reported as regular fills on the first cycle.
    import time as _time
    fill_collector._watermark_ts = int(_time.time())
    fill_collector._save_watermark()
    logger.debug("Fill watermark advanced to now (skip seed trades)")

    await discord.send_startup(
        market_mgr.market_count,
        settings.TOTAL_CAPITAL,
        settings.MODE.value,
    )

    # ── WebSocket feed (event-driven quoting) ────────────────────
    global _ws_feed, _hot_market_mgr, _hot_order_mgr, _hot_inventory, _hot_quoter, _hot_settings, _hot_discord
    ws_feed: Optional[WebSocketFeed] = None

    if settings.WS_ENABLED:
        ws_feed = WebSocketFeed(on_mid_change=hot_requote)
        _ws_feed = ws_feed
        _hot_market_mgr = market_mgr
        _hot_order_mgr = order_mgr
        _hot_inventory = inventory
        _hot_quoter = quoter
        _hot_settings = settings
        _hot_discord = discord

        token_ids = []
        tick_sizes: Dict[str, float] = {}
        for m in market_mgr.markets:
            if m.condition_id in unseeded_markets:
                continue
            tick = {"0.1": 0.1, "0.01": 0.01, "0.001": 0.001, "0.0001": 0.0001}.get(m.tick_size, 0.01)
            for tid in (m.token_id_yes, m.token_id_no):
                if tid:
                    token_ids.append(tid)
                    tick_sizes[tid] = tick
        await ws_feed.subscribe(token_ids, tick_sizes)
        logger.info("WS feed: subscribed to {} tokens", len(token_ids))
    else:
        logger.info("WS feed disabled (WS_ENABLED=false)")

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
        asyncio.create_task(ghost_order_reconciliation_task(order_mgr)),
        asyncio.create_task(daily_rewards_task(rewards, market_mgr, discord, scoring_monitor)),
        asyncio.create_task(reconciliation_task(client, inventory, discord)),
        asyncio.create_task(dynamic_realloc_task(market_mgr, scoring_monitor, settings)),
        asyncio.create_task(periodic_dashboard_task(
            market_mgr, order_mgr, inventory, scoring_monitor, discord, settings,
        )),
    ]
    if ws_feed:
        bg_tasks.append(asyncio.create_task(ws_feed.run_forever()))

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
                    toxicity=toxicity,
                    settings=settings,
                    cycle_index=cycle_index,
                    skip_cids=unseeded_markets,
                    ws_feed=ws_feed,
                )
                if result:
                    posted, volume, quoted = result
                    rewards.record_cycle_stats(volume_usd=quoted, orders_posted=posted, filled_usd=volume)
            except Exception as exc:
                logger.error("Unhandled cycle error: {}", exc)
                await discord.send_error(f"Cycle error: {exc}")

            cycle_index += 1
            elapsed = asyncio.get_event_loop().time() - cycle_start
            sleep_time = max(0, settings.REBALANCE_INTERVAL_SECONDS - elapsed + random.uniform(-2.0, 2.0))
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
