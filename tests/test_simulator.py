"""Tests for backtest/simulator.py — replay logic, fill detection, pauses."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional

import pytest

from backtest.fetcher import HistMid
from backtest.simulator import (
    BotParams,
    SimResult,
    SimulatedFill,
    _lookahead_mid,
    _mid_at_or_before,
    build_settings_for_params,
    simulate_market,
)
from config.markets_filter import MarketCandidate


# ────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────

def _params(
    quote_offset_ticks: int = 2,
    vol_max: float = 3.0,
    vpin_threshold: float = 0.7,
    vpin_pause: float = 0.85,
    mp_min: float = 0.18,
    mp_max: float = 0.82,
) -> BotParams:
    return BotParams(
        quote_offset_ticks=quote_offset_ticks,
        vol_spread_multiplier_max=vol_max,
        vpin_threshold=vpin_threshold,
        vpin_pause_threshold=vpin_pause,
        midpoint_guard_min=mp_min,
        midpoint_guard_max=mp_max,
    )


def _market(
    mid: float = 0.50,
    max_spread: float = 0.10,
    min_size: float = 5.0,
    tick_size: str = "0.01",
    days: float = 60.0,
) -> MarketCandidate:
    return MarketCandidate(
        condition_id="cid_test",
        token_id_yes="tok_yes",
        token_id_no="tok_no",
        max_spread=max_spread,
        min_size=min_size,
        midpoint=mid,
        tick_size=tick_size,
        end_date=datetime.now(timezone.utc) + timedelta(days=days),
    )


def _mids(
    prices: List[float],
    cid: str = "cid_test",
    token: str = "tok_yes",
    start_ts: int = 1_000_000,
    interval_s: int = 60,
) -> List[HistMid]:
    return [
        HistMid(condition_id=cid, token_id=token, ts=start_ts + i * interval_s, mid=p)
        for i, p in enumerate(prices)
    ]


# Tests use a 30s rebalance interval. Mid samples are spaced at 10s by default
# in fill-detection tests so each cycle window contains 3 samples — this is
# necessary to model intra-cycle price excursions (the bot adapts at cycle
# start, so a single dipped sample at cycle boundary just shifts the quote
# without producing a fill).
_FAST_INTERVAL_S = 30
_INTRA_CYCLE_INTERVAL_S = 10


# ────────────────────────────────────────────────────────────────
# Bisect helpers
# ────────────────────────────────────────────────────────────────

class TestLookaheadHelpers:
    def test_mid_at_or_before_basic(self) -> None:
        ts = [0, 60, 150]
        mids = [0.5, 0.4, 0.55]
        assert _mid_at_or_before(ts, mids, 100) == 0.4
        assert _mid_at_or_before(ts, mids, 60) == 0.4
        assert _mid_at_or_before(ts, mids, 200) == 0.55
        assert _mid_at_or_before(ts, mids, -10) is None

    def test_lookahead_returns_none_when_no_sample_in_tolerance(self) -> None:
        # Closest sample to target=360 is at ts=60 (gap=300s). With
        # tolerance=60 → no candidate within window → None.
        ts = [0, 60, 1000]
        mids = [0.5, 0.4, 0.7]
        assert _lookahead_mid(ts, mids, target_ts=360, tolerance_s=60) is None
        # Sample at t=1000 with target=2000 → gap=1000s, well beyond tolerance.
        assert _lookahead_mid(ts, mids, target_ts=2000, tolerance_s=60) is None

    def test_lookahead_picks_closest_to_target(self) -> None:
        ts = [0, 60, 150, 250, 320]
        mids = [0.50, 0.45, 0.55, 0.60, 0.70]
        # target=300, tolerance=60 → window [240, 360]. Candidates:
        #   ts=250 (dist=50), ts=320 (dist=20). Closest is 320 → 0.70.
        assert _lookahead_mid(ts, mids, target_ts=300, tolerance_s=60) == 0.70

    def test_lookahead_picks_before_when_only_before_in_window(self) -> None:
        ts = [0, 60, 250]
        mids = [0.50, 0.45, 0.60]
        # target=300, tolerance=60 → window [240, 360]. Only ts=250 (dist=50).
        assert _lookahead_mid(ts, mids, target_ts=300, tolerance_s=60) == 0.60


# ────────────────────────────────────────────────────────────────
# Simulator
# ────────────────────────────────────────────────────────────────

class TestEmptyAndStable:
    def test_empty_mids_returns_zero_cycles(self) -> None:
        result = simulate_market(_market(), [], _params())
        assert result.fills == []
        assert result.cycles_total == 0
        assert result.cycles_paused_midpoint == 0
        assert result.cycles_paused_toxicity == 0

    def test_stable_mid_no_fills(self) -> None:
        # 60 mids at exactly 0.50, bot quotes around 0.50 → no edge crossing
        mids = _mids([0.50] * 60)
        result = simulate_market(
            _market(), mids, _params(), rebalance_interval_seconds=_FAST_INTERVAL_S,
        )
        assert result.cycles_total > 0
        assert len(result.fills) == 0

    def test_settings_overrides_applied(self) -> None:
        s = build_settings_for_params(_params(quote_offset_ticks=5, vpin_threshold=0.6))
        assert s.QUOTE_OFFSET_TICKS == 5
        assert s.VPIN_THRESHOLD == 0.6
        # Sanity: defaults stay sane
        assert 0 < s.MIDPOINT_GUARD_MIN < s.MIDPOINT_GUARD_MAX < 1


def _excursion_tape(
    n_cycles: int = 5,
    base_mid: float = 0.50,
    excursion_mid: float = 0.40,
    excursion_every: int = 1,
    excursion_position: int = 1,
    start_ts: int = 1_000_000,
) -> List[HistMid]:
    """Build a tape with intra-cycle excursions to force fills.

    Each cycle (30s) holds 3 samples spaced 10s apart. ``excursion_position``
    selects which sample within a cycle deviates from ``base_mid`` to
    ``excursion_mid``. ``excursion_every`` controls cadence (every Nth cycle).

    The first sample of each cycle equals base_mid so the bot's mid_at_post
    is the base, and the in-cycle excursion crosses the bot's quote.
    """
    samples_per_cycle = _FAST_INTERVAL_S // _INTRA_CYCLE_INTERVAL_S
    out: List[HistMid] = []
    for c in range(n_cycles):
        for i in range(samples_per_cycle):
            ts = start_ts + (c * _FAST_INTERVAL_S) + (i * _INTRA_CYCLE_INTERVAL_S)
            mid = excursion_mid if (c % excursion_every == 0 and i == excursion_position) else base_mid
            out.append(HistMid("cid_test", "tok_yes", ts, mid))
    return out


def _intra_cycle_dip_tape() -> List[HistMid]:
    return _excursion_tape(n_cycles=5, base_mid=0.50, excursion_mid=0.40)


def _intra_cycle_pump_tape() -> List[HistMid]:
    return _excursion_tape(n_cycles=5, base_mid=0.50, excursion_mid=0.60)


class TestFillDetection:
    """Force fills via intra-cycle price excursions outside the bot's quotes."""

    def test_dip_below_bid_creates_yes_bid_fill(self) -> None:
        # Cycle 1 [t=0, t=30): mid_at_post=0.50, bot bid≈0.48.
        # Window contains samples at 0,10,20 with mids 0.50,0.40,0.50.
        # min=0.40 ≤ 0.48 → BID YES fill at ts=10 price=0.48.
        result = simulate_market(
            _market(), _intra_cycle_dip_tape(), _params(),
            rebalance_interval_seconds=_FAST_INTERVAL_S,
        )
        bid_yes_fills = [
            f for f in result.fills if f.side == "BID" and f.token_side == "YES"
        ]
        assert len(bid_yes_fills) >= 1
        for f in bid_yes_fills:
            assert f.fill_price < 0.50
            assert f.fill_size > 0
            assert f.spread_used > 0

    def test_pump_above_ask_creates_yes_ask_fill(self) -> None:
        result = simulate_market(
            _market(), _intra_cycle_pump_tape(), _params(),
            rebalance_interval_seconds=_FAST_INTERVAL_S,
        )
        ask_yes_fills = [
            f for f in result.fills if f.side == "ASK" and f.token_side == "YES"
        ]
        assert len(ask_yes_fills) >= 1
        for f in ask_yes_fills:
            assert f.fill_price > 0.50

    def test_no_token_fills_use_complement_scale(self) -> None:
        # Intra-cycle YES dip → NO mid bumps. NO ask filled when NO mid rises.
        result = simulate_market(
            _market(), _intra_cycle_dip_tape(), _params(),
            rebalance_interval_seconds=_FAST_INTERVAL_S,
        )
        no_fills = [f for f in result.fills if f.token_side == "NO"]
        assert len(no_fills) >= 1
        for f in no_fills:
            # NO mid_at_post is 1 - 0.50 = 0.50
            assert f.mid_at_post == pytest.approx(0.50, abs=0.01)


