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
        end_date=datetime.now(timezone.utc) + timedelta(days=days),
    )


class TestQuotePricing:
    def test_bid_below_mid_ask_above(self):
        q = Quoter(_make_settings())
        result = q.compute(_market(mid=0.50), live_mid=0.50)

        assert result.yes_quote is not None
        assert result.yes_quote.bid_price < 0.50
        assert result.yes_quote.ask_price > 0.50

    def test_no_mid_is_complement(self):
        q = Quoter(_make_settings())
        result = q.compute(_market(mid=0.60), live_mid=0.60)

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
        result = q.compute(_market(min_size=5.0), live_mid=0.50)

        assert result.yes_quote.bid_size >= 5.0
        assert result.yes_quote.ask_size >= 5.0

    def test_sizes_bounded_by_capital(self):
        q = Quoter(_make_settings())
        result = q.compute(_market(capital=10.0, min_size=1.0), live_mid=0.50)

        bid_notional = result.yes_quote.bid_price * result.yes_quote.bid_size
        assert bid_notional <= 10.0  # half_capital = 5.0 per book, total 10


class TestSkewAdjustment:
    def test_heavy_side_widens_bid(self):
        q = Quoter(_make_settings())
        m = _market()

        neutral = q.compute(m, live_mid=0.50, skew_ratio=0.0)
        skewed = q.compute(m, live_mid=0.50, skew_ratio=0.30, heavy_side="YES")

        assert skewed.yes_quote.bid_price < neutral.yes_quote.bid_price

    def test_paused_side_returns_none(self):
        q = Quoter(_make_settings())
        result = q.compute(
            _market(), live_mid=0.50,
            skew_ratio=0.50, heavy_side="YES", paused_side="YES",
        )
        assert result.yes_quote is None
        assert result.no_quote is not None


class TestDynamicTightness:
    def test_stable_market_tighter(self):
        q = Quoter(_make_settings())
        stable = _market(mid=0.95, days=90)
        volatile = _market(mid=0.50, days=5)

        tf_stable = q._effective_tightness(stable, 0.95)
        tf_volatile = q._effective_tightness(volatile, 0.50)

        assert tf_stable < tf_volatile

    def test_tightness_clamped(self):
        q = Quoter(_make_settings())
        m = _market(mid=0.50, days=1)
        tf = q._effective_tightness(m, 0.50)
        assert 0.25 <= tf <= 0.70


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
