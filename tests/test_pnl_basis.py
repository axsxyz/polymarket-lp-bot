"""TASK-PNL-BASIS — cost write-once; no phantom realised on ASK/rebalance."""

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


class TestUsIranPnLBasis:
    """Seed + BID/ASK + keep-50 rebalance must not mint +$150 realised."""

    def test_realised_bounded_after_fills_and_rebalance(self):
        inv = InventoryTracker(_make_settings())
        tape = _FakeTape()
        inv.set_tape(tape)
        cid = "us_iran"
        y_tid, n_tid = "tok_yes", "tok_no"
        inv.ensure_position(cid, y_tid, n_tid)
        yes_mid, no_mid = 0.615, 0.385
        inv.update_midpoints({y_tid: yes_mid, n_tid: no_mid})

        seed_n = 244.0
        seed_yes_usd = seed_n * yes_mid
        seed_no_usd = seed_n * no_mid
        pos = inv.positions[cid]
        pos.yes_size = seed_n
        pos.no_size = seed_n
        pos.yes_cost = seed_yes_usd
        pos.no_cost = seed_no_usd

        inv.record_fills([
            _fill(y_tid, cid, "BID", 0.60, 200.0),
            _fill(n_tid, cid, "ASK", 0.40, 200.0),
        ])
        pos = inv.get_position(cid)
        assert pos is not None
        assert pos.yes_size == pytest.approx(444.0)
        assert pos.no_size == pytest.approx(44.0)

        # Simulate buggy second sync: paper sizes post-fill, seed_costs original.
        paper = _FakePaper(pos.yes_size, pos.no_size, y_tid, n_tid)
        market = SimpleNamespace(
            condition_id=cid, token_id_yes=y_tid, token_id_no=n_tid,
        )
        seed_costs = {cid: {"yes": seed_yes_usd, "no": seed_no_usd}}
        cost_yes_before_sync = pos.yes_cost
        cost_no_before_sync = pos.no_cost
        _credit_paper_seed(inv, market, paper, seed_costs)
        pos = inv.get_position(cid)
        assert pos.yes_cost == pytest.approx(cost_yes_before_sync)
        assert pos.no_cost == pytest.approx(cost_no_before_sync)
        assert pos.yes_cost != pytest.approx(seed_yes_usd)

        surplus = abs(pos.yes_size - pos.no_size)
        sell = max(0.0, surplus - 50.0)
        assert sell > 0
        inv.reduce_position(cid, "YES", sell, yes_mid)

        pos = inv.get_position(cid)
        assert abs(inv.realised_pnl) < 15.0
        assert abs(pos.yes_size - pos.no_size) <= 50.0 + 1e-6
        assert not (pos.yes_size <= 1e-9 and pos.no_size <= 1e-9)

    def test_second_sync_does_not_restore_seed_cost(self):
        inv = InventoryTracker(_make_settings())
        cid = "c1"
        y_tid, n_tid = "y", "n"
        inv.ensure_position(cid, y_tid, n_tid)
        seed_yes = 244.0 * 0.615
        seed_no = 244.0 * 0.385
        pos = inv.positions[cid]
        pos.yes_size = 244.0
        pos.no_size = 244.0
        pos.yes_cost = seed_yes
        pos.no_cost = seed_no

        inv.record_fills([_fill(y_tid, cid, "BID", 0.60, 200.0)])
        pos = inv.get_position(cid)
        assert pos.yes_cost == pytest.approx(seed_yes + 120.0)

        paper = _FakePaper(pos.yes_size, pos.no_size, y_tid, n_tid)
        market = SimpleNamespace(
            condition_id=cid, token_id_yes=y_tid, token_id_no=n_tid,
        )
        _credit_paper_seed(
            inv, market, paper, {cid: {"yes": seed_yes, "no": seed_no}},
        )
        assert inv.get_position(cid).yes_cost == pytest.approx(seed_yes + 120.0)


class TestMissingCostBasis:
    def test_ask_with_zero_cost_inits_at_mid_no_phantom(self):
        inv = InventoryTracker(_make_settings())
        tape = _FakeTape()
        inv.set_tape(tape)
        cid = "c1"
        inv.ensure_position(cid, "tok_yes", "tok_no")
        inv.update_midpoints({"tok_yes": 0.50, "tok_no": 0.50})
        pos = inv.positions[cid]
        pos.yes_size = 100.0
        pos.yes_cost = 0.0

        inv.record_fills([_fill("tok_yes", cid, "ASK", 0.60, 50.0)])

        assert any(r == "cost_basis_missing" for _, r, _ in tape.skips)
        # Init at mid 0.50 → sell @ 0.60 realises (0.60-0.50)*50 = 5, not 30.
        assert inv.realised_pnl == pytest.approx(5.0)
        pos = inv.get_position(cid)
        assert pos.yes_size == pytest.approx(50.0)
        assert pos.yes_cost == pytest.approx(25.0)


class TestTokenMapping:
    def test_unknown_token_skipped(self):
        inv = InventoryTracker(_make_settings())
        inv.ensure_position("c1", "tok_yes", "tok_no")
        inv.record_fills([_fill("tok_other", "c1", "BID", 0.55, 10.0)])
        pos = inv.get_position("c1")
        assert pos.yes_size == 0.0
        assert pos.no_size == 0.0
        assert inv.realised_pnl == 0.0

    def test_no_guess_when_only_yes_token_registered(self):
        """Unknown token must not be treated as NO via elif yes_token_id fallback."""
        inv = InventoryTracker(_make_settings())
        inv.ensure_position("c1", yes_token_id="tok_yes")  # no no_token_id
        inv.record_fills([_fill("tok_mystery", "c1", "BID", 0.40, 25.0)])
        pos = inv.get_position("c1")
        assert pos.yes_size == 0.0
        assert pos.no_size == 0.0
