"""Tests for Gamma condition_ids join in ``_fetch_gamma_by_condition_ids``.

Contract (2026-08-29 TASK-GAMMA-JOIN):
  - CLOB cids ARE the fetch — chunk 100, ``GET /markets?condition_ids=…``.
  - Never ``offset=``, never ``/markets/keyset`` on refresh/enrich.
  - 422 → split 50/20/1; other chunks continue.
  - 429/5xx → retry that chunk then continue.
  - Miss after join → gamma_miss; CLOB end_date fills → not miss.

No live HTTP — ``aiohttp.ClientSession`` is faked; ``asyncio.sleep`` patched.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from config.markets_filter import MarketCandidate
from config.settings import Settings
from core import market_manager
from core.market_manager import (
    GAMMA_PAGE_LIMIT,
    MarketManager,
)


# ────────────────────────────────────────────────────────────────
# Fixtures / fakes
# ────────────────────────────────────────────────────────────────

def _make_settings(**overrides) -> Settings:
    defaults = {
        "PRIVATE_KEY": "0x" + "ab" * 32,
        "TOTAL_CAPITAL": 1000,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _gamma_market(cid: str, end: str = "2027-01-01T00:00:00Z") -> dict:
    """Minimal Gamma market payload that ``parse_gamma_market`` accepts."""
    return {
        "conditionId": cid,
        "question": f"Market {cid[-4:]}",
        "clobTokenIds": f'["tok_yes_{cid}", "tok_no_{cid}"]',
        "outcomes": '["Yes", "No"]',
        "endDateIso": end,
        "rewardsMaxSpread": 0.05,
        "rewardsMinSize": 5,
        "clobRewards": [{"rewardsDailyRate": 10}],
        "volume24hr": 5_000,
        "tags": ["politics"],
        "negRisk": False,
        "orderPriceMinTickSize": "0.01",
    }


def _cid(i: int) -> str:
    return f"0x{i:064x}"


class _FakeResp:
    """Stand-in for ``aiohttp.ClientResponse``."""

    def __init__(self, status: int, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        import json as _json
        if isinstance(self._payload, (dict, list)):
            return _json.dumps(self._payload)
        return str(self._payload)


class _FakeSession:
    """Stand-in for ``aiohttp.ClientSession()``.

    ``handler(params) -> (status, payload)`` drives responses. Records every
    ``(url, params)`` call for contract assertions.
    """

    def __init__(self, handler):
        self._handler = handler
        self.calls: List[Tuple[str, Any]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def get(self, url, timeout=None, params=None):  # noqa: D401 — mimics aiohttp
        self.calls.append((url, params))
        status, payload = self._handler(params)
        return _FakeResp(status, payload)


def _patch_aiohttp(monkeypatch, fake: _FakeSession) -> None:
    def factory(*_args, **_kwargs):
        return fake

    monkeypatch.setattr(market_manager.aiohttp, "ClientSession", factory)


def _patch_sleep(monkeypatch) -> List[float]:
    sleeps: List[float] = []

    async def fake_sleep(duration):
        sleeps.append(duration)

    monkeypatch.setattr(market_manager.asyncio, "sleep", fake_sleep)
    return sleeps


def _build_manager() -> MarketManager:
    settings = _make_settings()
    return MarketManager(client=None, settings=settings)  # type: ignore[arg-type]


def _condition_ids_from_params(params: Any) -> List[str]:
    if params is None:
        return []
    if isinstance(params, dict):
        v = params.get("condition_ids")
        if v is None:
            return []
        if isinstance(v, (list, tuple)):
            return list(v)
        return [str(v)]
    # list of tuples (aiohttp repeated-key form)
    return [str(v) for k, v in params if k == "condition_ids"]


def _assert_join_url(url: str) -> None:
    assert "offset=" not in url, f"offset= forbidden: {url}"
    assert "/markets/keyset" not in url, f"keyset forbidden on join: {url}"
    assert url.rstrip("/").endswith("/markets") or "/markets?" in url or url.endswith(
        "/markets"
    ), f"expected /markets: {url}"


def _candidate(cid: str, end_date: Optional[datetime] = None) -> MarketCandidate:
    return MarketCandidate(
        condition_id=cid,
        token_id_yes=f"y_{cid}",
        token_id_no=f"n_{cid}",
        max_spread=0.05,
        min_size=5.0,
        tick_size=0.01,
        reward_rate_daily=10.0,
        end_date=end_date,
    )


# ────────────────────────────────────────────────────────────────
# Tests
# ────────────────────────────────────────────────────────────────

class TestChunking:
    """250 cids → 3 chunks (100+100+50); condition_ids list; no offset/keyset."""

    def test_250_cids_three_chunks_no_offset_no_keyset(self, monkeypatch):
        seen_chunks: List[List[str]] = []

        def handler(params):
            cids = _condition_ids_from_params(params)
            seen_chunks.append(cids)
            return 200, [_gamma_market(c) for c in cids]

        fake = _FakeSession(handler)
        _patch_aiohttp(monkeypatch, fake)
        _patch_sleep(monkeypatch)

        cids = [_cid(i) for i in range(250)]
        mgr = _build_manager()
        lookup, stats = asyncio.run(mgr._fetch_gamma_by_condition_ids(cids))

        assert stats["chunks"] == 3
        assert len(seen_chunks) == 3
        assert sorted(len(c) for c in seen_chunks) == [50, 100, 100]
        assert len(lookup) == 250
        assert mgr._last_gamma_pages == 3

        for url, params in fake.calls:
            _assert_join_url(url)
            assert _condition_ids_from_params(params), "must send condition_ids"
            # limit present; never offset key in params
            if isinstance(params, dict):
                assert "offset" not in params
            else:
                assert all(k != "offset" for k, _ in (params or []))


class Test422Split:
    """One fat chunk 422 → split; other chunks still succeed."""

    def test_422_splits_and_continues(self, monkeypatch):
        call_n = {"n": 0}

        def handler(params):
            cids = _condition_ids_from_params(params)
            call_n["n"] += 1
            # First top-level chunk of 100 → 422; smaller splits succeed.
            if len(cids) >= 100:
                return 422, {"error": "payload too large"}
            return 200, [_gamma_market(c) for c in cids]

        fake = _FakeSession(handler)
        _patch_aiohttp(monkeypatch, fake)
        _patch_sleep(monkeypatch)

        # Two chunks of 100: first 422-splits, second succeeds (also ≥100 → 422
        # then splits). Use 150 so chunk1=100 (422→50s), chunk2=50 (ok).
        cids = [_cid(i) for i in range(150)]
        mgr = _build_manager()
        lookup, stats = asyncio.run(mgr._fetch_gamma_by_condition_ids(cids))

        assert stats["splits_422"] >= 1
        assert len(lookup) == 150
        # At least one request was size ≤50 after split
        sizes = [len(_condition_ids_from_params(p)) for _, p in fake.calls]
        assert any(s == 50 for s in sizes), f"expected size-50 split, got {sizes}"
        for url, _ in fake.calls:
            _assert_join_url(url)


class Test429Retry:
    """429 then 200 on retry for the same chunk."""

    def test_429_then_200_retries(self, monkeypatch):
        attempts_for_first: List[int] = []
        seen: Dict[str, int] = {}

        def handler(params):
            cids = _condition_ids_from_params(params)
            key = ",".join(cids)
            seen[key] = seen.get(key, 0) + 1
            if seen[key] == 1:
                attempts_for_first.append(1)
                return 429, {"error": "rate limited"}
            return 200, [_gamma_market(c) for c in cids]

        fake = _FakeSession(handler)
        _patch_aiohttp(monkeypatch, fake)
        sleeps = _patch_sleep(monkeypatch)

        cids = [_cid(i) for i in range(3)]
        mgr = _build_manager()
        lookup, stats = asyncio.run(mgr._fetch_gamma_by_condition_ids(cids))

        assert len(lookup) == 3
        assert stats["chunks"] == 1
        assert len(fake.calls) == 2  # 429 then 200
        assert any(s > 0 for s in sleeps), "expected retry backoff sleep"


class TestMissAndClobDate:
    """Miss → gamma_miss; CLOB date → not miss."""

    def test_miss_after_join_is_gamma_miss(self, monkeypatch):
        def handler(params):
            cids = _condition_ids_from_params(params)
            # Return only the first half
            return 200, [_gamma_market(c) for c in cids[:1]]

        fake = _FakeSession(handler)
        _patch_aiohttp(monkeypatch, fake)
        _patch_sleep(monkeypatch)

        hit = _candidate(_cid(0))
        miss = _candidate(_cid(1))
        mgr = _build_manager()
        asyncio.run(mgr._enrich_with_gamma([hit, miss]))

        assert hit.gamma_miss is False
        assert hit.has_end_date
        assert miss.gamma_miss is True
        assert not miss.has_end_date

    def test_clob_end_date_fills_not_miss(self, monkeypatch):
        def handler(params):
            return 200, []  # Gamma knows nothing

        fake = _FakeSession(handler)
        _patch_aiohttp(monkeypatch, fake)
        _patch_sleep(monkeypatch)

        dated = _candidate(
            _cid(0),
            end_date=datetime(2027, 6, 1, tzinfo=timezone.utc),
        )
        undated = _candidate(_cid(1))
        mgr = _build_manager()
        asyncio.run(mgr._enrich_with_gamma([dated, undated]))

        assert dated.has_end_date
        assert dated.gamma_miss is False
        assert undated.gamma_miss is True


class TestNoKeysetOnEnrich:
    """refresh enrich path never hits keyset."""

    def test_enrich_never_calls_keyset(self, monkeypatch):
        def handler(params):
            cids = _condition_ids_from_params(params)
            return 200, [_gamma_market(c) for c in cids]

        fake = _FakeSession(handler)
        _patch_aiohttp(monkeypatch, fake)
        _patch_sleep(monkeypatch)

        cands = [_candidate(_cid(i)) for i in range(5)]
        mgr = _build_manager()
        asyncio.run(mgr._enrich_with_gamma(cands))

        assert fake.calls, "expected at least one Gamma GET"
        for url, params in fake.calls:
            _assert_join_url(url)
            assert _condition_ids_from_params(params)


class TestChunkLimitConstant:
    def test_page_limit_is_100(self):
        assert GAMMA_PAGE_LIMIT == 100
