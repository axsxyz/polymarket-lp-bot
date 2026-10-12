#!/usr/bin/env python3
"""
Polymarket LP Farmer Bot — main entry point.

Orchestrates the full lifecycle:

  1. Initialise settings, logging, CLOB client, Discord notifier.
  2. Discover and filter reward-eligible markets.
  3. Run the quoting loop (cancel old quotes → compute new quotes → post).
  4. Background tasks: market refresh (15 min), fill ingest (2–5 s),
     daily rewards snapshot (midnight UTC), local tape HTTP (optional).
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
from core.resolution_guard import ResolutionGuard, exit_mode_for_reason
from core.rewards_tracker import RewardsTracker
from core.inventory_seeder import InventorySeeder
from core.scoring_monitor import ScoringMonitor
from core.toxicity_analyzer import ToxicityAnalyzer
from core.ws_feed import WebSocketFeed
from core.tape import LocalTape
from core.operator_digest import OperatorDigest
from core.hot_guards import hot_skip_reason, note_hot_skip_transition
from core.paper_broker import PaperBroker
from utils.discord import DiscordNotifier
from utils.helpers import seconds_until_midnight_utc
from utils.logging import setup_logging
from utils.tape_server import run_tape_server

# Locks protecting shared state from concurrent mutation by background tasks
_market_lock = asyncio.Lock()
_inventory_lock = asyncio.Lock()

# Per-market debounce timestamps for hot requotes
_hot_requote_last: Dict[str, float] = {}
# Last hot_skip_reason per cid — tape/digest emit only on appear/change
_hot_skip_last: Dict[str, str] = {}

# Track whether the daily drawdown alert has been sent for the current paused state
_drawdown_alert_sent: bool = False

# Shared with the hot path — written by cold cycle / seeder / refresh
_skip_cids: set[str] = set()
_exposure_paused: set[str] = set()
_recent_filled_cids: set[str] = set()
_recent_fill_events: list = []


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
_hot_resolution_guard: Optional[ResolutionGuard] = None
_hot_toxicity: Optional[ToxicityAnalyzer] = None
_hot_tape: Optional[LocalTape] = None
_hot_digest: Optional[OperatorDigest] = None
_hot_paper: Optional[PaperBroker] = None
_hot_fill_collector: Optional[FillCollector] = None


async def hot_requote(token_id: str, new_mid: float, is_fast: bool = False) -> None:
    """Event-driven requote triggered by WS mid-price change.

    Fires for a single market when its mid has moved >= 1 tick from the
    current order center.  Evaluates skew (inventory.evaluate) then
    posts via smart-diff.  Honors the same pause set as the cold path
    (toxicity, MarketState, exposure, skip_cids, drawdown, midpoint,
    spike) *before* compute/post.
    """
    if not all((_hot_market_mgr, _hot_order_mgr, _hot_inventory, _hot_quoter, _hot_settings)):
        return

    market = _hot_market_mgr.get_market_by_token(token_id)
    if not market:
        return

    cid = market.condition_id
    yes_mid_for_history = (
        new_mid if token_id == market.token_id_yes else (1.0 - new_mid)
    )
    try:
        _hot_quoter.record_mid(cid, yes_mid_for_history)
        if _hot_resolution_guard is not None:
            _hot_resolution_guard.record_midpoint(
                cid, yes_mid_for_history, datetime.now(timezone.utc),
            )
        if _hot_toxicity is not None:
            _hot_toxicity.record_mid(cid, yes_mid_for_history)
    except Exception as exc:
        if _hot_tape is not None:
            _hot_tape.exception("hot_record_mid", exc)

    reason = hot_skip_reason(
        cid,
        state=_hot_market_mgr.get_state(cid),
        toxic_paused=bool(_hot_toxicity and _hot_toxicity.is_toxic_paused(cid)),
        exposure_paused=cid in _exposure_paused,
        skip_cids=_skip_cids,
        drawdown=bool(_hot_inventory.is_drawdown_paused),
        midpoint_paused=_hot_quoter.is_midpoint_paused(cid),
        spike_paused=_hot_quoter.is_spike_paused(cid),
    )
    emit = note_hot_skip_transition(cid, reason, _hot_skip_last)
    if emit:
        if _hot_tape is not None:
            _hot_tape.skip(cid, emit)
        if _hot_digest is not None:
            _hot_digest.record_skip(cid, emit)
    if reason:
        return

    tick_value = {"0.1": 0.1, "0.01": 0.01, "0.001": 0.001, "0.0001": 0.0001}.get(
        market.tick_size, 0.01,
    )
    is_spike, ticks_moved = _hot_quoter.evaluate_spike(
        cid, tick_value,
    )
    if is_spike:
        _hot_quoter.trigger_spike_pause(cid)
        logger.warning(
            "SPIKE detected cid={} ticks_moved={} (>={}) within {}s — "
            "cancelling orders, pausing {}s",
            cid[:12], ticks_moved,
            _hot_settings.SPIKE_TICKS_THRESHOLD,
            _hot_settings.SPIKE_DETECTION_WINDOW_SECONDS,
            _hot_settings.SPIKE_PAUSE_SECONDS,
        )
        asyncio.create_task(_hot_spike_react(market, ticks_moved))
        return

    now = asyncio.get_event_loop().time()
    last = _hot_requote_last.get(cid, 0)
    debounce_s = _hot_settings.HOT_REQUOTE_DEBOUNCE_MS / 1000.0
    if now - last < debounce_s:
        return
    _hot_requote_last[cid] = now

    try:
        async with _hot_order_mgr.market_lock(cid):
            pos = _hot_inventory.get_position(cid)
            skew_action = _hot_inventory.evaluate(cid)

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
            if getattr(quotes, "bid_note", "") == "bid_capped_max_loss":
                return
            result = await _hot_order_mgr.cancel_and_replace(quotes)

        if result.posted and _ws_feed:
            _ws_feed.update_order_center(market.token_id_yes, yes_mid)
            _ws_feed.update_order_center(market.token_id_no, 1.0 - yes_mid)
            logger.info(
                "Hot requote {} — {} orders posted (fast={})",
                cid[:12], len(result.posted), is_fast,
            )
        elif result.kept and not result.posted:
            logger.debug(
                "Hot requote {} — kept {} orders unchanged (fast={})",
                cid[:12], result.kept, is_fast,
            )
    except Exception as exc:
        logger.error("Hot requote failed for {}: {}", cid[:12], exc)
        if _hot_tape is not None:
            _hot_tape.exception("hot_requote", exc)
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


async def apply_fills(
    fills,
    inventory: InventoryTracker,
    fill_collector: FillCollector,
    toxicity: ToxicityAnalyzer,
    discord: DiscordNotifier,
    tape: Optional[LocalTape] = None,
    *,
    commit: bool = True,
    persist: bool = True,
) -> None:
    """Apply fills under the inventory lock, then commit watermark + persist.

    Orphan cids (not in the current market list) still hit inventory.
    """
    global _recent_filled_cids, _recent_fill_events
    if not fills:
        return
    mids = getattr(inventory, "_midpoints", {}) or {}
    async with _inventory_lock:
        for f in fills:
            if f.mid_at_fill is None:
                f.mid_at_fill = mids.get(f.token_id)
        try:
            inventory.record_fills(fills)
        except Exception as exc:
            logger.error("record_fills failed — watermark not advanced: {}", exc)
            if tape is not None:
                tape.exception("record_fills", exc)
            return
        if commit:
            fill_collector.commit(fills)
        if persist:
            inventory.save_state()
    for f in fills:
        _recent_filled_cids.add(f.condition_id)
        _recent_fill_events.append(f)
        toxicity.record_fill(f)
        if tape is not None:
            tape.fill(
                condition_id=f.condition_id,
                token_id=f.token_id,
                side=f.side,
                price=f.price,
                filled_size=f.filled_size,
                mid_at_fill=f.mid_at_fill,
                trade_id=f.trade_id,
            )
            try:
                tape.snapshot_one(inventory, f.condition_id)
            except Exception:
                pass
        if _hot_digest is not None:
            _hot_digest.add_filled_notional(float(f.price) * float(f.filled_size))
        asyncio.create_task(
            discord.send_fill(f.condition_id, f.side, f.price, f.filled_size)
        )
    logger.info("Recorded {} fills into inventory", len(fills))


async def _paper_match(
    token_id: str,
    print_price: Optional[float] = None,
    print_size: float = 0.0,
    *,
    best_bid: Optional[float] = None,
    best_ask: Optional[float] = None,
) -> None:
    """DRY_RUN only: match live prints/BBO against the paper book. Fail-soft."""
    if _hot_paper is None or _hot_settings is None or not _hot_settings.DRY_RUN:
        return
    if _hot_inventory is None or _hot_fill_collector is None or _hot_toxicity is None:
        return
    if _hot_discord is None:
        return
    try:
        fills = _hot_paper.try_fill(
            token_id,
            print_price,
            print_size,
            best_bid=best_bid,
            best_ask=best_ask,
        )
    except Exception as extra:
        logger.warning("paper try_fill error: {}", extra)
        if _hot_tape is not None:
            _hot_tape.exception("paper_try_fill", extra)
        return
    if not fills:
        return
    if _hot_order_mgr is not None:
        try:
            _hot_order_mgr.sync_live_orders_from_paper()
        except Exception as extra:
            logger.debug("paper live-order sync failed: {}", extra)
    try:
        await apply_fills(
            fills,
            _hot_inventory,
            _hot_fill_collector,
            _hot_toxicity,
            _hot_discord,
            _hot_tape,
            commit=False,
            persist=False,
        )
    except Exception as extra:
        logger.warning("paper apply_fills error: {}", extra)
        if _hot_tape is not None:
            _hot_tape.exception("paper_apply_fills", extra)


async def _paper_on_last_trade(token_id: str, price: float, size: float) -> None:
    await _paper_match(token_id, print_price=price, print_size=size)


async def _paper_on_bbo(token_id: str, best_bid: float, best_ask: float) -> None:
    await _paper_match(
        token_id, print_price=None, print_size=0.0,
        best_bid=best_bid, best_ask=best_ask,
    )


def _credit_paper_seed(
    inventory: InventoryTracker,
    market,
    paper: PaperBroker,
    seed_costs: dict,
) -> None:
    """Copy paper YES/NO sizes; write-once first seed or add top-up USD."""
    pos = inventory.ensure_position(
        market.condition_id, market.token_id_yes, market.token_id_no,
    )
    prev_yes = float(pos.yes_size)
    prev_no = float(pos.no_size)
    if market.token_id_yes:
        pos.yes_size = paper.inventory_size(market.token_id_yes)
    if market.token_id_no:
        pos.no_size = paper.inventory_size(market.token_id_no)
    costs = seed_costs.get(market.condition_id, {})
    inventory.absorb_seed_acquisition(
        market.condition_id,
        prev_yes_size=prev_yes,
        prev_no_size=prev_no,
        yes_cost=float(costs.get("yes") or 0.0),
        no_cost=float(costs.get("no") or 0.0),
    )


async def fill_ingest_task(
    fill_collector: FillCollector,
    inventory: InventoryTracker,
    toxicity: ToxicityAnalyzer,
    discord: DiscordNotifier,
    tape: LocalTape,
    settings: Settings,
) -> None:
    """Poll CLOB trades off the quote clock (2–5s). Single watermark owner."""
    interval = settings.FILL_POLL_INTERVAL_SECONDS
    while True:
        await asyncio.sleep(interval)
        try:
            if settings.DRY_RUN:
                continue
            fills = await fill_collector.fetch()
            if fills:
                await apply_fills(
                    fills, inventory, fill_collector, toxicity, discord, tape,
                )
        except Exception as extra:
            logger.warning("Fill ingest failed: {}", extra)
            if tape is not None:
                tape.exception("fill_ingest", extra)


async def digest_tick_task(
    digest: OperatorDigest,
    tape: LocalTape,
    inventory: InventoryTracker,
    market_mgr: MarketManager,
    order_mgr: OrderManager,
    scoring_monitor: ScoringMonitor,
    ws_feed: Optional[WebSocketFeed],
    rewards: Optional[RewardsTracker] = None,
) -> None:
    """In-memory digest refresh at 1 Hz. Does not write JSONL."""
    while True:
        await asyncio.sleep(1.0)
        try:
            digest.set_tape_ok(tape.ok)
            digest.set_ws_connected(bool(ws_feed is not None and ws_feed.connected))
            digest.set_wallet_collateral(
                order_mgr.usdc_balance, order_mgr.committed_capital,
            )
            digest.rebuild_markets(
                market_mgr.markets,
                inventory,
                tape,
                scoring_rates=scoring_monitor.get_all_scoring_rates(),
                states=market_mgr.get_states_snapshot(),
            )
            if rewards is not None:
                est = rewards.estimate_our_share_usd(
                    market_mgr.markets,
                    session_started_at=digest.started_at,
                )
                digest.set_est_rewards(
                    est["est_rewards_usd"],
                    share=est["est_share"],
                    note=est["est_note"],
                )
        except Exception as extra:
            logger.debug("Digest tick failed: {}", extra)


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
    resolution_guard: ResolutionGuard,
    settings: Settings,
    cycle_index: int = 0,
    skip_cids: set | None = None,
    ws_feed: Optional[WebSocketFeed] = None,
    seeder: Optional[InventorySeeder] = None,
    paper: Optional[PaperBroker] = None,
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

    if seeder is not None:
        seeder.clear_reseed_blocks()

    cycle_start = asyncio.get_event_loop().time()

    # ── Step 1: Fills are ingested on the 2–5s task. Drain the
    # cids filled since last cycle so smart-diff force-replaces.
    global _recent_filled_cids, _recent_fill_events
    filled_cids: set = set(_recent_filled_cids)
    _recent_filled_cids.clear()
    new_fills = list(_recent_fill_events)
    _recent_fill_events = []

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
    if _hot_digest is not None:
        _hot_digest.set_wallet_collateral(
            order_mgr.usdc_balance, order_mgr.committed_capital,
        )

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

    # ── Step 3c: Resolution guard (P2d) ─────────────────────────
    # Pre-resolution risk gate: max-loss + 24h drift + extreme-mid net.
    # Trips BEFORE the exposure gate so a market headed to resolution
    # is exited even when it sits under its exposure cap. Independent of
    # the skew / spike / toxicity guards — those protect against bad
    # fills; this protects against terminal binary-payoff risk.
    # Pruned history is fed continuously by the hot WS path; here we
    # also append the latest cold-cycle mid before evaluating.
    active_cids = {m.condition_id for m in markets}
    paused_set: set[str] = set()
    skip_reasons: Dict[str, str] = {}
    now_utc = datetime.now(timezone.utc)
    for m in markets_this_cycle:
        cid = m.condition_id
        live_mid = midpoints.get(m.token_id_yes)
        if live_mid is None:
            continue
        resolution_guard.record_midpoint(cid, live_mid, now_utc)
        pos = inventory.get_position(cid)
        yes_sz = pos.yes_size if pos else 0.0
        no_sz = pos.no_size if pos else 0.0
        # Flatten-to-0 / unmatched within limit: drop legacy max_loss sticky
        # so we do not cancel+flatten 0/0 every cycle.
        resolution_guard.clear_sticky_if_flat(cid, yes_sz, no_sz)
        delist, reason = resolution_guard.should_delist(cid, yes_sz, no_sz, live_mid)
        if not delist:
            continue
        # Already flat + only a non-terminal sticky would have fired — skip loop.
        if (
            yes_sz <= 1e-9 and no_sz <= 1e-9
            and reason == "max_loss"
        ):
            resolution_guard.clear_max_loss_sticky(cid)
            continue
        _, max_loss, _ = resolution_guard.max_loss_check(cid, yes_sz, no_sz)
        _, move_pct, _ = resolution_guard.evaluate_move_24h(cid)
        mode = exit_mode_for_reason(reason)

        try:
            await order_mgr.cancel_orders_for_market(cid)
        except Exception as exc:
            logger.error(
                "Resolution guard: cancel_orders_for_market({}) failed: {}",
                cid[:10], exc,
            )

        if mode == "rebalance":
            rebalanced = False
            try:
                if seeder is not None:
                    await seeder.rebalance_unmatched(m, float(live_mid), inventory)
                elif paper is not None and settings.DRY_RUN:
                    no_mid = round(1.0 - float(live_mid), 4)
                    limit = float(settings.RESOLUTION_MAX_LOSS_USD)
                    onesided_before = (
                        min(yes_sz, no_sz) <= 1e-9
                        and abs(yes_sz - no_sz) > limit + 1e-9
                    )
                    result = paper.rebalance_unmatched(
                        cid, m.token_id_yes, m.token_id_no,
                        float(live_mid), no_mid, limit=limit,
                    )
                    sold = float(result.get("sold") or 0.0)
                    heavy = str(result.get("heavy") or "")
                    if sold > 1e-12 and heavy:
                        px = float(live_mid) if heavy == "YES" else no_mid
                        inventory.reduce_position(cid, heavy, sold, px)
                    if onesided_before and seeder is not None:
                        seeder.note_onesided_rebalance(cid)
                pos2 = inventory.get_position(cid)
                y2 = pos2.yes_size if pos2 else 0.0
                n2 = pos2.no_size if pos2 else 0.0
                still, unmatched_after, _ = resolution_guard.max_loss_check(
                    cid, y2, n2,
                )
                if still:
                    logger.warning(
                        "WARNING | resolution_guard | {} max_loss escape hatch "
                        "— unmatched still ${:.2f} after rebalance",
                        cid[:10], unmatched_after,
                    )
                else:
                    rebalanced = True
                    logger.warning(
                        "WARNING | resolution_guard | {} rebalanced surplus "
                        "(max_loss was ${:.2f} → unmatched ${:.2f})",
                        cid[:10], max_loss, unmatched_after,
                    )
                    asyncio.create_task(discord.send_alert(
                        title="Resolution Guard — Surplus Rebalanced",
                        message=(
                            f"Market `{cid[:12]}…` unmatched cap hit; sold surplus "
                            f"only. Still **SCORING**. "
                            f"max_loss was **${max_loss:.2f}** "
                            f"(limit ${settings.RESOLUTION_MAX_LOSS_USD:.2f}), "
                            f"now unmatched=**${unmatched_after:.2f}**, "
                            f"live_mid=**{live_mid:.3f}**."
                        ),
                        level="warning",
                    ))
            except Exception as exc:
                logger.error(
                    "Resolution guard: rebalance_unmatched({}) failed: {}",
                    cid[:10], exc,
                )
            if rebalanced:
                continue

        logger.warning(
            "WARNING | resolution_guard | {} delisted — {} "
            "(max_loss=${:.2f}, move={:.1f}%)",
            cid[:10], reason, max_loss, move_pct,
        )
        market_mgr.mark_resolution_inactive(cid)
        resolution_guard.mark_delisted(cid, reason or "sticky")
        paused_set.add(cid)
        skip_reasons[cid] = f"resolution_{reason}"
        if skip_cids is not None:
            skip_cids.add(cid)
        # Flatten inventory — do not sit INACTIVE with a book (Q=0).
        # extreme_mid / move_24h, or max_loss escape hatch after a failed
        # surplus rebalance. Do not re-seed this cid
        # (sticky INACTIVE + skip_cids).
        if seeder is not None:
            try:
                await seeder.flatten_market(m, float(live_mid), inventory)
            except Exception as exc:
                logger.error(
                    "Resolution guard: flatten_market({}) failed: {}",
                    cid[:10], exc,
                )
        elif paper is not None and settings.DRY_RUN:
            try:
                no_mid = round(1.0 - float(live_mid), 4)
                paper.flatten_market(
                    cid, m.token_id_yes, m.token_id_no,
                    float(live_mid), no_mid,
                )
                inventory.flatten_position(cid, float(live_mid), no_mid)
            except Exception as exc:
                logger.error(
                    "Resolution guard: paper flatten({}) failed: {}",
                    cid[:10], exc,
                )
        # After flatten (incl. 0/0), never leave a max_loss sticky.
        resolution_guard.clear_sticky_if_flat(cid, 0.0, 0.0)
        resolution_guard.clear_max_loss_sticky(cid)
        asyncio.create_task(discord.send_alert(
            title="Resolution Guard — Market Delisted",
            message=(
                f"Market `{cid[:12]}…` delisted by resolution guard.\n"
                f"Reason: **{reason}** — max_loss=**${max_loss:.2f}** "
                f"(limit ${settings.RESOLUTION_MAX_LOSS_USD:.2f}), "
                f"24h_move=**{move_pct:.1f}%** "
                f"(limit {settings.RESOLUTION_MAX_MOVE_24H_PCT:.1f}%), "
                f"live_mid=**{live_mid:.3f}**. Orders cancelled + inventory flattened."
            ),
            level="warning",
        ))

    # ── Step 4: Per-market exposure gate (Issue 5) ──────────────
    # Pause individual markets that breach MAX_MARKET_EXPOSURE_RATIO
    # BEFORE the global gross check so a runaway market is contained
    # without dragging the whole bot to the global limit.
    # Deployed = inventory mark + BID notional only (ASK excluded).
    for m in markets_this_cycle:
        bid_n = order_mgr.open_order_notional_for_market(m.condition_id)
        ask_n = order_mgr.open_ask_notional_for_market(m.condition_id)
        inv_m = inventory.inventory_mark_usd(m.condition_id)
        # Two-sided CLOB min_size BID floor (preferred overshoot — do not
        # INACTIVE solely for this).
        yes_mid = midpoints.get(m.token_id_yes) or m.midpoint or 0.5
        no_mid = max(0.01, 1.0 - float(yes_mid))
        minsize_floor = float(m.min_size) * (
            float(yes_mid) + float(no_mid)
        )
        breached, exposure_val, limit_val = inventory.is_market_exposure_breached(
            m.condition_id,
            allocated_capital=m.allocated_capital,
            open_order_notional_for_market=bid_n,
            minsize_bid_floor_usd=minsize_floor,
        )
        if breached:
            logger.warning(
                "WARNING | exposure | Market {} — per-market limit breached "
                "(inv=${:.2f}, bid=${:.2f}, ask=${:.2f}(excluded), "
                "limit=${:.2f}) — pausing quotes",
                m.condition_id[:12], inv_m, bid_n, ask_n, limit_val,
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
        _skip_cids.update(skip_cids)
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

    _exposure_paused.clear()
    _exposure_paused.update(paused_set)
    if _hot_tape is not None:
        for cid, reason in skip_reasons.items():
            _hot_tape.skip(cid, reason)
    if _hot_digest is not None:
        for cid, reason in skip_reasons.items():
            _hot_digest.record_skip(cid, reason)

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
                    if getattr(quotes, "bid_note", "") == "bid_capped_max_loss":
                        skip_reasons[market.condition_id] = "bid_capped_max_loss"
                        return (market.condition_id, 0, 0, 0, False)
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
                async with order_mgr.market_lock(market.condition_id):
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

        market_mgr.set_state(cid, state, reason=skip_reasons.get(cid, ""))

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
    utilization = 0.0
    if settings.TOTAL_CAPITAL > 0:
        utilization = order_mgr.open_order_notional / settings.TOTAL_CAPITAL
    if _hot_tape is not None:
        try:
            _hot_tape.cycle(
                n=cycle_index,
                selected=selected_count,
                active=active_count,
                posted=cycle_posted,
                kept=cycle_kept,
                errors=cycle_errors,
                elapsed_s=elapsed,
                utilization=utilization,
            )
            _hot_tape.snapshot_inventory(inventory, markets_this_cycle)
        except Exception as extra:
            logger.debug("Cycle tape snapshot failed: {}", extra)
    if _hot_digest is not None:
        try:
            _hot_digest.update_cycle(
                n=cycle_index,
                selected=selected_count,
                active=active_count,
                posted=cycle_posted,
                kept=cycle_kept,
                errors=cycle_errors,
                elapsed_s=elapsed,
                utilization=utilization,
                skip_reasons=skip_reasons,
            )
            _hot_digest.add_quoted_notional(cycle_quoted)
            _hot_digest.set_clob_ok(True)
            _hot_digest.rebuild_markets(
                market_mgr.markets,
                inventory,
                _hot_tape,
                scoring_rates=scoring_rates_now,
                states=market_mgr.get_states_snapshot(),
            )
        except Exception as extra:
            logger.debug("Cycle digest update failed: {}", extra)
    return cycle_posted, cycle_volume, cycle_quoted


# ──────────────────────────────────────────────────────────────────────
# Background tasks
# ──────────────────────────────────────────────────────────────────────


async def market_refresh_task(
    market_mgr: MarketManager,
    order_mgr: OrderManager,
    inventory: InventoryTracker,
    seeder: Optional[InventorySeeder],
    client: AsyncClobClient,
    settings: Settings,
    interval: int = 900,
    paper: Optional[PaperBroker] = None,
) -> None:
    """Refresh market universe every 15 minutes. Cancel OUT, seed or skip IN."""
    global _skip_cids
    while True:
        await asyncio.sleep(interval)
        try:
            async with _market_lock:
                await market_mgr.refresh()
                went_out = list(market_mgr.last_went_out)
                went_in = list(market_mgr.last_went_in)

            for m in went_out:
                try:
                    async with order_mgr.market_lock(m.condition_id):
                        await order_mgr.cancel_orders_for_market(m.condition_id)
                    _skip_cids.discard(m.condition_id)
                    logger.info("Refresh: cancelled OUT market {}", m.condition_id[:12])
                except Exception as extra:
                    logger.error("Refresh cancel OUT {} failed: {}", m.condition_id[:12], extra)

            if went_in:
                if settings.SEED_ENABLED and seeder is not None:
                    failed, seed_costs = await seeder.seed_all(went_in)
                    for m in went_in:
                        if m.condition_id in failed:
                            market_mgr.set_state(m.condition_id, MarketState.SEED_FAILED)
                            _skip_cids.add(m.condition_id)
                        else:
                            _skip_cids.discard(m.condition_id)
                            if settings.DRY_RUN and paper is not None:
                                _credit_paper_seed(inventory, m, paper, seed_costs)
                            else:
                                pos = inventory.ensure_position(
                                    m.condition_id, m.token_id_yes, m.token_id_no,
                                )
                                costs = seed_costs.get(m.condition_id, {})
                                yc = float(costs.get("yes") or 0.0)
                                nc = float(costs.get("no") or 0.0)
                                # Seeder returns THIS call's spend only. First
                                # write-once; top-up ADDs (size may lag chain).
                                had_yes = pos.yes_cost > 0
                                had_no = pos.no_cost > 0
                                inventory.apply_seed_costs_once(
                                    m.condition_id, yes_cost=yc, no_cost=nc,
                                )
                                if had_yes and yc > 0:
                                    inventory.add_seed_costs(
                                        m.condition_id, yes_cost=yc,
                                    )
                                if had_no and nc > 0:
                                    inventory.add_seed_costs(
                                        m.condition_id, no_cost=nc,
                                    )
                    token_ids = []
                    for m in went_in:
                        if m.condition_id in _skip_cids:
                            continue
                        if m.token_id_yes:
                            token_ids.append(m.token_id_yes)
                        if m.token_id_no:
                            token_ids.append(m.token_id_no)
                    if token_ids and not settings.DRY_RUN:
                        await client.sync_conditional_allowances(token_ids)
                else:
                    for m in went_in:
                        _skip_cids.add(m.condition_id)
                        market_mgr.set_state(m.condition_id, MarketState.SEED_FAILED)

            if _ws_feed and market_mgr.markets:
                token_ids = []
                tick_sizes: Dict[str, float] = {}
                for m in market_mgr.markets:
                    if m.condition_id in _skip_cids:
                        continue
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
    order_mgr: OrderManager,
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
                markets=list(market_mgr.markets),
                open_order_notional=order_mgr.open_order_notional,
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
            exchange_pos: Dict[str, Dict[str, float]] = {}
            for cid, pos in inventory.positions.items():
                rec: Dict[str, float] = {"yes": 0.0, "no": 0.0}
                if pos.yes_token_id:
                    rec["yes"] = await client.get_conditional_balance(pos.yes_token_id)
                if pos.no_token_id:
                    rec["no"] = await client.get_conditional_balance(pos.no_token_id)
                exchange_pos[cid] = rec
            if not exchange_pos:
                logger.debug("No balance data for reconciliation — skipping")
                continue

            async with _inventory_lock:
                corrected = inventory.reconcile(exchange_pos)
                if corrected > 0:
                    inventory.save_state()
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
    resolution_guard = ResolutionGuard(settings)
    order_mgr = OrderManager(client, settings)
    inventory = InventoryTracker(settings)
    fill_collector = FillCollector(client)
    scoring_monitor = ScoringMonitor(client, settings, discord, order_manager=order_mgr)
    rewards = RewardsTracker(client, settings, inventory)
    tape = LocalTape()
    digest = OperatorDigest()
    digest.configure_from_settings(settings, tape_path=str(tape.path))
    digest.set_clob_ok(True)
    order_mgr.set_tape(tape)
    order_mgr.set_digest(digest)
    market_mgr.set_tape(tape)
    market_mgr.set_digest(digest)
    quoter.set_tape(tape)
    inventory.set_tape(tape)

    # Operator terminal ASAP — before cancel_all / refresh / seed / inventory.
    # Markets and fills populate the UI as those steps emit tape events.
    tape_http_task: Optional[asyncio.Task] = None
    if settings.TAPE_HTTP_ENABLED:
        tape_http_task = asyncio.create_task(
            run_tape_server(
                tape, settings.TAPE_HTTP_PORT, digest=digest, discord=discord,
            ),
            name="tape_http",
        )
        await asyncio.sleep(0.05)  # yield so uvicorn can bind before long I/O
        logger.info(
            "Tape HTTP on http://127.0.0.1:{}/ (early — before seed)",
            settings.TAPE_HTTP_PORT,
        )

    paper: Optional[PaperBroker] = None
    if settings.DRY_RUN:
        paper = PaperBroker(settings.paper_cash_start)
        order_mgr.set_paper(paper)
        digest.set_wallet_collateral(paper.cash, paper.committed)
        logger.info(
            "Paper broker: cash ${:,.0f} (label paper, not CLOB wallet)",
            paper.cash,
        )

    # ── Cancel orphaned orders from previous sessions ───────────
    # Orders posted by earlier runs survive restarts.  If left live they
    # generate fills the inventory tracker cannot attribute correctly,
    # creating phantom one-sided exposure that triggers the global limit.
    cleared = await order_mgr.cancel_all_orders()
    if not cleared and not settings.DRY_RUN:
        logger.critical(
            "Startup cancel_all failed — refusing to quote over leftover GTCs"
        )
        await discord.send_error("Startup cancel_all failed — exiting (fail closed).")
        sys.exit(1)
    logger.info("Startup: cleared all pre-existing orders on exchange")

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
    seeder_ref = None
    if settings.SEED_ENABLED:
        seeder_ref = InventorySeeder(client, settings, discord, resolution_guard)
        seeder_ref.set_tape(tape)
        seeder_ref.set_digest(digest)
        if paper is not None:
            seeder_ref.set_paper(paper)
        unseeded_markets, seed_costs = await seeder_ref.seed_all(market_mgr.markets)
        _skip_cids.update(unseeded_markets)

        # Mark per-market state up front (Issue 4): SEED_FAILED is sticky
        # across cycles via market_mgr.set_state(); other states transition
        # freely as the bot quotes.
        for m in market_mgr.markets:
            if m.condition_id in unseeded_markets:
                market_mgr.set_state(
                    m.condition_id, MarketState.SEED_FAILED, reason="seed_failed",
                )
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
        if token_ids and not settings.DRY_RUN:
            await client.sync_conditional_allowances(token_ids)

    # ── Sync balances into inventory so quoter knows position sizes ──
    if settings.DRY_RUN and paper is not None:
        for m in market_mgr.markets:
            if m.condition_id in unseeded_markets:
                continue
            _credit_paper_seed(inventory, m, paper, seed_costs)
            logger.info(
                "Paper inventory: {} YES={:.2f} NO={:.2f}",
                m.condition_id[:12],
                paper.inventory_size(m.token_id_yes) if m.token_id_yes else 0.0,
                paper.inventory_size(m.token_id_no) if m.token_id_no else 0.0,
            )
        digest.set_wallet_collateral(paper.cash, paper.committed)
    else:
        for m in market_mgr.markets:
            if m.condition_id in unseeded_markets:
                continue
            pos = inventory.ensure_position(m.condition_id, m.token_id_yes, m.token_id_no)
            prev_yes = float(pos.yes_size)
            prev_no = float(pos.no_size)
            synced = False
            if m.token_id_yes:
                bal = await client.get_conditional_balance(m.token_id_yes)
                if bal > 0 and bal > pos.yes_size:
                    pos.yes_size = bal
                    synced = True
                    logger.info("Inventory sync: {} YES = {:.2f}", m.condition_id[:12], bal)
            if m.token_id_no:
                bal = await client.get_conditional_balance(m.token_id_no)
                if bal > 0 and bal > pos.no_size:
                    pos.no_size = bal
                    synced = True
                    logger.info("Inventory sync: {} NO = {:.2f}", m.condition_id[:12], bal)
            if synced:
                if m.midpoint and m.midpoint > 0:
                    mids = {}
                    if m.token_id_yes:
                        mids[m.token_id_yes] = float(m.midpoint)
                    if m.token_id_no:
                        mids[m.token_id_no] = max(0.01, 1.0 - float(m.midpoint))
                    if mids:
                        inventory.update_midpoints(mids)
                costs = seed_costs.get(m.condition_id, {})
                inventory.absorb_seed_acquisition(
                    m.condition_id,
                    prev_yes_size=prev_yes,
                    prev_no_size=prev_no,
                    yes_cost=float(costs.get("yes") or 0.0),
                    no_cost=float(costs.get("no") or 0.0),
                )

        inventory.apply_persisted_state()

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
    global _ws_feed, _hot_market_mgr, _hot_order_mgr, _hot_inventory, _hot_quoter, _hot_settings, _hot_discord, _hot_resolution_guard, _hot_toxicity, _hot_tape, _hot_digest, _hot_paper, _hot_fill_collector
    ws_feed: Optional[WebSocketFeed] = None
    _hot_tape = tape
    _hot_digest = digest
    _hot_market_mgr = market_mgr
    _hot_order_mgr = order_mgr
    _hot_inventory = inventory
    _hot_quoter = quoter
    _hot_settings = settings
    _hot_discord = discord
    _hot_resolution_guard = resolution_guard
    _hot_toxicity = toxicity
    _hot_paper = paper
    _hot_fill_collector = fill_collector

    if settings.WS_ENABLED:
        ws_kwargs: Dict[str, Any] = {"on_mid_change": hot_requote}
        if settings.DRY_RUN and paper is not None:
            ws_kwargs["on_last_trade"] = _paper_on_last_trade
            ws_kwargs["on_bbo"] = _paper_on_bbo
        ws_feed = WebSocketFeed(**ws_kwargs)
        _ws_feed = ws_feed

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

    def _signal_handler(sig, frame=None):
        logger.warning("Received signal {} — initiating shutdown", sig)
        shutdown_event.set()

    try:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, lambda: _signal_handler("SIGINT"))
        loop.add_signal_handler(signal.SIGTERM, lambda: _signal_handler("SIGTERM"))
    except (NotImplementedError, RuntimeError):
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)

    # ── Launch background tasks ──────────────────────────────────
    bg_tasks = [
        asyncio.create_task(market_refresh_task(
            market_mgr, order_mgr, inventory, seeder_ref, client, settings,
            paper=paper,
        )),
        asyncio.create_task(stale_order_cleanup_task(order_mgr)),
        asyncio.create_task(ghost_order_reconciliation_task(order_mgr)),
        asyncio.create_task(daily_rewards_task(
            rewards, market_mgr, discord, scoring_monitor, order_mgr,
        )),
        asyncio.create_task(dynamic_realloc_task(market_mgr, scoring_monitor, settings)),
        asyncio.create_task(periodic_dashboard_task(
            market_mgr, order_mgr, inventory, scoring_monitor, discord, settings,
        )),
        asyncio.create_task(fill_ingest_task(
            fill_collector, inventory, toxicity, discord, tape, settings,
        )),
        asyncio.create_task(digest_tick_task(
            digest, tape, inventory, market_mgr, order_mgr, scoring_monitor, ws_feed,
            rewards=rewards,
        )),
    ]
    if not settings.DRY_RUN:
        bg_tasks.append(asyncio.create_task(
            reconciliation_task(client, inventory, discord),
        ))
    if tape_http_task is not None:
        bg_tasks.append(tape_http_task)
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
                    resolution_guard=resolution_guard,
                    settings=settings,
                    cycle_index=cycle_index,
                    skip_cids=unseeded_markets,
                    ws_feed=ws_feed,
                    seeder=seeder_ref if settings.SEED_ENABLED else None,
                    paper=paper,
                )
                if result:
                    posted, volume, quoted = result
                    rewards.record_cycle_stats(volume_usd=quoted, orders_posted=posted, filled_usd=volume)
            except Exception as exc:
                logger.error("Unhandled cycle error: {}", exc)
                tape.exception("run_quoting_cycle", exc)
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
