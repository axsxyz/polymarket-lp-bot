#!/usr/bin/env python3
"""
Sell (liquidate) conditional token positions back to USDC.

Fetches ALL positions from the Polymarket Data API (not just cached markets),
shows balances, and sells via aggressive GTC+cancel orders.

Usage:
    python scripts/sell_positions.py              # sell all positions
    python scripts/sell_positions.py --check-only # just show balances, don't sell
    python scripts/sell_positions.py --token-id X # sell a specific token only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

import aiohttp
from eth_account import Account
from loguru import logger
from py_clob_client_v2 import OrderArgs, OrderType

SELL = "SELL"

from config.settings import Settings
from core.client import AsyncClobClient

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CACHE_FILE = ROOT / "data" / "selected_markets.json"


# ── Fetch all positions from the public Data API ─────────────────────

async def fetch_all_positions(address: str) -> list[dict]:
    """Call Polymarket Data API to get every open position for the wallet."""
    url = f"{DATA_API}/positions"
    params = {"user": address, "limit": 500, "sizeThreshold": 0.01}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning(
                        "Data API /positions returned {} — {}",
                        resp.status,
                        text[:200],
                    )
                    return []
                data = await resp.json()
                if isinstance(data, list):
                    return data
                if isinstance(data, dict):
                    return data.get("positions", data.get("data", []))
                return []
    except Exception as exc:
        logger.warning("Data API /positions failed: {}", exc)
        return []


async def enrich_with_gamma(condition_ids: set[str]) -> dict[str, str]:
    """Fetch market titles from Gamma API for display. Returns {condition_id: title}."""
    titles: dict[str, str] = {}
    if not condition_ids:
        return titles

    try:
        async with aiohttp.ClientSession() as session:
            for cid in condition_ids:
                url = f"{GAMMA_API}/markets"
                params = {"conditionId": cid, "limit": 5}
                try:
                    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if isinstance(data, list):
                                for item in data:
                                    item_cid = item.get("conditionId", "") or item.get("condition_id", "")
                                    if item_cid == cid:
                                        titles[cid] = item.get("question", "")[:80]
                                        break
                except Exception:
                    pass
    except Exception:
        pass
    return titles


def load_tokens_from_cache() -> dict[str, dict]:
    """Load token info from cache as fallback. Returns {token_id: info_dict}."""
    if not CACHE_FILE.exists():
        return {}

    with open(CACHE_FILE) as f:
        markets = json.load(f)

    result = {}
    for m in markets:
        question = m.get("question", "")[:60]
        cid = m.get("condition_id", "")
        for label, key in [("YES", "token_id_yes"), ("NO", "token_id_no")]:
            tid = m.get(key, "")
            if tid:
                result[tid] = {
                    "token_id": tid,
                    "label": label,
                    "condition_id": cid,
                    "display": f"{question} [{label}]",
                    "tick_size": m.get("tick_size", "0.01"),
                }
    return result


# ── Build unified position list ──────────────────────────────────────

async def discover_all_positions(
    client: AsyncClobClient,
    address: str,
) -> list[dict]:
    """Discover every position: Data API first, cache as fallback, CLOB balance check."""
    positions: list[dict] = []
    seen_tokens: set[str] = set()

    # 1) Data API — authoritative source for ALL positions
    api_positions = await fetch_all_positions(address)
    if api_positions:
        logger.info("Data API returned {} position(s)", len(api_positions))

        condition_ids: set[str] = set()
        for p in api_positions:
            cid = p.get("conditionId", "") or p.get("condition_id", "")
            if cid:
                condition_ids.add(cid)

        titles = await enrich_with_gamma(condition_ids)

        for p in api_positions:
            token_id = p.get("asset", "") or p.get("token_id", "")
            if not token_id or token_id in seen_tokens:
                continue

            size = float(p.get("size", 0) or 0)
            if size <= 0.01:
                continue

            cid = p.get("conditionId", "") or p.get("condition_id", "")
            outcome = p.get("outcome", "")
            title = titles.get(cid, p.get("title", ""))
            cur_price = float(p.get("currPrice", 0) or p.get("cur_price", 0) or 0)

            label = outcome if outcome else ("YES" if "yes" in token_id.lower() else "")
            display = f"{title} [{label}]" if title else f"Token {token_id[:20]}… [{label}]"

            positions.append({
                "token_id": token_id,
                "label": label or "?",
                "condition_id": cid,
                "display": display,
                "size": size,
                "cur_price": cur_price,
                "avg_price": float(p.get("avgPrice", 0) or 0),
                "pnl": float(p.get("totalPnl", 0) or p.get("cashPnl", 0) or 0),
            })
            seen_tokens.add(token_id)

    # 2) Cache fallback — check any tokens not already found via API
    cache = load_tokens_from_cache()
    for tid, info in cache.items():
        if tid in seen_tokens:
            continue
        balance = await client.get_conditional_balance(tid)
        if balance > 0.01:
            positions.append({
                "token_id": tid,
                "label": info["label"],
                "condition_id": info["condition_id"],
                "display": info["display"],
                "size": balance,
                "cur_price": 0.0,
                "avg_price": 0.0,
                "pnl": 0.0,
            })
            seen_tokens.add(tid)

    return positions


SELL_TIMEOUT = 5.0
SELL_MAX_ATTEMPTS = 3


async def _sell_with_gtc(
    client: AsyncClobClient,
    token_id: str,
    shares: float,
    mid: float,
) -> float:
    """Sell shares using GTC + timeout with escalating aggressiveness.

    Returns estimated USD proceeds from whatever was sold.
    """
    initial_bal = await client.get_conditional_balance(token_id)
    total_sold_value = 0.0

    for attempt in range(1, SELL_MAX_ATTEMPTS + 1):
        current_bal = await client.get_conditional_balance(token_id)
        remaining = int(current_bal)
        if remaining < 1:
            break

        discount_ticks = {1: 1, 2: 2, 3: 4}
        discount = discount_ticks.get(attempt, 4) * 0.01
        sell_price = round(max(0.01, mid - discount), 2)

        logger.info(
            "    Selling {:.2f} shares @ ${:.4f} GTC attempt {}/{}…",
            remaining, sell_price, attempt, SELL_MAX_ATTEMPTS,
        )

        order_id = None
        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=sell_price,
                size=remaining,
                side=SELL,
            )
            resp = await client.create_and_post_order(order_args, order_type=OrderType.GTC)

            if isinstance(resp, dict):
                order_id = resp.get("orderID", "") or resp.get("id", "")

            if not order_id:
                logger.warning("    ⚠ No order ID in response: {}", resp)
                continue

            await asyncio.sleep(SELL_TIMEOUT)

        except Exception as exc:
            logger.error("    ❌ Sell order failed: {}", exc)
            continue
        finally:
            if order_id:
                try:
                    await client.cancel(order_id)
                except Exception:
                    pass

        new_bal = await client.get_conditional_balance(token_id)
        filled = current_bal - new_bal
        if filled > 0.01:
            proceeds = filled * sell_price
            total_sold_value += proceeds
            logger.info(
                "    ✅ Sold {:.2f} @ ${:.4f} — ~${:.2f}",
                filled, sell_price, proceeds,
            )
        else:
            logger.warning(
                "    ⚠ 0 shares filled at ${:.4f}, {}",
                sell_price,
                "escalating discount…" if attempt < SELL_MAX_ATTEMPTS else "giving up.",
            )

    final_bal = await client.get_conditional_balance(token_id)
    total_filled = initial_bal - final_bal
    if total_filled > 0.01:
        logger.info("    Total sold: {:.2f} shares", total_filled)
    elif initial_bal > 1:
        logger.error(
            "    Could not sell {:.2f} shares after {} attempts — book may be empty",
            initial_bal, SELL_MAX_ATTEMPTS,
        )

    return total_sold_value


# ── Dust helpers ─────────────────────────────────────────────────────

DUST_THRESHOLD_USD = 1.0  # strict < 1.00


def _current_value(p: dict) -> float | None:
    """Estimated position value in USD from Data API fields.

    Returns ``None`` when the price is unknown (``cur_price == 0``) so
    the caller can decide whether to keep the position or not.
    Cache-fallback positions always return ``None``.
    """
    cur_price = float(p.get("cur_price", 0) or 0)
    if cur_price <= 0:
        return None
    return float(p.get("size", 0) or 0) * cur_price


def _is_dust(p: dict) -> bool:
    """True only when the estimated value is known AND < $1.00.

    Positions with unknown price (cur_price == 0, e.g. cache-fallback)
    are conservatively kept — we cannot rule them out.
    """
    value = _current_value(p)
    return value is not None and value < DUST_THRESHOLD_USD


# ── Display and sell ─────────────────────────────────────────────────

async def show_and_sell(
    client: AsyncClobClient,
    positions: list[dict],
    sell: bool = True,
    token_filter: str = "",
) -> None:
    """Display all positions and optionally sell them."""
    if token_filter:
        positions = [p for p in positions if p["token_id"] == token_filter]

    if not positions:
        logger.info("No positions found — nothing to do.")
        return

    total_value = 0.0
    total_proceeds = 0.0

    logger.info("")
    logger.info("  {:<55} {:>8} {:>8} {:>10} {:>8}", "Market", "Shares", "Price", "Value", "PnL")
    logger.info("  " + "─" * 95)

    for p in positions:
        token_id = p["token_id"]

        # Always fetch live CLOB balance (more accurate than Data API for recent trades)
        balance = await client.get_conditional_balance(token_id)
        if balance <= 0.01:
            continue

        # Fetch live midpoint
        mid = 0.0
        try:
            mid_resp = await client.get_midpoint(token_id)
            if isinstance(mid_resp, dict):
                mid = float(mid_resp.get("mid", 0))
            elif mid_resp is not None:
                mid = float(mid_resp)
        except Exception:
            pass

        if mid <= 0 and p["cur_price"] > 0:
            mid = p["cur_price"]

        value = balance * mid
        total_value += value
        pnl = p.get("pnl", 0.0)

        pnl_str = f"${pnl:+.2f}" if pnl != 0 else "—"
        logger.info(
            "  {:<55} {:>8.2f} {:>7.4f} {:>9.2f} {:>8}",
            p["display"][:55],
            balance,
            mid,
            value,
            pnl_str,
        )

        if not sell:
            continue

        if mid < 0.02:
            logger.warning("    ⚠ Skipping — midpoint too low ({:.4f})", mid)
            continue

        sell_shares = int(balance)
        if sell_shares < 1:
            logger.warning("    ⚠ Skipping — balance < 1 share")
            continue

        sold = await _sell_with_gtc(client, token_id, sell_shares, mid)
        if sold > 0:
            total_proceeds += sold

    logger.info("  " + "─" * 95)
    logger.info("  Total portfolio value: ${:.2f}", total_value)
    if sell and total_proceeds > 0:
        logger.info("  Total estimated proceeds: ${:.2f}", total_proceeds)
    if not sell:
        logger.info("\n  Run without --check-only to sell these positions.")


# ── Main ─────────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(description="Sell conditional token positions")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Just show balances, don't sell",
    )
    parser.add_argument(
        "--token-id",
        type=str,
        default="",
        help="Sell only this specific token ID",
    )
    args = parser.parse_args()

    settings = Settings()

    # Derive wallet address
    address = settings.FUNDER_ADDRESS
    if not address and settings.PRIVATE_KEY:
        try:
            address = Account.from_key(settings.PRIVATE_KEY).address
        except Exception as exc:
            logger.error("Cannot derive address from PRIVATE_KEY: {}", exc)
            return

    logger.info("=" * 60)
    logger.info("  Polymarket Position Manager")
    logger.info("  Wallet: {}", address)
    logger.info("  Mode: {}", "CHECK ONLY" if args.check_only else "SELL ALL")
    logger.info("=" * 60)

    client = AsyncClobClient(settings)
    await client.initialize()

    if args.token_id:
        positions = [{
            "token_id": args.token_id,
            "label": "CUSTOM",
            "condition_id": "",
            "display": f"Token {args.token_id[:24]}…",
            "size": 0,
            "cur_price": 0.0,
            "avg_price": 0.0,
            "pnl": 0.0,
        }]
    else:
        logger.info("Fetching all positions…")
        positions = await discover_all_positions(client, address)
        logger.info("Found {} position(s) with balance > 0", len(positions))

        dust = [p for p in positions if _is_dust(p)]
        if dust:
            logger.info("")
            logger.info(
                "  {} dust position(s) detected (estimated value < ${:.2f} each, "
                "based on Data API price — live mid may differ):",
                len(dust), DUST_THRESHOLD_USD,
            )
            for p in dust:
                val = _current_value(p)
                logger.info(
                    "    • {:<55}  ~${:.3f}",
                    p["display"][:55],
                    val if val is not None else 0.0,
                )
            logger.info("")
            try:
                answer = input(
                    f"  Include dust in display/sell? [y/N]: "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = ""
            include_dust = answer in ("y", "yes")
            if not include_dust:
                positions = [p for p in positions if not _is_dust(p)]
                logger.info(
                    "  Dust excluded — {} position(s) remaining.",
                    len(positions),
                )
            else:
                logger.info("  Dust included — processing all {} position(s).", len(positions))
            logger.info("")

    await show_and_sell(
        client,
        positions,
        sell=not args.check_only,
        token_filter=args.token_id,
    )

    logger.info("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
