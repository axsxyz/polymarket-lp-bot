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


def alloc_side_shares(allocated_capital: float, price: float) -> float:
    """Shares for one side from half of market allocation at ``price``.

    Notional = shares × price equals ``allocated_capital / 2`` (capped by
    construction). Caller floors at ``min_size`` / skips when below it.
    """
    half = allocated_capital / 2.0
    return half / max(float(price), 0.01)


def max_bid_shares(yes: float, no: float, side: str, limit: float) -> float:
    """Largest BID size that keeps unmatched ``|YES−NO|`` at or under ``limit``.

    At-limit is OK (same strict ``>`` as ``max_loss_check``). ``side`` is
    ``YES`` or ``NO``. Returns 0 when any add would push unmatched above
    the limit (heavy side already at/over cap).
    """
    yes_sz = float(yes)
    no_sz = float(no)
    lim = max(0.0, float(limit))
    key = (side or "").strip().upper()
    if key == "YES":
        delta = yes_sz - no_sz
    elif key == "NO":
        delta = no_sz - yes_sz
    else:
        return 0.0
    return max(0.0, lim - delta)


def max_ask_shares(yes: float, no: float, side: str, limit: float) -> float:
    """Largest ASK size that keeps unmatched ``|YES−NO|`` ≤ ``limit`` after a full fill.

    At-limit is OK (same strict ``>`` as ``max_loss_check``). ``side`` is
    ``YES`` or ``NO``. Result is also capped by current holdings on that side.
    """
    yes_sz = float(yes)
    no_sz = float(no)
    lim = max(0.0, float(limit))
    key = (side or "").strip().upper()
    if key == "YES":
        # After sell sz: unmatched = |(yes-sz)-no|. Feasible sz in [lo, hi].
        delta = yes_sz - no_sz
        lo = max(0.0, delta - lim)
        hi = min(yes_sz, delta + lim)
    elif key == "NO":
        delta = no_sz - yes_sz
        lo = max(0.0, delta - lim)
        hi = min(no_sz, delta + lim)
    else:
        return 0.0
    if hi + 1e-12 < lo:
        return 0.0
    return max(0.0, hi)


def leftover_bid_budget_usd(
    allocated_capital: float,
    inventory_mark_usd: float,
    exposure_ratio: float,
) -> float:
    """USD left for BIDs after inventory, capped by exposure headroom.

    ``headroom = allocated_capital × exposure_ratio`` (fill headroom).
    Leftover is split across YES/NO by the caller. Never raises the ratio.
    """
    if allocated_capital <= 0:
        return 0.0
    ratio = max(0.0, float(exposure_ratio))
    headroom = allocated_capital * ratio
    cap = min(allocated_capital, headroom)
    return max(0.0, cap - max(0.0, float(inventory_mark_usd)))


def matched_seed_usd(
    allocated_capital: float,
    yes_mid: float,
    no_mid: float,
    seed_max_usd_per_side: float = 0.0,
) -> float:
    """Approximate matched-pair seed cost (~N shares) for ranking.

    N = min(alloc/2/yes, alloc/2/no), optionally capped so each side's
    notional ≤ ``seed_max_usd_per_side``. Pair mark ≈ N for complementary mids.
    """
    yes_px = max(float(yes_mid), 0.01)
    no_px = max(float(no_mid), 0.01)
    ideal_yes = alloc_side_shares(allocated_capital, yes_px)
    ideal_no = alloc_side_shares(allocated_capital, no_px)
    n = min(ideal_yes, ideal_no)
    if seed_max_usd_per_side > 0:
        # Cap by expensive side (same spirit as seeder)
        expensive = max(yes_px, no_px)
        n_cap = seed_max_usd_per_side / expensive
        n = min(n, n_cap)
    return max(0.0, n)


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
