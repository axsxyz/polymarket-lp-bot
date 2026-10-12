"""Tests for core/order_manager.py — FillEvent, drain semantics."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.order_manager import FillEvent, should_replace_price


class TestFillEvent:
    def test_fields_match_trade(self):
        fill = FillEvent(
            token_id="tok_1",
            condition_id="cond_1",
            side="BID",
            price=0.42,
            filled_size=25.0,
        )
        assert fill.token_id == "tok_1"
        assert fill.condition_id == "cond_1"
        assert fill.side == "BID"
        assert fill.price == 0.42
        assert fill.filled_size == 25.0

    def test_partial_fill_size(self):
        fill = FillEvent(
            token_id="tok_1",
            condition_id="cond_1",
            side="BID",
            price=0.50,
            filled_size=50.0,
        )
        assert fill.filled_size == 50.0  # not 100 (original order)

    def test_timestamp_default(self):
        fill = FillEvent(
            token_id="t", condition_id="c",
            side="ASK", price=0.5, filled_size=10,
        )
        assert fill.timestamp is not None
        assert fill.timestamp.tzinfo is not None


class TestSmartDiffTick:
    def test_fine_tick_requires_smaller_move(self):
        # 0.001 market: 0.005 move is 5 ticks — replace
        assert should_replace_price(0.50, 0.505, tick=0.001, threshold_ticks=1) is True
        # 0.005 is only 0.5 of a 0.01 tick — must NOT replace if we wrongly used 0.01
        assert should_replace_price(0.50, 0.505, tick=0.01, threshold_ticks=1) is False

    def test_coarse_tick_ignores_subtick(self):
        # 0.1 market: 0.01 move is 0.1 tick — keep
        assert should_replace_price(0.50, 0.51, tick=0.1, threshold_ticks=1) is False
        assert should_replace_price(0.50, 0.61, tick=0.1, threshold_ticks=1) is True
