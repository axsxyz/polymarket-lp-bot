"""
Discord webhook notifications with rich embeds.

Sends beautifully formatted embeds for:
  - Bot lifecycle (startup / shutdown)
  - Order activity (posted, filled)
  - Cycle summaries
  - Skew warnings / pauses
  - Daily reward reports
  - Errors

Rate-limited internally so we never hit Discord's 5 msg/sec webhook throttle.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp
from loguru import logger

from config.settings import Settings

# ──────────────────────────────────────────────────────────────────────
# Theme
# ──────────────────────────────────────────────────────────────────────

_COLOURS = {
    "green": 0x00D26A,
    "blue": 0x5865F2,
    "orange": 0xFF9F43,
    "red": 0xEE4B2B,
    "purple": 0xA855F7,
    "gold": 0xF5C842,
    "grey": 0x95A5A6,
    "cyan": 0x00CED1,
}

BOT_NAME = "Polymarket LP Bot"
BOT_ICON = "https://polymarket.com/icons/icon-512x512.png"

_MIN_INTERVAL = 0.25


class DiscordNotifier:
    """Async Discord webhook sender with rate limiting and rich embeds."""

    def __init__(self, settings: Settings) -> None:
        self._url = settings.DISCORD_WEBHOOK_URL
        self._enabled = bool(self._url and "discord.com" in self._url)
        self._last_send = 0.0
        self._lock = asyncio.Lock()
        self._session: Optional[aiohttp.ClientSession] = None
        self._capital = settings.TOTAL_CAPITAL

        if not self._enabled:
            logger.warning("Discord webhook not configured — alerts disabled")

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def send_startup(self, market_count: int, capital: float, mode: str) -> None:
        self._capital = capital
        embed = {
            "title": "🟢  Bot Online",
            "description": "LP Farmer is live and quoting.",
            "color": _COLOURS["green"],
            "fields": [
                {"name": "💰 Capital", "value": f"${capital:,.0f}", "inline": True},
                {"name": "📊 Markets", "value": str(market_count), "inline": True},
                {"name": "⚙️ Mode", "value": mode.upper(), "inline": True},
            ],
            "footer": {"text": BOT_NAME, "icon_url": BOT_ICON},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self._post({"embeds": [embed]})

    async def send_shutdown(self, reason: str = "manual") -> None:
        embed = {
            "title": "🔴  Bot Offline",
            "description": f"Shutdown: **{reason}**. All orders cancelled.",
            "color": _COLOURS["red"],
            "footer": {"text": BOT_NAME},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self._post({"embeds": [embed]})

    # ------------------------------------------------------------------
    # Order activity
    # ------------------------------------------------------------------

    async def send_order_posted(
        self,
        condition_id: str,
        side: str,
        token_label: str,
        price: float,
        size: float,
        order_id: str,
    ) -> None:
        side_emoji = "🟩" if side == "BID" else "🟥"
        notional = price * size
        embed = {
            "title": f"{side_emoji}  {side} Order Posted",
            "color": _COLOURS["blue"],
            "fields": [
                {"name": "Token", "value": token_label, "inline": True},
                {"name": "Price", "value": f"${price:.4f}", "inline": True},
                {"name": "Size", "value": f"{size:,.0f} shares", "inline": True},
                {"name": "Notional", "value": f"${notional:,.2f}", "inline": True},
                {"name": "Market", "value": f"`{condition_id[:16]}…`", "inline": True},
                {"name": "Order ID", "value": f"`{order_id[:12]}…`", "inline": True},
            ],
            "footer": {"text": BOT_NAME},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self._post({"embeds": [embed]})

    async def send_cycle_summary(
        self,
        cycle_index: int,
        markets_count: int,
        orders_posted: int,
        errors: int,
        elapsed: float,
        fills: int = 0,
        scoring_pct: Optional[float] = None,
    ) -> None:
        if orders_posted == 0 and errors == 0 and fills == 0:
            return

        colour = _COLOURS["green"] if errors == 0 else _COLOURS["orange"]
        status = "✅" if errors == 0 else "⚠️"

        fields = [
            {"name": "📦 Orders", "value": str(orders_posted), "inline": True},
            {"name": "📈 Markets", "value": str(markets_count), "inline": True},
            {"name": "⏱️ Duration", "value": f"{elapsed:.1f}s", "inline": True},
        ]
        if fills > 0:
            fields.append({"name": "🎯 Fills", "value": str(fills), "inline": True})
        if errors > 0:
            fields.append({"name": "❌ Errors", "value": str(errors), "inline": True})
        if scoring_pct is not None:
            fields.append({"name": "🏆 Scoring", "value": f"{scoring_pct:.0%}", "inline": True})

        embed = {
            "title": f"{status}  Cycle #{cycle_index}",
            "color": colour,
            "fields": fields,
            "footer": {"text": BOT_NAME},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self._post({"embeds": [embed]})

    async def send_fill(
        self, condition_id: str, side: str, price: float, size: float
    ) -> None:
        notional = price * size
        emoji = "🟢" if side == "BID" else "🔴"
        embed = {
            "title": f"🎯  Fill — {emoji} {side}",
            "description": f"**{size:,.1f}** shares @ **${price:.4f}**",
            "color": _COLOURS["gold"],
            "fields": [
                {"name": "Notional", "value": f"${notional:,.2f}", "inline": True},
                {"name": "Market", "value": f"`{condition_id[:16]}…`", "inline": True},
            ],
            "footer": {"text": BOT_NAME},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self._post({"embeds": [embed]})

    # ------------------------------------------------------------------
    # Skew alerts
    # ------------------------------------------------------------------

    async def send_skew_warning(
        self, condition_id: str, skew_ratio: float, heavy_side: str
    ) -> None:
        bar = "█" * int(skew_ratio * 20) + "░" * (20 - int(skew_ratio * 20))
        embed = {
            "title": "⚠️  Skew Warning",
            "color": _COLOURS["orange"],
            "fields": [
                {"name": "Market", "value": f"`{condition_id[:16]}…`", "inline": True},
                {"name": "Heavy Side", "value": f"**{heavy_side}**", "inline": True},
                {"name": "Skew", "value": f"{skew_ratio:.0%}  `{bar}`", "inline": False},
            ],
            "footer": {"text": BOT_NAME},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self._post({"embeds": [embed]})

    async def send_skew_pause(
        self, condition_id: str, skew_ratio: float, heavy_side: str, pause_seconds: int
    ) -> None:
        embed = {
            "title": "🛑  Quoting Paused",
            "description": (
                f"**{heavy_side}** side paused for **{pause_seconds // 60}min** "
                f"due to extreme skew ({skew_ratio:.0%})"
            ),
            "color": _COLOURS["red"],
            "fields": [
                {"name": "Market", "value": f"`{condition_id[:16]}…`", "inline": True},
            ],
            "footer": {"text": BOT_NAME},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self._post({"embeds": [embed]})

    # ------------------------------------------------------------------
    # Periodic status dashboard
    # ------------------------------------------------------------------

    async def send_status_dashboard(
        self,
        capital_deployed: float,
        capital_total: float,
        scoring_pct: float,
        market_count: int,
        fill_count: int,
        pnl: float,
        skew_summary: str,
    ) -> None:
        utilization = capital_deployed / max(capital_total, 1) * 100
        colour = _COLOURS["green"] if scoring_pct >= 0.7 else (
            _COLOURS["orange"] if scoring_pct >= 0.4 else _COLOURS["red"]
        )
        embed = {
            "title": "📊  Status Dashboard",
            "color": colour,
            "fields": [
                {"name": "💰 Deployed", "value": f"${capital_deployed:,.0f} / ${capital_total:,.0f} ({utilization:.0f}%)", "inline": True},
                {"name": "🏆 Scoring", "value": f"{scoring_pct:.0%}", "inline": True},
                {"name": "📈 Markets", "value": str(market_count), "inline": True},
                {"name": "🎯 Fills", "value": str(fill_count), "inline": True},
                {"name": "💵 Realised PnL", "value": f"${pnl:,.4f}", "inline": True},
                {"name": "⚖️ Skew", "value": skew_summary or "n/a", "inline": True},
            ],
            "footer": {"text": BOT_NAME},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self._post({"embeds": [embed]})

    # ------------------------------------------------------------------
    # Daily summary
    # ------------------------------------------------------------------

    async def send_daily_summary(self, summary_text: str) -> None:
        embed = {
            "title": "📋  Daily LP Report",
            "description": summary_text,
            "color": _COLOURS["purple"],
            "footer": {"text": f"{BOT_NAME} • End of Day", "icon_url": BOT_ICON},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self._post({"embeds": [embed]})

    # ------------------------------------------------------------------
    # Errors & generic
    # ------------------------------------------------------------------

    async def send_error(self, message: str) -> None:
        embed = {
            "title": "🚨  Error",
            "description": message,
            "color": _COLOURS["red"],
            "footer": {"text": BOT_NAME},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self._post({"embeds": [embed]})

    async def send_alert(
        self,
        title: str,
        message: str,
        level: str = "info",
        fields: Optional[List[Dict[str, str]]] = None,
    ) -> None:
        if not self._enabled:
            logger.debug("Discord alert suppressed (no webhook): {}", title)
            return

        level_map = {
            "info": _COLOURS["blue"],
            "success": _COLOURS["green"],
            "warning": _COLOURS["orange"],
            "error": _COLOURS["red"],
            "critical": _COLOURS["purple"],
        }
        colour = level_map.get(level, _COLOURS["blue"])
        embed: Dict[str, Any] = {
            "title": title,
            "description": message,
            "color": colour,
            "footer": {"text": BOT_NAME},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if fields:
            embed["fields"] = [
                {"name": f["name"], "value": f["value"], "inline": f.get("inline", True)}
                for f in fields
            ]
        await self._post({"embeds": [embed]})

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _post(self, payload: Dict[str, Any]) -> None:
        if not self._enabled:
            return

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
                        logger.debug("Discord rate-limited, retrying in {:.1f}s", retry_after)
                        await asyncio.sleep(retry_after)
                        async with session.post(self._url, json=payload) as retry_resp:
                            if retry_resp.status >= 400:
                                logger.debug("Discord retry failed: {}", retry_resp.status)
                    elif resp.status >= 400:
                        body = await resp.text()
                        logger.debug("Discord webhook error {}: {}", resp.status, body[:200])
            except Exception as exc:
                logger.debug("Discord send failed: {}", exc)
            finally:
                self._last_send = asyncio.get_event_loop().time()
