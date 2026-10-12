"""Tests for utils/helpers.py — max_bid_shares / max_ask_shares unmatched caps."""

from __future__ import annotations

import pytest

from utils.helpers import max_ask_shares, max_bid_shares


class TestMaxBidShares:
    def test_matched_200_limit_50_each_side_max_50(self):
        assert max_bid_shares(200.0, 200.0, "YES", 50.0) == pytest.approx(50.0)
        assert max_bid_shares(200.0, 200.0, "NO", 50.0) == pytest.approx(50.0)

    def test_heavy_yes_at_limit_yes_zero_no_can_rebalance(self):
        """250/200 unmatched=50 (at-limit OK). YES cannot add; NO may add 100."""
        assert max_bid_shares(250.0, 200.0, "YES", 50.0) == pytest.approx(0.0)
        assert max_bid_shares(250.0, 200.0, "NO", 50.0) == pytest.approx(100.0)

    def test_heavy_yes_over_limit_yes_zero(self):
        assert max_bid_shares(251.0, 200.0, "YES", 50.0) == pytest.approx(0.0)
        assert max_bid_shares(251.0, 200.0, "NO", 50.0) == pytest.approx(101.0)

    def test_heavy_no_symmetric(self):
        assert max_bid_shares(200.0, 250.0, "NO", 50.0) == pytest.approx(0.0)
        assert max_bid_shares(200.0, 250.0, "YES", 50.0) == pytest.approx(100.0)

    def test_unknown_side_zero(self):
        assert max_bid_shares(200.0, 200.0, "BOTH", 50.0) == 0.0

    def test_at_limit_ok_strict_gt(self):
        """Sitting at limit: sz may bring unmatched to limit, not above."""
        assert max_bid_shares(0.0, 0.0, "YES", 50.0) == pytest.approx(50.0)
        assert max_bid_shares(50.0, 0.0, "YES", 50.0) == pytest.approx(0.0)


class TestMaxAskShares:
    def test_matched_200_limit_50_each_side_max_50(self):
        assert max_ask_shares(200.0, 200.0, "YES", 50.0) == pytest.approx(50.0)
        assert max_ask_shares(200.0, 200.0, "NO", 50.0) == pytest.approx(50.0)

    def test_matched_ask_yes_200_forbidden(self):
        """Full-inventory ASK would leave unmatched 200 > limit."""
        assert max_ask_shares(200.0, 200.0, "YES", 50.0) < 200.0 - 1e-9
        assert max_ask_shares(200.0, 200.0, "YES", 50.0) == pytest.approx(50.0)

    def test_heavy_yes_can_sell_down_to_limit(self):
        """250/200: ASK YES may sell up to 100 → 150/200 unmatched=50."""
        assert max_ask_shares(250.0, 200.0, "YES", 50.0) == pytest.approx(100.0)
        assert max_ask_shares(250.0, 200.0, "NO", 50.0) == pytest.approx(0.0)

    def test_unknown_side_zero(self):
        assert max_ask_shares(200.0, 200.0, "BOTH", 50.0) == 0.0
