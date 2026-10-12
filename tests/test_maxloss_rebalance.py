"""TASK-MAXLOSS-REBALANCE — routing, escape hatch, matched N/N preflight."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from config.markets_filter import MarketCandidate
from config.settings import Settings
from core.inventory import InventoryTracker
from core.inventory_seeder import InventorySeeder
from core.market_manager import MarketManager, MarketState
from core.paper_broker import PaperBroker
from core.resolution_guard import ResolutionGuard, exit_mode_for_reason


def _make_settings(**overrides) -> Settings:
    defaults = {
        "PRIVATE_KEY": "0x" + "ab" * 32,
        "TOTAL_CAPITAL": 10_000.0,
        "DRY_RUN": True,
        "RESOLUTION_MAX_LOSS_USD": 50.0,
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


class _StubDiscord:
    def __init__(self) -> None:
        self.alerts = []

    async def send_alert(self, **kwargs):
        self.alerts.append(kwargs)
        return None


class _FakeTape:
    def __init__(self) -> None:
        self.skips = []

    def skip(self, cid, reason, **extra):
        self.skips.append((cid, reason, extra))


@pytest.fixture
def loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()


class TestMaxLossStaysScoring:
    def test_rebalance_does_not_inactivate(self, loop, tmp_path):
        s = _make_settings()
        paper = PaperBroker(5_000.0)
        paper.seed_market("c1", "y_c1", "n_c1", 300.0, 200.0, 0.40, 0.60)
        inv = InventoryTracker(s)
        pos = inv.ensure_position("c1", "y_c1", "n_c1")
        pos.yes_size = 300.0
        pos.no_size = 200.0
        pos.yes_cost = 120.0
        pos.no_cost = 120.0

        guard = ResolutionGuard(s, persist_path=tmp_path / "delist.json")
        delist, reason = guard.should_delist("c1", 300.0, 200.0, 0.40)
        assert delist is True
        assert reason == "max_loss"
        assert exit_mode_for_reason(reason) == "rebalance"

        seeder = InventorySeeder(None, s, _StubDiscord())  # type: ignore[arg-type]
        seeder.set_paper(paper)
        tape = _FakeTape()
        seeder.set_tape(tape)
        loop.run_until_complete(seeder.rebalance_unmatched(_candidate(), 0.40, inv))

        pos2 = inv.get_position("c1")
        still, unmatched, _ = guard.max_loss_check("c1", pos2.yes_size, pos2.no_size)
        assert still is False
        assert unmatched == pytest.approx(50.0)
        assert pos2.yes_size == pytest.approx(250.0)
        assert pos2.no_size == pytest.approx(200.0)

        mgr = MarketManager.__new__(MarketManager)
        mgr._states = {}
        mgr._sticky_inactive = set()
        mgr._tape = None
        # Simulate success path: do NOT mark_resolution_inactive
        assert mgr.get_state("c1") != MarketState.INACTIVE
        mgr.set_state("c1", MarketState.SCORING)
        assert mgr.get_state("c1") == MarketState.SCORING
        assert not any(r == "resolution_flatten" for _, r, _ in tape.skips)
        assert any(r == "rebalance_surplus" for _, r, _ in tape.skips)

    def test_rebalance_0_311_keeps_limit_not_flat(self, loop):
        """0/311 → sell 261 NO, keep 50. Stay SCORING — never 0/0."""
        s = _make_settings()
        paper = PaperBroker(5_000.0)
        paper.seed_market("c1", "y_c1", "n_c1", 0.0, 311.0, 0.40, 0.60)
        inv = InventoryTracker(s)
        pos = inv.ensure_position("c1", "y_c1", "n_c1")
        pos.yes_size = 0.0
        pos.no_size = 311.0
        pos.no_cost = 311.0 * 0.60
        cash_before = paper.cash

        guard = ResolutionGuard(s)
        delist, reason = guard.should_delist("c1", 0.0, 311.0, 0.40)
        assert delist is True
        assert reason == "max_loss"
        assert exit_mode_for_reason(reason) == "rebalance"

        seeder = InventorySeeder(None, s, _StubDiscord())  # type: ignore[arg-type]
        seeder.set_paper(paper)
        tape = _FakeTape()
        seeder.set_tape(tape)
        loop.run_until_complete(seeder.rebalance_unmatched(_candidate(), 0.40, inv))

        pos2 = inv.get_position("c1")
        still, unmatched, _ = guard.max_loss_check("c1", pos2.yes_size, pos2.no_size)
        assert still is False
        assert unmatched == pytest.approx(50.0)
        assert pos2.yes_size == pytest.approx(0.0)
        assert pos2.no_size == pytest.approx(50.0)
        assert paper.inventory_size("y_c1") == pytest.approx(0.0)
        assert paper.inventory_size("n_c1") == pytest.approx(50.0)
        assert paper.cash == pytest.approx(cash_before + 261.0 * 0.60)
        assert "c1" in seeder._block_reseed
        assert not any(r == "resolution_flatten" for _, r, _ in tape.skips)

    def test_no_reseed_after_onesided_rebalance(self, loop):
        s = _make_settings(DRY_RUN=True)
        paper = PaperBroker(5_000.0)
        paper.seed_market("c1", "y_c1", "n_c1", 0.0, 50.0, 0.40, 0.60)
        seeder = InventorySeeder(None, s, _StubDiscord())  # type: ignore[arg-type]
        seeder.set_paper(paper)
        seeder.note_onesided_rebalance("c1")
        spent, ok, yc, nc = loop.run_until_complete(
            seeder._seed_market(_candidate(), 500.0)
        )
        assert ok is True
        assert spent == 0.0
        assert paper.inventory_size("y_c1") == pytest.approx(0.0)
        assert paper.inventory_size("n_c1") == pytest.approx(50.0)

    def test_extreme_mid_still_full_flatten(self, loop):
        s = _make_settings()
        paper = PaperBroker(5_000.0)
        paper.seed_market("c1", "y_c1", "n_c1", 200.0, 200.0, 0.03, 0.97)
        inv = InventoryTracker(s)
        pos = inv.ensure_position("c1", "y_c1", "n_c1")
        pos.yes_size = 200.0
        pos.no_size = 200.0
        pos.yes_cost = 6.0
        pos.no_cost = 194.0

        guard = ResolutionGuard(s)
        delist, reason = guard.should_delist("c1", 200.0, 200.0, 0.03)
        assert delist is True
        assert reason == "extreme_mid"
        assert exit_mode_for_reason(reason) == "flatten"

        seeder = InventorySeeder(None, s, _StubDiscord())  # type: ignore[arg-type]
        seeder.set_paper(paper)
        tape = _FakeTape()
        seeder.set_tape(tape)
        loop.run_until_complete(seeder.flatten_market(_candidate(), 0.03, inv))
        assert inv.get_position("c1").yes_size == 0.0
        assert inv.get_position("c1").no_size == 0.0
        assert paper.inventory_size("y_c1") == 0.0
        assert any(r == "resolution_flatten" for _, r, _ in tape.skips)

    def test_escape_hatch_flatten_when_rebalance_leaves_unmatched(self, loop):
        s = _make_settings(DRY_RUN=True)
        paper = PaperBroker(5_000.0)
        # Paper holds almost-matched; tracker is 300/200 — sell_target on
        # paper surplus (10) is 0 under limit 50, so inv stays over limit.
        paper.seed_market("c1", "y_c1", "n_c1", 210.0, 200.0, 0.40, 0.60)
        inv = InventoryTracker(s)
        pos = inv.ensure_position("c1", "y_c1", "n_c1")
        pos.yes_size = 300.0
        pos.no_size = 200.0
        pos.yes_cost = 120.0
        pos.no_cost = 120.0

        seeder = InventorySeeder(None, s, _StubDiscord())  # type: ignore[arg-type]
        seeder.set_paper(paper)
        loop.run_until_complete(seeder.rebalance_unmatched(_candidate(), 0.40, inv))
        pos2 = inv.get_position("c1")
        guard = ResolutionGuard(s)
        still, unmatched, _ = guard.max_loss_check("c1", pos2.yes_size, pos2.no_size)
        assert still is True
        assert unmatched > 50.0

        loop.run_until_complete(seeder.flatten_market(_candidate(), 0.40, inv))
        assert inv.get_position("c1").yes_size == 0.0
        assert inv.get_position("c1").no_size == 0.0

    def test_seeder_matched_nn_unmatched_zero(self):
        s = _make_settings()
        inv = InventoryTracker(s)
        pos = inv.ensure_position("c1", "y_c1", "n_c1")
        pos.yes_size = 200.0
        pos.no_size = 200.0
        guard = ResolutionGuard(s)
        breached, unmatched, _ = guard.max_loss_check("c1", pos.yes_size, pos.no_size)
        assert breached is False
        assert unmatched == pytest.approx(0.0)
        delist, reason = guard.should_delist("c1", 200.0, 200.0, 0.50)
        assert delist is False
        assert reason is None
