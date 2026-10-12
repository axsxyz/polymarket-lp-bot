"""Append-only local ledger (fills, posts, cancels, skips, errors, snapshots).

Writes never raise into the quoting path.
PnL fields are realised + mark-to-mid only — no estimated LP share.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections import deque
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set

from loguru import logger

TAPE_PATH = Path("data/tape.jsonl")
RING_MAX = 2000
MAX_JSONL_BYTES = 16 * 1024 * 1024
_SAMPLE_SUB_MAX = 32


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LocalTape:
    """Process-local JSONL tape. One JSON object per line."""

    def __init__(self, path: Path = TAPE_PATH) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._last_quotes: Dict[str, Dict[str, Any]] = {}
        self._seq = 0
        self._ring: Deque[Dict[str, Any]] = deque(maxlen=RING_MAX)
        self._subscribers: List[asyncio.Queue] = []
        self._last_write_ok = True
        self._file_day: Optional[date] = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.warning("Tape: could not create {}: {}", self._path.parent, exc)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def ok(self) -> bool:
        return self._last_write_ok

    @property
    def seq(self) -> int:
        with self._lock:
            return self._seq

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=_SAMPLE_SUB_MAX)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def remember_quote(
        self,
        cid: str,
        *,
        mid: Optional[float] = None,
        bid: Optional[float] = None,
        ask: Optional[float] = None,
        intended_bid: Optional[float] = None,
        intended_ask: Optional[float] = None,
        outcome: Optional[str] = None,
    ) -> None:
        """Cache last quote levels per market.

        ``outcome`` is ``\"yes\"`` / ``\"no\"`` so YES and NO books do not
        overwrite each other. Legacy callers with no outcome treat bid/ask
        as YES (and keep ``bid``/``ask`` aliases for snapshots).
        """
        rec = self._last_quotes.get(cid, {})
        if mid is not None:
            rec["mid"] = mid
        leg = (outcome or "yes").strip().lower()
        if leg not in ("yes", "no"):
            leg = "yes"
        if bid is not None:
            rec[f"{leg}_bid"] = bid
            if leg == "yes":
                rec["bid"] = bid
        if ask is not None:
            rec[f"{leg}_ask"] = ask
            if leg == "yes":
                rec["ask"] = ask
        if intended_bid is not None:
            rec["intended_bid"] = intended_bid
        if intended_ask is not None:
            rec["intended_ask"] = intended_ask
        self._last_quotes[cid] = rec

    def remember_two_sided(
        self,
        cid: str,
        *,
        mid: Optional[float] = None,
        yes_quote: Any = None,
        no_quote: Any = None,
    ) -> None:
        """Cache YES + NO quote levels from a ``TwoSidedQuotes`` compute.

        Call after every compute (even when smart-diff keeps) so the terminal
        shows both books aligned to mid, not only the last posted side.
        """
        if yes_quote is not None:
            y_bid = (
                float(yes_quote.bid_price)
                if getattr(yes_quote, "bid_size", 0) > 0
                and float(getattr(yes_quote, "bid_price", 0) or 0) > 0
                else None
            )
            y_ask = (
                float(yes_quote.ask_price)
                if getattr(yes_quote, "ask_size", 0) > 0
                and float(getattr(yes_quote, "ask_price", 0) or 0) > 0
                else None
            )
            y_mid = mid
            if y_mid is None:
                y_mid = getattr(yes_quote, "mid", None)
            self.remember_quote(
                cid,
                mid=y_mid,
                bid=y_bid,
                ask=y_ask,
                intended_bid=y_bid,
                intended_ask=y_ask,
                outcome="yes",
            )
        if no_quote is not None:
            n_bid = (
                float(no_quote.bid_price)
                if getattr(no_quote, "bid_size", 0) > 0
                and float(getattr(no_quote, "bid_price", 0) or 0) > 0
                else None
            )
            n_ask = (
                float(no_quote.ask_price)
                if getattr(no_quote, "ask_size", 0) > 0
                and float(getattr(no_quote, "ask_price", 0) or 0) > 0
                else None
            )
            self.remember_quote(
                cid,
                bid=n_bid,
                ask=n_ask,
                outcome="no",
            )

    def last_quote(self, cid: str) -> Dict[str, Any]:
        return dict(self._last_quotes.get(cid, {}))

    def all_last_quotes(self) -> Dict[str, Dict[str, Any]]:
        return {k: dict(v) for k, v in self._last_quotes.items()}

    def _rotate_if_needed(self) -> None:
        """Daily or size-cap rotate. Caller holds ``_lock``."""
        try:
            today = datetime.now(timezone.utc).date()
            too_big = False
            if self._path.exists():
                too_big = self._path.stat().st_size >= MAX_JSONL_BYTES
            if self._file_day is None:
                self._file_day = today
                if not too_big:
                    return
            if today == self._file_day and not too_big:
                return
            if self._path.exists():
                bak = self._path.with_name(self._path.name + ".1")
                if bak.exists():
                    bak.unlink()
                self._path.replace(bak)
            self._file_day = today
        except Exception as exc:
            logger.warning("Tape rotate failed: {}", exc)

    def _fanout(self, event: Dict[str, Any]) -> None:
        dead: List[asyncio.Queue] = []
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    dead.append(q)
            except Exception:
                dead.append(q)
        if dead:
            with self._lock:
                for q in dead:
                    if q in self._subscribers:
                        self._subscribers.remove(q)

    def _write(self, event: Dict[str, Any]) -> None:
        event.setdefault("ts", _utcnow_iso())
        stored: Dict[str, Any] = {}
        try:
            with self._lock:
                self._seq += 1
                event["seq"] = self._seq
                stored = dict(event)
                self._ring.append(stored)
                self._rotate_if_needed()
                line = json.dumps(stored, default=str) + "\n"
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
            self._last_write_ok = True
            self._fanout(stored)
        except Exception as exc:
            self._last_write_ok = False
            logger.warning("Tape write failed ({}): {}", event.get("type"), exc)

    def fill(
        self,
        *,
        condition_id: str,
        token_id: str,
        side: str,
        price: float,
        filled_size: float,
        mid_at_fill: Optional[float] = None,
        trade_id: str = "",
    ) -> None:
        self._write({
            "type": "fill",
            "cid": condition_id,
            "token_id": token_id,
            "side": side,
            "price": price,
            "size": filled_size,
            "mid_at_fill": mid_at_fill,
            "trade_id": trade_id,
        })

    def post(
        self,
        *,
        condition_id: str,
        token_id: str,
        side: str,
        price: float,
        size: float,
        order_id: str = "",
        outcome: str = "yes",
    ) -> None:
        self.remember_quote(
            condition_id,
            bid=price if side == "BID" else None,
            ask=price if side == "ASK" else None,
            outcome=outcome,
        )
        payload: Dict[str, Any] = {
            "type": "post",
            "cid": condition_id,
            "token_id": token_id,
            "side": side,
            "price": price,
            "size": size,
            "order_id": order_id,
            "outcome": outcome,
        }
        self._write(payload)

    def cancel(
        self,
        *,
        condition_id: str,
        token_id: str = "",
        side: str = "",
        price: float = 0.0,
        size: float = 0.0,
        order_id: str = "",
    ) -> None:
        self._write({
            "type": "cancel",
            "cid": condition_id,
            "token_id": token_id,
            "side": side,
            "price": price,
            "size": size,
            "order_id": order_id,
        })

    def skip(self, cid: str, reason: str, **extra: Any) -> None:
        payload: Dict[str, Any] = {"type": "skip", "cid": cid, "reason": reason}
        for key, val in extra.items():
            if val is not None:
                payload[key] = val
        self._write(payload)

    def exception(self, where: str, exc: BaseException) -> None:
        self._write({"type": "exception", "where": where, "exc": repr(exc)})

    def universe(
        self,
        *,
        fetched: int,
        gamma_matched: int,
        gamma_status: Any,
        pages: int,
    ) -> None:
        self._write({
            "type": "universe",
            "fetched": fetched,
            "gamma_matched": gamma_matched,
            "gamma_status": gamma_status,
            "pages": pages,
        })

    def filter(
        self,
        *,
        rejection_counts: Dict[str, int],
        samples: Dict[str, List[Dict[str, Any]]],
        selected: List[Dict[str, Any]],
        ranked: int = 0,
    ) -> None:
        self._write({
            "type": "filter",
            "rejection_counts": rejection_counts,
            "samples": samples,
            "selected": selected,
            "ranked": ranked,
        })

    def seed(
        self,
        *,
        cid: str,
        yes_cost: float,
        no_cost: float,
        cap: float,
        ok: bool,
        dry_run: bool,
        reason: str = "",
    ) -> None:
        self._write({
            "type": "seed",
            "cid": cid,
            "yes_cost": yes_cost,
            "no_cost": no_cost,
            "cap": cap,
            "ok": ok,
            "dry_run": dry_run,
            "reason": reason,
        })

    def cycle(
        self,
        *,
        n: int,
        selected: int,
        active: int,
        posted: int,
        kept: int,
        errors: int,
        elapsed_s: float,
        utilization: float,
    ) -> None:
        self._write({
            "type": "cycle",
            "n": n,
            "selected": selected,
            "active": active,
            "posted": posted,
            "kept": kept,
            "errors": errors,
            "elapsed_s": elapsed_s,
            "utilization": utilization,
        })

    def state(
        self,
        *,
        cid: str,
        from_state: str,
        to_state: str,
        reason: str = "",
    ) -> None:
        payload: Dict[str, Any] = {
            "type": "state",
            "cid": cid,
            "from": from_state,
            "to": to_state,
        }
        if reason:
            payload["reason"] = reason
        self._write(payload)

    def snapshot_row(
        self,
        *,
        cid: str,
        mid: Optional[float],
        bid: Optional[float],
        ask: Optional[float],
        yes: float,
        no: float,
        realised: float,
        mark_to_mid: float,
        yes_bid: Optional[float] = None,
        yes_ask: Optional[float] = None,
        no_bid: Optional[float] = None,
        no_ask: Optional[float] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "type": "snapshot",
            "cid": cid,
            "mid": mid,
            "bid": bid,
            "ask": ask,
            "yes": yes,
            "no": no,
            "realised": realised,
            "mark_to_mid": mark_to_mid,
        }
        if yes_bid is not None:
            payload["yes_bid"] = yes_bid
        if yes_ask is not None:
            payload["yes_ask"] = yes_ask
        if no_bid is not None:
            payload["no_bid"] = no_bid
        if no_ask is not None:
            payload["no_ask"] = no_ask
        self._write(payload)

    def snapshot_one(
        self,
        inventory: Any,
        cid: str,
        markets: Optional[List[Any]] = None,
    ) -> None:
        """Write one snapshot row for a single cid. Never raises."""
        try:
            pos = (getattr(inventory, "positions", {}) or {}).get(cid)
            if pos is None:
                return
            realised = float(getattr(inventory, "realised_pnl", 0.0) or 0.0)
            mids = getattr(inventory, "_midpoints", {}) or {}
            yes_mid = mids.get(getattr(pos, "yes_token_id", None))
            if yes_mid is None and markets:
                for m in markets:
                    if getattr(m, "condition_id", None) == cid:
                        yes_mid = getattr(m, "midpoint", None)
                        break
            q = self.last_quote(cid)
            yes_bid = q.get("yes_bid", q.get("bid"))
            yes_ask = q.get("yes_ask", q.get("ask"))
            mark = 0.0
            try:
                mark = float(pos.gross_exposure_at_mid(yes_mid if yes_mid is not None else 0.5))
            except Exception:
                mark = 0.0
            self.snapshot_row(
                cid=cid,
                mid=yes_mid if yes_mid is not None else q.get("mid"),
                bid=yes_bid,
                ask=yes_ask,
                yes=float(pos.yes_size),
                no=float(pos.no_size),
                realised=realised,
                mark_to_mid=mark,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                no_bid=q.get("no_bid"),
                no_ask=q.get("no_ask"),
            )
        except Exception as exc:
            logger.warning("Tape snapshot_one failed: {}", exc)

    def snapshot_inventory(self, inventory: Any, markets: Optional[List[Any]] = None) -> None:
        """One snapshot row per known position. Uses last-quote cache for bid/ask."""
        try:
            realised = float(getattr(inventory, "realised_pnl", 0.0) or 0.0)
            mids = getattr(inventory, "_midpoints", {}) or {}
            positions = getattr(inventory, "positions", {}) or {}
            market_by_cid: Dict[str, Any] = {}
            if markets:
                for m in markets:
                    market_by_cid[m.condition_id] = m
            for cid, pos in positions.items():
                yes_mid = mids.get(pos.yes_token_id)
                if yes_mid is None and cid in market_by_cid:
                    yes_mid = getattr(market_by_cid[cid], "midpoint", None)
                q = self.last_quote(cid)
                yes_bid = q.get("yes_bid", q.get("bid"))
                yes_ask = q.get("yes_ask", q.get("ask"))
                mark = 0.0
                try:
                    mark = float(pos.gross_exposure_at_mid(yes_mid if yes_mid is not None else 0.5))
                except Exception:
                    mark = 0.0
                self.snapshot_row(
                    cid=cid,
                    mid=yes_mid if yes_mid is not None else q.get("mid"),
                    bid=yes_bid,
                    ask=yes_ask,
                    yes=float(pos.yes_size),
                    no=float(pos.no_size),
                    realised=realised,
                    mark_to_mid=mark,
                    yes_bid=yes_bid,
                    yes_ask=yes_ask,
                    no_bid=q.get("no_bid"),
                    no_ask=q.get("no_ask"),
                )
        except Exception as exc:
            logger.warning("Tape snapshot failed: {}", exc)

    def events_since(
        self,
        since: int = 0,
        types: Optional[List[str]] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Ring-buffer tail. Returns newest-first events with seq > since."""
        wanted: Optional[Set[str]] = set(types) if types else None
        with self._lock:
            items = list(self._ring)
        out: List[Dict[str, Any]] = []
        for obj in items:
            seq = int(obj.get("seq") or 0)
            if seq <= since:
                continue
            if wanted is not None and obj.get("type") not in wanted:
                continue
            out.append(obj)
        if limit > 0 and len(out) > limit:
            out = out[-limit:]
        out.reverse()
        return out

    def read_events(self, types: Optional[List[str]] = None, limit: int = 200) -> List[Dict[str, Any]]:
        """Tail-scan the JSONL file. Returns the last *limit* matching events (newest last)."""
        if not self._path.exists():
            return []
        wanted = set(types) if types else None
        out: List[Dict[str, Any]] = []
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if wanted is not None and obj.get("type") not in wanted:
                        continue
                    out.append(obj)
            if limit > 0 and len(out) > limit:
                return out[-limit:]
            return out
        except Exception as exc:
            logger.warning("Tape read failed: {}", exc)
            return []
