"""Tests for the per-market state machine in core/market_manager.MarketManager.

Issue 4 introduces explicit MarketState values so dynamic-realloc, the
cycle-summary table, and Discord alerts can distinguish WHY a market is
not scoring (seed-failed vs one-sided rebalancing vs inactive).

Invariants enforced here:
  - SEED_FAILED is sticky across set_state() calls until the universe is
    rebuilt by refresh().
  - reallocate_capital() pulls capital from SEED_FAILED + INACTIVE only,
    NEVER from ONE_SIDED (the bot is mid-rebalance and needs the capital).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List

import pytest

from config.markets_filter import MarketCandidate
from config.settings import Settings
from core.market_manager import MarketManager, MarketState


def _make_settings(**overrides) -> Settings:
    defaults = {
        "PRIVATE_KEY": "0x" + "ab" * 32,
        "TOTAL_CAPITAL": 1000,
        "MAX_CAPITAL_PER_MARKET": 500,
        "MIN_CAPITAL_PER_MARKET": 50,
        "DYNAMIC_REALLOC_MAX_SHIFT_PCT": 0.20,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _candidate(cid: str, allocated: float = 200.0) -> MarketCandidate:
    return MarketCandidate(
        condition_id=cid,
        token_id_yes=f"y_{cid}",
        token_id_no=f"n_{cid}",
        max_spread=0.10,
        min_size=5.0,
        midpoint=0.50,
        allocated_capital=allocated,
        end_date=datetime.now(timezone.utc) + timedelta(days=150),
        tick_size="0.01",
    )


def _make_manager(markets: List[MarketCandidate]) -> MarketManager:
    settings = _make_settings()
    mgr = MarketManager.__new__(MarketManager)  # bypass __init__ (no client needed)
    mgr._client = None  # type: ignore[assignment]
    mgr._settings = settings
    mgr._discord = None
    mgr._markets = list(markets)
    mgr._token_index = {}
    mgr._last_refresh = None
    mgr._states = {}
    return mgr


class TestSetState:
    def test_default_unknown(self):
        mgr = _make_manager([_candidate("c1")])
        assert mgr.get_state("c1") == MarketState.UNKNOWN

    def test_set_and_read(self):
        mgr = _make_manager([_candidate("c1")])
        mgr.set_state("c1", MarketState.SCORING)
        assert mgr.get_state("c1") == MarketState.SCORING

    def test_seed_failed_is_sticky(self):
        """SEED_FAILED must NOT be downgraded by later set_state calls."""
        mgr = _make_manager([_candidate("c1")])
        mgr.set_state("c1", MarketState.SEED_FAILED)
        mgr.set_state("c1", MarketState.SCORING)  # ignored
        assert mgr.get_state("c1") == MarketState.SEED_FAILED

    def test_seed_failed_to_seed_failed_no_op(self):
        mgr = _make_manager([_candidate("c1")])
        mgr.set_state("c1", MarketState.SEED_FAILED)
        mgr.set_state("c1", MarketState.SEED_FAILED)
        assert mgr.get_state("c1") == MarketState.SEED_FAILED

    def test_cids_in_state(self):
        mgr = _make_manager([_candidate("c1"), _candidate("c2"), _candidate("c3")])
        mgr.set_state("c1", MarketState.SCORING)
        mgr.set_state("c2", MarketState.ONE_SIDED)
        mgr.set_state("c3", MarketState.SCORING)
        assert sorted(mgr.cids_in_state(MarketState.SCORING)) == ["c1", "c3"]
        assert mgr.cids_in_state(MarketState.ONE_SIDED) == ["c2"]


class TestReallocSelection:
    """reallocate_capital() must source ONLY from SEED_FAILED + INACTIVE."""

    def test_pulls_from_seed_failed(self):
        mgr = _make_manager([
            _candidate("scoring", allocated=200.0),
            _candidate("seed_failed", allocated=200.0),
        ])
        mgr.set_state("scoring", MarketState.SCORING)
        mgr.set_state("seed_failed", MarketState.SEED_FAILED)
        old_scoring = mgr._markets[0].allocated_capital
        old_failed = mgr._markets[1].allocated_capital

        adjusted = mgr.reallocate_capital(scoring_rates={})
        assert adjusted >= 1
        # SCORING gained, SEED_FAILED lost
        assert mgr._markets[0].allocated_capital > old_scoring
        assert mgr._markets[1].allocated_capital < old_failed

    def test_does_not_pull_from_one_sided(self):
        """ONE_SIDED is mid-rebalance — its capital is preserved."""
        mgr = _make_manager([
            _candidate("scoring", allocated=200.0),
            _candidate("one_sided", allocated=200.0),
        ])
        mgr.set_state("scoring", MarketState.SCORING)
        mgr.set_state("one_sided", MarketState.ONE_SIDED)
        old_scoring = mgr._markets[0].allocated_capital
        old_one_sided = mgr._markets[1].allocated_capital

        adjusted = mgr.reallocate_capital(scoring_rates={})
        # No donor in {SEED_FAILED, INACTIVE} → no shift
        assert adjusted == 0
        assert mgr._markets[0].allocated_capital == old_scoring
        assert mgr._markets[1].allocated_capital == old_one_sided

    def test_pulls_from_inactive(self):
        mgr = _make_manager([
            _candidate("scoring", allocated=200.0),
            _candidate("inactive", allocated=200.0),
        ])
        mgr.set_state("scoring", MarketState.SCORING)
        mgr.set_state("inactive", MarketState.INACTIVE)
        old_inactive = mgr._markets[1].allocated_capital

        adjusted = mgr.reallocate_capital(scoring_rates={})
        assert adjusted >= 1
        assert mgr._markets[1].allocated_capital < old_inactive

    def test_no_recipients_no_shift(self):
        """When no SCORING markets exist, capital is not redistributed."""
        mgr = _make_manager([
            _candidate("seed_failed", allocated=200.0),
            _candidate("one_sided", allocated=200.0),
        ])
        mgr.set_state("seed_failed", MarketState.SEED_FAILED)
        mgr.set_state("one_sided", MarketState.ONE_SIDED)
        old_failed = mgr._markets[0].allocated_capital
        old_one_sided = mgr._markets[1].allocated_capital

        adjusted = mgr.reallocate_capital(scoring_rates={})
        assert adjusted == 0
        assert mgr._markets[0].allocated_capital == old_failed
        assert mgr._markets[1].allocated_capital == old_one_sided
