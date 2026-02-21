"""
Discord webhook notifications.

Sends colour-coded embeds for:
  - Bot lifecycle events (start, stop, errors)
  - Skew warnings / pause events
  - Large fills
  - Daily reward summaries

Rate-limited internally so we never hit Discord's 5 msg/sec webhook throttle.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

import aiohttp
from loguru import logger

from config.settings import Settings

# Discord embed colour palette
_COLOURS = {
    "info": 0x3498DB,     # blue
    "success": 0x2ECC71,  # green
    "warning": 0xF39C12,  # orange
    "error": 0xE74C3C,    # red
    "critical": 0x8E44AD,  # purple
}

# Minimum interval between messages (seconds) to avoid Discord throttling
_MIN_INTERVAL = 0.25


class DiscordNotifier:
    """Async Discord webhook sender with rate limiting."""

    def __init__(self, settings: Settings) -> None:
        self._url = settings.DISCORD_WEBHOOK_URL
        self._enabled = bool(self._url and "discord.com" in self._url)
        self._last_send = 0.0
        self._lock = asyncio.Lock()
        self._session: Optional[aiohttp.ClientSession] = None

        if not self._enabled:
            logger.warning("Discord webhook not configured — alerts disabled")

    async def close(self) -> None:
        """Close the shared aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def send_alert(
        self,
        title: str,
        message: str,
        level: str = "info",
        fields: Optional[List[Dict[str, str]]] = None,
    ) -> None:
        """Send a colour-coded embed to the configured webhook."""
        if not self._enabled:
            logger.debug("Discord alert suppressed (no webhook): {}", title)
            return

        colour = _COLOURS.get(level, _COLOURS["info"])
        embed: Dict[str, Any] = {
            "title": title,
            "description": message,
            "color": colour,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        if fields:
            embed["fields"] = [
                {"name": f["name"], "value": f["value"], "inline": f.get("inline", True)}
                for f in fields
            ]

        await self._post({"embeds": [embed]})

    async def send_daily_summary(self, summary_text: str) -> None:
        """Send the daily rewards summary as a rich embed."""
        await self.send_alert(
            title="Daily LP Summary",
            message=summary_text,
            level="success",
        )

    async def send_skew_warning(
        self, condition_id: str, skew_ratio: float, heavy_side: str
    ) -> None:
        await self.send_alert(
            title="Skew Warning",
            message=(
                f"Market `{condition_id[:16]}…` skew is **{skew_ratio:.0%}** "
                f"(heavy on **{heavy_side}**)"
            ),
            level="warning",
        )

    async def send_skew_pause(
        self, condition_id: str, skew_ratio: float, heavy_side: str, pause_seconds: int
    ) -> None:
        await self.send_alert(
            title="Skew PAUSE Activated",
            message=(
                f"Market `{condition_id[:16]}…` paused **{heavy_side}** side "
                f"for {pause_seconds}s (skew {skew_ratio:.0%})"
            ),
            level="error",
        )

    async def send_fill(
        self, condition_id: str, side: str, price: float, size: float
    ) -> None:
        await self.send_alert(
            title="Fill Detected",
            message=(
                f"**{side}** {size:.1f} shares @ {price:.4f}\n"
                f"Market: `{condition_id[:16]}…`"
            ),
            level="info",
        )

    async def send_error(self, message: str) -> None:
        await self.send_alert(title="Bot Error", message=message, level="error")

    async def send_startup(self, market_count: int, capital: float, mode: str) -> None:
        await self.send_alert(
            title="Bot Started",
            message=(
                f"Mode: **{mode}**\n"
                f"Markets: **{market_count}**\n"
                f"Capital: **${capital:,.0f}**"
            ),
            level="success",
        )

    async def send_shutdown(self, reason: str = "manual") -> None:
        await self.send_alert(
            title="Bot Shutting Down",
            message=f"Reason: {reason}",
            level="warning",
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _post(self, payload: Dict[str, Any]) -> None:
        async with self._lock:
            now = asyncio.get_event_loop().time()
            elapsed = now - self._last_send
            if elapsed < _MIN_INTERVAL:
                await asyncio.sleep(_MIN_INTERVAL - elapsed)

            try:
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession()
                session = self._session
                async with session.post(
                    self._url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 429:
                        retry_after = float(
                            (await resp.json()).get("retry_after", 1.0)
                        )
                        logger.warning("Discord rate-limited, retrying in {:.1f}s", retry_after)
                        await asyncio.sleep(retry_after)
                        async with session.post(self._url, json=payload) as retry_resp:
                            if retry_resp.status >= 400:
                                logger.warning("Discord retry failed: {}", retry_resp.status)
                    elif resp.status >= 400:
                        body = await resp.text()
                        logger.warning("Discord webhook error {}: {}", resp.status, body[:200])
            except Exception as exc:
                logger.warning("Discord send failed: {}", exc)
            finally:
                self._last_send = asyncio.get_event_loop().time()
