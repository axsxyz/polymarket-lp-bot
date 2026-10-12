"""WebSocketFeed: last_trade_price must not overwrite BBO mid or freshness."""

from __future__ import annotations

import asyncio
import time

from core.ws_feed import WebSocketFeed


async def _noop(_token_id: str, _mid: float, _fast: bool) -> None:
    return None


def test_last_trade_does_not_overwrite_mid_or_freshness():
    async def _run() -> None:
        feed = WebSocketFeed(on_mid_change=_noop)
        feed._mid_prices["tok"] = 0.50
        stamped = 1_000.0
        feed._last_update["tok"] = stamped
        await feed._handle_last_trade({
            "event_type": "last_trade_price",
            "asset_id": "tok",
            "price": "0.99",
        })
        assert feed._mid_prices["tok"] == 0.50
        assert feed._last_update["tok"] == stamped

    asyncio.run(_run())


def test_last_trade_message_is_ignored():
    feed = WebSocketFeed(on_mid_change=_noop)
    feed._mid_prices["tok"] = 0.42
    stamped = 2_000.0
    feed._last_update["tok"] = stamped
    asyncio.run(feed._handle_message({
        "event_type": "last_trade_price",
        "asset_id": "tok",
        "price": "0.11",
    }))
    assert feed._mid_prices["tok"] == 0.42
    assert feed._last_update["tok"] == stamped


def test_bbo_process_mid_update_still_writes():
    async def _run() -> None:
        feed = WebSocketFeed(on_mid_change=_noop)
        before = time.monotonic()
        await feed._process_mid_update("tok", 0.51)
        assert feed._mid_prices["tok"] == 0.51
        assert feed._last_update["tok"] >= before

    asyncio.run(_run())


def test_last_trade_callback_fires_without_touching_mid():
    seen: list = []

    async def _on_trade(token_id: str, price: float, size: float) -> None:
        seen.append((token_id, price, size))

    async def _run() -> None:
        feed = WebSocketFeed(on_mid_change=_noop, on_last_trade=_on_trade)
        feed._mid_prices["tok"] = 0.50
        stamped = 3_000.0
        feed._last_update["tok"] = stamped
        await feed._handle_message({
            "event_type": "last_trade_price",
            "asset_id": "tok",
            "price": "0.41",
            "size": "7",
        })
        assert seen == [("tok", 0.41, 7.0)]
        assert feed._mid_prices["tok"] == 0.50
        assert feed._last_update["tok"] == stamped

    asyncio.run(_run())
