"""In-process paper book for DRY_RUN.

Never talks to the network. Cash, seed inventory, resting GTCs, and
maker fills live here. InventoryTracker remains the PnL/skew source of
truth; this broker is the paper cash + book used for pre-checks and
matching against live WS prints.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from loguru import logger

from core.order_manager import FillEvent

_SIZE_EPS = 1e-9


@dataclass
class PaperRestingOrder:
    order_id: str
    condition_id: str
    token_id: str
    side: str
    price: float
    size: float
    posted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class PaperBroker:
    """Session-scoped paper cash + GTC book. Writes never hit the CLOB."""

    def __init__(self, cash: float) -> None:
        self._cash = float(cash)
        self._committed = 0.0
        self._inventory: Dict[str, float] = {}
        self._ask_reserved: Dict[str, float] = {}
        self._orders: Dict[str, PaperRestingOrder] = {}
        self._token_cid: Dict[str, str] = {}
        self._id_seq = itertools.count(1)
        self._fill_seq = itertools.count(1)
        self._starting_cash = float(cash)

    def next_order_id(self) -> str:
        return f"paper_{next(self._id_seq)}"

    @property
    def starting_cash(self) -> float:
        return self._starting_cash

    @property
    def cash(self) -> float:
        """Total paper cash still held (free + BID reserves)."""
        return self._cash + self._committed

    @property
    def committed(self) -> float:
        return self._committed

    @property
    def spendable(self) -> float:
        return self._cash

    def inventory_size(self, token_id: str) -> float:
        return float(self._inventory.get(token_id, 0.0))

    def free_ask_size(self, token_id: str) -> float:
        held = self._inventory.get(token_id, 0.0)
        reserved = self._ask_reserved.get(token_id, 0.0)
        return max(0.0, held - reserved)

    def resting_sizes(self) -> Dict[str, float]:
        return {oid: rec.size for oid, rec in self._orders.items()}

    def precheck_bid(self, usd: float) -> bool:
        return float(usd) <= self._cash + _SIZE_EPS

    def precheck_ask(self, token_id: str, size: float) -> bool:
        return float(size) <= self.free_ask_size(token_id) + _SIZE_EPS

    def seed_market(
        self,
        condition_id: str,
        yes_token_id: str,
        no_token_id: str,
        yes_size: float,
        no_size: float,
        yes_price: float,
        no_price: float,
    ) -> Optional[tuple[float, float]]:
        """Credit YES+NO atomically and debit cash. Both sides or neither."""
        if yes_size < 0 or no_size < 0:
            return None
        if yes_size <= _SIZE_EPS and no_size <= _SIZE_EPS:
            return (0.0, 0.0)
        yes_cost = float(yes_size) * float(yes_price)
        no_cost = float(no_size) * float(no_price)
        total = yes_cost + no_cost
        if total > self._cash + _SIZE_EPS:
            logger.warning(
                "Paper seed {} rejected — need ${:.2f} spendable ${:.2f}",
                condition_id[:12], total, self._cash,
            )
            return None
        self._cash -= total
        if yes_size > _SIZE_EPS:
            self._inventory[yes_token_id] = (
                self._inventory.get(yes_token_id, 0.0) + float(yes_size)
            )
            self._token_cid[yes_token_id] = condition_id
        if no_size > _SIZE_EPS:
            self._inventory[no_token_id] = (
                self._inventory.get(no_token_id, 0.0) + float(no_size)
            )
            self._token_cid[no_token_id] = condition_id
        return (yes_cost, no_cost)

    def flatten_market(
        self,
        condition_id: str,
        yes_token_id: str,
        no_token_id: str,
        yes_price: float,
        no_price: float,
    ) -> Dict[str, float]:
        """Paper-sell all YES+NO for a cid: inventory → 0, cash credited."""
        self.cancel_market(condition_id)
        yes_sz = float(self._inventory.get(yes_token_id, 0.0)) if yes_token_id else 0.0
        no_sz = float(self._inventory.get(no_token_id, 0.0)) if no_token_id else 0.0
        yes_proceeds = yes_sz * float(yes_price)
        no_proceeds = no_sz * float(no_price)
        if yes_token_id:
            self._inventory[yes_token_id] = 0.0
            self._ask_reserved[yes_token_id] = 0.0
        if no_token_id:
            self._inventory[no_token_id] = 0.0
            self._ask_reserved[no_token_id] = 0.0
        self._cash += yes_proceeds + no_proceeds
        return {
            "yes_size": yes_sz,
            "no_size": no_sz,
            "yes_proceeds": yes_proceeds,
            "no_proceeds": no_proceeds,
        }

    def rebalance_unmatched(
        self,
        condition_id: str,
        yes_token_id: str,
        no_token_id: str,
        yes_price: float,
        no_price: float,
        limit: float = 50.0,
    ) -> Dict[str, float]:
        """Paper-sell heavy surplus until unmatched ≤ ``limit``. Never wipe 0/N→0/0."""
        self.cancel_market(condition_id)
        yes_before = float(self._inventory.get(yes_token_id, 0.0)) if yes_token_id else 0.0
        no_before = float(self._inventory.get(no_token_id, 0.0)) if no_token_id else 0.0
        surplus = abs(yes_before - no_before)
        lim = max(0.0, float(limit))
        # Sell only the overshoot past the unmatched cap (keep ≤ limit on heavy).
        sell_target = max(0.0, surplus - lim)
        heavy = ""
        sold = 0.0
        yes_proceeds = 0.0
        no_proceeds = 0.0
        if sell_target > _SIZE_EPS:
            if yes_before > no_before:
                heavy = "YES"
                sold = min(sell_target, yes_before)
                yes_proceeds = sold * float(yes_price)
                if yes_token_id:
                    self._inventory[yes_token_id] = yes_before - sold
                    self._ask_reserved[yes_token_id] = 0.0
                self._cash += yes_proceeds
            else:
                heavy = "NO"
                sold = min(sell_target, no_before)
                no_proceeds = sold * float(no_price)
                if no_token_id:
                    self._inventory[no_token_id] = no_before - sold
                    self._ask_reserved[no_token_id] = 0.0
                self._cash += no_proceeds
        yes_after = float(self._inventory.get(yes_token_id, 0.0)) if yes_token_id else 0.0
        no_after = float(self._inventory.get(no_token_id, 0.0)) if no_token_id else 0.0
        return {
            "heavy": heavy,
            "surplus": surplus,
            "sold": sold,
            "yes_before": yes_before,
            "no_before": no_before,
            "yes_after": yes_after,
            "no_after": no_after,
            "yes_proceeds": yes_proceeds,
            "no_proceeds": no_proceeds,
        }

    def post(
        self,
        order_id: str,
        condition_id: str,
        token_id: str,
        side: str,
        price: float,
        size: float,
    ) -> bool:
        """Rest a paper GTC. BID reserves cash; ASK reserves inventory."""
        side_u = side.upper()
        if side_u == "BID":
            notional = float(price) * float(size)
            if not self.precheck_bid(notional):
                return False
            self._cash -= notional
            self._committed += notional
        elif side_u == "ASK":
            if not self.precheck_ask(token_id, size):
                return False
            self._ask_reserved[token_id] = (
                self._ask_reserved.get(token_id, 0.0) + float(size)
            )
        else:
            return False
        self._orders[order_id] = PaperRestingOrder(
            order_id=order_id,
            condition_id=condition_id,
            token_id=token_id,
            side=side_u,
            price=float(price),
            size=float(size),
        )
        self._token_cid[token_id] = condition_id
        return True

    def cancel(self, order_id: str) -> None:
        rec = self._orders.pop(order_id, None)
        if rec is None:
            return
        self._release(rec)

    def cancel_market(self, condition_id: str) -> None:
        for oid in [
            oid for oid, rec in self._orders.items()
            if rec.condition_id == condition_id
        ]:
            self.cancel(oid)

    def cancel_all(self) -> None:
        for oid in list(self._orders):
            self.cancel(oid)

    def try_fill(
        self,
        token_id: str,
        print_price: Optional[float],
        print_size: float = 0.0,
        *,
        best_bid: Optional[float] = None,
        best_ask: Optional[float] = None,
    ) -> List[FillEvent]:
        """Fill resting quotes when a print / BBO is at or through our price.

        Maker-only at our limit. Partials allowed. Fail-soft: never raises.
        """
        try:
            return self._try_fill_inner(
                token_id, print_price, print_size,
                best_bid=best_bid, best_ask=best_ask,
            )
        except Exception as exc:
            logger.warning("paper try_fill failed: {}", exc)
            return []

    def _try_fill_inner(
        self,
        token_id: str,
        print_price: Optional[float],
        print_size: float,
        *,
        best_bid: Optional[float],
        best_ask: Optional[float],
    ) -> List[FillEvent]:
        remaining_print = (
            float("inf") if print_size is None or print_size <= _SIZE_EPS
            else float(print_size)
        )
        resting = sorted(
            (rec for rec in self._orders.values() if rec.token_id == token_id),
            key=lambda r: r.posted_at,
        )
        fills: List[FillEvent] = []
        for rec in resting:
            if remaining_print <= _SIZE_EPS:
                break
            if not self._is_hit(rec, print_price, best_bid, best_ask):
                continue
            fill_sz = min(rec.size, remaining_print)
            if fill_sz <= _SIZE_EPS:
                continue
            self._apply_fill(rec, fill_sz)
            remaining_print -= fill_sz
            cid = rec.condition_id or self._token_cid.get(token_id, "")
            fills.append(FillEvent(
                token_id=token_id,
                condition_id=cid,
                side=rec.side,
                price=rec.price,
                filled_size=fill_sz,
                timestamp=datetime.now(timezone.utc),
                trade_id=f"paper_fill_{next(self._fill_seq)}",
            ))
            if rec.size <= _SIZE_EPS:
                self._orders.pop(rec.order_id, None)
        return fills

    def _is_hit(
        self,
        order: PaperRestingOrder,
        print_price: Optional[float],
        best_bid: Optional[float],
        best_ask: Optional[float],
    ) -> bool:
        if order.side == "BID":
            if print_price is not None and print_price > 0 and print_price <= order.price + 1e-12:
                return True
            if best_ask is not None and best_ask > 0 and best_ask <= order.price + 1e-12:
                return True
            return False
        if print_price is not None and print_price > 0 and print_price >= order.price - 1e-12:
            return True
        if best_bid is not None and best_bid > 0 and best_bid >= order.price - 1e-12:
            return True
        return False

    def _apply_fill(self, rec: PaperRestingOrder, fill_sz: float) -> None:
        notional = rec.price * fill_sz
        if rec.side == "BID":
            self._committed = max(0.0, self._committed - notional)
            self._inventory[rec.token_id] = (
                self._inventory.get(rec.token_id, 0.0) + fill_sz
            )
        else:
            reserved = self._ask_reserved.get(rec.token_id, 0.0)
            self._ask_reserved[rec.token_id] = max(0.0, reserved - fill_sz)
            held = self._inventory.get(rec.token_id, 0.0)
            self._inventory[rec.token_id] = max(0.0, held - fill_sz)
            self._cash += notional
        rec.size = max(0.0, rec.size - fill_sz)

    def _release(self, rec: PaperRestingOrder) -> None:
        if rec.side == "BID":
            notional = rec.price * rec.size
            self._committed = max(0.0, self._committed - notional)
            self._cash += notional
        else:
            reserved = self._ask_reserved.get(rec.token_id, 0.0)
            self._ask_reserved[rec.token_id] = max(0.0, reserved - rec.size)