class TestMidpointGuard:
    def test_pauses_after_three_consecutive_breaches(self) -> None:
        # mid=0.10 is below MIDPOINT_GUARD_MIN=0.18 → all cycles breach.
        # After MIDPOINT_GUARD_CYCLES (default 3), cycles get marked paused.
        mids = _mids([0.10] * 30)
        result = simulate_market(
            _market(), mids, _params(),
            rebalance_interval_seconds=_FAST_INTERVAL_S,
        )
        # Default MIDPOINT_GUARD_CYCLES=3 → cycles 4..N should be paused
        assert result.cycles_paused_midpoint > 0
        # No fills allowed during pause (and the few un-paused cycles see
        # mid=0.10 which is far below any bid → no fills there either).
        assert len(result.fills) == 0

    def test_in_band_mid_no_pause(self) -> None:
        mids = _mids([0.50] * 20)
        result = simulate_market(
            _market(), mids, _params(),
            rebalance_interval_seconds=_FAST_INTERVAL_S,
        )
        assert result.cycles_paused_midpoint == 0


class TestVpinSymmetry:
    """Documents and verifies the VPIN-signal limitation of /prices-history backtest.

    The naive fill model assumes both YES and NO orderbooks observe the same
    mid trajectory, so any cycle-window mid excursion that hits YES BID also
    hits NO ASK (the YES-space thresholds are identical: yes_bid_price ==
    1 - no_ask_price). This produces PAIRED BID+ASK fills per cycle → VPIN
    imbalance is structurally near zero. Real-market VPIN signal comes from
    per-book independence which is not modelled here.

    Implication for the grid search: VPIN_THRESHOLD dimension is largely
    degenerate; documented in grid_search.py.
    """

    def test_naive_fill_model_produces_paired_bid_ask(self) -> None:
        mids = _excursion_tape(n_cycles=20, base_mid=0.50, excursion_mid=0.40)
        result = simulate_market(
            _market(), mids, _params(),
            rebalance_interval_seconds=_FAST_INTERVAL_S,
        )
        bid_count = sum(1 for f in result.fills if f.side == "BID")
        ask_count = sum(1 for f in result.fills if f.side == "ASK")
        assert bid_count > 5
        assert ask_count > 5
        # Paired symmetric: counts must be equal (same trigger threshold)
        assert bid_count == ask_count

    def test_vpin_pause_unreachable_with_symmetric_fills(self) -> None:
        # Even with extremely permissive VPIN params, paired fills keep
        # imbalance at 0 → no toxicity pause should ever trip from this tape.
        mids = _excursion_tape(n_cycles=40, base_mid=0.50, excursion_mid=0.40)
        params = _params(vpin_threshold=0.05, vpin_pause=0.10)
        result = simulate_market(
            _market(), mids, params,
            rebalance_interval_seconds=_FAST_INTERVAL_S,
        )
        assert result.cycles_paused_toxicity == 0


