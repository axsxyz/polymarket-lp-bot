"""Tests for config/markets_filter.py — hard filters, scoring, ranking, parsing."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from config.markets_filter import (
    MarketCandidate,
    _normalise_spread,
    parse_clob_simplified_market,
    parse_gamma_market,
    passes_hard_filters,
)
from config.settings import Settings


def _make_settings(**overrides) -> Settings:
    defaults = {
        "PRIVATE_KEY": "0x" + "ab" * 32,
        "TOTAL_CAPITAL": 10000,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _candidate(**overrides) -> MarketCandidate:
    defaults = {
        "condition_id": "cond_test",
        "token_id_yes": "tok_yes",
        "token_id_no": "tok_no",
        "max_spread": 0.10,
        "min_size": 5.0,
        "reward_rate_daily": 10.0,
        "midpoint": 0.50,
        "end_date": datetime.now(timezone.utc) + timedelta(days=150),
        "volume_24h": 5000.0,
        "tick_size": "0.01",
    }
    defaults.update(overrides)
    return MarketCandidate(**defaults)


class TestHardFilters:
    def test_good_candidate_passes(self):
        s = _make_settings()
        assert passes_hard_filters(_candidate(), s)

    def test_extreme_midpoint_low_fails(self):
        s = _make_settings()
        assert not passes_hard_filters(_candidate(midpoint=0.05), s)

    def test_extreme_midpoint_high_fails(self):
        s = _make_settings()
        assert not passes_hard_filters(_candidate(midpoint=0.96), s)

    def test_zero_max_spread_fails(self):
        s = _make_settings()
        assert not passes_hard_filters(_candidate(max_spread=0.0), s)

    def test_zero_min_size_fails(self):
        s = _make_settings()
        assert not passes_hard_filters(_candidate(min_size=0.0), s)

    def test_short_expiry_fails(self):
        s = _make_settings()
        c = _candidate(end_date=datetime.now(timezone.utc) + timedelta(days=5))
        assert not passes_hard_filters(c, s)

    def test_excessive_volume_fails(self):
        s = _make_settings()
        assert not passes_hard_filters(_candidate(volume_24h=1_000_000), s)

    def test_no_end_date_fails(self):
        s = _make_settings()
        assert not passes_hard_filters(_candidate(end_date=None), s)

    def test_too_illiquid_fails(self):
        s = _make_settings()
        assert not passes_hard_filters(_candidate(volume_24h=100), s)

    def test_low_reward_rate_fails(self):
        s = _make_settings()
        assert not passes_hard_filters(_candidate(reward_rate_daily=1.0), s)


class TestDaysRemaining:
    def test_future_end_date(self):
        c = _candidate(end_date=datetime.now(timezone.utc) + timedelta(days=30))
        assert 29 < c.days_remaining < 31

    def test_past_end_date(self):
        c = _candidate(end_date=datetime.now(timezone.utc) - timedelta(days=5))
        assert c.days_remaining == 0.0

    def test_no_end_date(self):
        c = _candidate(end_date=None)
        assert c.days_remaining == 0.0
        assert not c.has_end_date


class TestNormaliseSpread:
    """Verify cent-to-dollar conversion for max_spread values."""

    def test_cents_value_converted(self):
        assert _normalise_spread(3.5) == pytest.approx(0.035)

    def test_large_cents_value(self):
        assert _normalise_spread(10.0) == pytest.approx(0.10)

    def test_already_decimal_untouched(self):
        assert _normalise_spread(0.05) == pytest.approx(0.05)

    def test_boundary_one_untouched(self):
        assert _normalise_spread(1.0) == pytest.approx(1.0)

    def test_zero_untouched(self):
        assert _normalise_spread(0.0) == pytest.approx(0.0)


class TestParseGammaMarketSpread:
    """Ensure parse_gamma_market normalises max_spread from cents."""

    def test_camel_case_rewards_normalised(self):
        raw = {
            "conditionId": "cond_abc",
            "rewardsMaxSpread": 4.5,
            "rewardsMinSize": 200,
            "clobTokenIds": json.dumps(["tok_yes_id", "tok_no_id"]),
            "outcomes": json.dumps(["Yes", "No"]),
        }
        mc = parse_gamma_market(raw)
        assert mc.max_spread == pytest.approx(0.045)

    def test_legacy_rewards_normalised(self):
        raw = {
            "condition_id": "cond_legacy",
            "rewards": {"max_spread": 5.0, "min_size": 10, "rates": []},
            "tokens": [
                {"outcome": "Yes", "token_id": "ty", "price": 0.40},
                {"outcome": "No", "token_id": "tn", "price": 0.60},
            ],
        }
        mc = parse_gamma_market(raw)
        assert mc.max_spread == pytest.approx(0.05)

    def test_decimal_spread_untouched(self):
        raw = {
            "conditionId": "cond_dec",
            "rewardsMaxSpread": 0.08,
            "rewardsMinSize": 10,
        }
        mc = parse_gamma_market(raw)
        assert mc.max_spread == pytest.approx(0.08)


class TestParseClobSimplifiedSpread:
    """Ensure parse_clob_simplified_market normalises max_spread."""

    def test_cents_normalised(self):
        raw = {
            "condition_id": "cond_clob",
            "tokens": [
                {"outcome": "Yes", "token_id": "ty", "price": 0.50},
                {"outcome": "No", "token_id": "tn", "price": 0.50},
            ],
            "rewards": {"max_spread": 3.5, "min_size": 200, "rates": []},
        }
        mc = parse_clob_simplified_market(raw)
        assert mc.max_spread == pytest.approx(0.035)
