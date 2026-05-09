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
from dataclasses import asdict, is_dataclass
from functools import wraps
from typing import Any, Dict, List, Optional, Union

from loguru import logger
from py_clob_client_v2 import (
    ApiCreds,
    BalanceAllowanceParams,
    BookParams,
    ClobClient,
    MarketOrderArgs,
    OpenOrderParams,
    OrderArgs,
    OrderMarketCancelParams,
    OrderPayload,
    OrdersScoringParams,
    OrderType,
    TradeParams,
)

BUY, SELL = "BUY", "SELL"

from config.settings import Settings


def _book_params_to_json(params: List[Any]) -> List[dict]:
    """Convert BookParams dataclass instances to dicts for httpx JSON encoding."""
    out: List[dict] = []
    for p in params:
        if is_dataclass(p):
            out.append(asdict(p))
        elif isinstance(p, dict):
            out.append(p)
        else:
            raise TypeError(
                f"get_midpoints/get_order_books expect BookParams or dict, got {type(p)}"
            )
    return out

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
                now = time.monotonic()
                self._tokens += (now - self._last) * self._rate
                self._last = now
            self._tokens -= 1.0


# ---------------------------------------------------------------------------
# Retry decorator
# ---------------------------------------------------------------------------

_NON_RETRYABLE_PATTERNS = (
    "couldn't be fully filled",
    "is invalid. duplicated",
    "invalid amount for a marketable",
    "not enough balance",
    "not enough allowance",
)


def _is_non_retryable(exc: Exception) -> bool:
    """Return True if the exception represents a terminal API error (not transient)."""
    msg = str(exc).lower()
    return any(p in msg for p in _NON_RETRYABLE_PATTERNS)