class TestDeterminism:
    def test_same_input_same_output(self) -> None:
        # Mix of dip and pump excursions for variety
        mids = _excursion_tape(
            n_cycles=15, base_mid=0.50, excursion_mid=0.42,
            excursion_every=2, excursion_position=1,
        )
        m = _market()
        p = _params()
        r1 = simulate_market(m, mids, p, rebalance_interval_seconds=_FAST_INTERVAL_S)
        r2 = simulate_market(m, mids, p, rebalance_interval_seconds=_FAST_INTERVAL_S)
        assert r1.cycles_total == r2.cycles_total
        assert r1.cycles_paused_midpoint == r2.cycles_paused_midpoint
        assert len(r1.fills) == len(r2.fills)
        # Test must produce SOME fills to exercise the determinism check
        assert len(r1.fills) > 0
        for a, b in zip(r1.fills, r2.fills):
            assert a.ts == b.ts
            assert a.side == b.side
            assert a.token_side == b.token_side
            assert a.fill_price == b.fill_price
            assert a.fill_size == b.fill_size
            assert a.mid_t5min == b.mid_t5min


class TestSizeAndScale:
    def test_fill_size_uses_alloc(self) -> None:
        # allocated_capital=100 → half=50 → @~0.50 ≈ 100 shares (not min×SIZE_MULT).
        m = _market(min_size=5.0)
        m.allocated_capital = 100.0
        mids = _excursion_tape(n_cycles=5)
        result = simulate_market(
            m, mids, _params(), rebalance_interval_seconds=_FAST_INTERVAL_S,
        )
        bid_fills = [f for f in result.fills if f.side == "BID"]
        assert bid_fills, "test setup must produce BID fills"
        for f in bid_fills:
            assert f.fill_size == pytest.approx(100.0, abs=15.0)


