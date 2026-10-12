"""Tests for the per-market exposure gate (Issue 5).

Invariant: a single market that exceeds
``allocated_capital × MAX_MARKET_EXPOSURE_RATIO`` must trip its own pause
*before* the global gross exposure limit kicks in, so a runaway position
on one market cannot drag the whole bot through the global gate.

ASK notional is excluded (selling owned inventory). BID leftover sizing
must keep matched books under the limit except CLOB min_size floor.
"""

from __future__ import annotations

import pytest

from config.settings import Settings
from core.inventory import InventoryTracker
from core.order_manager import BUY, SELL, OrderManager, OrderRecord


def _make_settings(**overrides) -> Settings:
    defaults = {
        "PRIVATE_KEY": "0x" + "ab" * 32,
        "TOTAL_CAPITAL": 350,
        "MAX_EXPOSURE_RATIO": 0.80,
        "MAX_MARKET_EXPOSURE_RATIO": 0.40,
    }
    defaults.update(overrides)
    return Settings(**defaults)


class TestMarketExposureBreached:
    def test_zero_allocation_never_breaches(self):
        inv = InventoryTracker(_make_settings())
        breached, _, _ = inv.is_market_exposure_breached("c1", allocated_capital=0.0)
        assert breached is False

    def test_below_limit_no_breach(self):
        s = _make_settings(MAX_MARKET_EXPOSURE_RATIO=0.40)
        inv = InventoryTracker(s)
        inv.ensure_position("c1", yes_token_id="y", no_token_id="n")
        inv._positions["c1"].yes_size = 100
        inv._midpoints["y"] = 0.30  # gross at mid = 100 * 0.30 = $30

        breached, exposure, limit = inv.is_market_exposure_breached(
            "c1", allocated_capital=100.0,  # limit = 100 * 0.40 = $40
            open_order_notional_for_market=0.0,
        )
        assert breached is False
        assert exposure == pytest.approx(30.0)
        assert limit == pytest.approx(40.0)

    def test_above_limit_breached(self):
        s = _make_settings(MAX_MARKET_EXPOSURE_RATIO=0.40)
        inv = InventoryTracker(s)
        inv.ensure_position("c1", yes_token_id="y", no_token_id="n")
        inv._positions["c1"].yes_size = 200
        inv._midpoints["y"] = 0.50  # gross = 200 * 0.50 = $100

        breached, exposure, limit = inv.is_market_exposure_breached(
            "c1", allocated_capital=100.0,  # limit = $40
            open_order_notional_for_market=0.0,
        )
        assert breached is True
        assert exposure == pytest.approx(100.0)
        assert limit == pytest.approx(40.0)

    def test_open_bid_orders_count_in_exposure(self):
        """BID notional is added; ASK is excluded by the order_manager callers."""
        s = _make_settings(MAX_MARKET_EXPOSURE_RATIO=0.40)
        inv = InventoryTracker(s)
        breached, exposure, _ = inv.is_market_exposure_breached(
            "c1", allocated_capital=100.0,
            open_order_notional_for_market=50.0,
        )
        assert breached is True
        assert exposure == pytest.approx(50.0)

    def test_matched_315_leftover_bid_no_breach_at_090(self):
        """Vance-class: inv $315 + leftover BID under 0.90*500; ASK ignored."""
        s = _make_settings(MAX_MARKET_EXPOSURE_RATIO=0.90)
        inv = InventoryTracker(s)
        inv.ensure_position("vance", yes_token_id="y", no_token_id="n")
        inv._positions["vance"].yes_size = 315
        inv._positions["vance"].no_size = 315
        inv._midpoints["y"] = 0.50  # mark = 315

        bid_notional = 135.0
        ask_notional = 315.0  # excluded from gate input
        breached, exposure, limit = inv.is_market_exposure_breached(
            "vance",
            allocated_capital=500.0,
            open_order_notional_for_market=bid_notional,
        )
        assert breached is False
        assert exposure == pytest.approx(315.0 + bid_notional)
        assert limit == pytest.approx(450.0)
        assert 315.0 + bid_notional + ask_notional > limit

    def test_independent_alloc_half_bid_would_breach(self):
        """alloc/2 BID on both tokens atop $315 seed would trip 0.90*500."""
        s = _make_settings(MAX_MARKET_EXPOSURE_RATIO=0.90)
        inv = InventoryTracker(s)
        inv.ensure_position("vance", yes_token_id="y", no_token_id="n")
        inv._positions["vance"].yes_size = 315
        inv._positions["vance"].no_size = 315
        inv._midpoints["y"] = 0.50
        breached, exposure, limit = inv.is_market_exposure_breached(
            "vance",
            allocated_capital=500.0,
            open_order_notional_for_market=500.0,
        )
        assert breached is True
        assert exposure == pytest.approx(815.0)
        assert limit == pytest.approx(450.0)

    def test_minsize_floor_overshoot_not_breached(self):
        """Two-sided min_size BID floor may exceed ratio — do not INACTIVE."""
        s = _make_settings(MAX_MARKET_EXPOSURE_RATIO=0.90)
        inv = InventoryTracker(s)
        inv.ensure_position("vance", yes_token_id="y", no_token_id="n")
        inv._positions["vance"].yes_size = 315
        inv._positions["vance"].no_size = 315
        inv._midpoints["y"] = 0.50
        bid_floor = 200.0
        breached, exposure, limit = inv.is_market_exposure_breached(
            "vance",
            allocated_capital=500.0,
            open_order_notional_for_market=bid_floor,
            minsize_bid_floor_usd=bid_floor,
        )
        assert exposure > limit
        assert breached is False


class TestBidOnlyOpenNotional:
    def test_ask_excluded_from_open_notional(self):
        om = OrderManager.__new__(OrderManager)
        om._live_orders = {
            "1": OrderRecord(
                order_id="1", token_id="y", condition_id="c1",
                side=BUY, price=0.40, size=100.0,
            ),
            "2": OrderRecord(
                order_id="2", token_id="y", condition_id="c1",
                side=SELL, price=0.60, size=100.0,
            ),
        }
        assert om.open_order_notional_for_market("c1") == pytest.approx(40.0)
        assert om.open_ask_notional_for_market("c1") == pytest.approx(60.0)
        assert om.open_order_notional == pytest.approx(40.0)
        assert om.open_ask_notional == pytest.approx(60.0)


class TestGlobalExposureWithDeployed:
    """Global gross exposure scales with deployed capital when provided."""

    def test_deployed_capital_overrides_total(self):
        s = _make_settings(MAX_EXPOSURE_RATIO=0.80, TOTAL_CAPITAL=350)
        inv = InventoryTracker(s)
        inv.ensure_position("c1", yes_token_id="y", no_token_id="n")
        inv._positions["c1"].yes_size = 200
        inv._midpoints["y"] = 0.50  # gross = $100

        assert inv.is_gross_exposure_breached(
            open_order_notional=0.0,
            active_cids={"c1"},
            deployed_capital=100.0,
        ) is True

        assert inv.is_gross_exposure_breached(
            open_order_notional=0.0,
            active_cids={"c1"},
        ) is False
