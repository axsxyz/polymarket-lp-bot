"""FillCollector apply-then-commit: fetch must not persist; commit after apply."""

from __future__ import annotations

import asyncio

from core.fill_collector import FillCollector


class _FakeClient:
    def __init__(self, trades):
        self._trades = trades

    async def get_trades_since(self, after_ts: int):
        return self._trades


def _trade(tid: str, ts: int = 1_700_000_100) -> dict:
    return {
        "id": tid,
        "side": "BUY",
        "asset_id": "tok_yes",
        "market": "cid_1",
        "price": 0.40,
        "size": 10.0,
        "timestamp": ts,
    }


def test_fetch_does_not_write_watermark(tmp_path):
    path = tmp_path / "trade_watermark.json"
    collector = FillCollector(_FakeClient([_trade("t1")]), path=path)  # type: ignore[arg-type]
    collector._watermark_ts = 1_700_000_000
    collector._seen_ids = {}

    fills = asyncio.run(collector.fetch())

    assert len(fills) == 1
    assert fills[0].trade_id == "t1"
    assert path.exists() is False
    assert "t1" not in collector._seen_ids


def test_commit_after_apply_persists(tmp_path):
    path = tmp_path / "trade_watermark.json"
    collector = FillCollector(
        _FakeClient([_trade("t2", ts=1_700_000_200)]), path=path,
    )  # type: ignore[arg-type]
    collector._watermark_ts = 1_700_000_000
    collector._seen_ids = {}

    fills = asyncio.run(collector.fetch())
    assert path.exists() is False
    collector.commit(fills)
    assert path.exists() is True
    assert "t2" in collector._seen_ids

    fills2 = asyncio.run(collector.fetch())
    assert fills2 == []


def test_uncommitted_fetch_is_replayable(tmp_path):
    path = tmp_path / "trade_watermark.json"
    collector = FillCollector(_FakeClient([_trade("t3")]), path=path)  # type: ignore[arg-type]
    collector._watermark_ts = 1_700_000_000
    collector._seen_ids = {}

    first = asyncio.run(collector.fetch())
    second = asyncio.run(collector.fetch())
    assert [f.trade_id for f in first] == ["t3"]
    assert [f.trade_id for f in second] == ["t3"]
    collector.commit(first)
    third = asyncio.run(collector.fetch())
    assert third == []
