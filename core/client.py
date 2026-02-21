"""
Async wrapper around the synchronous py-clob-client SDK.

py-clob-client uses `requests` internally, so every call is blocking.
We run each SDK call inside `asyncio.to_thread()` to avoid stalling the
event loop, and layer on:

  - Token-bucket rate limiter  (stays under the ~100 req/min CLOB limit)
  - Exponential-backoff retry  (handles transient 429/5xx errors)
  - Structured logging via loguru
"""

from __future__ import annotations

import asyncio
import time
from functools import wraps
from typing import Any, Dict, List, Optional

from loguru import logger
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    BookParams,
    MarketOrderArgs,
    OpenOrderParams,
    OrderArgs,
    OrderType,
)
from py_clob_client.order_builder.constants import BUY, SELL

try:
    from py_clob_client.clob_types import TradeParams as _TradeParams
except ImportError:
    _TradeParams = None  # type: ignore[assignment,misc]

from config.settings import Settings

# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class TokenBucketRateLimiter:
    """
    Async-safe token bucket.  Tokens refill at `rate` per second up to `capacity`.
    Each API call consumes one token; if the bucket is empty the caller sleeps
    until a token becomes available.
    """

    def __init__(self, rate: float = 1.3, capacity: float = 10.0):
        self._rate = rate
        self._capacity = capacity
        self._tokens = capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._last = now

            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self._rate
                await asyncio.sleep(wait)
                self._tokens = 0.0
                self._last = time.monotonic()
            else:
                self._tokens -= 1.0


# ---------------------------------------------------------------------------
# Retry decorator
# ---------------------------------------------------------------------------

