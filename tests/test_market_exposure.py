"""Tests for the per-market exposure gate (Issue 5).

Invariant: a single market that exceeds
``allocated_capital × MAX_MARKET_EXPOSURE_RATIO`` must trip its own pause
*before* the global gross exposure limit kicks in, so a runaway position
on one market cannot drag the whole bot through the global gate.
"""

from __future__ import annotations

import pytest

from config.settings import Settings
from core.inventory import InventoryTracker


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

    def test_open_orders_count_in_exposure(self):
        """Open ASK/BID notional must be added to filled position."""
        s = _make_settings(MAX_MARKET_EXPOSURE_RATIO=0.40)
        inv = InventoryTracker(s)
        # No fills, but $50 of open orders > $40 cap on $100 alloc
        breached, exposure, _ = inv.is_market_exposure_breached(
            "c1", allocated_capital=100.0,
            open_order_notional_for_market=50.0,
        )
        assert breached is True
        assert exposure == pytest.approx(50.0)


class TestGlobalExposureWithDeployed:
    """Global gross exposure scales with deployed capital when provided."""

    def test_deployed_capital_overrides_total(self):
        # TOTAL_CAPITAL=350, but deployed only $100 → limit = 100 * 0.80 = $80
        s = _make_settings(MAX_EXPOSURE_RATIO=0.80, TOTAL_CAPITAL=350)
        inv = InventoryTracker(s)
        inv.ensure_position("c1", yes_token_id="y", no_token_id="n")
        inv._positions["c1"].yes_size = 200
        inv._midpoints["y"] = 0.50  # gross = $100

        # With deployed capital $100, limit is $80 → breach
        assert inv.is_gross_exposure_breached(
            open_order_notional=0.0,
            active_cids={"c1"},
            deployed_capital=100.0,
        ) is True

        # Without deployed_capital, falls back to legacy
        # GLOBAL_MAX_GROSS_EXPOSURE_PCT × TOTAL_CAPITAL = 0.80 × 350 = $280 → no breach
        assert inv.is_gross_exposure_breached(
            open_order_notional=0.0,
            active_cids={"c1"},
        ) is False
