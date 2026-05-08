"""Tests for backtest/fetcher.py — FetchFilter, _market_passes_filter,
_load_condition_ids_from_json."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backtest.fetcher import (
    FetchFilter,
    _FilterSettings,
    _load_condition_ids_from_json,
    _market_passes_filter,
)
from config.markets_filter import MarketCandidate


# ────────────────────────────────────────────────────────────────
# Fixture helpers
# ────────────────────────────────────────────────────────────────

def _market(**overrides) -> MarketCandidate:
    """Build a MarketCandidate that passes all FetchFilter defaults."""
    defaults = dict(
        condition_id="0xtest",
        token_id_yes="tok_yes",
        token_id_no="tok_no",
        max_spread=0.05,
        min_size=5.0,
        reward_rate_daily=50.0,
        midpoint=0.50,
        end_date=datetime.now(timezone.utc) + timedelta(days=200),
        volume_24h=10_000.0,
        tick_size="0.01",
        tags=[],
    )
    defaults.update(overrides)
    return MarketCandidate(**defaults)


# ────────────────────────────────────────────────────────────────
# FetchFilter — defaults
# ────────────────────────────────────────────────────────────────

class TestFetchFilterDefaults:
    def test_min_reward_rate(self):
        assert FetchFilter().min_reward_rate == 5.0

    def test_midpoint_bounds(self):
        ff = FetchFilter()
        assert ff.midpoint_min == 0.15
        assert ff.midpoint_max == 0.85

    def test_volume_bounds(self):
        ff = FetchFilter()
        assert ff.min_volume == 500.0
        assert ff.max_volume == 800_000.0

    def test_min_end_date_days(self):
        assert FetchFilter().min_end_date_days == 120

    def test_exclude_tags_empty(self):
        assert FetchFilter().exclude_tags == []

    def test_independent_instances(self):
        """exclude_tags must not be shared between instances (dataclass field default)."""
        a = FetchFilter()
        b = FetchFilter()
        a.exclude_tags.append("x")
        assert b.exclude_tags == []


# ────────────────────────────────────────────────────────────────
# _market_passes_filter — None filter
# ────────────────────────────────────────────────────────────────

class TestNoFilter:
    def test_none_filter_always_passes(self):
        bad = _market(reward_rate_daily=0.0, midpoint=0.01, volume_24h=0.0,
                      end_date=None)
        assert _market_passes_filter(bad, None) is True

    def test_valid_market_passes_none(self):
        assert _market_passes_filter(_market(), None) is True


# ────────────────────────────────────────────────────────────────
# _market_passes_filter — reward rate
# ────────────────────────────────────────────────────────────────

class TestRewardRateFilter:
    def test_valid_passes(self):
        assert _market_passes_filter(_market(reward_rate_daily=50.0), FetchFilter()) is True

    def test_at_boundary_passes(self):
        assert _market_passes_filter(_market(reward_rate_daily=5.0), FetchFilter()) is True

    def test_below_minimum_rejected(self):
        assert _market_passes_filter(_market(reward_rate_daily=1.0), FetchFilter()) is False

    def test_zero_rejected(self):
        assert _market_passes_filter(_market(reward_rate_daily=0.0), FetchFilter()) is False

    def test_custom_threshold(self):
        ff = FetchFilter(min_reward_rate=100.0)
        assert _market_passes_filter(_market(reward_rate_daily=99.9), ff) is False
        assert _market_passes_filter(_market(reward_rate_daily=100.0), ff) is True


# ────────────────────────────────────────────────────────────────
# _market_passes_filter — midpoint
# ────────────────────────────────────────────────────────────────

class TestMidpointFilter:
    def test_mid_range_passes(self):
        assert _market_passes_filter(_market(midpoint=0.50), FetchFilter()) is True

    def test_at_lower_boundary_passes(self):
        assert _market_passes_filter(_market(midpoint=0.15), FetchFilter()) is True

    def test_at_upper_boundary_passes(self):
        assert _market_passes_filter(_market(midpoint=0.85), FetchFilter()) is True

    def test_below_lower_rejected(self):
        assert _market_passes_filter(_market(midpoint=0.10), FetchFilter()) is False

    def test_above_upper_rejected(self):
        assert _market_passes_filter(_market(midpoint=0.90), FetchFilter()) is False

    def test_custom_narrow_band(self):
        ff = FetchFilter(midpoint_min=0.40, midpoint_max=0.60)
        assert _market_passes_filter(_market(midpoint=0.30), ff) is False
        assert _market_passes_filter(_market(midpoint=0.50), ff) is True
        assert _market_passes_filter(_market(midpoint=0.70), ff) is False


# ────────────────────────────────────────────────────────────────
# _market_passes_filter — volume
# ────────────────────────────────────────────────────────────────

class TestVolumeFilter:
    def test_in_range_passes(self):
        assert _market_passes_filter(_market(volume_24h=10_000.0), FetchFilter()) is True

    def test_at_min_boundary_passes(self):
        assert _market_passes_filter(_market(volume_24h=500.0), FetchFilter()) is True

    def test_at_max_boundary_passes(self):
        assert _market_passes_filter(_market(volume_24h=800_000.0), FetchFilter()) is True

    def test_below_min_rejected(self):
        assert _market_passes_filter(_market(volume_24h=100.0), FetchFilter()) is False

    def test_above_max_rejected(self):
        assert _market_passes_filter(_market(volume_24h=900_000.0), FetchFilter()) is False

    def test_custom_range(self):
        ff = FetchFilter(min_volume=1_000.0, max_volume=50_000.0)
        assert _market_passes_filter(_market(volume_24h=500.0), ff) is False
        assert _market_passes_filter(_market(volume_24h=10_000.0), ff) is True
        assert _market_passes_filter(_market(volume_24h=60_000.0), ff) is False


# ────────────────────────────────────────────────────────────────
# _market_passes_filter — end date / days remaining
# ────────────────────────────────────────────────────────────────

class TestEndDateFilter:
    def test_far_future_passes(self):
        m = _market(end_date=datetime.now(timezone.utc) + timedelta(days=365))
        assert _market_passes_filter(m, FetchFilter()) is True

    def test_just_above_min_passes(self):
        m = _market(end_date=datetime.now(timezone.utc) + timedelta(days=121))
        assert _market_passes_filter(m, FetchFilter()) is True

    def test_below_min_rejected(self):
        m = _market(end_date=datetime.now(timezone.utc) + timedelta(days=30))
        assert _market_passes_filter(m, FetchFilter()) is False

    def test_no_end_date_rejected(self):
        m = _market(end_date=None)
        assert _market_passes_filter(m, FetchFilter()) is False

    def test_custom_min_days(self):
        ff = FetchFilter(min_end_date_days=30)
        m_ok = _market(end_date=datetime.now(timezone.utc) + timedelta(days=60))
        m_bad = _market(end_date=datetime.now(timezone.utc) + timedelta(days=10))
        assert _market_passes_filter(m_ok, ff) is True
        assert _market_passes_filter(m_bad, ff) is False


# ────────────────────────────────────────────────────────────────
# _market_passes_filter — passes_hard_filters structural checks
# ────────────────────────────────────────────────────────────────

class TestStructuralChecks:
    def test_zero_max_spread_rejected(self):
        m = _market(max_spread=0.0)
        assert _market_passes_filter(m, FetchFilter()) is False

    def test_zero_min_size_rejected(self):
        m = _market(min_size=0.0)
        assert _market_passes_filter(m, FetchFilter()) is False

    def test_permissive_filter_still_rejects_zero_spread(self):
        ff = FetchFilter(min_reward_rate=0.0, min_volume=0.0, min_end_date_days=1)
        m = _market(max_spread=0.0)
        assert _market_passes_filter(m, ff) is False


# ────────────────────────────────────────────────────────────────
# _market_passes_filter — exclude_tags
# ────────────────────────────────────────────────────────────────

class TestExcludeTagsFilter:
    def test_no_excluded_tags_passes(self):
        m = _market(tags=["politics", "election"])
        assert _market_passes_filter(m, FetchFilter()) is True

    def test_matching_tag_rejected(self):
        m = _market(tags=["politics", "election"])
        ff = FetchFilter(exclude_tags=["politics"])
        assert _market_passes_filter(m, ff) is False

    def test_case_insensitive_match(self):
        m = _market(tags=["Politics"])
        ff = FetchFilter(exclude_tags=["politics"])
        assert _market_passes_filter(m, ff) is False

    def test_no_overlap_passes(self):
        m = _market(tags=["sports", "crypto"])
        ff = FetchFilter(exclude_tags=["politics"])
        assert _market_passes_filter(m, ff) is True

    def test_any_match_in_multiple_tags_rejects(self):
        m = _market(tags=["crypto"])
        ff = FetchFilter(exclude_tags=["politics", "crypto"])
        assert _market_passes_filter(m, ff) is False

    def test_market_no_tags_passes_exclude_list(self):
        m = _market(tags=[])
        ff = FetchFilter(exclude_tags=["politics"])
        assert _market_passes_filter(m, ff) is True

    def test_empty_exclude_list_passes_all(self):
        m = _market(tags=["politics"])
        ff = FetchFilter(exclude_tags=[])
        assert _market_passes_filter(m, ff) is True


# ────────────────────────────────────────────────────────────────
# _load_condition_ids_from_json
# ────────────────────────────────────────────────────────────────

class TestLoadConditionIdsFromJson:
    def test_reads_selected_markets_format(self, tmp_path: Path):
        data = [
            {"condition_id": "0xabc", "question": "Will X?"},
            {"condition_id": "0xdef", "question": "Will Y?"},
        ]
        p = tmp_path / "selected.json"
        p.write_text(json.dumps(data))
        result = _load_condition_ids_from_json(p)
        assert result == ["0xabc", "0xdef"]

    def test_preserves_order(self, tmp_path: Path):
        cids = [f"0x{i:04x}" for i in range(10)]
        data = [{"condition_id": c} for c in cids]
        p = tmp_path / "ordered.json"
        p.write_text(json.dumps(data))
        assert _load_condition_ids_from_json(p) == cids

    def test_skips_items_without_condition_id(self, tmp_path: Path):
        data = [
            {"condition_id": "0xabc"},
            {"question": "No cid here"},
            {"condition_id": ""},
            {"condition_id": None},
        ]
        p = tmp_path / "partial.json"
        p.write_text(json.dumps(data))
        assert _load_condition_ids_from_json(p) == ["0xabc"]

    def test_missing_file_returns_empty(self, tmp_path: Path):
        result = _load_condition_ids_from_json(tmp_path / "nonexistent.json")
        assert result == []

    def test_non_array_json_returns_empty(self, tmp_path: Path):
        p = tmp_path / "object.json"
        p.write_text(json.dumps({"condition_id": "0xabc"}))
        assert _load_condition_ids_from_json(p) == []

    def test_invalid_json_returns_empty(self, tmp_path: Path):
        p = tmp_path / "invalid.json"
        p.write_text("not json {{{")
        assert _load_condition_ids_from_json(p) == []

    def test_empty_array_returns_empty(self, tmp_path: Path):
        p = tmp_path / "empty.json"
        p.write_text("[]")
        assert _load_condition_ids_from_json(p) == []

    def test_real_selected_markets_shape(self, tmp_path: Path):
        """Matches the exact shape written by market_manager._save_cache."""
        data = [
            {
                "condition_id": "0x13e60c7b",
                "question": "Will there be a ceasefire?",
                "token_id_yes": "4764188637",
                "token_id_no": "3238332664",
                "max_spread": 0.045,
                "min_size": 20.0,
                "reward_rate_daily": 50.0,
                "allocated_capital": 80.0,
                "score": 73.675795,
                "midpoint": 0.395,
                "end_date": "2026-10-31T00:00:00+00:00",
                "tags": [],
                "neg_risk": False,
                "tick_size": "0.01",
            }
        ]
        p = tmp_path / "real.json"
        p.write_text(json.dumps(data))
        assert _load_condition_ids_from_json(p) == ["0x13e60c7b"]