def _with_retry(max_retries: int = 3, base_delay: float = 1.0):
    """Decorator that retries on transient exceptions with exponential backoff."""

    def decorator(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            last_exc: Optional[Exception] = None
            for attempt in range(max_retries + 1):
                try:
                    return await fn(*args, **kwargs)
                except Exception as exc:
                    last_exc = exc
                    if attempt < max_retries:
                        delay = base_delay * (2 ** attempt)
                        logger.warning(
                            "Retry {}/{} for {} after {:.1f}s — {}",
                            attempt + 1,
                            max_retries,
                            fn.__name__,
                            delay,
                            exc,
                        )
                        await asyncio.sleep(delay)
                    else:
                        logger.error(
                            "All {} retries exhausted for {}: {}",
                            max_retries,
                            fn.__name__,
                            exc,
                        )
            raise last_exc  # type: ignore[misc]

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Async CLOB client
# ---------------------------------------------------------------------------

class AsyncClobClient:
    """
    Async facade over `py_clob_client.ClobClient`.

    Usage::

        client = AsyncClobClient(settings)
        await client.initialize()          # derives API creds
        book = await client.get_order_book(token_id)
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._limiter = TokenBucketRateLimiter(
            rate=settings.API_RATE_LIMIT_RPS,
            capacity=settings.API_RATE_LIMIT_BURST,
        )
        self._client: Optional[ClobClient] = None

    async def initialize(self) -> None:
        """Create the underlying SDK client and derive API credentials."""
        self._client = await asyncio.to_thread(self._build_client)
        logger.info("ClobClient initialised (signature_type={})", self._settings.SIGNATURE_TYPE)

        creds = await asyncio.to_thread(self._client.create_or_derive_api_creds)
        self._client.set_api_creds(creds)
        logger.info("API credentials derived successfully")

    def _build_client(self) -> ClobClient:
        kwargs: Dict[str, Any] = {
            "host": self._settings.CLOB_HOST,
            "key": self._settings.PRIVATE_KEY,
            "chain_id": self._settings.CHAIN_ID,
            "signature_type": self._settings.SIGNATURE_TYPE,
        }
        if self._settings.FUNDER_ADDRESS:
            kwargs["funder"] = self._settings.FUNDER_ADDRESS
        return ClobClient(**kwargs)

    @property
    def raw(self) -> ClobClient:
        assert self._client is not None, "Call initialize() first"
        return self._client

    # ── helpers ──────────────────────────────────────────────────────

    async def _call(self, method_name: str, *args, **kwargs) -> Any:
        """Rate-limited, thread-offloaded call to the underlying SDK."""
        await self._limiter.acquire()
        fn = getattr(self._client, method_name)
        return await asyncio.to_thread(fn, *args, **kwargs)

    # ── Public (unauthenticated) methods ─────────────────────────────

    @_with_retry()
    async def get_ok(self) -> Any:
        return await self._call("get_ok")

    @_with_retry()
    async def get_server_time(self) -> Any:
        return await self._call("get_server_time")

    @_with_retry()
    async def get_sampling_simplified_markets(self, next_cursor: str = "") -> Dict[str, Any]:
        return await self._call("get_sampling_simplified_markets", next_cursor)

    @_with_retry()
    async def get_sampling_markets(self, next_cursor: str = "") -> Dict[str, Any]:
        return await self._call("get_sampling_markets", next_cursor)

    @_with_retry()
    async def get_market(self, condition_id: str) -> Dict[str, Any]:
        return await self._call("get_market", condition_id)

    @_with_retry()
    async def get_order_book(self, token_id: str) -> Any:
        return await self._call("get_order_book", token_id)

    @_with_retry()
    async def get_order_books(self, params: List[BookParams]) -> Any:
        return await self._call("get_order_books", params)

    async def get_midpoint(self, token_id: str) -> Any:
        """Silent on invalid token id to reduce log spam."""
        try:
            return await self._call("get_midpoint", token_id)
        except Exception as exc:
            if "Invalid token id" in str(exc) or "400" in str(exc):
                logger.debug("Skipping invalid token_id: {}", token_id[:20])
                return None
            raise

    @_with_retry(max_retries=2)
    async def get_midpoints(self, params: List) -> Any:
        """Batch midpoint fetch for multiple tokens at once."""
        return await self._call("get_midpoints", params)

    @_with_retry()
    async def get_price(self, token_id: str, side: str) -> Any:
        return await self._call("get_price", token_id, side)

    @_with_retry()
    async def get_last_trade_price(self, token_id: str) -> Any:
        return await self._call("get_last_trade_price", token_id)

    @_with_retry()
    async def get_tick_size(self, token_id: str) -> Any:
        return await self._call("get_tick_size", token_id)

    @_with_retry()
    async def get_neg_risk(self, token_id: str) -> bool:
        return await self._call("get_neg_risk", token_id)

    async def are_orders_scoring(self, condition_id: str) -> bool:
        """Check if our orders on a market meet LP reward criteria."""
        try:
            result = await self._call("are_orders_scoring", condition_id)
            if isinstance(result, bool):
                return result
            if isinstance(result, dict):
                return bool(result.get("scoring", False))
            return bool(result)
        except Exception:
            return False

    # ── Authenticated (L2) methods ──────────────────────────────────

    @_with_retry()
    async def get_orders(self, params: Optional[OpenOrderParams] = None) -> List[Dict]:
        return await self._call("get_orders", params or OpenOrderParams())

    @_with_retry()
    async def get_trades(self, **kwargs) -> List[Dict]:
        return await self._call("get_trades", **kwargs)

    @_with_retry(max_retries=2)
    async def get_trades_since(self, after_ts: int) -> List[Dict]:
        """Fetch user's own trades since the given Unix timestamp."""
        if _TradeParams is not None:
            params = _TradeParams(after=after_ts)
            return await self._call("get_trades", params)
        all_trades = await self._call("get_trades")
        if not isinstance(all_trades, list):
            return []
        return [
            t for t in all_trades
            if isinstance(t, dict)
            and int(float(t.get("match_time", 0) or 0)) >= after_ts
        ]

    @_with_retry(max_retries=2)
    async def create_order(self, order_args: OrderArgs) -> Any:
        """Sign a limit order (EIP-712) — does NOT post it yet."""
        return await self._call("create_order", order_args)

    @_with_retry(max_retries=2)
    async def post_order(self, signed_order: Any, order_type: OrderType = OrderType.GTC) -> Dict:
        return await self._call("post_order", signed_order, order_type)

    async def create_and_post_order(self, order_args: OrderArgs) -> Dict:
        """Convenience: sign + post a GTC limit order in one shot."""
        signed = await self.create_order(order_args)
        return await self.post_order(signed, OrderType.GTC)

    @_with_retry(max_retries=2)
    async def cancel(self, order_id: str) -> Any:
        return await self._call("cancel", order_id)

    @_with_retry(max_retries=2)
    async def cancel_all(self) -> Any:
        return await self._call("cancel_all")

    @_with_retry(max_retries=2)
    async def cancel_market_orders(self, market: str) -> Any:
        """Cancel all orders for a specific market condition_id."""
        return await self._call("cancel_market_orders", market)

    async def get_balance_allowance(self, params: Optional[Dict] = None) -> Any:
        """Fetch user's conditional token balances for reconciliation."""
        try:
            if params:
                return await self._call("get_balance_allowance", params)
            return await self._call("get_balance_allowance")
        except Exception as exc:
            logger.debug("get_balance_allowance failed: {}", exc)
            return None
