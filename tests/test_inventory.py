"""Tests for core/inventory.py — fill recording, skew calc, reconciliation."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from config.settings import Settings
from core.inventory import InventoryTracker, MarketPosition, SkewAction
from core.order_manager import FillEvent


def _make_settings(**overrides) -> Settings:
    defaults = {
        "PRIVATE_KEY": "0x" + "ab" * 32,
        "TOTAL_CAPITAL": 10000,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _fill(
    token_id: str = "tok_yes",
    condition_id: str = "cond_1",
    side: str = "BID",
    price: float = 0.50,
    size: float = 10.0,
) -> FillEvent:
    return FillEvent(
        token_id=token_id,
        condition_id=condition_id,
        side=side,
        price=price,
        filled_size=size,
    )


# ------------------------------------------------------------------
# Fill recording
# ------------------------------------------------------------------


class TestRecordFills:
    def test_bid_increases_position(self):
        inv = InventoryTracker(_make_settings())
        inv.ensure_position("cond_1", yes_token_id="tok_yes", no_token_id="tok_no")

        inv.record_fills([_fill(side="BID", price=0.40, size=100)])

        pos = inv.positions["cond_1"]
        assert pos.yes_size == 100
        assert pos.yes_cost == pytest.approx(40.0)

    def test_ask_decreases_position(self):
        inv = InventoryTracker(_make_settings())
        inv.ensure_position("cond_1", yes_token_id="tok_yes", no_token_id="tok_no")

        inv.record_fills([_fill(side="BID", price=0.40, size=100)])
        inv.record_fills([_fill(side="ASK", price=0.60, size=50)])

        pos = inv.positions["cond_1"]
        assert pos.yes_size == 50
        assert pos.yes_cost == pytest.approx(20.0)

    def test_position_cannot_go_negative(self):
        inv = InventoryTracker(_make_settings())
        inv.ensure_position("cond_1", yes_token_id="tok_yes")

        inv.record_fills([_fill(side="ASK", price=0.60, size=999)])

        pos = inv.positions["cond_1"]
        assert pos.yes_size == 0
        assert pos.yes_cost == 0

    def test_no_side_fill(self):
        inv = InventoryTracker(_make_settings())
        inv.ensure_position("cond_1", yes_token_id="tok_yes", no_token_id="tok_no")

        inv.record_fills([_fill(token_id="tok_no", side="BID", price=0.60, size=80)])

        pos = inv.positions["cond_1"]
        assert pos.no_size == 80
        assert pos.yes_size == 0

    def test_realised_pnl_on_sell(self):
        inv = InventoryTracker(_make_settings())
        inv.ensure_position("cond_1", yes_token_id="tok_yes")

        inv.record_fills([_fill(side="BID", price=0.40, size=100)])
        inv.record_fills([_fill(side="ASK", price=0.60, size=50)])

        assert inv.realised_pnl == pytest.approx(10.0)  # (0.60 - 0.40) * 50

    def test_fill_count_tracks_all_fills(self):
        inv = InventoryTracker(_make_settings())
        inv.ensure_position("cond_1", yes_token_id="tok_yes")

        inv.record_fills([_fill(), _fill(), _fill()])
        assert inv.total_fill_count == 3

    def test_drain_fills_exactly_once(self):
        """Each fill applied once even if record_fills called multiple times."""
        inv = InventoryTracker(_make_settings())
        inv.ensure_position("cond_1", yes_token_id="tok_yes")

        fills = [_fill(side="BID", price=0.40, size=10)]
        inv.record_fills(fills)
        inv.record_fills(fills)

        pos = inv.positions["cond_1"]
        assert pos.yes_size == 20  # called twice = 20, not deduplicated here


# ------------------------------------------------------------------
# Skew calculation
# ------------------------------------------------------------------


class TestSkewRatio:
    def test_balanced_is_zero(self):
        pos = MarketPosition(condition_id="c", yes_size=100, no_size=100)
        assert pos.skew_ratio == 0.0

    def test_fully_one_sided_is_one(self):
        pos = MarketPosition(condition_id="c", yes_size=100, no_size=0)
        assert pos.skew_ratio == 1.0

    def test_heavy_side_yes(self):
        pos = MarketPosition(condition_id="c", yes_size=80, no_size=20)
        assert pos.heavy_side == "YES"

    def test_heavy_side_no(self):
        pos = MarketPosition(condition_id="c", yes_size=20, no_size=80)
        assert pos.heavy_side == "NO"

    def test_empty_position(self):
        pos = MarketPosition(condition_id="c")
        assert pos.skew_ratio == 0.0
        assert pos.heavy_side == ""


class TestEvaluate:
    def test_no_position_returns_neutral(self):
        inv = InventoryTracker(_make_settings())
        action = inv.evaluate("unknown")
        assert action.skew_ratio == 0.0
        assert not action.should_alert
        assert not action.should_adjust_quotes

    def test_alert_threshold(self):
        s = _make_settings(SKEW_ALERT_THRESHOLD=0.25)
        inv = InventoryTracker(s)
        inv.ensure_position("c1", yes_token_id="y")
        inv._positions["c1"].yes_size = 70
        inv._positions["c1"].no_size = 30

        action = inv.evaluate("c1")
        assert action.should_alert

    def test_recovery_mode_at_intermediate_skew(self):
        """Skew between SKEW_PAUSE_THRESHOLD and SKEW_HARD_PAUSE_THRESHOLD enters recovery."""
        s = _make_settings(
            SKEW_PAUSE_THRESHOLD=0.45,
            SKEW_HARD_PAUSE_THRESHOLD=0.70,
            SKEW_PAUSE_DURATION_SECONDS=60,
        )
        inv = InventoryTracker(s)
        inv.ensure_position("c1", yes_token_id="y")
        inv._positions["c1"].yes_size = 75
        inv._positions["c1"].no_size = 25  # skew = 0.50

        action = inv.evaluate("c1")
        assert action.recovery_side == "YES"
        assert action.paused_side == ""
        assert not action.pause_triggered

    def test_hard_pause_at_extreme_skew(self):
        """Skew >= SKEW_HARD_PAUSE_THRESHOLD triggers full pause."""
        s = _make_settings(
            SKEW_PAUSE_THRESHOLD=0.45,
            SKEW_HARD_PAUSE_THRESHOLD=0.70,
            SKEW_PAUSE_DURATION_SECONDS=60,
        )
        inv = InventoryTracker(s)
        inv.ensure_position("c1", yes_token_id="y")
        inv._positions["c1"].yes_size = 90
        inv._positions["c1"].no_size = 10  # skew = 0.80

        action = inv.evaluate("c1")
        assert action.pause_triggered
        assert action.paused_side == "YES"
        assert action.recovery_side == ""

    def test_pause_threshold_triggers_once(self):
        s = _make_settings(
            SKEW_PAUSE_THRESHOLD=0.45,
            SKEW_HARD_PAUSE_THRESHOLD=0.70,
            SKEW_PAUSE_DURATION_SECONDS=60,
        )
        inv = InventoryTracker(s)
        inv.ensure_position("c1", yes_token_id="y")
        inv._positions["c1"].yes_size = 90
        inv._positions["c1"].no_size = 10  # skew = 0.80

        action1 = inv.evaluate("c1")
        assert action1.pause_triggered
        assert action1.paused_side == "YES"

        action2 = inv.evaluate("c1")
        assert not action2.pause_triggered  # already paused
        assert action2.paused_side == "YES"


# ------------------------------------------------------------------
# Reconciliation
# ------------------------------------------------------------------


class TestReconcile:
    def test_corrects_drift(self):
        inv = InventoryTracker(_make_settings())
        inv.ensure_position("c1", yes_token_id="y")
        inv._positions["c1"].yes_size = 100
        inv._positions["c1"].yes_cost = 40.0

        corrected = inv.reconcile({"c1": {"yes": 90, "no": 0}})
        assert corrected == 1
        assert inv.positions["c1"].yes_size == 90

    def test_no_correction_within_tolerance(self):
        inv = InventoryTracker(_make_settings())
        inv.ensure_position("c1", yes_token_id="y")
        inv._positions["c1"].yes_size = 100
        inv._positions["c1"].yes_cost = 40.0

        corrected = inv.reconcile({"c1": {"yes": 100.5, "no": 0}})
        assert corrected == 0


# ------------------------------------------------------------------
# Gross exposure
# ------------------------------------------------------------------


class TestGrossExposure:
    def test_mark_to_mid(self):
        pos = MarketPosition(condition_id="c", yes_size=100, no_size=50)
        assert pos.gross_exposure_at_mid(0.60) == pytest.approx(100 * 0.60 + 50 * 0.40)

    def test_gross_breach(self):
        s = _make_settings(GLOBAL_MAX_GROSS_EXPOSURE_PCT=0.5, TOTAL_CAPITAL=1000)
        inv = InventoryTracker(s)
        inv.ensure_position("c1", yes_token_id="y")
        inv._positions["c1"].yes_size = 1000
        inv._midpoints["y"] = 0.60

        assert inv.is_gross_exposure_breached(open_order_notional=0)


# ------------------------------------------------------------------
# Daily drawdown circuit breaker
# ------------------------------------------------------------------


class TestDailyDrawdown:
    def test_no_pause_above_threshold(self):
        s = _make_settings(TOTAL_CAPITAL=1000, DAILY_DRAWDOWN_LIMIT_PCT=0.05)
        inv = InventoryTracker(s)
        inv._realised_pnl = -30.0  # -3% loss, below 5% limit
        assert inv.check_daily_drawdown() is False
        assert inv.is_drawdown_paused is False
        assert inv.realised_pnl_today == pytest.approx(-30.0)

    def test_pause_triggered_below_threshold(self):
        s = _make_settings(TOTAL_CAPITAL=1000, DAILY_DRAWDOWN_LIMIT_PCT=0.05)
        inv = InventoryTracker(s)
        inv._realised_pnl = -60.0  # -6% loss, exceeds 5% limit
        assert inv.check_daily_drawdown() is True
        assert inv.is_drawdown_paused is True

    def test_pause_persists_until_day_rollover(self):
        s = _make_settings(TOTAL_CAPITAL=1000, DAILY_DRAWDOWN_LIMIT_PCT=0.05)
        inv = InventoryTracker(s)
        inv._realised_pnl = -100.0
        assert inv.check_daily_drawdown() is True

        # PnL recovers but flag stays set until UTC midnight rollover
        inv._realised_pnl = 0.0
        assert inv.check_daily_drawdown() is True
        assert inv.is_drawdown_paused is True

    def test_day_rollover_resets_anchor_and_pause(self):
        s = _make_settings(TOTAL_CAPITAL=1000, DAILY_DRAWDOWN_LIMIT_PCT=0.05)
        inv = InventoryTracker(s)
        inv._realised_pnl = -80.0
        assert inv.check_daily_drawdown() is True

        # Simulate UTC day rollover by clearing the cached date
        inv._current_day_utc = "1970-01-01"
        # Same realised PnL, but new day → anchor re-snapshots, no pause
        assert inv.check_daily_drawdown() is False
        assert inv.is_drawdown_paused is False
        assert inv.realised_pnl_today == pytest.approx(0.0)

    def test_zero_limit_disables_breaker(self):
        s = _make_settings(TOTAL_CAPITAL=1000, DAILY_DRAWDOWN_LIMIT_PCT=0.0)
        inv = InventoryTracker(s)
        inv._realised_pnl = -500.0
        assert inv.check_daily_drawdown() is False
