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
        # Use SKEW_PENALTY=1.0 so a 0.30 skew * 3-tick base_offset clears
        # the round-to-tick threshold (0.30 * 1.0 * 0.03 = 0.009 ≥ half tick).
        q = Quoter(_make_settings(QUOTE_OFFSET_TICKS=3, SKEW_PENALTY=1.0))
        m = _market(max_spread=0.20)

        neutral = q.compute(m, live_mid=0.50, skew_ratio=0.0,
                            yes_position=_POS, no_position=_POS)
        skewed = q.compute(m, live_mid=0.50, skew_ratio=0.30, heavy_side="YES",
                           yes_position=_POS, no_position=_POS)

        assert skewed.yes_quote.bid_price < neutral.yes_quote.bid_price

    def test_skew_penalty_zero_disables_adjustment(self):
        """SKEW_PENALTY=0 → quotes ignore inventory imbalance entirely."""
        q = Quoter(_make_settings(QUOTE_OFFSET_TICKS=3, SKEW_PENALTY=0.0))
        m = _market(max_spread=0.20)

        neutral = q.compute(m, live_mid=0.50, skew_ratio=0.0,
                            yes_position=_POS, no_position=_POS)
        skewed = q.compute(m, live_mid=0.50, skew_ratio=0.50, heavy_side="YES",
                           yes_position=_POS, no_position=_POS)

        assert skewed.yes_quote.bid_price == neutral.yes_quote.bid_price
        assert skewed.yes_quote.ask_price == neutral.yes_quote.ask_price

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

    def test_offset_uses_toxicity_multiplier(self):
        """When an analyzer is attached, its multiplier scales the offset."""
        from datetime import datetime, timezone
        from core.order_manager import FillEvent
        from core.toxicity_analyzer import ToxicityAnalyzer

        s = _make_settings(QUOTE_OFFSET_TICKS=2)
        analyzer = ToxicityAnalyzer(s)
        # 35 BID + 5 ASK fills → imb = 30/40 = 0.75 (between THRESHOLD 0.7
        # and PAUSE 0.85, ratio = (0.75-0.7)/(0.85-0.7) = 1/3)
        # → vpin_mult = 1.0 + 1/3 * (2.5 - 1.0) = 1.5
        ts = datetime.now(timezone.utc)
        for _ in range(35):
            analyzer.record_fill(FillEvent(
                token_id="tok_yes", condition_id="cond_test", side="BID",
                price=0.50, filled_size=1.0, timestamp=ts,
            ))
        for _ in range(5):
            analyzer.record_fill(FillEvent(
                token_id="tok_yes", condition_id="cond_test", side="ASK",
                price=0.50, filled_size=1.0, timestamp=ts,
            ))

        q = Quoter(s)
        q.attach_toxicity_analyzer(analyzer)
        m = _market(max_spread=0.10)
        offset = q._compute_offset(m, 0.50)
        # base = QUOTE_OFFSET_TICKS * tick * vol_mult * tox_mult
        #      = 2 * 0.01 * 1.0 * 1.5 = 0.030
        assert offset == pytest.approx(0.03, rel=1e-3)

    def test_offset_no_analyzer_neutral(self):
        """Without an analyzer attached, tox_mult = 1.0 (cold-start safe)."""
        q = Quoter(_make_settings(QUOTE_OFFSET_TICKS=2))
        m = _market(max_spread=0.10)
        offset = q._compute_offset(m, 0.50)
        # base = 2 * 0.01 * 1.0 (vol_mult) * 1.0 (tox_mult) = 0.02
        assert offset == pytest.approx(0.02)

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


class TestVolatilitySpread:
    """P2a — realised-vol multiplier on QUOTE_OFFSET_TICKS."""

    def test_neutral_multiplier_with_no_history(self):
        """Empty history → multiplier = 1.0 (neutral, no regression)."""
        q = Quoter(_make_settings(QUOTE_OFFSET_TICKS=2))
        m = _market(max_spread=0.10)
        offset = q._compute_offset(m, 0.50)
        assert offset == pytest.approx(0.02)

    def test_calm_book_tightens_spread(self):
        """Constant mid → stdev=0 → multiplier=MIN → spread below baseline."""
        q = Quoter(_make_settings(
            QUOTE_OFFSET_TICKS=2,
            VOL_SPREAD_MULTIPLIER_MIN=0.5,
            VOL_SPREAD_MULTIPLIER_MAX=3.0,
        ))
        for _ in range(20):
            q.record_mid("cond_test", 0.5000)
        m = _market(max_spread=0.10)
        offset = q._compute_offset(m, 0.50)
        # base = 2 ticks * 0.5 = 0.01, but final clamp ≥ 1 tick
        assert offset == pytest.approx(0.01)

    def test_volatile_book_widens_spread(self):
        """High realised stdev → multiplier saturates at MAX → wide spread."""
        q = Quoter(_make_settings(
            QUOTE_OFFSET_TICKS=2,
            VOL_SPREAD_MULTIPLIER_MAX=3.0,
        ))
        for p in [0.50, 0.55, 0.50, 0.45, 0.55, 0.45] * 5:
            q.record_mid("cond_test", p)
        m = _market(max_spread=0.20)
        offset = q._compute_offset(m, 0.50)
        # stdev ≈ 5 ticks → ratio=1 → multiplier=MAX=3 → base=2*0.01*3=0.06
        assert offset >= 0.05
        assert offset <= m.max_spread * 0.499 + 1e-9

    def test_legacy_mode_unaffected_by_vol(self):
        """QUOTE_OFFSET_TICKS=0 → tightness-factor path, vol multiplier ignored."""
        q = Quoter(_make_settings(
            QUOTE_OFFSET_TICKS=0,
            TIGHTNESS_FACTOR_SAFE=0.30,
        ))
        for p in [0.50, 0.55, 0.45] * 10:
            q.record_mid("cond_test", p)
        m = _market(max_spread=0.10)
        offset = q._compute_offset(m, 0.50)
        assert offset == pytest.approx(0.03)

    def test_record_mid_rejects_out_of_range(self):
        """Mids outside (0, 1) must not pollute the rolling window."""
        q = Quoter(_make_settings())
        q.record_mid("cond_test", 0.0)
        q.record_mid("cond_test", 1.0)
        q.record_mid("cond_test", -0.5)
        q.record_mid("cond_test", 1.5)
        assert "cond_test" not in q._price_history or len(q._price_history["cond_test"]) == 0

    def test_multiplier_clamped_within_bounds(self):
        """Even with extreme stdev the multiplier respects MIN/MAX."""
        q = Quoter(_make_settings(
            QUOTE_OFFSET_TICKS=1,
            VOL_SPREAD_MULTIPLIER_MIN=0.8,
            VOL_SPREAD_MULTIPLIER_MAX=2.0,
        ))
        # Inject an enormous swing — should saturate at MAX
        for p in [0.10, 0.90] * 10:
            q.record_mid("cond_test", p)
        m = _market(max_spread=0.20)
        offset = q._compute_offset(m, 0.50)
        # base = 1 tick * 2.0 = 0.02 (multiplier capped at MAX)
        assert offset == pytest.approx(0.02)


