"""Paper broker (DRY_RUN shadow book) + wiring tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, List

import pytest

from config.markets_filter import MarketCandidate
from config.settings import Settings
from core.inventory import InventoryTracker
from core.inventory_seeder import InventorySeeder
from core.order_manager import FillEvent, OrderManager
from core.paper_broker import PaperBroker
from core.scoring_monitor import ScoringMonitor


def _make_settings(**overrides) -> Settings:
    defaults = {
        "PRIVATE_KEY": "0x" + "ab" * 32,
        "TOTAL_CAPITAL": 10_000.0,
        "DRY_RUN": True,
        "SEED_SHARES": 10.0,
        "SEED_MAX_USD_PER_SIDE": 150.0,
        "SEED_AGGRESSIVE_TICKS": 1,
        "SEED_BUDGET_PCT": 0.30,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _candidate(cid: str = "cid1") -> MarketCandidate:
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
    def __init__(self, asks=None, bids=None):
        self.asks = asks or [{"price": 0.51, "size": 1000}]
        self.bids = bids or []


class _StubClient:
    def __init__(self) -> None:
        self.posted_orders: List[Any] = []
        self.cancel_calls: List[str] = []

    async def get_conditional_balance(self, token_id: str) -> float:
        return 0.0

    async def get_midpoint(self, token_id: str) -> float:
        return 0.50

    async def get_order_book(self, token_id: str):
        return _StubBook()

    async def create_and_post_order(self, order_args, order_type=None):
        self.posted_orders.append((order_args, order_type))
        return {"orderID": f"live_{len(self.posted_orders)}"}

    async def cancel(self, order_id: str) -> None:
        self.cancel_calls.append(order_id)

    async def are_orders_scoring(self, order_ids):
        raise AssertionError("are_orders_scoring must not be called in DRY_RUN")


class _StubDiscord:
    async def send_alert(self, **kwargs):
        return None

    async def send_fill(self, *args, **kwargs):
        return None


class _SpyPaper:
    def __init__(self) -> None:
        self.posts: List[Any] = []

    def precheck_bid(self, usd: float) -> bool:
        return True

    def precheck_ask(self, token_id: str, size: float) -> bool:
        return True

    def post(self, *args, **kwargs) -> bool:
        self.posts.append((args, kwargs))
        return True

    def next_order_id(self) -> str:
        return "paper_should_not_be_used"

    @property
    def committed(self) -> float:
        return 0.0

    @property
    def cash(self) -> float:
        return 1000.0

    @property
    def spendable(self) -> float:
        return 1000.0


class TestPaperBrokerSeed:
    def test_seed_credits_both_sides_and_debits_cash(self):
        pb = PaperBroker(100.0)
        result = pb.seed_market("cid", "yes", "no", 10.0, 10.0, 0.40, 0.60)
        assert result == (4.0, 6.0)
        assert pb.inventory_size("yes") == 10.0
        assert pb.inventory_size("no") == 10.0
        assert pb.cash == pytest.approx(90.0)
        assert pb.spendable == pytest.approx(90.0)

    def test_seed_is_atomic_when_cash_short(self):
        pb = PaperBroker(5.0)
        result = pb.seed_market("cid", "yes", "no", 10.0, 10.0, 0.40, 0.60)
        assert result is None
        assert pb.inventory_size("yes") == 0.0
        assert pb.inventory_size("no") == 0.0
        assert pb.cash == pytest.approx(5.0)


class TestPaperBrokerPrecheck:
    def test_bid_rejected_when_cash_short(self):
        pb = PaperBroker(1.0)
        assert pb.precheck_bid(5.0) is False
        assert pb.post("o1", "cid", "tok", "BID", 0.50, 10.0) is False
        assert pb.resting_sizes() == {}

    def test_ask_rejected_without_inventory(self):
        pb = PaperBroker(100.0)
        assert pb.precheck_ask("tok", 5.0) is False
        assert pb.post("o1", "cid", "tok", "ASK", 0.55, 5.0) is False


class TestPaperBrokerFills:
    def test_print_through_bid_fills_and_updates_cash(self):
        pb = PaperBroker(100.0)
        pb.seed_market("cid", "yes", "no", 5.0, 5.0, 0.50, 0.50)
        cash_after_seed = pb.cash
        assert pb.post("b1", "cid", "yes", "BID", 0.40, 10.0) is True
        reserved = 0.40 * 10.0
        assert pb.spendable == pytest.approx(cash_after_seed - reserved)
        fills = pb.try_fill("yes", 0.39, 10.0)
        assert len(fills) == 1
        assert fills[0].side == "BID"
        assert fills[0].price == pytest.approx(0.40)
        assert fills[0].filled_size == pytest.approx(10.0)
        assert pb.inventory_size("yes") == pytest.approx(15.0)
        assert pb.committed == pytest.approx(0.0)
        assert pb.cash == pytest.approx(cash_after_seed - reserved)
        assert "b1" not in pb.resting_sizes()

        inv = InventoryTracker(_make_settings())
        inv.ensure_position("cid", "yes", "no")
        inv.record_fills(fills)
        pos = inv.get_position("cid")
        assert pos is not None
        assert pos.yes_size == pytest.approx(10.0)

    def test_print_through_ask_credits_cash(self):
        pb = PaperBroker(100.0)
        pb.seed_market("cid", "yes", "no", 10.0, 10.0, 0.50, 0.50)
        cash_after_seed = pb.cash
        assert pb.post("a1", "cid", "yes", "ASK", 0.60, 4.0) is True
        fills = pb.try_fill("yes", 0.61, 4.0)
        assert len(fills) == 1
        assert fills[0].side == "ASK"
        assert fills[0].price == pytest.approx(0.60)
        assert pb.inventory_size("yes") == pytest.approx(6.0)
        assert pb.cash == pytest.approx(cash_after_seed + 0.60 * 4.0)

    def test_partial_reduces_resting_size(self):
        pb = PaperBroker(100.0)
        assert pb.post("b1", "cid", "tok", "BID", 0.40, 10.0) is True
        fills = pb.try_fill("tok", 0.40, 3.0)
        assert len(fills) == 1
        assert fills[0].filled_size == pytest.approx(3.0)
        assert pb.resting_sizes()["b1"] == pytest.approx(7.0)
        assert pb.committed == pytest.approx(0.40 * 7.0)

    def test_bbo_cross_fills_bid(self):
        pb = PaperBroker(100.0)
        pb.post("b1", "cid", "tok", "BID", 0.42, 5.0)
        fills = pb.try_fill("tok", None, 0.0, best_bid=0.40, best_ask=0.41)
        assert len(fills) == 1
        assert fills[0].side == "BID"

    def test_try_fill_fail_soft(self):
        pb = PaperBroker(100.0)
        assert pb.try_fill("", None, -1.0) == []


class TestDryRunFalseNeverTouchesPaper:
    def test_live_post_does_not_call_paper(self):
        s = _make_settings(DRY_RUN=False)
        client = _StubClient()
        om = OrderManager(client, s)
        spy = _SpyPaper()
        om.set_paper(spy)
        om._usdc_balance = 10_000.0
        rec = asyncio.run(
            om._post_single("cid", "tok12345678", "BUY", 0.40, 10.0)
        )
        assert spy.posts == []
        assert rec is not None
        assert rec.order_id == "live_1"
        assert client.posted_orders, "live path must post to CLOB"


class TestSeederCreditsPaper:
    def test_would_seed_debits_paper_and_returns_costs(self):
        s = _make_settings(DRY_RUN=True, SEED_SHARES=10, SEED_MAX_USD_PER_SIDE=150)
        client = _StubClient()
        seeder = InventorySeeder(client, s, _StubDiscord())
        paper = PaperBroker(10_000.0)
        seeder.set_paper(paper)
        spent, ok, yes_cost, no_cost = asyncio.run(
            seeder._seed_market(_candidate(), budget_remaining=300.0)
        )
        assert ok is True
        assert spent > 0
        assert yes_cost > 0 and no_cost > 0
        assert paper.inventory_size("y_cid1") > 0
        assert paper.inventory_size("n_cid1") > 0
        assert paper.cash < 10_000.0
        assert client.posted_orders == []


class TestScoringMonitorDryRun:
    def test_dry_run_skips_are_orders_scoring(self):
        s = _make_settings(DRY_RUN=True, SCORING_CHECK_INTERVAL_SECONDS=60)
        mon = ScoringMonitor(_StubClient(), s, _StubDiscord())
        mon._last_check = 0.0
        result = asyncio.run(
            mon.check_if_due([_candidate()])
        )
        assert result == set()


class TestDryRunPostsEnterLiveOrders:
    def test_dry_post_rests_in_live_orders_and_paper(self):
        s = _make_settings(DRY_RUN=True)
        om = OrderManager(_StubClient(), s)
        paper = PaperBroker(10_000.0)
        paper.seed_market("cid", "tok", "tok_no", 20.0, 20.0, 0.5, 0.5)
        om.set_paper(paper)
        rec = asyncio.run(
            om._post_single("cid", "tok", "BUY", 0.40, 10.0)
        )
        assert rec is not None
        assert rec.order_id in om._live_orders
        assert rec.order_id in paper.resting_sizes()
        assert rec.order_id.startswith("paper_")
