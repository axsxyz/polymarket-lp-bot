"""
Fill collection via the CLOB trades API.

Replaces the broken "order disappeared → must be a fill" heuristic with
actual trade data from the authenticated get_trades endpoint.

Design:
  - Poll get_trades(after=watermark) once per cycle (not per-market)
  - Deduplicate by trade ID (bounded set of recently seen IDs)
  - Advance watermark to max observed trade timestamp (not wall-clock now)
  - Persist watermark to disk for crash recovery
  - On any API error, abort without recording fills (watermark unchanged)
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from loguru import logger

from core.client import AsyncClobClient
from core.order_manager import FillEvent

WATERMARK_PATH = Path("data/trade_watermark.json")
MAX_SEEN_IDS = 1000
OVERLAP_SECONDS = 2


class FillCollector:
    """
    Cycle-level fill collection from the CLOB trades API.

    Usage::

        collector = FillCollector(client)
        # Once per cycle:
        await collector.poll()
        fills = collector.drain()
        inventory.record_fills(fills)
    """

    def __init__(self, client: AsyncClobClient) -> None:
        self._client = client
        self._watermark_ts: int = self._load_watermark()
        self._seen_ids: Dict[str, None] = {}
        self._pending_fills: List[FillEvent] = []

    @property
    def watermark_ts(self) -> int:
        return self._watermark_ts

    async def poll(self) -> None:
        """
        Fetch trades since watermark, create FillEvents, deduplicate.

        Contract: on any API error, abort without recording fills.
        The watermark is not advanced on error.
        """
        try:
            after_ts = max(0, self._watermark_ts - OVERLAP_SECONDS)
            trades = await self._client.get_trades_since(after_ts)
        except Exception as exc:
            logger.warning("Fill poll API error — no fills recorded: {}", exc)
            return

        if not trades or not isinstance(trades, list):
            return

        max_ts = self._watermark_ts
        new_count = 0

        for trade in trades:
            if not isinstance(trade, dict):
                continue

            trade_id = str(trade.get("id", ""))
            if not trade_id or trade_id in self._seen_ids:
                continue

            self._seen_ids[trade_id] = None

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
            if trade_ts > max_ts:
                max_ts = trade_ts

            trade_dt = (
                datetime.fromtimestamp(trade_ts, tz=timezone.utc)
                if trade_ts > 0
                else datetime.now(timezone.utc)
            )

            fill = FillEvent(
                token_id=token_id,
                condition_id=condition_id,
                side=side,
                price=price,
                filled_size=size,
                timestamp=trade_dt,
            )
            self._pending_fills.append(fill)
            new_count += 1

        if max_ts > self._watermark_ts:
            self._watermark_ts = max_ts
            self._save_watermark()

        # Evict oldest entries to bound memory (dict preserves insertion order)
        while len(self._seen_ids) > MAX_SEEN_IDS:
            self._seen_ids.pop(next(iter(self._seen_ids)))

        if new_count > 0:
            logger.info("Fill collector: {} new trades detected", new_count)

    def drain(self) -> List[FillEvent]:
        """Return pending fills and clear the buffer. Exactly-once delivery."""
        fills = self._pending_fills
        self._pending_fills = []
        return fills

    def _load_watermark(self) -> int:
        """Load persisted watermark from disk, fall back to current time."""
        try:
            if WATERMARK_PATH.exists():
                data = json.loads(WATERMARK_PATH.read_text())
                ts = int(data.get("watermark_ts", 0))
                if ts > 0:
                    logger.info(
                        "Loaded trade watermark: {} ({})",
                        ts,
                        datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                    )
                    return ts
        except Exception as exc:
            logger.warning("Failed to load trade watermark: {}", exc)
        return int(time.time())

    def _save_watermark(self) -> None:
        """Persist watermark to disk for crash recovery."""
        try:
            WATERMARK_PATH.parent.mkdir(parents=True, exist_ok=True)
            WATERMARK_PATH.write_text(json.dumps({
                "watermark_ts": self._watermark_ts,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }))
        except Exception as exc:
            logger.warning("Failed to save trade watermark: {}", exc)


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