def _with_retry(max_retries: int = 3, base_delay: float = 1.0):
    """Decorator that retries on transient exceptions with exponential backoff.

    Terminal errors (FOK kills, duplicated orders, invalid amounts) are raised
    immediately without wasting retries.
    """

    def decorator(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            last_exc: Optional[Exception] = None
            for attempt in range(max_retries + 1):
                try:
                    return await fn(*args, **kwargs)
                except Exception as exc:
                    last_exc = exc
                    if _is_non_retryable(exc):
                        logger.debug(
                            "Non-retryable error in {} — {}",
                            fn.__name__,
                            exc,
                        )
                        raise
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
    Async facade over `py_clob_client_v2.ClobClient`.

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

        creds = await asyncio.to_thread(self._client.create_or_derive_api_key)
        self._client.set_api_creds(creds)
        logger.info("API credentials derived successfully")

        await self._sync_balance_allowances()

    async def _sync_balance_allowances(self) -> None:
        """Sync USDC (COLLATERAL) balance & allowance with the CLOB server.

        Required for the server to accept BID orders. CONDITIONAL (ERC1155) sync
        is done per token_id after markets are loaded — see sync_conditional_allowances().
        """
        sig_type = self._settings.SIGNATURE_TYPE
        try:
            await asyncio.to_thread(
                self._client.update_balance_allowance,
                BalanceAllowanceParams(asset_type="COLLATERAL", signature_type=sig_type),
            )
            logger.info("Balance/allowance synced with CLOB (COLLATERAL)")
        except Exception as exc:
            logger.warning("Failed to sync USDC allowance (non-blocking): {}", exc)

    async def sync_collateral(self, log_success: bool = False) -> None:
        """Re-sync USDC (COLLATERAL) with the CLOB. Call before posting orders so the server
        has fresh balance/allowance (e.g. for collateralized ASKs if supported).
        """
        sig_type = self._settings.SIGNATURE_TYPE
        try:
            await asyncio.to_thread(
                self._client.update_balance_allowance,
                BalanceAllowanceParams(asset_type="COLLATERAL", signature_type=sig_type),
            )
            if log_success:
                logger.info("Balance/allowance synced with CLOB (COLLATERAL)")
            else:
                logger.debug("COLLATERAL re-synced with CLOB")
        except Exception as exc:
            logger.warning("Failed to re-sync COLLATERAL (non-blocking): {}", exc)

    async def sync_conditional_allowances(self, token_ids: List[str]) -> None:
        """Sync ERC1155 (CONDITIONAL) balance & allowance per token_id so the CLOB accepts ASK orders.

        The API requires a valid token_id for CONDITIONAL; call this after markets are loaded
        with the list of token_id_yes and token_id_no for active markets.
        """
        if not token_ids:
            return
        sig_type = self._settings.SIGNATURE_TYPE
        synced = 0
        for token_id in token_ids:
            if not token_id:
                continue
            try:
                await asyncio.to_thread(
                    self._client.update_balance_allowance,
                    BalanceAllowanceParams(
                        asset_type="CONDITIONAL",
                        token_id=token_id,
                        signature_type=sig_type,
                    ),
                )
                synced += 1
            except Exception as exc:
                logger.warning(
                    "Failed to sync CONDITIONAL for {} (non-blocking): {}",
                    token_id[:16],
                    exc,
                )
        if synced:
            logger.info("Balance/allowance synced with CLOB (CONDITIONAL, {} token(s))", synced)

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
    async def get_order_books(self, params: List[Union[BookParams, dict]]) -> Any:
        return await self._call("get_order_books", _book_params_to_json(params))

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
    async def get_midpoints(self, params: List[Union[BookParams, dict]]) -> Any:
        """Batch midpoint fetch for multiple tokens at once."""
        return await self._call("get_midpoints", _book_params_to_json(params))

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

    async def are_orders_scoring(self, order_ids: List[str]) -> Dict[str, bool]:
        """Check if specific orders are currently scoring LP rewards.

        Args:
            order_ids: List of order ID strings to check.

        Returns:
            Dict mapping order_id -> is_scoring. Empty dict on failure.
        """
        if not order_ids:
            return {}
        try:
            params = OrdersScoringParams(orderIds=order_ids)
            result = await self._call("are_orders_scoring", params)
            scoring_count = 0
            total = 0
            if isinstance(result, dict):
                total = len(result)
                scoring_count = sum(1 for v in result.values() if v)
            elif isinstance(result, list):
                total = len(result)
                scoring_count = sum(1 for v in result if v)
            logger.debug(
                "are_orders_scoring: {}/{} scoring (type={})",
                scoring_count, total, type(result).__name__,
            )
            if isinstance(result, dict):
                return {str(k): bool(v) for k, v in result.items()}
            if isinstance(result, list):
                return {oid: bool(v) for oid, v in zip(order_ids, result)}
            return {}
        except Exception as exc:
            logger.warning("are_orders_scoring failed for {} orders: {}", len(order_ids), exc)
            return {}

    # ── Authenticated (L2) methods ──────────────────────────────────

    @_with_retry()
    async def get_orders(self, params: Optional[OpenOrderParams] = None) -> List[Dict]:
        return await self._call("get_open_orders", params or OpenOrderParams())

    @_with_retry()
    async def get_trades(self, **kwargs) -> List[Dict]:
        return await self._call("get_trades", **kwargs)

    @_with_retry(max_retries=2)
    async def get_trades_since(self, after_ts: int) -> List[Dict]:
        """Fetch user's own trades since the given Unix timestamp."""
        return await self._call("get_trades", TradeParams(after=after_ts))

    async def create_order(self, order_args: OrderArgs) -> Any:
        """Sign a limit order (EIP-712) — local crypto, no network call."""
        fn = getattr(self._client, "create_order")
        return await asyncio.to_thread(fn, order_args)

    @_with_retry(max_retries=2)
    async def post_order(self, signed_order: Any, order_type: OrderType = OrderType.GTC) -> Dict:
        return await self._call("post_order", signed_order, order_type)

    @_with_retry(max_retries=2)
    async def create_market_order(self, order_args: MarketOrderArgs) -> Any:
        """Sign a market order (FOK) — does NOT post it yet."""
        return await self._call("create_market_order", order_args)

    async def create_and_post_order(
        self,
        order_args: OrderArgs,
        order_type: OrderType = OrderType.GTC,
    ) -> Dict:
        """Sign + post a limit order. Default is GTC; pass FOK for immediate-fill seeds."""
        signed = await self.create_order(order_args)
        return await self.post_order(signed, order_type)

    @_with_retry(max_retries=2)
    async def cancel(self, order_id: str) -> Any:
        return await self._call("cancel_order", OrderPayload(orderID=order_id))

    @_with_retry(max_retries=2)
    async def cancel_all(self) -> Any:
        return await self._call("cancel_all")

    @_with_retry(max_retries=2)
    async def cancel_market_orders(self, market: str) -> Any:
        """Cancel all orders for a specific market condition_id."""
        return await self._call(
            "cancel_market_orders", OrderMarketCancelParams(market=market)
        )

    async def get_balance_allowance(self, params: Optional[Dict] = None) -> Any:
        """Fetch user's conditional token balances for reconciliation."""
        try:
            if params:
                return await self._call("get_balance_allowance", params)
            return await self._call("get_balance_allowance")
        except Exception as exc:
            logger.debug("get_balance_allowance failed: {}", exc)
            return None

    async def get_conditional_balance(self, token_id: str) -> float:
        """Return the balance in shares (human-readable) for a conditional token.

        The CLOB API returns raw units with 6 decimals (like USDC),
        so 6000000 raw = 6.0 shares.  Returns 0.0 on any error.
        """
        try:
            resp = await self._call(
                "get_balance_allowance",
                BalanceAllowanceParams(
                    asset_type="CONDITIONAL",
                    token_id=token_id,
                    signature_type=self._settings.SIGNATURE_TYPE,
                ),
            )
            if isinstance(resp, dict):
                raw = float(resp.get("balance", 0) or 0)
                # Normalize: raw uses 6 decimal places (USDC precision)
                if raw > 1_000:
                    return raw / 1_000_000
                return raw
            return 0.0
        except Exception as exc:
            logger.debug("get_conditional_balance failed for {}: {}", token_id[:16], exc)
            return 0.0

    async def log_conditional_allowance_and_balance(self, token_id: str) -> None:
        """
        Log on-chain ERC1155 allowance (isApprovedForAll per operator) and balance
        for the given token_id. Call before posting ASK to debug «not enough balance/allowance».
        """
        from eth_account import Account

        owner = self._settings.FUNDER_ADDRESS
        if not owner and self._settings.PRIVATE_KEY:
            try:
                owner = Account.from_key(self._settings.PRIVATE_KEY).address
            except Exception:
                owner = ""
        if not owner:
            logger.warning("log_conditional_allowance_and_balance: no owner (set FUNDER_ADDRESS or PRIVATE_KEY)")
            return
        rpc_url = __import__("os").environ.get("POLYGON_RPC_URL", "https://polygon.drpc.org")
        from utils.allowance_check import fetch_conditional_allowance_and_balance

        result = await asyncio.to_thread(
            fetch_conditional_allowance_and_balance,
            owner,
            token_id,
            rpc_url,
        )
        if result.get("error"):
            logger.warning(
                "Allowance/balance check failed: {}",
                result.get("error"),
            )
            return
        ops = result.get("operators") or {}
        balance = result.get("balance")
        logger.info(
            "ASK pre-check token_id={}… owner={} | CTF_Exchange={} NegRisk_Exchange={} NegRisk_Adapter={} | balance={}",
            token_id[:16],
            result.get("owner", "")[-10:],
            ops.get("CTF Exchange"),
            ops.get("Neg Risk Exchange"),
            ops.get("Neg Risk Adapter"),
            balance,
        )
