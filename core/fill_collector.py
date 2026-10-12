"""
Fill collection via the CLOB trades API.

Design:
  - Fetch get_trades(after=watermark) on its own 2–5s task (not the 45s cycle)
  - Deduplicate by trade ID (bounded set of recently seen IDs)
  - Advance watermark to max observed trade timestamp (not wall-clock now)
  - Persist watermark ONLY after inventory has applied the fills (apply-then-commit)
  - On any API error, abort without recording fills (watermark unchanged)
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

from core.client import AsyncClobClient
from core.order_manager import FillEvent

WATERMARK_PATH = Path("data/trade_watermark.json")
MAX_SEEN_IDS = 500
OVERLAP_SECONDS = 5
WATERMARK_SAFETY_MARGIN = 1  # advance watermark to max_ts - this many seconds


class FillCollector:
    """
    Fill collection from the CLOB trades API.

    Usage::

        fills = await collector.fetch()          # no disk mutation
        inventory.record_fills(fills)           # apply
        collector.commit(fills)                  # then persist watermark
    """

    def __init__(self, client: AsyncClobClient, path: Optional[Path] = None) -> None:
        self._client = client
        self._path = path or WATERMARK_PATH
        wm, seen = self._load_watermark()
        self._watermark_ts: int = wm
        self._seen_ids: Dict[str, None] = seen

    @property
    def watermark_ts(self) -> int:
        return self._watermark_ts

    @property
    def path(self) -> Path:
        return self._path

    async def fetch(self) -> List[FillEvent]:
        """
        Fetch trades since watermark and build FillEvents.

        Does NOT mutate seen_ids or the watermark file. Trades already in
        seen_ids are skipped. On API error returns [] and leaves state intact.
        """
        try:
            after_ts = max(0, self._watermark_ts - OVERLAP_SECONDS)
            trades = await self._client.get_trades_since(after_ts)
        except Exception as exc:
            logger.warning("Fill poll API error — no fills recorded: {}", exc)
            return []

        if not trades or not isinstance(trades, list):
            return []

        fills: List[FillEvent] = []
        for trade in trades:
            if not isinstance(trade, dict):
                continue

            trade_id = str(trade.get("id", "") or trade.get("trade_id", ""))
            if not trade_id or trade_id in self._seen_ids:
                continue

            side_raw = str(trade.get("side", "")).upper()
            if side_raw in ("BUY", "BID", "B"):
                side = "BID"
            elif side_raw in ("SELL", "ASK", "S"):
                side = "ASK"
            else:
                logger.debug("Skipping trade {} with unrecognised side '{}'", trade_id[:12], side_raw)
                continue

            token_id = str(trade.get("asset_id") or trade.get("token_id") or "")
            condition_id = str(trade.get("market") or trade.get("condition_id") or "")

            try:
                price = float(trade.get("price", 0))
                size = float(trade.get("size", 0))
            except (TypeError, ValueError):
                continue

            if size <= 0:
                continue

            trade_ts = _extract_timestamp(trade)
            trade_dt = (
                datetime.fromtimestamp(trade_ts, tz=timezone.utc)
                if trade_ts > 0
                else datetime.now(timezone.utc)
            )

            fills.append(FillEvent(
                token_id=token_id,
                condition_id=condition_id,
                side=side,
                price=price,
                filled_size=size,
                timestamp=trade_dt,
                trade_id=trade_id,
            ))

        if fills:
            logger.info("Fill collector: {} new trades detected", len(fills))
        return fills

    def commit(self, fills: List[FillEvent]) -> None:
        """Mark fills seen and persist watermark. Call AFTER inventory apply."""
        if not fills:
            return
        max_ts = self._watermark_ts
        for fill in fills:
            if fill.trade_id:
                self._seen_ids[fill.trade_id] = None
            ts = int(fill.timestamp.timestamp()) if fill.timestamp else 0
            if ts > max_ts:
                max_ts = ts
        if max_ts > self._watermark_ts:
            self._watermark_ts = max(self._watermark_ts, max_ts - WATERMARK_SAFETY_MARGIN)
        while len(self._seen_ids) > MAX_SEEN_IDS:
            self._seen_ids.pop(next(iter(self._seen_ids)))
        self._save_watermark()

    def _load_watermark(self) -> tuple[int, Dict[str, None]]:
        """Load persisted watermark + seen_ids from disk, fall back to current time."""
        seen: Dict[str, None] = {}
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text())
                ts = int(data.get("watermark_ts", 0))
                for sid in data.get("seen_ids", []):
                    seen[str(sid)] = None
                if ts > 0:
                    logger.info(
                        "Loaded trade watermark: {} ({}) with {} seen IDs",
                        ts,
                        datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                        len(seen),
                    )
                    return ts, seen
        except Exception as exc:
            logger.warning("Failed to load trade watermark: {}", exc)
        return int(time.time()), seen

    def _save_watermark(self) -> None:
        """Persist watermark + recent seen_ids to disk for crash recovery."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            recent_ids = list(self._seen_ids.keys())[-MAX_SEEN_IDS:]
            self._path.write_text(json.dumps({
                "watermark_ts": self._watermark_ts,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "seen_ids": recent_ids,
            }))
        except Exception as ext:
            logger.warning("Failed to save trade watermark: {}", ext)


def _extract_timestamp(trade: Dict) -> int:
    """Extract Unix timestamp from a trade record, trying multiple field names."""
    for field_name in ("match_time", "timestamp", "created_at", "createdAt"):
        raw = trade.get(field_name)
        if raw is not None:
            try:
                ts = int(float(raw))
                if ts > 0:
                    return ts
            except (TypeError, ValueError):
                continue
    return 0
