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
from core.inventory import InventoryTracker
from core.inventory_seeder import InventorySeeder, compute_matched_seed_n
from core.paper_broker import PaperBroker


def _make_settings(**overrides) -> Settings:
    defaults = {
        "PRIVATE_KEY": "0x" + "ab" * 32,
        "TOTAL_CAPITAL": 100,
        "DRY_RUN": True,                 # never actually post — pre-check still runs
        "SEED_BUDGET_PCT": 0.30,
        "SEED_MAX_USD_PER_SIDE": 150.0,
        "SEED_AGGRESSIVE_TICKS": 1,
        "SEED_SHARES": 5,
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


class _FakeTape:
    def __init__(self) -> None:
        self.skips: List[tuple] = []

    def skip(self, cid: str, reason: str, **extra) -> None:
        self.skips.append((cid, reason, extra))


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
        """Cap too low to fund even min_size → no order on either side."""
        s = _make_settings(
            SEED_MAX_USD_PER_SIDE=1.0,        # < min_size×est_price
            SEED_AGGRESSIVE_TICKS=1,
            SEED_SHARES=5,
        )
        client = _StubClient(bal_yes=0.0, bal_no=0.0, mid_yes=0.50)
        seeder = InventorySeeder(client, s, _StubDiscord())
        m = _candidate()
        m.min_size = 5.0
        m.allocated_capital = 200.0

        spent, ok, yes_cost, no_cost = loop.run_until_complete(
            seeder._seed_market(m, budget_remaining=10.0)
        )

        assert spent == 0.0
        assert ok is False
        assert yes_cost == 0.0
        assert no_cost == 0.0
        assert client.posted_orders == [], "Pre-check failed → no orders allowed"

    def test_seed_target_scales_with_alloc_under_cap(self, loop):
        """When alloc/2/price fits under SEED_MAX, DRY_RUN cost reflects that size."""
        s = _make_settings(
            SEED_MAX_USD_PER_SIDE=150.0,
            SEED_AGGRESSIVE_TICKS=1,
            SEED_SHARES=5,
            DRY_RUN=True,
        )
        client = _StubClient(bal_yes=0.0, bal_no=0.0, mid_yes=0.50)
        seeder = InventorySeeder(client, s, _StubDiscord())
        m = _candidate()
        m.min_size = 5.0
        m.allocated_capital = 200.0  # half=100 → ~200 shares @ 0.50

        spent, ok, yes_cost, no_cost = loop.run_until_complete(
            seeder._seed_market(m, budget_remaining=300.0)
        )

        assert ok is True
        # ~200 shares × ~0.51 ≈ $102 per side — well above min_size×price
        assert yes_cost > 50.0
        assert no_cost > 50.0
        assert client.posted_orders == []  # DRY_RUN never posts

    def test_seed_caps_to_max_that_fits(self, loop):
        """Full alloc target > SEED_MAX → seed max affordable, not fail."""
        s = _make_settings(
            SEED_MAX_USD_PER_SIDE=20.0,
            SEED_AGGRESSIVE_TICKS=1,
            SEED_SHARES=5,
            DRY_RUN=True,
        )
        client = _StubClient(bal_yes=0.0, bal_no=0.0, mid_yes=0.50)
        seeder = InventorySeeder(client, s, _StubDiscord())
        m = _candidate()
        m.min_size = 5.0
        m.allocated_capital = 500.0  # ideal ~500 sh ≫ $20 cap

        spent, ok, yes_cost, no_cost = loop.run_until_complete(
            seeder._seed_market(m, budget_remaining=100.0)
        )

        assert ok is True
        # Cap $20 / ~0.55 worst ≈ ~36 shares; cost ≤ ~$20/side
        assert yes_cost <= 20.0 + 0.01
        assert no_cost <= 20.0 + 0.01
        assert yes_cost > 5.0 * 0.4  # more than min_size lot


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
        m = _candidate()
        m.allocated_capital = 100.0  # half=50 → target ~100 @ 0.50; bal 200 ≥

        spent, ok, yes_cost, no_cost = loop.run_until_complete(
            seeder._seed_market(m, budget_remaining=100.0)
        )

        assert ok is True
        assert spent == 0.0
        assert yes_cost == 0.0
        assert no_cost == 0.0
        assert client.posted_orders == []


class TestMatchedSeedShares:
    """Seed N YES + N NO (not dollar-neutral independent sizes)."""

    def test_compute_n_16_84_matched(self):
        # alloc=500 → ideal_yes=500/2/0.16=1562.5, ideal_no=500/2/0.84≈297.6
        # N=min=297; expensive 0.84 under $150 → floor(150/0.84)=178
        n = compute_matched_seed_n(
            500.0, 0.16, 0.84, 150.0, 5.0,
            yes_est_buy=0.17, no_est_buy=0.85,
        )
        assert n == 176  # floor(150/0.85)
        assert abs(n - n) < 1  # matched by construction

    def test_compute_n_25_75(self):
        n = compute_matched_seed_n(
            400.0, 0.25, 0.75, 150.0, 5.0,
            yes_est_buy=0.26, no_est_buy=0.76,
        )
        # ideal_yes=800, ideal_no≈266.67 → min 266; cap 150/0.76≈197
        assert n == 197
        assert n * 0.76 <= 150.0 + 0.01

    def test_compute_n_50_50(self):
        n = compute_matched_seed_n(
            200.0, 0.50, 0.50, 150.0, 5.0,
            yes_est_buy=0.51, no_est_buy=0.51,
        )
        # ideal=200; cap 150/0.51≈294 → N=200
        assert n == 200

    def test_expensive_side_spends_more_usd(self, loop):
        s = _make_settings(
            SEED_MAX_USD_PER_SIDE=150.0,
            SEED_AGGRESSIVE_TICKS=1,
            SEED_SHARES=5,
            DRY_RUN=True,
            RESOLUTION_MAX_LOSS_USD=50.0,
        )
        client = _StubClient(bal_yes=0.0, bal_no=0.0, mid_yes=0.16)
        seeder = InventorySeeder(client, s, _StubDiscord())
        paper = PaperBroker(10_000.0)
        seeder.set_paper(paper)
        m = _candidate("ossoff")
        m.min_size = 5.0
        m.allocated_capital = 500.0
        m.midpoint = 0.16

        spent, ok, yes_cost, no_cost = loop.run_until_complete(
            seeder._seed_market(m, budget_remaining=300.0)
        )
        assert ok is True
        yes_sz = paper.inventory_size("y_ossoff")
        no_sz = paper.inventory_size("n_ossoff")
        assert abs(yes_sz - no_sz) <= 1.0
        assert abs(yes_sz - no_sz) < 50.0  # under RESOLUTION_MAX_LOSS
        # Expensive side (NO @ ~0.84) costs more USD than cheap YES
        assert no_cost > yes_cost
        # Pair cost ≈ N dollars (yes_px + no_px ≈ 1)
        assert abs((yes_cost + no_cost) - yes_sz) < yes_sz * 0.15 + 1.0

    def test_50_50_matched_equal_cost(self, loop):
        s = _make_settings(DRY_RUN=True, RESOLUTION_MAX_LOSS_USD=50.0)
        client = _StubClient(bal_yes=0.0, bal_no=0.0, mid_yes=0.50)
        seeder = InventorySeeder(client, s, _StubDiscord())
        paper = PaperBroker(10_000.0)
        seeder.set_paper(paper)
        m = _candidate()
        m.allocated_capital = 200.0

        spent, ok, yes_cost, no_cost = loop.run_until_complete(
            seeder._seed_market(m, budget_remaining=300.0)
        )
        assert ok is True
        yes_sz = paper.inventory_size("y_c1")
        no_sz = paper.inventory_size("n_c1")
        assert abs(yes_sz - no_sz) <= 1.0
        assert abs(yes_cost - no_cost) < 1.0

    def test_cost_cap_leaves_zero_inventory(self, loop):
        """min_size=200 vs expensive-side ~$162 → cost_cap, 0/0."""
        s = _make_settings(
            SEED_MAX_USD_PER_SIDE=150.0,
            SEED_SHARES=5,
            DRY_RUN=True,
            RESOLUTION_MAX_LOSS_USD=50.0,
        )
        client = _StubClient(bal_yes=0.0, bal_no=0.0, mid_yes=0.20)
        seeder = InventorySeeder(client, s, _StubDiscord())
        paper = PaperBroker(10_000.0)
        seeder.set_paper(paper)
        m = _candidate("rubio")
        m.min_size = 200.0  # expensive NO @0.80 → 150/0.81 < 200
        m.allocated_capital = 500.0

        spent, ok, yes_cost, no_cost = loop.run_until_complete(
            seeder._seed_market(m, budget_remaining=300.0)
        )
        assert ok is False
        assert spent == 0.0
        assert yes_cost == 0.0 and no_cost == 0.0
        assert paper.inventory_size("y_rubio") == 0.0
        assert paper.inventory_size("n_rubio") == 0.0
        assert client.posted_orders == []

    def test_preflight_never_posts_breach(self, loop):
        """Existing unmatched inventory that would breach → no posts."""
        s = _make_settings(
            DRY_RUN=True,
            RESOLUTION_MAX_LOSS_USD=50.0,
            SEED_SHARES=5,
            SEED_MAX_USD_PER_SIDE=150.0,
        )
        # Already hold 200 YES / 0 NO — buying up to N leaves unmatched ≥ 200
        client = _StubClient(bal_yes=200.0, bal_no=0.0, mid_yes=0.50)
        seeder = InventorySeeder(client, s, _StubDiscord())
        m = _candidate()
        m.allocated_capital = 100.0  # N≈100 after caps

        spent, ok, yes_cost, no_cost = loop.run_until_complete(
            seeder._seed_market(m, budget_remaining=300.0)
        )
        assert ok is False
        assert spent == 0.0
        assert client.posted_orders == []

    def test_sticky_max_loss_unmatched_zero_still_seeds(self, loop, tmp_path):
        """Legacy sticky max_loss must not block matched N/N (unmatched=0)."""
        from core.resolution_guard import ResolutionGuard
        from core.paper_broker import PaperBroker

        path = tmp_path / "delist.json"
        path.write_text(
            '{"delisted": {"cond_test_abc": "max_loss"}, "updated_at": "x"}'
        )
        s = _make_settings(
            DRY_RUN=True,
            RESOLUTION_MAX_LOSS_USD=50.0,
            SEED_SHARES=5,
            SEED_MAX_USD_PER_SIDE=150.0,
        )
        guard = ResolutionGuard(s, persist_path=path)
        assert guard.is_delisted("cond_test_abc") is False  # scrubbed

        client = _StubClient(bal_yes=0.0, bal_no=0.0, mid_yes=0.50)
        paper = PaperBroker(10_000.0)
        seeder = InventorySeeder(client, s, _StubDiscord(), guard)
        seeder.set_paper(paper)
        m = _candidate()
        m.condition_id = "cond_test_abc"
        m.allocated_capital = 100.0
        m.min_size = 5.0
        m.midpoint = 0.50

        spent, ok, yes_cost, no_cost = loop.run_until_complete(
            seeder._seed_market(m, budget_remaining=500.0)
        )
        assert ok is True
        assert spent > 0.0
        assert "preflight_max_loss" not in str(ok)

    def test_dry_run_false_same_n_math(self):
        """DRY_RUN flag must not change matched-N arithmetic."""
        n_a = compute_matched_seed_n(
            500.0, 0.16, 0.84, 150.0, 5.0,
            yes_est_buy=0.17, no_est_buy=0.85,
        )
        n_b = compute_matched_seed_n(
            500.0, 0.16, 0.84, 150.0, 5.0,
            yes_est_buy=0.17, no_est_buy=0.85,
        )
        assert n_a == n_b == 176
        s_live = _make_settings(DRY_RUN=False, RESOLUTION_MAX_LOSS_USD=50.0)
        s_paper = _make_settings(DRY_RUN=True, RESOLUTION_MAX_LOSS_USD=50.0)
        assert s_live.DRY_RUN is False and s_paper.DRY_RUN is True
        seeder_live = InventorySeeder(_StubClient(), s_live, _StubDiscord())
        seeder_paper = InventorySeeder(_StubClient(), s_paper, _StubDiscord())
        m = _candidate()
        m.allocated_capital = 500.0
        m.midpoint = 0.16
        n_live = seeder_live._matched_seed_n(m, 0.16, 0.84, 0.17, 0.85, 150.0)
        n_paper = seeder_paper._matched_seed_n(m, 0.16, 0.84, 0.17, 0.85, 150.0)
        assert n_live == n_paper


class TestFlattenOnDelist:
    def test_paper_flatten_zeros_inventory_credits_cash(self):
        paper = PaperBroker(1_000.0)
        credited = paper.seed_market("cid", "y", "n", 100.0, 100.0, 0.16, 0.84)
        assert credited is not None
        cash_after_seed = paper.cash
        assert paper.inventory_size("y") == 100.0
        assert paper.inventory_size("n") == 100.0

        inv = InventoryTracker(_make_settings())
        pos = inv.ensure_position("cid", "y", "n")
        pos.yes_size = 100.0
        pos.no_size = 100.0
        pos.yes_cost = credited[0]
        pos.no_cost = credited[1]

        result = paper.flatten_market("cid", "y", "n", 0.16, 0.84)
        inv.flatten_position("cid", 0.16, 0.84)

        assert paper.inventory_size("y") == 0.0
        assert paper.inventory_size("n") == 0.0
        assert abs(paper.cash - (cash_after_seed + result["yes_proceeds"] + result["no_proceeds"])) < 1e-6
        assert paper.cash > cash_after_seed
        pos2 = inv.get_position("cid")
        assert pos2 is not None
        assert pos2.yes_size == 0.0 and pos2.no_size == 0.0

    def test_seeder_flatten_dry_run(self, loop):
        s = _make_settings(DRY_RUN=True)
        paper = PaperBroker(5_000.0)
        paper.seed_market("c1", "y_c1", "n_c1", 50.0, 50.0, 0.40, 0.60)
        inv = InventoryTracker(s)
        pos = inv.ensure_position("c1", "y_c1", "n_c1")
        pos.yes_size = 50.0
        pos.no_size = 50.0
        pos.yes_cost = 20.0
        pos.no_cost = 30.0

        seeder = InventorySeeder(_StubClient(), s, _StubDiscord())
        seeder.set_paper(paper)
        result = loop.run_until_complete(
            seeder.flatten_market(_candidate(), 0.40, inv)
        )
        assert paper.inventory_size("y_c1") == 0.0
        assert paper.inventory_size("n_c1") == 0.0
        assert result["yes_size"] == 50.0
        assert inv.get_position("c1").yes_size == 0.0
        assert inv.get_position("c1").no_size == 0.0


class TestRebalanceUnmatched:
    def test_paper_rebalance_300_200_keeps_at_limit(self):
        paper = PaperBroker(1_000.0)
        paper.seed_market("cid", "y", "n", 300.0, 200.0, 0.40, 0.60)
        cash_before = paper.cash
        result = paper.rebalance_unmatched("cid", "y", "n", 0.40, 0.60, limit=50.0)
        assert result["heavy"] == "YES"
        assert result["surplus"] == pytest.approx(100.0)
        assert result["sold"] == pytest.approx(50.0)
        assert paper.inventory_size("y") == pytest.approx(250.0)
        assert paper.inventory_size("n") == pytest.approx(200.0)
        assert paper.cash == pytest.approx(cash_before + 20.0)

    def test_paper_rebalance_0_311_keeps_50(self):
        paper = PaperBroker(1_000.0)
        paper.seed_market("cid", "y", "n", 0.0, 311.0, 0.40, 0.60)
        cash_before = paper.cash
        result = paper.rebalance_unmatched("cid", "y", "n", 0.40, 0.60, limit=50.0)
        assert result["heavy"] == "NO"
        assert result["sold"] == pytest.approx(261.0)
        assert paper.inventory_size("y") == pytest.approx(0.0)
        assert paper.inventory_size("n") == pytest.approx(50.0)
        assert paper.cash == pytest.approx(cash_before + 261.0 * 0.60)
        assert not (result["yes_after"] == 0 and result["no_after"] == 0)

    def test_seeder_rebalance_dry_run(self, loop):
        s = _make_settings(DRY_RUN=True)
        paper = PaperBroker(5_000.0)
        paper.seed_market("c1", "y_c1", "n_c1", 300.0, 200.0, 0.40, 0.60)
        inv = InventoryTracker(s)
        pos = inv.ensure_position("c1", "y_c1", "n_c1")
        pos.yes_size = 300.0
        pos.no_size = 200.0
        pos.yes_cost = 120.0
        pos.no_cost = 120.0

        tape = _FakeTape()
        seeder = InventorySeeder(_StubClient(), s, _StubDiscord())
        seeder.set_paper(paper)
        seeder.set_tape(tape)
        result = loop.run_until_complete(
            seeder.rebalance_unmatched(_candidate(), 0.40, inv)
        )
        assert paper.inventory_size("y_c1") == pytest.approx(250.0)
        assert paper.inventory_size("n_c1") == pytest.approx(200.0)
        assert inv.get_position("c1").yes_size == pytest.approx(250.0)
        assert inv.get_position("c1").no_size == pytest.approx(200.0)
        assert result["surplus"] == pytest.approx(100.0)
        assert any(r == "rebalance_surplus" for _, r, _ in tape.skips)
        assert not any(r == "resolution_flatten" for _, r, _ in tape.skips)