class TestLookaheadInResult:
    def test_t1min_t5min_populated_when_data_available(self) -> None:
        # Single dip at the start, then a long trailing tail of stable mids
        # so the +60s and +300s lookups land inside the tape.
        # Note: _excursion_tape produces back-to-back samples; we extend manually.
        head = _excursion_tape(n_cycles=2, excursion_every=1)
        # Add 40 more 0.50 samples (40 * 10s = 400s) past the head
        last_ts = head[-1].ts
        tail = [
            HistMid("cid_test", "tok_yes", last_ts + (i + 1) * 10, 0.50)
            for i in range(40)
        ]
        mids = head + tail
        result = simulate_market(
            _market(), mids, _params(),
            rebalance_interval_seconds=_FAST_INTERVAL_S,
        )
        bid_fills = [f for f in result.fills if f.side == "BID" and f.token_side == "YES"]
        assert bid_fills, "test setup must produce at least one BID fill"
        first = bid_fills[0]
        assert first.mid_t1min is not None
        assert first.mid_t5min is not None

    def test_t5min_none_when_tape_ends_too_early(self) -> None:
        # Tape: 1 cycle of intra-cycle dip → fill at the dip ts.
        # No trailing data beyond → mid_t5min must be None for the last fill.
        mids = _excursion_tape(n_cycles=1, excursion_every=1)
        result = simulate_market(
            _market(), mids, _params(),
            rebalance_interval_seconds=_FAST_INTERVAL_S,
        )
        bid_fills = [f for f in result.fills if f.side == "BID" and f.token_side == "YES"]
        assert bid_fills, "test setup must produce at least one BID fill"
        f = bid_fills[-1]
        # mid_t5min target = fill_ts + 300; tape spans ~30s only → None
        assert f.mid_t5min is None
        # mid_t1min may be None or populated depending on whether ts+60 falls
        # within the tape. With our 30-cycle layout it likely won't be present.
