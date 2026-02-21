"""
Pure-function utilities shared across modules.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone


# --------------------------------------------------------------------------
# Price helpers
# --------------------------------------------------------------------------

_TICK_MAP = {
    "0.1": 0.1,
    "0.01": 0.01,
    "0.001": 0.001,
    "0.0001": 0.0001,
}


def round_to_tick(price: float, tick_size: str = "0.01") -> float:
    """Round a price to the nearest valid tick increment."""
    tick = _TICK_MAP.get(tick_size, 0.01)
    return round(round(price / tick) * tick, 4)


def clamp_price(price: float, lo: float = 0.01, hi: float = 0.99) -> float:
    """Clamp a price into the valid Polymarket range."""
    return max(lo, min(hi, price))


# --------------------------------------------------------------------------
# Reward estimation
# --------------------------------------------------------------------------

def estimate_reward_score(
    max_spread: float,
    spread_from_mid: float,
    size: float,
) -> float:
    """
    Instantaneous Q contribution of one resting order.

    S(v, s) = ((v - s) / v)^2 * size

    where v = max_spread, s = actual spread from adjusted mid.
    """
    if max_spread <= 0 or spread_from_mid >= max_spread:
        return 0.0
    ratio = (max_spread - spread_from_mid) / max_spread
    return (ratio ** 2) * size


def two_sided_q_score(
    max_spread: float,
    bid_spread: float,
    bid_size: float,
    ask_spread: float,
    ask_size: float,
    scaling_factor: float = 3.0,
) -> float:
    """
    Estimate the min(Q_one, Q_two) score for a two-sided quote.

    For simplicity this assumes our quotes are symmetric on m and m'.
    The actual protocol samples every minute, but this gives a good
    instantaneous approximation.
    """
    q_one = estimate_reward_score(max_spread, bid_spread, bid_size)
    q_two = estimate_reward_score(max_spread, ask_spread, ask_size)

    q_min = min(q_one, q_two)
    q_max_single = max(q_one, q_two) / scaling_factor

    return max(q_min, q_max_single)


# --------------------------------------------------------------------------
# Math / safety
# --------------------------------------------------------------------------

def safe_div(a: float, b: float, default: float = 0.0) -> float:
    """Division that returns `default` on zero denominator."""
    return a / b if b != 0 else default


def pct(value: float, total: float) -> float:
    """Percentage as a 0-100 float."""
    return safe_div(value, total) * 100


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def seconds_until_midnight_utc() -> float:
    """Seconds remaining until next midnight UTC (for reward scheduling)."""
    now = utc_now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if now >= midnight:
        from datetime import timedelta

        midnight += timedelta(days=1)
    return (midnight - now).total_seconds()
