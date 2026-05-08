"""Tests for core/toxicity_analyzer.py — VPIN + fill-vs-mid scoring.

Time-dependent paths (pause TTL, horizon evaluation, sample pruning)
use a `clock` fixture that monkey-patches `time.monotonic` inside the
analyzer module to a deterministic FakeClock. Other paths are pure
state-machine tests and don't need the fixture.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from config.settings import Settings
from core.order_manager import FillEvent
from core.toxicity_analyzer import ToxicityAnalyzer


# ──────────────────────────────────────────────────────────────────────
# Fixtures & helpers
# ──────────────────────────────────────────────────────────────────────

def _make_settings(**overrides) -> Settings:
    defaults = {"PRIVATE_KEY": "0x" + "ab" * 32}
    defaults.update(overrides)
    return Settings(**defaults)


def _fill(side: str, *, cid: str = "cid_a", price: float = 0.50) -> FillEvent:
    return FillEvent(
        token_id="tok_yes",
        condition_id=cid,
        side=side,
        price=price,
        filled_size=1.0,
        timestamp=datetime.now(timezone.utc),
    )


class FakeClock:
    """Deterministic substitute for time.monotonic.

    Use the `clock` fixture to install. Advance with `.advance(dt)` or
    set absolute time with `.set(t)`.
    """
    def __init__(self, t0: float = 1000.0) -> None:
        self._t = t0

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt

    def set(self, t: float) -> None:
        self._t = t


@pytest.fixture
def clock(monkeypatch):
    """Replace `time.monotonic` (as seen by the analyzer) with FakeClock."""
    fc = FakeClock()
    monkeypatch.setattr("core.toxicity_analyzer.time.monotonic", fc)
    return fc


# ──────────────────────────────────────────────────────────────────────
# Mid history & fill ingestion
# ──────────────────────────────────────────────────────────────────────

class TestRecordMid:
    def test_in_band_appended(self, clock):
        a = ToxicityAnalyzer(_make_settings())
        a.record_mid("cid_a", 0.50)
        assert list(a._mid_history["cid_a"]) == [(1000.0, 0.50)]

    def test_out_of_band_dropped(self, clock):
        a = ToxicityAnalyzer(_make_settings())
        for bad in (0.0, 1.0, -0.5, 1.5):
            a.record_mid("cid_a", bad)
        assert "cid_a" not in a._mid_history

    def test_history_bounded_by_maxlen(self, clock):
        from core.toxicity_analyzer import _MID_HISTORY_MAXLEN
        a = ToxicityAnalyzer(_make_settings())
        for _ in range(_MID_HISTORY_MAXLEN + 100):
            a.record_mid("cid_a", 0.50)
        assert len(a._mid_history["cid_a"]) == _MID_HISTORY_MAXLEN


class TestRecordFill:
    def test_bid_appended(self, clock):
        a = ToxicityAnalyzer(_make_settings())
        a.record_fill(_fill("BID"))
        assert list(a._fill_sides["cid_a"]) == ["BID"]
        assert len(a._pending_fills["cid_a"]) == 1
        assert a._pending_fills["cid_a"][0].posted_at == 1000.0

    def test_invalid_side_dropped(self, clock):
        a = ToxicityAnalyzer(_make_settings())
        a.record_fill(_fill("WHATEVER"))
        a.record_fill(_fill(""))
        assert "cid_a" not in a._fill_sides
        assert "cid_a" not in a._pending_fills

    def test_empty_cid_dropped(self, clock):
        a = ToxicityAnalyzer(_make_settings())
        a.record_fill(_fill("BID", cid=""))
        assert not a._fill_sides

    def test_fills_window_bounded(self, clock):
        a = ToxicityAnalyzer(_make_settings(VPIN_WINDOW_FILLS=10))
        for _ in range(50):
            a.record_fill(_fill("BID"))
        assert len(a._fill_sides["cid_a"]) == 10


# ──────────────────────────────────────────────────────────────────────
# VPIN imbalance
# ──────────────────────────────────────────────────────────────────────

class TestVPIN:
    def test_empty_zero(self):
        a = ToxicityAnalyzer(_make_settings())
        assert a.vpin_imbalance("nope") == 0.0

    def test_all_bids_one(self, clock):
        a = ToxicityAnalyzer(_make_settings())
        for _ in range(20):
            a.record_fill(_fill("BID"))
        assert a.vpin_imbalance("cid_a") == 1.0

    def test_balanced_zero(self, clock):
        a = ToxicityAnalyzer(_make_settings())
        for _ in range(10):
            a.record_fill(_fill("BID"))
            a.record_fill(_fill("ASK"))
        assert a.vpin_imbalance("cid_a") == 0.0

    def test_skewed_75pct(self, clock):
        a = ToxicityAnalyzer(_make_settings())
        for _ in range(15):
            a.record_fill(_fill("BID"))
        for _ in range(5):
            a.record_fill(_fill("ASK"))
        # |15 - 5| / 20 = 0.5
        assert a.vpin_imbalance("cid_a") == pytest.approx(0.5)


# ──────────────────────────────────────────────────────────────────────
# Spread multiplier
# ──────────────────────────────────────────────────────────────────────

class TestSpreadMultiplier:
    def test_unknown_cid_neutral(self):
        a = ToxicityAnalyzer(_make_settings())
        assert a.get_spread_multiplier("nope", 0.01) == 1.0

    def test_below_min_fills_neutral(self, clock):
        """imb=1.0 returns 1.0 when N < _VPIN_MIN_FILLS (=10 module const)."""
        a = ToxicityAnalyzer(_make_settings())
        for _ in range(3):
            a.record_fill(_fill("BID"))
        assert a.get_spread_multiplier("cid_a", 0.01) == 1.0

    def test_below_threshold_neutral(self, clock):
        a = ToxicityAnalyzer(_make_settings())
        # 13 BID + 7 ASK (n=20 ≥ 10), imb = 0.3 < 0.7
        for _ in range(13):
            a.record_fill(_fill("BID"))
        for _ in range(7):
            a.record_fill(_fill("ASK"))
        assert a.get_spread_multiplier("cid_a", 0.01) == 1.0

    def test_linear_ramp_mid_band(self, clock):
        """imb = 0.75 → mult = 1.5 (ratio 1/3, default MAX 2.5)."""
        s = _make_settings()
        a = ToxicityAnalyzer(s)
        for _ in range(35):
            a.record_fill(_fill("BID"))
        for _ in range(5):
            a.record_fill(_fill("ASK"))
        # band = 0.85 - 0.70 = 0.15; ratio = 0.05/0.15 = 1/3
        # mult = 1.0 + (1/3) * (2.5 - 1.0) = 1.5
        assert a.get_spread_multiplier("cid_a", 0.01) == pytest.approx(1.5)

    def test_above_pause_threshold_capped_at_max(self, clock):
        """imb >= PAUSE_THRESHOLD → mult saturates exactly at MAX."""
        s = _make_settings()
        a = ToxicityAnalyzer(s)
        for _ in range(15):
            a.record_fill(_fill("BID"))
        # imb = 1.0 ≥ PAUSE 0.85 → ratio clamped to 1.0
        assert a.get_spread_multiplier("cid_a", 0.01) == s.TOXICITY_SPREAD_MULTIPLIER_MAX


# ──────────────────────────────────────────────────────────────────────
# VPIN-driven toxic pause
# ──────────────────────────────────────────────────────────────────────

class TestToxicPause:
    def test_no_pause_below_threshold(self, clock):
        a = ToxicityAnalyzer(_make_settings())
        for _ in range(13):
            a.record_fill(_fill("BID"))
        for _ in range(7):
            a.record_fill(_fill("ASK"))
        a.evaluate("cid_a", 0.01)
        assert not a.is_toxic_paused("cid_a")

    def test_pause_triggered_above_threshold(self, clock):
        a = ToxicityAnalyzer(_make_settings())
        for _ in range(15):
            a.record_fill(_fill("BID"))
        a.evaluate("cid_a", 0.01)
        assert a.is_toxic_paused("cid_a")

    def test_pause_below_min_fills_skipped(self, clock):
        """evaluate() doesn't trigger pause when N < _VPIN_MIN_FILLS."""
        a = ToxicityAnalyzer(_make_settings())
        for _ in range(3):
            a.record_fill(_fill("BID"))
        a.evaluate("cid_a", 0.01)
        assert not a.is_toxic_paused("cid_a")

    def test_ttl_correct(self, clock):
        s = _make_settings()
        a = ToxicityAnalyzer(s)
        for _ in range(15):
            a.record_fill(_fill("BID"))
        a.evaluate("cid_a", 0.01)
        assert a.toxic_pause_ttl("cid_a") == pytest.approx(s.TOXICITY_PAUSE_SECONDS)

    def test_pause_auto_clears_on_expiry(self, clock):
        s = _make_settings()
        a = ToxicityAnalyzer(s)
        for _ in range(15):
            a.record_fill(_fill("BID"))
        a.evaluate("cid_a", 0.01)
        assert a.is_toxic_paused("cid_a")
        clock.advance(s.TOXICITY_PAUSE_SECONDS + 1)
        assert not a.is_toxic_paused("cid_a")
        assert a.toxic_pause_ttl("cid_a") == 0.0

    def test_unknown_cid_no_pause(self):
        a = ToxicityAnalyzer(_make_settings())
        assert not a.is_toxic_paused("nope")
        assert a.toxic_pause_ttl("nope") == 0.0


