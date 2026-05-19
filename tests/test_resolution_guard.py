"""Tests for core/resolution_guard.py — P2d max-loss + 24h move delister.

Pure unit tests, no async, no I/O. Time is mocked by monkeypatching
``core.resolution_guard._utcnow`` rather than the stdlib datetime
module (the same pattern toxicity_analyzer + quoter tests use for
``time.monotonic``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from config.settings import Settings
from core.resolution_guard import ResolutionGuard


# ──────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ──────────────────────────────────────────────────────────────────────


def _make_settings(**overrides) -> Settings:
    defaults = {
        "PRIVATE_KEY": "0x" + "ab" * 32,
        "TOTAL_CAPITAL": 10000,
        "RESOLUTION_MAX_LOSS_USD": 25.0,
        "RESOLUTION_MAX_MOVE_24H_PCT": 15.0,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _freeze_now(monkeypatch, dt: datetime) -> list:
    """Monkeypatch ``core.resolution_guard._utcnow`` to return ``dt``.

    Returns a single-element list holding the current frozen time;
    mutate ``box[0]`` to advance the clock without re-patching.
    """
    box = [dt]
    monkeypatch.setattr("core.resolution_guard._utcnow", lambda: box[0])
    return box


_CID = "0xabcdef0123456789"
_OTHER_CID = "0xfedcba9876543210"


# ──────────────────────────────────────────────────────────────────────
# max_loss_check
# ──────────────────────────────────────────────────────────────────────


class TestMaxLossCheck:
    def test_not_breached_small_positions(self):
        g = ResolutionGuard(_make_settings(RESOLUTION_MAX_LOSS_USD=25.0))
        breached, max_loss, limit = g.max_loss_check(_CID, yes_size=10.0, no_size=8.0)
        assert breached is False
        assert max_loss == pytest.approx(10.0)
        assert limit == pytest.approx(25.0)

    def test_breached_heavy_yes(self):
        g = ResolutionGuard(_make_settings(RESOLUTION_MAX_LOSS_USD=25.0))
        breached, max_loss, _ = g.max_loss_check(_CID, yes_size=40.0, no_size=10.0)
        assert breached is True
        assert max_loss == pytest.approx(40.0)

    def test_breached_heavy_no(self):
        g = ResolutionGuard(_make_settings(RESOLUTION_MAX_LOSS_USD=25.0))
        breached, max_loss, _ = g.max_loss_check(_CID, yes_size=5.0, no_size=30.0)
        assert breached is True
        assert max_loss == pytest.approx(30.0)

    def test_at_threshold_strict_not_breached(self):
        """Strict ``>``: exactly at the limit must NOT trip."""
        g = ResolutionGuard(_make_settings(RESOLUTION_MAX_LOSS_USD=25.0))
        breached, max_loss, _ = g.max_loss_check(_CID, yes_size=25.0, no_size=0.0)
        assert breached is False
        assert max_loss == pytest.approx(25.0)

    def test_equal_yes_no_uses_max_not_sum(self):
        """Worst-case is heavy side → 0, not both sides summed.

        25 YES + 25 NO must NOT breach a $25 limit (worst case is 25 USD
        if the loser side resolves to 0).
        """
        g = ResolutionGuard(_make_settings(RESOLUTION_MAX_LOSS_USD=25.0))
        breached, max_loss, _ = g.max_loss_check(_CID, yes_size=25.0, no_size=25.0)
        assert breached is False
        assert max_loss == pytest.approx(25.0)

    def test_zero_positions(self):
        g = ResolutionGuard(_make_settings())
        breached, max_loss, _ = g.max_loss_check(_CID, yes_size=0.0, no_size=0.0)
        assert breached is False
        assert max_loss == 0.0

    def test_configurable_limit_lower(self):
        g = ResolutionGuard(_make_settings(RESOLUTION_MAX_LOSS_USD=5.0))
        breached, _, limit = g.max_loss_check(_CID, yes_size=6.0, no_size=0.0)
        assert breached is True
        assert limit == pytest.approx(5.0)


# ──────────────────────────────────────────────────────────────────────
# record_midpoint
# ──────────────────────────────────────────────────────────────────────


class TestRecordMidpoint:
    def test_appends_in_order(self, monkeypatch):
        g = ResolutionGuard(_make_settings())
        now = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
        _freeze_now(monkeypatch, now)
        for i in range(5):
            g.record_midpoint(_CID, 0.50 + i * 0.01, now + timedelta(minutes=i))
        # Newest mid is the most recent value we appended.
        assert g.history_len(_CID) == 5
        # 24h move requires ≥ 20min of history; here we have 4 min only
        # so the call returns the default tuple (insufficient data).
        breached, move_pct, _ = g.evaluate_move_24h(_CID)
        assert breached is False
        assert move_pct == 0.0

    def test_purges_entries_older_than_25h(self, monkeypatch):
        """Retention is hard-capped at 25h — anything older drops on next append."""
        g = ResolutionGuard(_make_settings())
        anchor = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
        _freeze_now(monkeypatch, anchor)
        # Insert an entry 26h before the anchor → should be purged on the
        # next append at 'anchor'.
        g.record_midpoint(_CID, 0.55, anchor - timedelta(hours=26))
        assert g.history_len(_CID) == 1
        g.record_midpoint(_CID, 0.51, anchor)
        # Pruning is triggered by the new append; only the fresh entry remains.
        assert g.history_len(_CID) == 1

    def test_keeps_entries_within_25h(self, monkeypatch):
        g = ResolutionGuard(_make_settings())
        anchor = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
        _freeze_now(monkeypatch, anchor)
        g.record_midpoint(_CID, 0.55, anchor - timedelta(hours=24))  # in band
        g.record_midpoint(_CID, 0.56, anchor - timedelta(hours=12))
        g.record_midpoint(_CID, 0.57, anchor)
        assert g.history_len(_CID) == 3

    def test_rejects_out_of_band_mid(self):
        """Mids outside (0, 1) are dropped — same contract as Quoter / Toxicity."""
        g = ResolutionGuard(_make_settings())
        now = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
        g.record_midpoint(_CID, 0.0, now)
        g.record_midpoint(_CID, 1.0, now)
        g.record_midpoint(_CID, -0.5, now)
        g.record_midpoint(_CID, 1.5, now)
        assert g.history_len(_CID) == 0

    def test_rejects_empty_cid(self):
        g = ResolutionGuard(_make_settings())
        now = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
        g.record_midpoint("", 0.50, now)
        assert g.history_len("") == 0

    def test_naive_datetime_accepted_as_utc(self):
        """Naive datetimes (no tzinfo) are treated as UTC for compat with
        ``datetime.utcnow()`` callers."""
        g = ResolutionGuard(_make_settings())
        naive = datetime(2026, 5, 19, 12, 0)  # no tzinfo
        g.record_midpoint(_CID, 0.50, naive)
        assert g.history_len(_CID) == 1


# ──────────────────────────────────────────────────────────────────────
# evaluate_move_24h
# ──────────────────────────────────────────────────────────────────────


class TestEvaluateMove24h:
    def test_no_data_returns_default(self):
        g = ResolutionGuard(_make_settings(RESOLUTION_MAX_MOVE_24H_PCT=15.0))
        breached, move_pct, limit = g.evaluate_move_24h(_CID)
        assert breached is False
        assert move_pct == 0.0
        assert limit == pytest.approx(15.0)

    def test_insufficient_history_under_20min(self, monkeypatch):
        g = ResolutionGuard(_make_settings())
        now = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
        _freeze_now(monkeypatch, now)
        g.record_midpoint(_CID, 0.50, now - timedelta(minutes=10))
        g.record_midpoint(_CID, 0.80, now)
        breached, move_pct, _ = g.evaluate_move_24h(_CID)
        assert breached is False
        assert move_pct == 0.0

    def test_no_point_in_tolerance(self, monkeypatch):
        """22 min of data — well past the 20-min minimum, but no point
        sits within ±30 min of (now − 24h). Returns the default."""
        g = ResolutionGuard(_make_settings())
        now = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
        _freeze_now(monkeypatch, now)
        g.record_midpoint(_CID, 0.50, now - timedelta(minutes=22))
        g.record_midpoint(_CID, 0.51, now - timedelta(minutes=11))
        g.record_midpoint(_CID, 0.52, now)
        breached, move_pct, _ = g.evaluate_move_24h(_CID)
        assert breached is False
        assert move_pct == 0.0

    def test_move_below_threshold(self, monkeypatch):
        g = ResolutionGuard(_make_settings(RESOLUTION_MAX_MOVE_24H_PCT=15.0))
        now = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
        _freeze_now(monkeypatch, now)
        # Reference 24h ago at 0.50 → current 0.55 → move = 10% (< 15%).
        g.record_midpoint(_CID, 0.50, now - timedelta(hours=24))
        g.record_midpoint(_CID, 0.52, now - timedelta(hours=12))
        g.record_midpoint(_CID, 0.55, now)
        breached, move_pct, limit = g.evaluate_move_24h(_CID)
        assert breached is False
        assert move_pct == pytest.approx(10.0, rel=1e-3)
        assert limit == pytest.approx(15.0)

    def test_move_above_threshold(self, monkeypatch):
        g = ResolutionGuard(_make_settings(RESOLUTION_MAX_MOVE_24H_PCT=15.0))
        now = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
        _freeze_now(monkeypatch, now)
        # Reference 24h ago at 0.50 → current 0.65 → move = 30% (> 15%).
        g.record_midpoint(_CID, 0.50, now - timedelta(hours=24))
        g.record_midpoint(_CID, 0.55, now - timedelta(hours=12))
        g.record_midpoint(_CID, 0.65, now)
        breached, move_pct, _ = g.evaluate_move_24h(_CID)
        assert breached is True
        assert move_pct == pytest.approx(30.0, rel=1e-3)

    def test_tolerance_window_includes_29min(self, monkeypatch):
        """A point at -23h31m (= 29 min away from -24h anchor) is in tolerance."""
        g = ResolutionGuard(_make_settings(RESOLUTION_MAX_MOVE_24H_PCT=15.0))
        now = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
        _freeze_now(monkeypatch, now)
        g.record_midpoint(_CID, 0.50, now - timedelta(hours=23, minutes=31))
        g.record_midpoint(_CID, 0.60, now)
        breached, move_pct, _ = g.evaluate_move_24h(_CID)
        # 29 min ≤ 30 min tolerance → reference accepted, move = 20% > 15%.
        assert breached is True
        assert move_pct == pytest.approx(20.0, rel=1e-3)

    def test_tolerance_window_excludes_31min(self, monkeypatch):
        """A point at -23h29m (= 31 min away from -24h anchor) is out of tolerance."""
        g = ResolutionGuard(_make_settings(RESOLUTION_MAX_MOVE_24H_PCT=15.0))
        now = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
        _freeze_now(monkeypatch, now)
        # Single point 23h29m ago is the only candidate — > 30 min from anchor.
        # No "now" sample either; with two samples both outside tolerance the
        # lookup returns None and we fall back to the default.
        g.record_midpoint(_CID, 0.50, now - timedelta(hours=23, minutes=29))
        g.record_midpoint(_CID, 0.60, now)
        breached, move_pct, _ = g.evaluate_move_24h(_CID)
        assert breached is False
        assert move_pct == 0.0

    def test_picks_closest_point_within_tolerance(self, monkeypatch):
        """Multiple points in tolerance — the closest to -24h wins."""
        g = ResolutionGuard(_make_settings(RESOLUTION_MAX_MOVE_24H_PCT=5.0))
        now = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
        _freeze_now(monkeypatch, now)
        # -24h05m at 0.40 (closer to anchor, 5 min away)
        # -23h45m at 0.30 (further, 15 min away — should be ignored)
        g.record_midpoint(_CID, 0.40, now - timedelta(hours=24, minutes=5))
        g.record_midpoint(_CID, 0.30, now - timedelta(hours=23, minutes=45))
        g.record_midpoint(_CID, 0.50, now)
        breached, move_pct, _ = g.evaluate_move_24h(_CID)
        # Reference 0.40 → current 0.50 → move = 25%, threshold 5% → breach.
        assert breached is True
        assert move_pct == pytest.approx(25.0, rel=1e-3)


# ──────────────────────────────────────────────────────────────────────
# should_delist (aggregator)
# ──────────────────────────────────────────────────────────────────────


class TestShouldDelist:
    def test_extreme_mid_low_forces_delist(self):
        g = ResolutionGuard(_make_settings())
        delist, reason = g.should_delist(_CID, 0.0, 0.0, mid=0.04)
        assert delist is True
        assert reason == "extreme_mid"

    def test_extreme_mid_high_forces_delist(self):
        g = ResolutionGuard(_make_settings())
        delist, reason = g.should_delist(_CID, 0.0, 0.0, mid=0.96)
        assert delist is True
        assert reason == "extreme_mid"

    def test_extreme_mid_at_boundary_in_band(self):
        """Exact 0.05 and 0.95 are in-band (strict ``<`` / ``>``)."""
        g = ResolutionGuard(_make_settings(RESOLUTION_MAX_LOSS_USD=100.0))
        delist, reason = g.should_delist(_CID, 1.0, 1.0, mid=0.05)
        assert delist is False
        assert reason is None
        delist, reason = g.should_delist(_CID, 1.0, 1.0, mid=0.95)
        assert delist is False
        assert reason is None

    def test_max_loss_reason(self):
        g = ResolutionGuard(_make_settings(RESOLUTION_MAX_LOSS_USD=25.0))
        # In-band mid (0.50) → extreme_mid net does not trigger.
        delist, reason = g.should_delist(_CID, yes_size=40.0, no_size=10.0, mid=0.50)
        assert delist is True
        assert reason == "max_loss"

    def test_move_24h_reason(self, monkeypatch):
        g = ResolutionGuard(_make_settings(
            RESOLUTION_MAX_LOSS_USD=1000.0,  # high so max_loss can't trigger
            RESOLUTION_MAX_MOVE_24H_PCT=15.0,
        ))
        now = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
        _freeze_now(monkeypatch, now)
        g.record_midpoint(_CID, 0.50, now - timedelta(hours=24))
        g.record_midpoint(_CID, 0.70, now)
        delist, reason = g.should_delist(_CID, yes_size=5.0, no_size=5.0, mid=0.70)
        assert delist is True
        assert reason == "move_24h"

    def test_extreme_takes_priority_over_max_loss(self):
        """When both extreme_mid AND max_loss would fire, the safety net wins."""
        g = ResolutionGuard(_make_settings(RESOLUTION_MAX_LOSS_USD=10.0))
        delist, reason = g.should_delist(_CID, yes_size=100.0, no_size=0.0, mid=0.03)
        assert delist is True
        assert reason == "extreme_mid"

    def test_max_loss_takes_priority_over_move_24h(self, monkeypatch):
        """When max_loss AND move_24h would both fire, max_loss wins."""
        g = ResolutionGuard(_make_settings(
            RESOLUTION_MAX_LOSS_USD=25.0,
            RESOLUTION_MAX_MOVE_24H_PCT=15.0,
        ))
        now = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
        _freeze_now(monkeypatch, now)
        g.record_midpoint(_CID, 0.50, now - timedelta(hours=24))
        g.record_midpoint(_CID, 0.70, now)
        delist, reason = g.should_delist(_CID, yes_size=40.0, no_size=10.0, mid=0.70)
        assert delist is True
        assert reason == "max_loss"

    def test_no_trigger_returns_false_none(self):
        g = ResolutionGuard(_make_settings(
            RESOLUTION_MAX_LOSS_USD=25.0,
            RESOLUTION_MAX_MOVE_24H_PCT=15.0,
        ))
        delist, reason = g.should_delist(_CID, yes_size=10.0, no_size=8.0, mid=0.50)
        assert delist is False
        assert reason is None


# ──────────────────────────────────────────────────────────────────────
# Integration: record → evaluate on synthetic timeline
# ──────────────────────────────────────────────────────────────────────


class TestIntegration:
    def test_record_then_evaluate_realistic_timeline(self, monkeypatch):
        """Simulate ~25 hours of mid samples on a market that drifts
        from 0.50 → 0.70 over 24h; verify the guard catches the move at
        the 24h mark and that retention prunes earlier samples."""
        g = ResolutionGuard(_make_settings(
            RESOLUTION_MAX_LOSS_USD=100.0,
            RESOLUTION_MAX_MOVE_24H_PCT=15.0,
        ))
        anchor = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
        now_box = _freeze_now(monkeypatch, anchor - timedelta(hours=25))

        # T-25h: first sample (will be purged once 25h pass).
        g.record_midpoint(_CID, 0.50, anchor - timedelta(hours=25))
        # T-24h: reference for the move check.
        now_box[0] = anchor - timedelta(hours=24)
        g.record_midpoint(_CID, 0.50, anchor - timedelta(hours=24))
        # Hourly samples drifting upward.
        for h in range(23, -1, -1):
            now_box[0] = anchor - timedelta(hours=h)
            mid = 0.50 + (24 - h) * (0.20 / 24)
            g.record_midpoint(_CID, mid, anchor - timedelta(hours=h))

        # "Now" = anchor → evaluate move from -24h sample (0.50) to "now"
        # sample (last recorded ≈ 0.70). Move ≈ 40% > 15% → breach.
        now_box[0] = anchor
        breached, move_pct, _ = g.evaluate_move_24h(_CID)
        assert breached is True
        assert move_pct == pytest.approx(40.0, rel=1e-2)

        # 26 samples total (T-25h, T-24h, T-23h..T-0h). T-25h is exactly
        # at the retention boundary and only drops once we append a sample
        # whose ts is strictly > T-25h + 25h. Verify the boundary by
        # appending one minute past the anchor.
        assert g.history_len(_CID) == 26
        now_box[0] = anchor + timedelta(minutes=1)
        g.record_midpoint(_CID, 0.70, anchor + timedelta(minutes=1))
        # T-25h sample is now < cutoff (= anchor + 1min - 25h) → purged.
        # 26 prior + 1 new − 1 purged = 26.
        assert g.history_len(_CID) == 26

        # should_delist surfaces "move_24h" (extreme_mid does not trigger
        # at 0.70, max_loss does not trigger at 5 shares).
        delist, reason = g.should_delist(_CID, yes_size=5.0, no_size=5.0, mid=0.70)
        assert delist is True
        assert reason == "move_24h"

    def test_per_market_isolation(self, monkeypatch):
        """History is keyed per cid — a move on one market must not leak."""
        g = ResolutionGuard(_make_settings(RESOLUTION_MAX_MOVE_24H_PCT=15.0))
        now = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
        _freeze_now(monkeypatch, now)
        g.record_midpoint(_CID, 0.50, now - timedelta(hours=24))
        g.record_midpoint(_CID, 0.80, now)
        # _OTHER_CID has no data → returns default.
        breached_a, _, _ = g.evaluate_move_24h(_CID)
        breached_b, _, _ = g.evaluate_move_24h(_OTHER_CID)
        assert breached_a is True
        assert breached_b is False
