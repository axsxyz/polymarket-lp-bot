"""Tests for backtest/metrics.py — Q-score, PnL, adverse rate, Sharpe."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import pytest

from backtest.metrics import (
    AggregateMetrics,
    Metrics,
    aggregate_metrics,
    compute_metrics,
    compute_pnl_fill,
    compute_q_score_fill,
    compute_theoretical_q_max,
    is_adverse,
)
from backtest.simulator import BotParams, SimResult, SimulatedFill
from config.markets_filter import MarketCandidate


# ────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────

def _params() -> BotParams:
    return BotParams(
        quote_offset_ticks=2, vol_spread_multiplier_max=3.0,
        vpin_threshold=0.7, vpin_pause_threshold=0.85,
        midpoint_guard_min=0.18, midpoint_guard_max=0.82,
    )


def _market(
    cid: str = "cid_test",
    max_spread: float = 0.10,
    min_size: float = 5.0,
    tick_size: str = "0.01",
) -> MarketCandidate:
    return MarketCandidate(
        condition_id=cid,
        token_id_yes=f"{cid}_yes",
        token_id_no=f"{cid}_no",
        max_spread=max_spread, min_size=min_size,
        tick_size=tick_size,
        end_date=datetime.now(timezone.utc) + timedelta(days=60),
    )


def _fill(
    side: str = "BID", token_side: str = "YES",
    fill_price: float = 0.48, fill_size: float = 10.0,
    mid_at_post: float = 0.50, max_spread: float = 0.10,
    mid_t1min: Optional[float] = 0.50, mid_t5min: Optional[float] = 0.50,
    ts: int = 1_700_000_000, cycle_idx: int = 1,
) -> SimulatedFill:
    spread_used = abs(fill_price - mid_at_post)
    return SimulatedFill(
        cycle_idx=cycle_idx, ts=ts, side=side, token_side=token_side,
        fill_price=fill_price, fill_size=fill_size,
        mid_at_post=mid_at_post, spread_used=spread_used,
        mid_t1min=mid_t1min, mid_t5min=mid_t5min,
        max_spread=max_spread,
    )


def _result(
    fills: List[SimulatedFill],
    cycles_total: int = 100,
    cycles_paused_midpoint: int = 0,
    cycles_paused_toxicity: int = 0,
    cid: str = "cid_test",
) -> SimResult:
    return SimResult(
        condition_id=cid, params=_params(),
        fills=fills,
        cycles_total=cycles_total,
        cycles_paused_midpoint=cycles_paused_midpoint,
        cycles_paused_toxicity=cycles_paused_toxicity,
        duration_seconds=cycles_total * 30,
    )


# ────────────────────────────────────────────────────────────────
# Per-fill Q-score
# ────────────────────────────────────────────────────────────────

class TestComputeQScoreFill:
    def test_zero_when_size_zero(self) -> None:
        f = _fill(fill_size=0.0)
        assert compute_q_score_fill(f) == 0.0

    def test_zero_when_max_spread_zero(self) -> None:
        f = _fill(max_spread=0.0)
        assert compute_q_score_fill(f) == 0.0

    def test_zero_when_spread_exceeds_max(self) -> None:
        # spread_used = |fill - mid_at_post| = 0.10, max_spread = 0.05
        f = _fill(fill_price=0.40, mid_at_post=0.50, max_spread=0.05)
        assert compute_q_score_fill(f) == 0.0

    def test_q_score_formula(self) -> None:
        # v = 0.10, s = 0.02, size = 10
        # q = ((0.10 - 0.02) / 0.10)^2 × 10 = (0.8)^2 × 10 = 6.4
        f = _fill(fill_price=0.48, mid_at_post=0.50, max_spread=0.10, fill_size=10.0)
        assert compute_q_score_fill(f) == pytest.approx(6.4)

    def test_q_score_max_at_zero_spread(self) -> None:
        f = _fill(fill_price=0.50, mid_at_post=0.50, max_spread=0.10, fill_size=10.0)
        assert compute_q_score_fill(f) == pytest.approx(10.0)


# ────────────────────────────────────────────────────────────────
# PnL & adverse selection
# ────────────────────────────────────────────────────────────────

class TestPnlAndAdverse:
    def test_bid_favourable_pnl(self) -> None:
        # Bot bought at 0.48, mid drifted UP to 0.52 → favourable.
        f = _fill(side="BID", fill_price=0.48, fill_size=10.0, mid_t5min=0.52)
        assert compute_pnl_fill(f) == pytest.approx((0.52 - 0.48) * 10.0)
        assert is_adverse(f) is False

    def test_bid_adverse_pnl(self) -> None:
        # Bot bought at 0.48, mid kept dropping to 0.45 → adverse.
        f = _fill(side="BID", fill_price=0.48, fill_size=10.0, mid_t5min=0.45)
        assert compute_pnl_fill(f) == pytest.approx((0.45 - 0.48) * 10.0)
        assert is_adverse(f) is True

    def test_ask_favourable_pnl(self) -> None:
        # Bot sold at 0.52, mid dropped to 0.48 → favourable.
        f = _fill(side="ASK", fill_price=0.52, fill_size=10.0, mid_t5min=0.48)
        assert compute_pnl_fill(f) == pytest.approx((0.52 - 0.48) * 10.0)
        assert is_adverse(f) is False

    def test_ask_adverse_pnl(self) -> None:
        # Bot sold at 0.52, mid kept rising to 0.55 → adverse.
        f = _fill(side="ASK", fill_price=0.52, fill_size=10.0, mid_t5min=0.55)
        assert compute_pnl_fill(f) == pytest.approx((0.52 - 0.55) * 10.0)
        assert is_adverse(f) is True

    def test_pnl_none_when_t5min_missing(self) -> None:
        f = _fill(mid_t5min=None)
        assert compute_pnl_fill(f) is None
        assert is_adverse(f) is None


# ────────────────────────────────────────────────────────────────
# Theoretical Q-max — user spec test #3
# ────────────────────────────────────────────────────────────────

class TestTheoreticalQMax:
    def test_zero_inputs_yield_zero(self) -> None:
        assert compute_theoretical_q_max(0, 0.01, 5, 100) == 0.0
        assert compute_theoretical_q_max(0.10, 0.01, 0, 100) == 0.0
        assert compute_theoretical_q_max(0.10, 0.01, 5, 0) == 0.0

    def test_polymarket_two_sided_formula(self) -> None:
        # User spec: q_theo_max = sum over {YES, NO} of min(Q_bid, Q_ask) × min_size
        # = 2 × ((v - tick) / v)^2 × min_size  (symmetric quoting)
        # Per cycle. Multiply by n_quoting_cycles for total ceiling.
        v = 0.10
        tick = 0.01
        min_size = 5.0
        n_cycles = 100
        # Per cycle: 2 × ((0.10 - 0.01) / 0.10)^2 × 5 = 2 × 0.81 × 5 = 8.1
        # Total: 8.1 × 100 = 810
        expected_per_cycle = 2.0 * ((v - tick) / v) ** 2 * min_size
        expected_total = expected_per_cycle * n_cycles
        assert compute_theoretical_q_max(v, tick, min_size, n_cycles) == pytest.approx(
            expected_total
        )
        assert expected_total == pytest.approx(810.0)

    def test_zero_when_tick_exceeds_max_spread(self) -> None:
        # Pathological market where tick == max_spread: tightest spread is
        # already at the reward boundary → score = 0.
        assert compute_theoretical_q_max(
            max_spread=0.01, tick_dollars=0.01, min_size=5.0, n_quoting_cycles=10,
        ) == 0.0


# ────────────────────────────────────────────────────────────────
# Per-market metrics integration
# ────────────────────────────────────────────────────────────────

class TestComputeMetrics:
    def test_empty_result(self) -> None:
        m = compute_metrics(_result([]), _market())
        assert m.fills_total == 0
        assert m.fills_with_t5min == 0
        assert m.realized_q_score == 0.0
        assert m.adverse_rate == 0.0
        assert m.net_pnl == 0.0
        assert m.daily_pnls == {}
        # Theoretical max != 0 because n_quoting_cycles > 0
        assert m.theoretical_max_q_score > 0
        # Capture = 0 when realized = 0
        assert m.capture_rate == 0.0

    def test_basic_metrics_aggregation(self) -> None:
        # 4 fills: 1 favourable BID, 1 adverse BID, 1 favourable ASK, 1 adverse ASK
        ts0 = int(datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc).timestamp())
        fills = [
            _fill(side="BID", fill_price=0.48, mid_t5min=0.52, ts=ts0),
            _fill(side="BID", fill_price=0.48, mid_t5min=0.45, ts=ts0 + 60),
            _fill(side="ASK", fill_price=0.52, mid_t5min=0.48, ts=ts0 + 120),
            _fill(side="ASK", fill_price=0.52, mid_t5min=0.55, ts=ts0 + 180),
        ]
        m = compute_metrics(_result(fills), _market())
        assert m.fills_total == 4
        assert m.fills_with_t5min == 4
        # 2 of 4 fills are adverse
        assert m.adverse_rate == 0.5
        # Net PnL: +0.04*10 - 0.03*10 + 0.04*10 - 0.03*10 = 0.4 - 0.3 + 0.4 - 0.3 = 0.20
        assert m.net_pnl == pytest.approx(0.20)
        # Realized: each fill has spread=0.02, max=0.10 → 6.4 each × 4 = 25.6
        assert m.realized_q_score == pytest.approx(25.6)

    def test_t5min_missing_excluded_from_pnl_and_adverse(self) -> None:
        fills = [
            _fill(side="BID", fill_price=0.48, mid_t5min=0.45),  # adverse, has t5
            _fill(side="BID", fill_price=0.48, mid_t5min=None),  # no t5
        ]
        m = compute_metrics(_result(fills), _market())
        assert m.fills_total == 2
        assert m.fills_with_t5min == 1
        # Adverse rate computed only over fills_with_t5min
        assert m.adverse_rate == 1.0

    def test_daily_pnls_grouped_by_utc_date(self) -> None:
        # Two fills on 2026-05-01, one fill on 2026-05-02
        d1 = int(datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc).timestamp())
        d2 = int(datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc).timestamp())
        fills = [
            _fill(side="BID", fill_price=0.48, mid_t5min=0.50, ts=d1),       # PnL +0.20
            _fill(side="BID", fill_price=0.48, mid_t5min=0.49, ts=d1 + 60),  # PnL +0.10
            _fill(side="BID", fill_price=0.48, mid_t5min=0.46, ts=d2),       # PnL -0.20
        ]
        m = compute_metrics(_result(fills), _market())
        assert "2026-05-01" in m.daily_pnls
        assert "2026-05-02" in m.daily_pnls
        assert m.daily_pnls["2026-05-01"] == pytest.approx(0.30)
        assert m.daily_pnls["2026-05-02"] == pytest.approx(-0.20)

    def test_paused_cycles_reduce_theoretical_max(self) -> None:
        m_no_pause = compute_metrics(
            _result([], cycles_total=100, cycles_paused_midpoint=0),
            _market(),
        )
        m_paused = compute_metrics(
            _result([], cycles_total=100, cycles_paused_midpoint=50),
            _market(),
        )
        # Half the cycles paused → half the theoretical max
        assert m_paused.theoretical_max_q_score == pytest.approx(
            m_no_pause.theoretical_max_q_score * 0.5
        )


# ────────────────────────────────────────────────────────────────
# Aggregate metrics — portfolio-level Sharpe
# ────────────────────────────────────────────────────────────────

class TestAggregateMetrics:
    def test_empty_list_yields_zeros(self) -> None:
        agg = aggregate_metrics([])
        assert agg.n_markets == 0
        assert agg.fills_total == 0
        assert agg.sharpe_ratio_monthly == 0.0
        assert agg.sharpe_n_days == 0

    def test_single_market_sharpe_zero_when_no_variance(self) -> None:
        # All daily PnLs equal → stdev = 0 → sharpe = 0
        m = Metrics(
            condition_id="cid1", fills_total=2, fills_with_t5min=2,
            realized_q_score=10.0, theoretical_max_q_score=20.0, capture_rate=0.5,
            adverse_rate=0.0, net_pnl=2.0,
            daily_pnls={"2026-05-01": 1.0, "2026-05-02": 1.0},
            cycles_total=100, cycles_paused=0,
        )
        agg = aggregate_metrics([m])
        assert agg.sharpe_ratio_monthly == 0.0
        assert agg.sharpe_n_days == 2

    def test_sharpe_zero_with_single_day(self) -> None:
        m = Metrics(
            condition_id="cid1", fills_total=1, fills_with_t5min=1,
            realized_q_score=5.0, theoretical_max_q_score=10.0, capture_rate=0.5,
            adverse_rate=0.0, net_pnl=1.0,
            daily_pnls={"2026-05-01": 1.0},
            cycles_total=100, cycles_paused=0,
        )
        agg = aggregate_metrics([m])
        assert agg.sharpe_ratio_monthly == 0.0
        assert agg.sharpe_n_days == 1

    def test_portfolio_sharpe_sums_daily_across_markets(self) -> None:
        # Market A: +1 on day1, -1 on day2 → daily=[1, -1]
        # Market B: +1 on day1, +1 on day2 → daily=[1, 1]
        # Portfolio: day1=2, day2=0 → mean=1, stdev=sqrt(2), sharpe = 1/sqrt(2) × sqrt(30)
        m_a = Metrics(
            condition_id="A", fills_total=2, fills_with_t5min=2,
            realized_q_score=0, theoretical_max_q_score=0, capture_rate=0,
            adverse_rate=0, net_pnl=0,
            daily_pnls={"2026-05-01": 1.0, "2026-05-02": -1.0},
            cycles_total=100, cycles_paused=0,
        )
        m_b = Metrics(
            condition_id="B", fills_total=2, fills_with_t5min=2,
            realized_q_score=0, theoretical_max_q_score=0, capture_rate=0,
            adverse_rate=0, net_pnl=0,
            daily_pnls={"2026-05-01": 1.0, "2026-05-02": 1.0},
            cycles_total=100, cycles_paused=0,
        )
        agg = aggregate_metrics([m_a, m_b])
        # Portfolio daily series: [2, 0]; mean=1, stdev=sqrt(2)
        expected_sharpe = (1.0 / math.sqrt(2.0)) * math.sqrt(30.0)
        assert agg.sharpe_ratio_monthly == pytest.approx(expected_sharpe)
        assert agg.sharpe_n_days == 2

    def test_portfolio_sharpe_differs_from_naive_market_avg(self) -> None:
        # Demonstrates why naive averaging of per-market sharpes is wrong.
        # Market A: daily pnls [1, -1] → sharpe ≈ 0 (mean=0)
        # Market B: daily pnls [-1, 1] → sharpe ≈ 0 (mean=0)
        # Per-market average sharpe = 0.
        # BUT portfolio sums to [0, 0] → stdev=0 → sharpe=0.
        # In this trivial case both give 0, but the principle remains: aggregate
        # daily, recompute. Use anti-correlated PnLs for a stronger demo.
        # Market A: [+2, 0], Market B: [-1, +1]
        # Portfolio: [1, 1] → stdev=0 → sharpe=0
        # Per-market: A sharpe ≠ 0 generally, B sharpe ≠ 0
        # Portfolio sharpe = 0 (stdev=0) — diversification effect.
        m_a = Metrics(
            condition_id="A", fills_total=2, fills_with_t5min=2,
            realized_q_score=0, theoretical_max_q_score=0, capture_rate=0,
            adverse_rate=0, net_pnl=0,
            daily_pnls={"2026-05-01": 2.0, "2026-05-02": 0.0},
            cycles_total=100, cycles_paused=0,
        )
        m_b = Metrics(
            condition_id="B", fills_total=2, fills_with_t5min=2,
            realized_q_score=0, theoretical_max_q_score=0, capture_rate=0,
            adverse_rate=0, net_pnl=0,
            daily_pnls={"2026-05-01": -1.0, "2026-05-02": 1.0},
            cycles_total=100, cycles_paused=0,
        )
        agg = aggregate_metrics([m_a, m_b])
        # Portfolio: day1=1, day2=1 → stdev=0 → sharpe=0
        assert agg.sharpe_ratio_monthly == 0.0

    def test_weighted_adverse_rate(self) -> None:
        # Market A: 2 fills, 100% adverse. Market B: 8 fills, 0% adverse.
        # Naive average = 50%. Weighted by fills_with_t5min = 2/(2+8) = 20%.
        m_a = Metrics(
            condition_id="A", fills_total=2, fills_with_t5min=2,
            realized_q_score=0, theoretical_max_q_score=0, capture_rate=0,
            adverse_rate=1.0, net_pnl=0, daily_pnls={},
            cycles_total=100, cycles_paused=0,
        )
        m_b = Metrics(
            condition_id="B", fills_total=8, fills_with_t5min=8,
            realized_q_score=0, theoretical_max_q_score=0, capture_rate=0,
            adverse_rate=0.0, net_pnl=0, daily_pnls={},
            cycles_total=100, cycles_paused=0,
        )
        agg = aggregate_metrics([m_a, m_b])
        assert agg.adverse_rate == pytest.approx(0.20)