# ──────────────────────────────────────────────────────────────────────
# Fill-vs-mid scoring
# ──────────────────────────────────────────────────────────────────────

class TestFillVsMid:
    def test_bid_unfavourable_mid_drop(self, clock):
        """BID @ 0.50 + mid drops to 0.45 at all 3 horizons → mean 5 ticks."""
        a = ToxicityAnalyzer(_make_settings())
        a.record_fill(_fill("BID", price=0.50))
        for offset in (5.0, 30.0, 300.0):
            clock.set(1000.0 + offset)
            a.record_mid("cid_a", 0.45)
        a.evaluate("cid_a", tick=0.01)
        # adverse = (0.50 - 0.45) / 0.01 = 5 at all 3 horizons
        # weighted = (0.2 + 0.3 + 0.5) * 5 = 5.0
        assert a.toxicity_score_ticks("cid_a") == pytest.approx(5.0)

    def test_bid_favourable_mid_rise_clamped_zero(self, clock):
        """BID @ 0.50 + mid rises to 0.55 → adverse clamped to 0."""
        a = ToxicityAnalyzer(_make_settings())
        a.record_fill(_fill("BID", price=0.50))
        for offset in (5.0, 30.0, 300.0):
            clock.set(1000.0 + offset)
            a.record_mid("cid_a", 0.55)
        a.evaluate("cid_a", tick=0.01)
        assert a.toxicity_score_ticks("cid_a") == 0.0

    def test_ask_unfavourable_mid_rise(self, clock):
        """ASK @ 0.50 + mid rises to 0.55 → adverse = 5 ticks."""
        a = ToxicityAnalyzer(_make_settings())
        a.record_fill(_fill("ASK", price=0.50))
        for offset in (5.0, 30.0, 300.0):
            clock.set(1000.0 + offset)
            a.record_mid("cid_a", 0.55)
        a.evaluate("cid_a", tick=0.01)
        assert a.toxicity_score_ticks("cid_a") == pytest.approx(5.0)

    def test_ask_favourable_mid_drop_clamped_zero(self, clock):
        """ASK @ 0.50 + mid drops to 0.45 → adverse clamped to 0."""
        a = ToxicityAnalyzer(_make_settings())
        a.record_fill(_fill("ASK", price=0.50))
        for offset in (5.0, 30.0, 300.0):
            clock.set(1000.0 + offset)
            a.record_mid("cid_a", 0.45)
        a.evaluate("cid_a", tick=0.01)
        assert a.toxicity_score_ticks("cid_a") == 0.0

    def test_horizon_weighting(self, clock):
        """5s=2t, 30s=4t, 300s=10t → weighted = 0.2*2 + 0.3*4 + 0.5*10 = 6.6."""
        a = ToxicityAnalyzer(_make_settings())
        a.record_fill(_fill("BID", price=0.50))
        clock.set(1005.0)
        a.record_mid("cid_a", 0.48)  # 2 ticks adverse
        clock.set(1030.0)
        a.record_mid("cid_a", 0.46)  # 4 ticks adverse
        clock.set(1300.0)
        a.record_mid("cid_a", 0.40)  # 10 ticks adverse
        a.evaluate("cid_a", tick=0.01)
        # 0.2*2 + 0.3*4 + 0.5*10 = 0.4 + 1.2 + 5.0 = 6.6
        assert a.toxicity_score_ticks("cid_a") == pytest.approx(6.6)

    def test_mid_lookup_within_tolerance(self, clock):
        """Mid recorded 1.5s before horizon target → still found (tol = 2s)."""
        a = ToxicityAnalyzer(_make_settings())
        a.record_fill(_fill("BID", price=0.50))
        for target in (1005.0, 1030.0, 1300.0):
            clock.set(target - 1.5)  # 1.5s before, dt = 1.5 < 2.0
            a.record_mid("cid_a", 0.45)
        clock.set(1300.0)
        a.evaluate("cid_a", tick=0.01)
        assert a.toxicity_score_ticks("cid_a") == pytest.approx(5.0)

    def test_mid_lookup_outside_tolerance_gives_up(self, clock):
        """Only one mid 5s before 5s horizon → all horizons give up at 0."""
        a = ToxicityAnalyzer(_make_settings())
        a.record_fill(_fill("BID", price=0.50))  # t=1000
        clock.set(1000.0)
        a.record_mid("cid_a", 0.45)  # only this one — too far from any horizon
        clock.set(1300.0)
        a.evaluate("cid_a", tick=0.01)
        # 5s and 30s both past target+4 → set 0.0
        # 300s exactly at target=1300, now=1300, 1300 > 1304 false → stays None
        # Fill aged out (300 >= TOXICITY_FILL_WINDOW_SECONDS) → push with skip
        # Score = (0.2*0 + 0.3*0) / 0.5 = 0
        assert a.toxicity_score_ticks("cid_a") == 0.0
        assert a._pending_fills.get("cid_a", []) == []

    def test_score_window_pruning(self, clock):
        """Old toxicity samples beyond TOXICITY_FILL_WINDOW_SECONDS are pruned."""
        s = _make_settings()
        a = ToxicityAnalyzer(s)
        a.record_fill(_fill("BID", price=0.50))
        for offset in (5.0, 30.0, 300.0):
            clock.set(1000.0 + offset)
            a.record_mid("cid_a", 0.48)  # 2 ticks adverse
        a.evaluate("cid_a", tick=0.01)
        assert a.toxicity_score_ticks("cid_a") > 0.0

        # Advance well past the window
        clock.advance(s.TOXICITY_FILL_WINDOW_SECONDS + 100)
        a.evaluate("cid_a", tick=0.01)  # triggers pruning
        assert a.toxicity_score_ticks("cid_a") == 0.0


# ──────────────────────────────────────────────────────────────────────
# Idempotency & no-side-effects
# ──────────────────────────────────────────────────────────────────────

class TestEvaluateIdempotency:
    def test_repeated_evaluate_no_double_count(self, clock):
        """Calling evaluate twice in a row must not double-count fills."""
        a = ToxicityAnalyzer(_make_settings())
        a.record_fill(_fill("BID", price=0.50))
        for offset in (5.0, 30.0, 300.0):
            clock.set(1000.0 + offset)
            a.record_mid("cid_a", 0.45)
        a.evaluate("cid_a", tick=0.01)
        first = a.toxicity_score_ticks("cid_a")
        # Pending list is now empty; second call must not re-push.
        a.evaluate("cid_a", tick=0.01)
        second = a.toxicity_score_ticks("cid_a")
        assert first == second

    def test_evaluate_unknown_cid_noop(self, clock):
        """evaluate() on a cid with no state must not raise."""
        a = ToxicityAnalyzer(_make_settings())
        a.evaluate("nope", tick=0.01)
        assert a.get_spread_multiplier("nope", 0.01) == 1.0
        assert not a.is_toxic_paused("nope")
