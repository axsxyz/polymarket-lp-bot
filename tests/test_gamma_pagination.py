"""Tests for Gamma /markets pagination in ``core/market_manager._fetch_gamma_markets``.

Regression suite for the 2026-05-19 incident where the bot exited at boot with
"After filtering: 0 markets" because the previous implementation requested
``limit=200`` and bailed out as soon as ``len(items) < 200`` — Gamma actually
caps every response at 100 items so the first page always triggered the early
exit and the bot never saw markets beyond the first batch.

These tests verify the new pagination contract:
  - ``limit=100`` matches the API ceiling.
  - Loop continues until the API returns an empty list.
  - ``MAX_GAMMA_PAGES`` acts as a hard safety bound.
  - ``asyncio.sleep(0.7)`` is awaited *between* pages (never before page 0).
  - HTTP errors (e.g. 500) log a warning and return the partial lookup.

No live HTTP — ``aiohttp.ClientSession`` is replaced by a fake that serves a
canned page queue, and ``asyncio.sleep`` is patched on the
``core.market_manager`` module so the test suite runs in milliseconds.
"""

from __future__ import annotations

import asyncio
from typing import List, Tuple
from unittest.mock import patch

import pytest

from config.settings import Settings
from core import market_manager
from core.market_manager import (
    GAMMA_PAGE_LIMIT,
    GAMMA_PAGE_SLEEP_S,
    MAX_GAMMA_PAGES,
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


def _gamma_market(cid: str) -> dict:
    """Minimal Gamma /markets payload that ``parse_gamma_market`` accepts."""
    return {
        "conditionId": cid,
        "question": f"Market {cid[-4:]}",
        "clobTokenIds": f'["tok_yes_{cid}", "tok_no_{cid}"]',
        "outcomes": '["Yes", "No"]',
        "endDateIso": "2027-01-01T00:00:00Z",
        "rewardsMaxSpread": 0.05,
        "rewardsMinSize": 5,
        "clobRewards": [{"rewardsDailyRate": 10}],
        "volume24hr": 5_000,
        "tags": ["politics"],
        "negRisk": False,
        "orderPriceMinTickSize": "0.01",
    }


def _page(n: int, base_cid: str = "0xabc") -> List[dict]:
    """Build a page of N synthetic markets with deterministic distinct cids."""
    return [_gamma_market(f"{base_cid}{i:04x}") for i in range(n)]


class _FakeResp:
    """Stand-in for ``aiohttp.ClientResponse`` returned by ``session.get(...)``."""

    def __init__(self, status: int, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self):
        return self._payload


class _FakeSession:
    """Stand-in for ``aiohttp.ClientSession()``.

    Acts as its own async context manager (matches the production call
    ``async with aiohttp.ClientSession() as session``).  Each ``get()`` pops
    the next ``(status, payload)`` from ``pages``; if the queue is exhausted
    it returns a 200 / empty list so the production loop exits cleanly
    (mirrors the live Gamma "no more data" behaviour).
    """

    def __init__(self, pages: List[Tuple[int, object]]):
        self._pages = list(pages)
        self.urls_called: List[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def get(self, url, timeout=None):  # noqa: D401 — mimics aiohttp signature
        self.urls_called.append(url)
        if not self._pages:
            return _FakeResp(200, [])
        status, payload = self._pages.pop(0)
        return _FakeResp(status, payload)


def _patch_aiohttp(monkeypatch, fake: _FakeSession) -> None:
    """Swap ``aiohttp.ClientSession`` in ``core.market_manager`` for the fake."""

    def factory(*_args, **_kwargs):
        return fake

    monkeypatch.setattr(market_manager.aiohttp, "ClientSession", factory)


def _patch_sleep(monkeypatch) -> List[float]:
    """Replace ``asyncio.sleep`` on the module so tests don't actually wait.

    Returns a list that records every sleep duration awaited.
    """
    sleeps: List[float] = []

    async def fake_sleep(duration):
        sleeps.append(duration)

    monkeypatch.setattr(market_manager.asyncio, "sleep", fake_sleep)
    return sleeps


def _build_manager() -> MarketManager:
    """Construct a MarketManager with a stub client.

    ``_fetch_gamma_markets`` only touches ``self._settings`` so the client can
    be ``None`` for the purposes of these unit tests.
    """
    settings = _make_settings()
    return MarketManager(client=None, settings=settings)  # type: ignore[arg-type]


# ────────────────────────────────────────────────────────────────
# Tests
# ────────────────────────────────────────────────────────────────

class TestSinglePageBelowCeiling:
    """A single page of 80 items is fully collected, then the loop exits.

    Because we use ``if not items: break`` (the safer/more explicit
    termination — matching ``backtest/fetcher.py``), the loop performs one
    follow-up "probe" request that comes back empty before exiting.  What
    matters is the *behaviour*: all 80 items end up in the lookup, the loop
    stops cleanly, and ``offset`` / ``limit`` query params are correct.
    """

    def test_single_page_80_items(self, monkeypatch):
        fake = _FakeSession([(200, _page(80))])  # queue exhausts → empty probe
        _patch_aiohttp(monkeypatch, fake)
        _patch_sleep(monkeypatch)

        mgr = _build_manager()
        lookup = asyncio.run(mgr._fetch_gamma_markets())

        assert len(lookup) == 80
        # First request must use offset=0 and limit=100 (matches API ceiling).
        assert "offset=0" in fake.urls_called[0]
        assert f"limit={GAMMA_PAGE_LIMIT}" in fake.urls_called[0]
        # Loop terminates within a tight bound (1 real + 1 empty probe = 2).
        assert len(fake.urls_called) == 2, (
            "Expected exactly one follow-up empty probe after the sub-ceiling page"
        )
        # Second probe must page forward to offset=100.
        assert "offset=100" in fake.urls_called[1]


class TestMultiPagePagination:
    """100 + 100 + 50 = 250 entries; a fourth empty probe terminates the loop."""

    def test_three_real_pages_then_empty_probe(self, monkeypatch):
        fake = _FakeSession([
            (200, _page(100, base_cid="0xaaa")),
            (200, _page(100, base_cid="0xbbb")),
            (200, _page(50,  base_cid="0xccc")),
        ])  # queue exhausts → 4th request returns empty automatically
        _patch_aiohttp(monkeypatch, fake)
        _patch_sleep(monkeypatch)

        mgr = _build_manager()
        lookup = asyncio.run(mgr._fetch_gamma_markets())

        assert len(lookup) == 250, "All three real pages must be aggregated"
        assert len(fake.urls_called) == 4, (
            "Three real pages + one empty probe = four HTTP calls"
        )
        for idx, url in enumerate(fake.urls_called):
            assert f"offset={idx * GAMMA_PAGE_LIMIT}" in url, (
                f"Offset must increment by {GAMMA_PAGE_LIMIT} each page"
            )

    def test_three_full_pages_then_explicit_empty(self, monkeypatch):
        """Even three full-size pages are followed by an explicit empty page
        that terminates the loop without inflating the lookup."""
        fake = _FakeSession([
            (200, _page(100, base_cid="0xaaa")),
            (200, _page(100, base_cid="0xbbb")),
            (200, _page(100, base_cid="0xccc")),
            (200, []),
        ])
        _patch_aiohttp(monkeypatch, fake)
        _patch_sleep(monkeypatch)

        mgr = _build_manager()
        lookup = asyncio.run(mgr._fetch_gamma_markets())

        assert len(lookup) == 300
        assert len(fake.urls_called) == 4


class TestSafetyBound:
    """If the API never stops returning data we must not loop forever."""

    def test_stops_at_max_gamma_pages(self, monkeypatch):
        # Queue strictly more pages than the safety bound to prove we stop.
        infinite_pages = [
            (200, _page(GAMMA_PAGE_LIMIT, base_cid=f"0x{i:03x}"))
            for i in range(MAX_GAMMA_PAGES + 20)
        ]
        fake = _FakeSession(infinite_pages)
        _patch_aiohttp(monkeypatch, fake)
        _patch_sleep(monkeypatch)

        mgr = _build_manager()
        lookup = asyncio.run(mgr._fetch_gamma_markets())

        assert len(fake.urls_called) == MAX_GAMMA_PAGES, (
            "Safety bound must hard-stop the loop after MAX_GAMMA_PAGES requests"
        )
        assert len(lookup) == MAX_GAMMA_PAGES * GAMMA_PAGE_LIMIT


class TestEmptyPage:
    """An empty list at page 2 cleanly terminates iteration."""

    def test_empty_second_page_stops(self, monkeypatch):
        fake = _FakeSession([
            (200, _page(100, base_cid="0xaaa")),
            (200, []),
            # A third page is queued but must NEVER be requested.
            (200, _page(50, base_cid="0xzzz")),
        ])
        _patch_aiohttp(monkeypatch, fake)
        _patch_sleep(monkeypatch)

        mgr = _build_manager()
        lookup = asyncio.run(mgr._fetch_gamma_markets())

        assert len(lookup) == 100
        assert len(fake.urls_called) == 2, (
            "Loop must stop at the first empty page, not poke a third one"
        )
        assert "0xzzz0000" not in lookup


class TestApiError:
    """A 500 response is logged but never crashes — return the partial lookup."""

    def test_status_500_returns_partial(self, monkeypatch):
        fake = _FakeSession([
            (200, _page(100, base_cid="0xaaa")),
            (500, {"error": "server exploded"}),
            # Queued but unreachable past the error.
            (200, _page(80, base_cid="0xbbb")),
        ])
        _patch_aiohttp(monkeypatch, fake)
        _patch_sleep(monkeypatch)

        mgr = _build_manager()

        with patch.object(market_manager.logger, "warning") as warn:
            lookup = asyncio.run(mgr._fetch_gamma_markets())

        assert len(lookup) == 100, "Page 1 entries must survive the error on page 2"
        assert len(fake.urls_called) == 2
        # At least one warning must mention the bad status (other warnings —
        # e.g. parse failures — may also fire, so we just look for a match).
        assert any(
            call.args and "status" in str(call.args[0]).lower()
            for call in warn.call_args_list
        ), f"Expected a warning about the HTTP status, got: {warn.call_args_list}"


class TestRateLimitSleep:
    """``asyncio.sleep(0.7)`` is awaited between pages but never before page 0.

    With ``if not items: break``, the loop performs ``N+1`` HTTP requests for
    ``N`` real pages (the +1 being the empty-probe terminator).  ``sleep`` is
    awaited *before* each request except the very first → exactly ``N`` sleeps.
    """

    def test_sleep_between_pages_not_before_first(self, monkeypatch):
        # 3 real pages + 1 empty probe = 4 requests = 3 inter-page sleeps.
        fake = _FakeSession([
            (200, _page(100, base_cid="0xaaa")),
            (200, _page(100, base_cid="0xbbb")),
            (200, _page(30,  base_cid="0xccc")),
        ])
        _patch_aiohttp(monkeypatch, fake)
        sleeps = _patch_sleep(monkeypatch)

        mgr = _build_manager()
        asyncio.run(mgr._fetch_gamma_markets())

        assert len(fake.urls_called) == 4, "3 real + 1 empty probe"
        assert len(sleeps) == 3, (
            f"Expected 3 inter-page sleeps for 4 HTTP calls, got {sleeps}"
        )
        assert all(s == GAMMA_PAGE_SLEEP_S for s in sleeps), (
            f"Every sleep must equal GAMMA_PAGE_SLEEP_S ({GAMMA_PAGE_SLEEP_S}s), "
            f"got {sleeps}"
        )

    def test_no_sleep_before_first_request(self, monkeypatch):
        """First request must fire immediately — no sleep before page 0."""
        fake = _FakeSession([(200, [])])  # zero real pages
        _patch_aiohttp(monkeypatch, fake)
        sleeps = _patch_sleep(monkeypatch)

        mgr = _build_manager()
        asyncio.run(mgr._fetch_gamma_markets())

        assert len(fake.urls_called) == 1
        assert sleeps == [], (
            "Empty first response must terminate WITHOUT any prior sleep"
        )
