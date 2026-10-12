"""Process-live operator digest for GET /digest.

No quoting logic. Numbers come from tape hooks, settings, and live bot state.
"""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

_MID_HIST = 90
_SEED_KEEP = 64


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def explain_idle(
    *,
    state: str,
    skip_reason: str,
    seed_reason: str,
    dry_run: bool,
    needed_usd: Optional[float] = None,
    available_usd: Optional[float] = None,
) -> str:
    """One readable sentence from real fields. Empty book is a valid answer."""
    if skip_reason == "collateral":
        need = f"${needed_usd:.2f}" if needed_usd is not None else "n/a"
        avail = f"${available_usd:.2f}" if available_usd is not None else "n/a"
        return f"BID skipped: collateral pre-check need {need} vs {avail} spendable."
    if skip_reason.startswith("hot_"):
        return f"Hot path skip: {skip_reason}."
    if skip_reason.startswith("resolution_"):
        return f"Resolution guard delisted this market ({skip_reason})."
    if skip_reason == "midpoint_guard":
        return "Paused: midpoint outside the quoting band."
    if skip_reason == "spike_paused":
        return "Paused: spike detector."
    if skip_reason == "toxicity_paused":
        return "Paused: toxicity / adverse flow."
    if skip_reason in ("exposure_per_market", "exposure_global"):
        return f"Paused: exposure gate ({skip_reason})."
    if state == "SEED_FAILED" or skip_reason == "seed_failed":
        if seed_reason:
            return f"Seed failed ({seed_reason}); market excluded from quoting."
        return "Seed failed; market excluded from quoting."
    if skip_reason:
        return f"Not quoting: {skip_reason}."
    if state in ("INACTIVE", "UNKNOWN"):
        return f"State {state}; no live quotes this cycle."
    if state == "ONE_SIDED":
        return "Quoting one side only (skew); Q-score is zero until two-sided."
    return ""


