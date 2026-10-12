"""TASK-PNL-TOPUP — second seed must ADD USD; no phantom realised on ASK."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from config.settings import Settings
from core.inventory import InventoryTracker
from core.order_manager import FillEvent
from main import _credit_paper_seed


def _make_settings(**overrides) -> Settings:
    defaults = {
        "PRIVATE_KEY": "0x" + "ab" * 32,
        "TOTAL_CAPITAL": 10_000.0,
        "DRY_RUN": True,
        "RESOLUTION_MAX_LOSS_USD": 50.0,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _fill(
    token_id: str,
    condition_id: str = "us_iran",
    side: str = "BID",
    price: float = 0.50,
    size: float = 10.0,
) -> FillEvent:
    return FillEvent(
        token_id=token_id,
        condition_id=condition_id,
        side=side,
        price=price,
        filled_size=size,
    )


class _FakePaper:
    def __init__(self, yes: float, no: float, yes_tid: str, no_tid: str) -> None:
        self._sizes = {yes_tid: yes, no_tid: no}

    def inventory_size(self, token_id: str) -> float:
        return float(self._sizes.get(token_id, 0.0))

    def set_size(self, token_id: str, size: float) -> None:
        self._sizes[token_id] = float(size)


class _FakeTape:
    def __init__(self) -> None:
        self.skips: list = []

    def skip(self, cid: str, reason: str, **extra) -> None:
        self.skips.append((cid, reason, extra))


class TestIranTopUpPnL:
    """248→347 top-up must keep avg ~seed; ASK @ 0.41 realises dollars not +$23."""

    def test_topup_adds_cost_ask_and_rebalance_bounded(self):
        inv = InventoryTracker(_make_settings())
        tape = _FakeTape()
        inv.set_tape(tape)
        cid = "us_iran"
        y_tid, n_tid = "tok_yes", "tok_no"
        inv.ensure_position(cid, y_tid, n_tid)
        yes_mid, no_mid = 0.615, 0.405
        inv.update_midpoints({y_tid: yes_mid, n_tid: no_mid})

        market = SimpleNamespace(
            condition_id=cid, token_id_yes=y_tid, token_id_no=n_tid,
        )

        # First seed 248/248 @ tape costs
        paper = _FakePaper(248.0, 248.0, y_tid, n_tid)
        seed1 = {cid: {"yes": 152.52, "no": 100.44}}
        _credit_paper_seed(inv, market, paper, seed1)
        pos = inv.get_position(cid)
        assert pos.yes_size == pytest.approx(248.0)
        assert pos.no_size == pytest.approx(248.0)
        assert pos.yes_cost == pytest.approx(152.52)
        assert pos.no_cost == pytest.approx(100.44)

        # Refresh top-up +99/+99 (seeder returns THIS call's spend only)
        paper.set_size(y_tid, 347.0)
        paper.set_size(n_tid, 347.0)
        seed2 = {cid: {"yes": 60.885, "no": 40.095}}
        _credit_paper_seed(inv, market, paper, seed2)
        pos = inv.get_position(cid)
        assert pos.yes_size == pytest.approx(347.0)
        assert pos.no_size == pytest.approx(347.0)
        assert pos.yes_cost == pytest.approx(213.405)
        assert pos.no_cost == pytest.approx(140.535)
        assert pos.yes_cost / pos.yes_size == pytest.approx(0.615, abs=0.002)
        assert pos.no_cost / pos.no_size == pytest.approx(0.405, abs=0.002)
        # Must NOT be stuck at first-seed USD / diluted avg
        assert pos.yes_cost != pytest.approx(152.52)
        assert pos.yes_cost / pos.yes_size != pytest.approx(152.52 / 347.0)

        # ASK 200 NO @ 0.41 — vs avg 0.405 → ~$1, not +$23
        pnl_before = inv.realised_pnl
        inv.record_fills([_fill(n_tid, cid, "ASK", 0.41, 200.0)])
        ask_pnl = inv.realised_pnl - pnl_before
        assert abs(ask_pnl) < 5.0, f"phantom ASK realised={ask_pnl}"

        pos = inv.get_position(cid)
        assert pos.yes_size == pytest.approx(347.0)
        assert pos.no_size == pytest.approx(147.0)

        # keep-50 rebalance: sell 150 YES @ 0.595 vs avg ~0.615 → small loss
        pnl_before = inv.realised_pnl
        sell = 150.0
        inv.reduce_position(cid, "YES", sell, 0.595)
        rebal_pnl = inv.realised_pnl - pnl_before
        assert abs(rebal_pnl) < 10.0, f"phantom rebalance realised={rebal_pnl}"

        pos = inv.get_position(cid)
        assert abs(pos.yes_size - pos.no_size) <= 50.0 + 1e-6
        assert not (pos.yes_size <= 1e-9 and pos.no_size <= 1e-9)

        # Third sync with *original* seed USD must not reset cost down
        cost_yes = pos.yes_cost
        cost_no = pos.no_cost
        paper.set_size(y_tid, pos.yes_size)
        paper.set_size(n_tid, pos.no_size)
        _credit_paper_seed(inv, market, paper, seed1)
        pos = inv.get_position(cid)
        assert pos.yes_cost == pytest.approx(cost_yes)
        assert pos.no_cost == pytest.approx(cost_no)
        assert pos.yes_cost != pytest.approx(152.52)

    def test_size_growth_without_seed_usd_tapes_gap(self):
        inv = InventoryTracker(_make_settings())
        tape = _FakeTape()
        inv.set_tape(tape)
        cid = "gap1"
        y_tid, n_tid = "y", "n"
        inv.ensure_position(cid, y_tid, n_tid)
        inv.update_midpoints({y_tid: 0.60, n_tid: 0.40})
        pos = inv.positions[cid]
        pos.yes_size = 100.0
        pos.no_size = 100.0
        pos.yes_cost = 60.0
        pos.no_cost = 40.0

        paper = _FakePaper(150.0, 150.0, y_tid, n_tid)
        market = SimpleNamespace(
            condition_id=cid, token_id_yes=y_tid, token_id_no=n_tid,
        )
        _credit_paper_seed(inv, market, paper, {})  # size grew, zero seeder USD
        pos = inv.get_position(cid)
        assert pos.yes_size == pytest.approx(150.0)
        assert pos.yes_cost == pytest.approx(60.0 + 50.0 * 0.60)
        assert pos.no_cost == pytest.approx(40.0 + 50.0 * 0.40)
        assert any(r == "seed_cost_gap" for _, r, _ in tape.skips)

    def test_add_seed_costs_never_replaces(self):
        inv = InventoryTracker(_make_settings())
        inv.ensure_position("c1", "y", "n")
        pos = inv.positions["c1"]
        pos.yes_cost = 100.0
        pos.no_cost = 50.0
        inv.add_seed_costs("c1", yes_cost=25.0, no_cost=10.0)
        assert pos.yes_cost == pytest.approx(125.0)
        assert pos.no_cost == pytest.approx(60.0)
        inv.apply_seed_costs_once("c1", yes_cost=999.0, no_cost=999.0)
        assert pos.yes_cost == pytest.approx(125.0)
        assert pos.no_cost == pytest.approx(60.0)
