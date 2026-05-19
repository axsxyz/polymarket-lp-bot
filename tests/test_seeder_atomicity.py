"""Tests for the atomic-pair invariant of the inventory seeder (Issue 1).

Invariant: ``InventorySeeder._seed_market`` MUST treat YES and NO as an
atomic pair.  If either side fails the upfront cost-or-depth pre-check,
NEITHER side is posted — the bot must never enter a unilateral
directional position from the seeding step.

These tests use a stub AsyncClobClient that records every call so we can
assert ``create_and_post_order`` was *not* invoked when the pre-check
should reject.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest

from config.markets_filter import MarketCandidate
from config.settings import Settings
from core.inventory_seeder import InventorySeeder


def _make_settings(**overrides) -> Settings:
    defaults = {
        "PRIVATE_KEY": "0x" + "ab" * 32,
        "TOTAL_CAPITAL": 100,
        "DRY_RUN": True,                 # never actually post — pre-check still runs
        "SEED_BUDGET_PCT": 0.30,
        "SEED_MAX_USD_PER_SIDE": 15.0,
        "SEED_AGGRESSIVE_TICKS": 1,
        "SEED_SHARES": 50,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _candidate(cid: str = "c1") -> MarketCandidate:
    return MarketCandidate(
        condition_id=cid,
        token_id_yes=f"y_{cid}",
        token_id_no=f"n_{cid}",
        max_spread=0.10,
        min_size=5.0,
        midpoint=0.50,
        allocated_capital=200.0,
        end_date=datetime.now(timezone.utc) + timedelta(days=150),
        tick_size="0.01",
    )


class _StubBook:
    def __init__(self, asks: List[Dict[str, float]] = None, bids: List[Dict[str, float]] = None):
        self.asks = asks or []
        self.bids = bids or []


class _StubClient:
    """Records every call.  Returns deterministic balances + book depth."""

    def __init__(
        self,
        *,
        bal_yes: float = 0.0,
        bal_no: float = 0.0,
        mid_yes: float = 0.50,
        yes_book: _StubBook = None,
        no_book: _StubBook = None,
    ) -> None:
        self.bal_yes = bal_yes
        self.bal_no = bal_no
        self.mid_yes = mid_yes
        self.yes_book = yes_book or _StubBook(asks=[{"price": 0.51, "size": 1000}])
        self.no_book = no_book or _StubBook(asks=[{"price": 0.51, "size": 1000}])
        self.posted_orders: List[Any] = []
        self.cancel_calls: List[str] = []

    async def get_conditional_balance(self, token_id: str) -> float:
        if token_id.startswith("y_"):
            return self.bal_yes
        return self.bal_no

    async def get_midpoint(self, token_id: str) -> float:
        return self.mid_yes

    async def get_order_book(self, token_id: str):
        if token_id.startswith("y_"):
            return self.yes_book
        return self.no_book

    async def create_and_post_order(self, order_args, order_type=None):
        self.posted_orders.append((order_args, order_type))
        return {"orderID": f"oid_{len(self.posted_orders)}"}

    async def cancel(self, order_id: str) -> None:
        self.cancel_calls.append(order_id)


class _StubDiscord:
    async def send_alert(self, **kwargs):
        return None


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()


class TestAtomicPreCheck:
    def test_cost_check_skips_both_sides(self, loop):
        """Per-side cost > cap → no order posted on either side."""
        s = _make_settings(
            SEED_MAX_USD_PER_SIDE=5.0,        # cap so low even mid×50 > cap
            SEED_AGGRESSIVE_TICKS=1,
            SEED_SHARES=50,
        )
        client = _StubClient(bal_yes=0.0, bal_no=0.0, mid_yes=0.50)
        seeder = InventorySeeder(client, s, _StubDiscord())

        spent, ok, yes_cost, no_cost = loop.run_until_complete(
            seeder._seed_market(_candidate(), budget_remaining=10.0)
        )

        assert spent == 0.0
        assert ok is False
        assert yes_cost == 0.0
        assert no_cost == 0.0
        assert client.posted_orders == [], "Pre-check failed → no orders allowed"

    def test_empty_yes_book_skips_both_sides(self, loop):
        """YES book empty → NO must NOT be posted either."""
        s = _make_settings()
        client = _StubClient(
            bal_yes=0.0, bal_no=0.0, mid_yes=0.50,
            yes_book=_StubBook(asks=[], bids=[]),     # truly empty
            no_book=_StubBook(asks=[{"price": 0.51, "size": 1000}]),
        )
        seeder = InventorySeeder(client, s, _StubDiscord())

        spent, ok, yes_cost, no_cost = loop.run_until_complete(
            seeder._seed_market(_candidate(), budget_remaining=100.0)
        )

        assert ok is False
        assert spent == 0.0
        assert client.posted_orders == [], "Atomic skip required when YES book empty"

    def test_empty_no_book_skips_both_sides(self, loop):
        """NO book empty → YES must NOT be posted either."""
        s = _make_settings()
        client = _StubClient(
            bal_yes=0.0, bal_no=0.0, mid_yes=0.50,
            yes_book=_StubBook(asks=[{"price": 0.51, "size": 1000}]),
            no_book=_StubBook(asks=[], bids=[]),
        )
        seeder = InventorySeeder(client, s, _StubDiscord())

        spent, ok, yes_cost, no_cost = loop.run_until_complete(
            seeder._seed_market(_candidate(), budget_remaining=100.0)
        )

        assert ok is False
        assert spent == 0.0
        assert client.posted_orders == [], "Atomic skip required when NO book empty"

    def test_no_midpoint_skips_both_sides(self, loop):
        """If midpoint cannot be fetched, both sides must be skipped."""
        s = _make_settings()

        class _NoMidClient(_StubClient):
            async def get_midpoint(self, token_id: str):
                return None

        client = _NoMidClient(bal_yes=0.0, bal_no=0.0)
        seeder = InventorySeeder(client, s, _StubDiscord())

        spent, ok, yes_cost, no_cost = loop.run_until_complete(
            seeder._seed_market(_candidate(), budget_remaining=100.0)
        )

        assert ok is False
        assert spent == 0.0
        assert client.posted_orders == []

    def test_already_seeded_short_circuits_success(self, loop):
        """Both sides already hold target → no probe, success returned."""
        s = _make_settings(SEED_SHARES=50)
        client = _StubClient(bal_yes=200.0, bal_no=200.0)
        seeder = InventorySeeder(client, s, _StubDiscord())

        spent, ok, yes_cost, no_cost = loop.run_until_complete(
            seeder._seed_market(_candidate(), budget_remaining=100.0)
        )

        assert ok is True
        assert spent == 0.0
        assert yes_cost == 0.0
        assert no_cost == 0.0
        assert client.posted_orders == []