class OperatorDigest:
    """Mutable live blob. ``to_dict()`` is what the terminal paints."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started_at = _utcnow_iso()
        self.dry_run: bool = True
        self.mode: str = ""
        self.clob_ok: bool = False
        self.ws_connected: bool = False
        self.ws_enabled: bool = False
        self.tape_ok: bool = True
        self.tape_path: str = ""
        self.total_capital: float = 0.0
        self.max_markets: int = 0
        self.interval_s: float = 0.0
        self.wallet_collateral: Optional[float] = None
        self.committed_capital: float = 0.0
        self.cash_source: str = "wallet"
        self.cycle_n: int = 0
        self.elapsed_s: float = 0.0
        self.posted: int = 0
        self.kept: int = 0
        self.errors: int = 0
        self.selected_count: int = 0
        self.active_count: int = 0
        self.utilization: float = 0.0
        self.session_quoted: float = 0.0
        self.session_filled: float = 0.0
        self.realised: float = 0.0
        self.est_rewards_usd: float = 0.0
        self.est_share: float = 0.10
        self.est_note: str = "ESTIM, not CLOB Q-score"
        self.funnel: Dict[str, Any] = {
            "fetched": 0,
            "gamma_matched": 0,
            "gamma_status": None,
            "pages": 0,
            "rejection_counts": {},
            "samples": {},
            "ranked": 0,
            "selected": [],
        }
        self.seeds: Dict[str, Dict[str, Any]] = {}
        self.skip_reasons: Dict[str, str] = {}
        self.skip_extra: Dict[str, Dict[str, Any]] = {}
        self.last_collateral_skip: Optional[Dict[str, Any]] = None
        self._mid_hist: Dict[str, Deque[Optional[float]]] = defaultdict(
            lambda: deque(maxlen=_MID_HIST)
        )
        self._markets_cache: List[Dict[str, Any]] = []
        self._pnl_markets: List[Dict[str, Any]] = []

    def configure_from_settings(self, settings: Any, tape_path: str = "") -> None:
        with self._lock:
            self.dry_run = bool(getattr(settings, "DRY_RUN", True))
            self.cash_source = "paper" if self.dry_run else "wallet"
            mode = getattr(settings, "MODE", "")
            self.mode = mode.value if hasattr(mode, "value") else str(mode)
            self.total_capital = float(getattr(settings, "TOTAL_CAPITAL", 0.0) or 0.0)
            self.interval_s = float(
                getattr(settings, "REBALANCE_INTERVAL_SECONDS", 0.0) or 0.0
            )
            self.ws_enabled = bool(getattr(settings, "WS_ENABLED", False))
            self.tape_path = tape_path
            self.max_markets = int(getattr(settings, "MAX_MARKETS", 0) or 0)
            self.est_share = float(getattr(settings, "LP_ESTIMATED_SHARE", 0.10) or 0.10)

    def set_est_rewards(
        self,
        usd: float,
        share: Optional[float] = None,
        note: str = "ESTIM, not CLOB Q-score",
    ) -> None:
        """LP reward estimate only — never fold into ``realised``."""
        with self._lock:
            self.est_rewards_usd = float(usd)
            if share is not None:
                self.est_share = float(share)
            self.est_note = note

    def set_clob_ok(self, ok: bool) -> None:
        with self._lock:
            self.clob_ok = bool(ok)

    def set_ws_connected(self, connected: bool) -> None:
        with self._lock:
            self.ws_connected = bool(connected)

    def set_tape_ok(self, ok: bool) -> None:
        with self._lock:
            self.tape_ok = bool(ok)

    def set_wallet_collateral(
        self, balance: Optional[float], committed: float = 0.0
    ) -> None:
        with self._lock:
            self.wallet_collateral = balance
            self.committed_capital = float(committed)

    def set_funnel(
        self,
        *,
        fetched: int,
        gamma_matched: int,
        gamma_status: Any,
        pages: int,
        rejection_counts: Dict[str, int],
        samples: Dict[str, List[Dict[str, Any]]],
        selected: List[Dict[str, Any]],
        ranked: int,
    ) -> None:
        with self._lock:
            self.funnel = {
                "fetched": fetched,
                "gamma_matched": gamma_matched,
                "gamma_status": gamma_status,
                "pages": pages,
                "rejection_counts": dict(rejection_counts),
                "samples": samples,
                "ranked": ranked,
                "selected": selected,
            }

    def record_seed(self, row: Dict[str, Any]) -> None:
        cid = str(row.get("cid") or "")
        if not cid:
            return
        with self._lock:
            self.seeds[cid] = dict(row)
            while len(self.seeds) > _SEED_KEEP:
                self.seeds.pop(next(iter(self.seeds)))

    def record_skip(
        self,
        cid: str,
        reason: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._lock:
            self.skip_reasons[cid] = reason
            if extra:
                self.skip_extra[cid] = dict(extra)
                if reason == "collateral":
                    self.last_collateral_skip = {
                        "cid": cid,
                        "reason": reason,
                        **dict(extra),
                    }

    def add_filled_notional(self, usd: float) -> None:
        with self._lock:
            self.session_filled += float(usd)

    def add_quoted_notional(self, usd: float) -> None:
        with self._lock:
            self.session_quoted += float(usd)

    def record_mid(self, cid: str, mid: Optional[float]) -> None:
        if not cid:
            return
        with self._lock:
            self._mid_hist[cid].append(mid)

    def update_cycle(
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
        skip_reasons: Optional[Dict[str, str]] = None,
    ) -> None:
        with self._lock:
            self.cycle_n = n
            self.selected_count = selected
            self.active_count = active
            self.posted = posted
            self.kept = kept
            self.errors = errors
            self.elapsed_s = elapsed_s
            self.utilization = utilization
            if skip_reasons:
                self.skip_reasons.update(skip_reasons)

    def rebuild_markets(
        self,
        markets: List[Any],
        inventory: Any,
        tape: Any,
        scoring_rates: Optional[Dict[str, float]] = None,
        states: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Refresh the markets table from live objects. Never raises into callers."""
        try:
            rates = scoring_rates or {}
            states = states or {}
            positions = getattr(inventory, "positions", {}) or {}
            mids = getattr(inventory, "_midpoints", {}) or {}
            realised = float(getattr(inventory, "realised_pnl", 0.0) or 0.0)
            dry = self.dry_run
            rows: List[Dict[str, Any]] = []
            pnl_rows: List[Dict[str, Any]] = []
            with self._lock:
                skip_reasons = dict(self.skip_reasons)
                skip_extra = dict(self.skip_extra)
                seeds = dict(self.seeds)
                quotes_all = tape.all_last_quotes() if tape is not None else {}
            for rank_i, m in enumerate(markets, start=1):
                cid = m.condition_id
                st = states.get(cid)
                state_s = st.value if hasattr(st, "value") else (str(st) if st else "UNKNOWN")
                pos = positions.get(cid)
                yes = float(pos.yes_size) if pos is not None else 0.0
                no = float(pos.no_size) if pos is not None else 0.0
                yes_mid = mids.get(getattr(pos, "yes_token_id", None) or m.token_id_yes)
                if yes_mid is None:
                    yes_mid = getattr(m, "midpoint", None)
                q = quotes_all.get(cid, {})
                yes_bid = q.get("yes_bid", q.get("bid"))
                yes_ask = q.get("yes_ask", q.get("ask"))
                no_bid = q.get("no_bid")
                no_ask = q.get("no_ask")
                bid = yes_bid
                ask = yes_ask
                intended_bid = q.get("intended_bid")
                intended_ask = q.get("intended_ask")
                mark = 0.0
                if pos is not None:
                    try:
                        mark = float(
                            pos.gross_exposure_at_mid(
                                yes_mid if yes_mid is not None else 0.5
                            )
                        )
                    except Exception:
                        mark = 0.0
                seed = seeds.get(cid, {})
                skip = skip_reasons.get(cid, "")
                extra = skip_extra.get(cid, {})
                why = explain_idle(
                    state=state_s,
                    skip_reason=skip,
                    seed_reason=str(seed.get("reason") or ""),
                    dry_run=dry,
                    needed_usd=_as_float(extra.get("needed_usd")),
                    available_usd=_as_float(extra.get("available_usd")),
                )
                scoring = rates.get(cid)
                if dry:
                    bid_ok = yes_bid is not None and float(yes_bid) > 0
                    ask_ok = yes_ask is not None and float(yes_ask) > 0
                    mid_v = float(yes_mid) if yes_mid is not None else None
                    max_sp = float(getattr(m, "max_spread", 0.0) or 0.0)
                    in_band = False
                    if bid_ok and ask_ok and mid_v is not None and max_sp > 0:
                        in_band = (
                            (mid_v - float(yes_bid)) <= max_sp + 1e-9
                            and (float(yes_ask) - mid_v) <= max_sp + 1e-9
                        )
                    elif bid_ok and ask_ok:
                        in_band = True
                    scoring = "est" if in_band else scoring
                hist = list(self._mid_hist.get(cid, []))
                if yes_mid is not None:
                    if not hist or hist[-1] != yes_mid:
                        self._mid_hist[cid].append(yes_mid)
                        hist = list(self._mid_hist[cid])
                row = {
                    "cid": cid,
                    "rank": rank_i,
                    "score": getattr(m, "score", 0.0),
                    "question": m.question or "",
                    "state": state_s,
                    "mid": yes_mid,
                    "spread": getattr(m, "max_spread", None),
                    "bid": bid,
                    "ask": ask,
                    "yes_bid": yes_bid,
                    "yes_ask": yes_ask,
                    "no_bid": no_bid,
                    "no_ask": no_ask,
                    "intended_bid": intended_bid,
                    "intended_ask": intended_ask,
                    "yes": yes,
                    "no": no,
                    "alloc": getattr(m, "allocated_capital", 0.0),
                    "reward_day": getattr(m, "reward_rate_daily", 0.0),
                    "min_size": getattr(m, "min_size", 0.0),
                    "seed_yes": seed.get("yes_cost"),
                    "seed_no": seed.get("no_cost"),
                    "seed_cap": seed.get("cap"),
                    "seed_ok": seed.get("ok"),
                    "seed_reason": seed.get("reason") or "",
                    "seed_dry_run": seed.get("dry_run"),
                    "skip_reason": skip or None,
                    "scoring": scoring,
                    "why": why,
                    "mark_to_mid": mark,
                    "mid_hist": hist,
                    "tags": list(getattr(m, "tags", None) or []),
                }
                rows.append(row)
                pnl_rows.append({
                    "cid": cid,
                    "yes": yes,
                    "no": no,
                    "mid": yes_mid,
                    "mark_to_mid": mark,
                })
            with self._lock:
                self._markets_cache = rows
                self._pnl_markets = pnl_rows
                self.realised = realised
        except Exception:
            pass

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            spendable = None
            if self.wallet_collateral is not None:
                spendable = self.wallet_collateral - self.committed_capital
            last_col = dict(self.last_collateral_skip) if self.last_collateral_skip else None
            seeds = [dict(v) for v in self.seeds.values()]
            funnel = dict(self.funnel)
            funnel["max_markets"] = self.max_markets
            return {
                "ts": _utcnow_iso(),
                "started_at": self.started_at,
                "header": {
                    "name": "qv/Polymarket",
                    "dry_run": self.dry_run,
                    "mode": self.mode,
                    "clob_ok": self.clob_ok,
                    "ws_connected": self.ws_connected,
                    "ws_enabled": self.ws_enabled,
                    "tape_ok": self.tape_ok,
                    "tape_path": self.tape_path,
                    "total_capital": self.total_capital,
                    "max_markets": self.max_markets,
                    "wallet_collateral": self.wallet_collateral,
                    "committed_capital": self.committed_capital,
                    "spendable": spendable,
                    "cash_source": self.cash_source,
                    "interval_s": self.interval_s,
                    "cycle_n": self.cycle_n,
                    "elapsed_s": self.elapsed_s,
                    "posted": self.posted,
                    "kept": self.kept,
                    "errors": self.errors,
                    "selected": self.selected_count,
                    "active": self.active_count,
                    "utilization": self.utilization,
                    "est_rewards_usd": self.est_rewards_usd,
                    "est_share": self.est_share,
                    "est_note": self.est_note,
                },
                "funnel": funnel,
                "markets": list(self._markets_cache),
                "last_cycle": {
                    "n": self.cycle_n,
                    "selected": self.selected_count,
                    "active": self.active_count,
                    "posted": self.posted,
                    "kept": self.kept,
                    "errors": self.errors,
                    "elapsed_s": self.elapsed_s,
                    "utilization": self.utilization,
                },
                "pnl": {
                    "realised": self.realised,
                    "markets": list(self._pnl_markets),
                    "session_quoted": self.session_quoted,
                    "session_filled": self.session_filled,
                    "est_rewards_usd": self.est_rewards_usd,
                    "est_share": self.est_share,
                    "est_note": self.est_note,
                },
                "seeds": seeds,
                "collateral": {
                    "config_capital": self.total_capital,
                    "wallet": self.wallet_collateral,
                    "committed": self.committed_capital,
                    "spendable": spendable,
                    "last_skip": last_col,
                    "cash_source": self.cash_source,
                    "note": (
                        "cfg is TOTAL_CAPITAL. paper $ is simulated cash used by "
                        "the BID pre-check (not the CLOB wallet)."
                        if self.dry_run else
                        "TOTAL_CAPITAL is config, not wallet cash. "
                        "Spendable is CLOB collateral used by the BID pre-check."
                    ),
                },
            }


def _as_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