class TestSpikeDetector:
    """P2a — spike pause based on range over SPIKE_DETECTION_WINDOW_SECONDS."""

    def test_no_spike_with_empty_history(self):
        q = Quoter(_make_settings())
        is_spike, ticks = q.evaluate_spike("cond_test", 0.01)
        assert is_spike is False
        assert ticks == 0

    def test_no_spike_below_threshold(self):
        q = Quoter(_make_settings(SPIKE_TICKS_THRESHOLD=5))
        for p in [0.50, 0.51, 0.52, 0.51]:  # range = 2 ticks
            q.record_mid("cond_test", p)
        is_spike, ticks = q.evaluate_spike("cond_test", 0.01)
        assert is_spike is False
        assert ticks == 2

    def test_spike_detected_on_pump(self):
        q = Quoter(_make_settings(SPIKE_TICKS_THRESHOLD=5))
        for p in [0.50, 0.52, 0.54, 0.56]:  # range = 6 ticks
            q.record_mid("cond_test", p)
        is_spike, ticks = q.evaluate_spike("cond_test", 0.01)
        assert is_spike is True
        assert ticks >= 5

    def test_spike_detected_on_pump_and_revert(self):
        """Range-based detection also catches pump-then-revert patterns."""
        q = Quoter(_make_settings(SPIKE_TICKS_THRESHOLD=5))
        for p in [0.50, 0.56, 0.51]:  # net move ≈ 0 but range = 6 ticks
            q.record_mid("cond_test", p)
        is_spike, ticks = q.evaluate_spike("cond_test", 0.01)
        assert is_spike is True
        assert ticks >= 5

    def test_spike_pause_lifecycle(self, monkeypatch):
        q = Quoter(_make_settings(SPIKE_PAUSE_SECONDS=60))
        fake_now = [1000.0]
        monkeypatch.setattr("core.quoter.time.monotonic", lambda: fake_now[0])

        assert q.is_spike_paused("cond_test") is False
        assert q.spike_pause_ttl("cond_test") == 0.0

        q.trigger_spike_pause("cond_test")
        assert q.is_spike_paused("cond_test") is True
        assert q.spike_pause_ttl("cond_test") == pytest.approx(60.0)

        fake_now[0] += 30
        assert q.is_spike_paused("cond_test") is True
        assert q.spike_pause_ttl("cond_test") == pytest.approx(30.0)

        fake_now[0] += 31
        # Auto-clears on first read past the deadline
        assert q.is_spike_paused("cond_test") is False
        assert q.spike_pause_ttl("cond_test") == 0.0

    def test_spike_window_excludes_old_samples(self, monkeypatch):
        """Samples older than SPIKE_DETECTION_WINDOW_SECONDS must not trigger."""
        q = Quoter(_make_settings(
            SPIKE_TICKS_THRESHOLD=5,
            SPIKE_DETECTION_WINDOW_SECONDS=30,
        ))
        fake_now = [1000.0]
        monkeypatch.setattr("core.quoter.time.monotonic", lambda: fake_now[0])

        q.record_mid("cond_test", 0.10)  # would trigger if still in window
        fake_now[0] += 60                # advance past the 30s window
        q.record_mid("cond_test", 0.50)
        q.record_mid("cond_test", 0.51)

        is_spike, ticks = q.evaluate_spike("cond_test", 0.01)
        assert is_spike is False
        assert ticks <= 1

    def test_spike_per_market_isolation(self):
        q = Quoter(_make_settings(SPIKE_TICKS_THRESHOLD=5))
        for p in [0.50, 0.56]:  # range = 6 ticks → spike
            q.record_mid("cid_1", p)
        for p in [0.50, 0.51]:  # range = 1 tick → no spike
            q.record_mid("cid_2", p)
        assert q.evaluate_spike("cid_1", 0.01)[0] is True
        assert q.evaluate_spike("cid_2", 0.01)[0] is False

        q.trigger_spike_pause("cid_1")
        assert q.is_spike_paused("cid_1") is True
        assert q.is_spike_paused("cid_2") is False
