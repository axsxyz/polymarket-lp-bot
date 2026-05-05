"""Tests for core/quoter.py — quote pricing, sizing, skew adjustments."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from config.markets_filter import MarketCandidate
from config.settings import Settings
from core.quoter import Quoter, estimate_q_score


def _make_settings(**overrides) -> Settings:
    defaults = {
        "PRIVATE_KEY": "0x" + "ab" * 32,
        "TOTAL_CAPITAL": 10000,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _market(
    mid: float = 0.50,
    max_spread: float = 0.10,
    min_size: float = 5.0,
    capital: float = 400.0,
    days: float = 60.0,
    neg_risk: bool = True,
) -> MarketCandidate:
    return MarketCandidate(
        condition_id="cond_test",
        token_id_yes="tok_yes",
        token_id_no="tok_no",
        max_spread=max_spread,
        min_size=min_size,
        allocated_capital=capital,
        midpoint=mid,
        tick_size="0.01",
        neg_risk=neg_risk,
        end_date=datetime.now(timezone.utc) + timedelta(days=days),
    )


_POS = 1000.0  # default large position so ASK sizing isn't limited by holdings


class TestQuotePricing:
    def test_bid_below_mid_ask_above(self):
        q = Quoter(_make_settings())
        result = q.compute(_market(mid=0.50), live_mid=0.50,
                           yes_position=_POS, no_position=_POS)

        assert result.yes_quote is not None
        assert result.yes_quote.bid_price < 0.50
        assert result.yes_quote.ask_price > 0.50

    def test_no_mid_is_complement(self):
        q = Quoter(_make_settings())
        result = q.compute(_market(mid=0.60), live_mid=0.60,
                           yes_position=_POS, no_position=_POS)

        assert result.no_quote is not None
        assert result.no_quote.mid == pytest.approx(0.40, abs=0.01)

    def test_prices_clamped(self):
        q = Quoter(_make_settings())
        result = q.compute(_market(mid=0.99), live_mid=0.99)

        if result.yes_quote:
            assert result.yes_quote.bid_price >= 0.01
            assert result.yes_quote.ask_price <= 0.99

    def test_missing_token_ids_skip(self):
        q = Quoter(_make_settings())
        m = _market()
        m.token_id_yes = ""
        result = q.compute(m, live_mid=0.50)
        assert result.yes_quote is None
        assert result.no_quote is None


class TestQuoteSizing:
    def test_sizes_respect_min(self):
        q = Quoter(_make_settings())
        result = q.compute(_market(min_size=5.0), live_mid=0.50,
                           yes_position=_POS, no_position=_POS)

        assert result.yes_quote.bid_size >= 5.0
        assert result.yes_quote.ask_size >= 5.0

    def test_sizes_bounded_by_capital(self):
        q = Quoter(_make_settings())
        result = q.compute(_market(capital=10.0, min_size=1.0), live_mid=0.50,
                           yes_position=_POS, no_position=_POS)

        bid_notional = result.yes_quote.bid_price * result.yes_quote.bid_size
        assert bid_notional <= 10.0  # half_capital = 5.0 per book, total 10


class TestSkewAdjustment:
    def test_heavy_side_widens_bid(self):
        q = Quoter(_make_settings(QUOTE_OFFSET_TICKS=3))
        m = _market(max_spread=0.20)

        neutral = q.compute(m, live_mid=0.50, skew_ratio=0.0,
                            yes_position=_POS, no_position=_POS)
        skewed = q.compute(m, live_mid=0.50, skew_ratio=0.30, heavy_side="YES",
                           yes_position=_POS, no_position=_POS)

        assert skewed.yes_quote.bid_price < neutral.yes_quote.bid_price

    def test_paused_side_returns_none(self):
        q = Quoter(_make_settings())
        result = q.compute(
            _market(), live_mid=0.50,
            skew_ratio=0.50, heavy_side="YES", paused_side="YES",
            yes_position=_POS, no_position=_POS,
        )
        assert result.yes_quote is None
        assert result.no_quote is not None


class TestComputeOffset:
    """Test the tick-based _compute_offset replacement for _effective_tightness."""

    def test_offset_is_tick_based(self):
        """With QUOTE_OFFSET_TICKS=1, offset should equal 1 tick."""
        q = Quoter(_make_settings(QUOTE_OFFSET_TICKS=1))
        m = _market(max_spread=0.10)
        offset = q._compute_offset(m, 0.50)
        assert offset == pytest.approx(0.01)

    def test_offset_scales_with_ticks(self):
        """With QUOTE_OFFSET_TICKS=3, offset should equal 3 ticks."""
        q = Quoter(_make_settings(QUOTE_OFFSET_TICKS=3))
        m = _market(max_spread=0.10)
        offset = q._compute_offset(m, 0.50)
        assert offset == pytest.approx(0.03)

    def test_offset_clamped_to_max_spread(self):
        """Offset must not exceed max_spread * 0.499."""
        q = Quoter(_make_settings(QUOTE_OFFSET_TICKS=10))
        m = _market(max_spread=0.05)
        offset = q._compute_offset(m, 0.50)
        assert offset <= 0.05 * 0.499 + 1e-9

    def test_offset_at_least_one_tick(self):
        """Offset must always be at least 1 tick."""
        q = Quoter(_make_settings(QUOTE_OFFSET_TICKS=0))
        m = _market(max_spread=0.10)
        offset = q._compute_offset(m, 0.50)
        assert offset >= 0.01

    def test_adverse_fill_rate_widens(self):
        """High fill rate should add extra ticks of widening."""
        q = Quoter(_make_settings(QUOTE_OFFSET_TICKS=1, ADVERSE_FILL_RATE_THRESHOLD=3))
        q.update_fill_rates({"cond_test": 5.0})
        m = _market(max_spread=0.10)
        offset = q._compute_offset(m, 0.50)
        assert offset == pytest.approx(0.02)

    def test_extreme_adverse_fills_double_widen(self):
        """Very high fill rate (>3x threshold) should add +2 ticks."""
        q = Quoter(_make_settings(QUOTE_OFFSET_TICKS=1, ADVERSE_FILL_RATE_THRESHOLD=3))
        q.update_fill_rates({"cond_test": 12.0})
        m = _market(max_spread=0.10)
        offset = q._compute_offset(m, 0.50)
        assert offset == pytest.approx(0.03)

    def test_fallback_to_tightness_factor(self):
        """With QUOTE_OFFSET_TICKS=0, should fall back to legacy tightness."""
        q = Quoter(_make_settings(QUOTE_OFFSET_TICKS=0, TIGHTNESS_FACTOR_SAFE=0.30))
        m = _market(max_spread=0.10)
        offset = q._compute_offset(m, 0.50)
        assert offset == pytest.approx(0.03)


class TestRecoveryMode:
    """Verify recovery mode produces quotes with adjusted sizing."""

    def test_recovery_mode_still_produces_quotes(self):
        """Recovery mode should NOT return None (unlike full pause)."""
        q = Quoter(_make_settings())
        result = q.compute(
            _market(), live_mid=0.50,
            skew_ratio=0.50, heavy_side="YES", recovery_side="YES",
            yes_position=_POS, no_position=_POS,
        )
        assert result.yes_quote is not None
        assert result.no_quote is not None

    def test_recovery_mode_reduces_ask_size(self):
        """Heavy side ask size should be reduced in recovery but not below min_size."""
        q = Quoter(_make_settings(
            SKEW_RECOVERY_ASK_SIZE_PCT=0.50,
            SIZE_MULTIPLIER=3.0,
        ))
        normal = q.compute(_market(), live_mid=0.50, skew_ratio=0.0,
                           yes_position=_POS, no_position=_POS)
        recovery = q.compute(
            _market(), live_mid=0.50,
            skew_ratio=0.50, heavy_side="YES", recovery_side="YES",
            yes_position=_POS, no_position=_POS,
        )
        assert recovery.yes_quote.ask_size < normal.yes_quote.ask_size
        assert recovery.yes_quote.ask_size >= _market().min_size

    def test_recovery_mode_widens_heavy_bid(self):
        """Heavy side bid should be wider (lower) in recovery."""
        q = Quoter(_make_settings())
        normal = q.compute(_market(), live_mid=0.50, skew_ratio=0.0,
                           yes_position=_POS, no_position=_POS)
        recovery = q.compute(
            _market(), live_mid=0.50,
            skew_ratio=0.50, heavy_side="YES", recovery_side="YES",
            yes_position=_POS, no_position=_POS,
        )
        assert recovery.yes_quote.bid_price < normal.yes_quote.bid_price

    def test_full_pause_still_returns_none(self):
        """Full pause (paused_side set) must still skip the side entirely."""
        q = Quoter(_make_settings())
        result = q.compute(
            _market(), live_mid=0.50,
            skew_ratio=0.80, heavy_side="YES", paused_side="YES",
            yes_position=_POS, no_position=_POS,
        )
        assert result.yes_quote is None
        assert result.no_quote is not None


class TestPositionBasedAsks:
    """CLOB requires tokens for SELL — ask_size always capped to position."""

    def test_zero_position_produces_zero_asks(self):
        q = Quoter(_make_settings())
        result = q.compute(_market(), live_mid=0.50,
                           yes_position=0.0, no_position=0.0)
        assert result.yes_quote is not None
        assert result.yes_quote.ask_size == 0.0
        assert result.no_quote is not None
        assert result.no_quote.ask_size == 0.0

    def test_small_position_caps_ask_size(self):
        q = Quoter(_make_settings())
        result = q.compute(_market(min_size=5.0), live_mid=0.50,
                           yes_position=3.0, no_position=3.0)
        assert result.yes_quote.ask_size == 0.0  # 3 < min_size 5 → skipped

    def test_with_position_always_posts_asks(self):
        q = Quoter(_make_settings())
        result = q.compute(_market(), live_mid=0.50,
                           yes_position=100.0, no_position=100.0)
        assert result.yes_quote is not None
        assert result.yes_quote.ask_size > 0


class TestQScore:
    def test_zero_spread_max_score(self):
        score = estimate_q_score(0.10, 0.0, 10.0)
        assert score == pytest.approx(10.0)

    def test_max_spread_zero_score(self):
        score = estimate_q_score(0.10, 0.10, 10.0)
        assert score == 0.0

    def test_intermediate_spread(self):
        score = estimate_q_score(0.10, 0.05, 10.0)
        assert 0 < score < 10.0


class TestMidpointGuard:
    """Continuous midpoint guard — pauses a market after N out-of-band cycles."""

    def test_in_band_does_not_increment(self):
        q = Quoter(_make_settings())
        is_paused, count = q.evaluate_midpoint_guard("cid_1", 0.50)
        assert is_paused is False
        assert count == 0

    def test_below_band_increments(self):
        q = Quoter(_make_settings(MIDPOINT_GUARD_CYCLES=3))
        for i in range(2):
            is_paused, count = q.evaluate_midpoint_guard("cid_1", 0.10)
            assert is_paused is False
            assert count == i + 1

    def test_above_band_increments(self):
        q = Quoter(_make_settings(MIDPOINT_GUARD_CYCLES=3))
        for i in range(2):
            is_paused, count = q.evaluate_midpoint_guard("cid_1", 0.90)
            assert is_paused is False
            assert count == i + 1

    def test_pauses_after_n_cycles(self):
        q = Quoter(_make_settings(MIDPOINT_GUARD_CYCLES=3))
        for _ in range(3):
            q.evaluate_midpoint_guard("cid_1", 0.10)
        assert q.is_midpoint_paused("cid_1") is True

    def test_in_band_resets_counter_and_unpauses(self):
        q = Quoter(_make_settings(MIDPOINT_GUARD_CYCLES=3))
        for _ in range(3):
            q.evaluate_midpoint_guard("cid_1", 0.10)
        assert q.is_midpoint_paused("cid_1") is True
        is_paused, count = q.evaluate_midpoint_guard("cid_1", 0.50)
        assert is_paused is False
        assert count == 0
        assert q.is_midpoint_paused("cid_1") is False

    def test_per_market_isolation(self):
        q = Quoter(_make_settings(MIDPOINT_GUARD_CYCLES=2))
        q.evaluate_midpoint_guard("cid_1", 0.10)
        q.evaluate_midpoint_guard("cid_1", 0.10)
        assert q.is_midpoint_paused("cid_1") is True
        assert q.is_midpoint_paused("cid_2") is False

    def test_custom_thresholds(self):
        q = Quoter(_make_settings(
            MIDPOINT_GUARD_MIN=0.25, MIDPOINT_GUARD_MAX=0.75,
            MIDPOINT_GUARD_CYCLES=2,
        ))
        is_paused, _ = q.evaluate_midpoint_guard("cid_1", 0.20)
        assert is_paused is False
        is_paused, _ = q.evaluate_midpoint_guard("cid_1", 0.20)
        assert is_paused is True

    def test_at_band_edge_in_band(self):
        """Exact MIN/MAX values should be IN-band (strict <, > comparison)."""
        q = Quoter(_make_settings(MIDPOINT_GUARD_MIN=0.18, MIDPOINT_GUARD_MAX=0.82))
        _, count_min = q.evaluate_midpoint_guard("cid_1", 0.18)
        assert count_min == 0
        _, count_max = q.evaluate_midpoint_guard("cid_1", 0.82)
        assert count_max == 0
