"""Tests for core/order_manager.py — FillEvent, drain semantics."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.order_manager import FillEvent


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
