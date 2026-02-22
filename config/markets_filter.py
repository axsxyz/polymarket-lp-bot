"""
Market filtering & scoring rules for LP-farming market selection.

The goal is to select long-dated, reward-rich, low-volatility markets where
our two-sided quotes earn maximum quadratic-score rewards without excessive
fill risk.  We avoid extreme-probability markets (mid < 0.15 or > 0.85)
because the rewards formula requires two-sided liquidity to score in those
ranges, and the adverse-selection risk is higher.
"""

from __future__ import annotations

import json as _json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from config.settings import Settings


@dataclass
class MarketCandidate:
    """Intermediate representation of a market being evaluated for selection."""

    condition_id: str
    question: str = ""
    token_id_yes: str = ""
    token_id_no: str = ""
    max_spread: float = 0.0
    min_size: float = 0.0
    reward_rate_daily: float = 0.0
    midpoint: float = 0.5
    end_date: Optional[datetime] = None
    volume_24h: float = 0.0
    tags: List[str] = field(default_factory=list)
    neg_risk: bool = False
    tick_size: str = "0.01"
    allocated_capital: float = 0.0
    score: float = 0.0

    @property
    def days_remaining(self) -> float:
        if self.end_date is None:
            return 999.0
        delta = self.end_date - datetime.now(timezone.utc)
        return max(delta.total_seconds() / 86400, 0.0)


def passes_hard_filters(m: MarketCandidate, s: Settings) -> bool:
    """Return True if the market passes all hard (non-negotiable) filters."""

    if m.max_spread < s.MIN_REWARDS_MAX_SPREAD:
        return False

    if m.days_remaining < s.MIN_END_DATE_DAYS:
        return False

    if not (s.MIDPOINT_MIN <= m.midpoint <= s.MIDPOINT_MAX):
        return False

    if m.volume_24h > s.MAX_24H_VOLUME:
        return False

    if m.max_spread <= 0 or m.min_size <= 0:
        return False

    estimated_per_market = s.TOTAL_CAPITAL / max(s.MAX_MARKETS, 1)
    half_budget = estimated_per_market / 2.0
    expensive_side = max(m.midpoint, 1.0 - m.midpoint) if m.midpoint > 0 else 0.5
    min_cost_per_side = m.min_size * expensive_side
    if min_cost_per_side > half_budget * 1.5:
        return False

    return True


def tag_overlap_score(m: MarketCandidate, preferred: List[str]) -> float:
    """
    Score 0-1 based on how many preferred tags the market matches.
    A market matching all preferred tags gets 1.0.
    """
    if not preferred:
        return 0.5
    market_tags = {t.lower() for t in m.tags}
    overlap = sum(1 for t in preferred if t in market_tags)
    return overlap / len(preferred)


def estimate_volatility(m: MarketCandidate) -> float:
    """
    Rough volatility proxy for a binary market.
    Binary variance = p*(1-p), maximised at p=0.50.
    Long-dated markets near the extremes are lower volatility.
    We combine this with a time-decay factor: further expiry → lower vol.
    """
    p = max(0.01, min(0.99, m.midpoint))
    binary_var = p * (1.0 - p)
    time_factor = 1.0 / (1.0 + math.log1p(max(m.days_remaining, 1) / 30.0))
    return binary_var * time_factor


def rank_score(m: MarketCandidate, s: Settings) -> float:
    """
    Composite ranking score used to sort filtered candidates.
    Higher is better.

    score = reward_rate / (1 + estimated_vol)
            * (1 + tag_bonus)
            * (1 + spread_bonus)
            * capital_efficiency

    - reward_rate is the daily USDC reward pool for this market
    - estimated_vol penalises high-churn markets
    - tag_bonus gives up to 30% boost for preferred categories
    - spread_bonus rewards wider max_spread (easier to score)
    - capital_efficiency penalises markets that are expensive to seed/quote
    """
    vol = estimate_volatility(m)
    tag_bonus = tag_overlap_score(m, s.PREFERRED_TAGS) * 0.3
    denominator = 1.0 + vol

    spread_bonus = min(m.max_spread / 0.05, 1.0) * 0.25

    expensive_side = max(m.midpoint, 1.0 - m.midpoint) if m.midpoint > 0 else 0.5
    seed_cost = m.min_size * expensive_side * 2
    per_market_budget = s.TOTAL_CAPITAL / max(s.MAX_MARKETS, 1)
    capital_eff = 1.0 if seed_cost <= 0 else min(per_market_budget / seed_cost, 2.0) / 2.0

    return (m.reward_rate_daily / denominator) * (1.0 + tag_bonus) * (1.0 + spread_bonus) * capital_eff


def filter_and_rank(
    candidates: List[MarketCandidate],
    settings: Settings,
) -> List[MarketCandidate]:
    """
    Apply hard filters, compute ranking scores, sort descending, and
    return the top MAX_MARKETS candidates.
    """
    from loguru import logger

    passed = [m for m in candidates if passes_hard_filters(m, settings)]

    for m in passed:
        m.score = rank_score(m, settings)

    passed.sort(key=lambda m: m.score, reverse=True)

    selected = passed[: settings.MAX_MARKETS]

    for i, m in enumerate(selected):
        expensive_side = max(m.midpoint, 1.0 - m.midpoint) if m.midpoint > 0 else 0.5
        seed_est = m.min_size * expensive_side * 2
        logger.info(
            "Rank #{}: {} score={:.2f} reward=${:.2f}/day spread={:.3f} "
            "mid={:.2f} min_size={:.0f} seed_est=${:.0f} days={:.0f} tags={}",
            i + 1,
            (m.question[:50] + "…") if len(m.question) > 50 else (m.question or m.condition_id[:16]),
            m.score,
            m.reward_rate_daily,
            m.max_spread,
            m.midpoint,
            m.min_size,
            seed_est,
            m.days_remaining,
            ",".join(m.tags[:3]) if m.tags else "none",
        )

    return selected


def parse_gamma_market(raw: Dict[str, Any]) -> MarketCandidate:
    """
    Build a MarketCandidate from a raw Gamma API /markets (or /events)
    JSON object.

    Supports **both** the current live Gamma shape (camelCase fields,
    ``clobTokenIds``/``outcomes`` as JSON-encoded strings, top-level
    ``rewardsMinSize``/``rewardsMaxSpread``, ``clobRewards``, etc.) and
    the legacy snake_case shape (``condition_id``, ``tokens`` array,
    nested ``rewards`` object).  When both exist the camelCase value wins
    for identifiers; for tokens the ``tokens`` array is preferred if
    non-empty because it is richer.
    """

    # ── condition_id (camelCase first, then snake_case) ──────────
    condition_id: str = (
        raw.get("condition_id")
        or raw.get("conditionId")
        or ""
    )

    # ── tokens ───────────────────────────────────────────────────
    # Legacy shape: tokens=[{outcome:"Yes", token_id:"…", price:…}, …]
    # Live shape:   clobTokenIds='["id0","id1"]', outcomes='["Yes","No"]'
    token_yes = ""
    token_no = ""
    tokens_list: list = raw.get("tokens") or []

    if tokens_list and isinstance(tokens_list, list):
        # Legacy / rich tokens array
        for tok in tokens_list:
            outcome = (tok.get("outcome") or "").upper()
            if outcome == "YES":
                token_yes = tok.get("token_id", "")
            elif outcome == "NO":
                token_no = tok.get("token_id", "")
    else:
        # Current Gamma shape: parse clobTokenIds + outcomes JSON strings
        token_ids = _safe_json_list(raw.get("clobTokenIds"))
        outcomes = _safe_json_list(raw.get("outcomes"))
        for idx, outcome_str in enumerate(outcomes):
            if idx >= len(token_ids):
                break
            norm = (outcome_str or "").strip().upper()
            if norm == "YES":
                token_yes = token_ids[idx]
            elif norm == "NO":
                token_no = token_ids[idx]

    # ── end_date ─────────────────────────────────────────────────
    end_str: str = (
        raw.get("end_date_iso")
        or raw.get("endDateIso")
        or raw.get("end_date")
        or raw.get("endDate")
        or ""
    )
    end_date: Optional[datetime] = None
    if end_str:
        try:
            end_date = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            if end_date is not None and end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pass

    # ── rewards ──────────────────────────────────────────────────
    # Legacy: rewards={max_spread, min_size, rates:[{rewards_daily_rate}]}
    # Live:   top-level rewardsMaxSpread, rewardsMinSize (numbers)
    #         clobRewards=[{rewardsDailyRate:…}, …]
    rewards_obj: dict = raw.get("rewards") or {}
    max_spread = _normalise_spread(float(rewards_obj.get("max_spread", 0) or 0))
    min_size = float(rewards_obj.get("min_size", 0) or 0)

    # Legacy daily rate from nested rewards.rates
    legacy_rates: list = rewards_obj.get("rates") or []
    daily_rate = 0.0
    for r in legacy_rates:
        daily_rate += float(r.get("rewards_daily_rate", 0) or 0)

    # Override with top-level fields when present (live Gamma shape)
    if max_spread == 0:
        max_spread = _normalise_spread(float(raw.get("rewardsMaxSpread", 0) or 0))
    if min_size == 0:
        min_size = float(raw.get("rewardsMinSize", 0) or 0)
    if daily_rate == 0:
        clob_rewards: list = raw.get("clobRewards") or []
        for cr in clob_rewards:
            if isinstance(cr, dict):
                daily_rate += float(
                    cr.get("rewardsDailyRate", 0)
                    or cr.get("rewards_daily_rate", 0)
                    or 0
                )

    # ── tags ─────────────────────────────────────────────────────
    tags_raw: list = raw.get("tags") or []
    tags: List[str] = []
    for t in tags_raw:
        if isinstance(t, dict):
            label = t.get("label") or t.get("slug") or ""
            tags.append(label.lower())
        else:
            tags.append(str(t).lower())

    # ── volume 24h ───────────────────────────────────────────────
    vol_24h = float(
        raw.get("volume_num_24hr", 0)
        or raw.get("volume24hr", 0)
        or 0
    )

    # ── neg_risk ─────────────────────────────────────────────────
    neg_risk = bool(raw.get("neg_risk") or raw.get("negRisk") or False)

    # ── tick_size ────────────────────────────────────────────────
    tick_raw = (
        raw.get("minimum_tick_size")
        or raw.get("orderPriceMinTickSize")
        or "0.01"
    )
    tick_size = str(tick_raw)

    # ── midpoint (from tokens list if available) ─────────────────
    midpoint = _avg_token_price(tokens_list) if tokens_list else 0.50

    return MarketCandidate(
        condition_id=condition_id,
        question=raw.get("question", ""),
        token_id_yes=token_yes,
        token_id_no=token_no,
        max_spread=max_spread,
        min_size=min_size,
        reward_rate_daily=daily_rate,
        midpoint=midpoint,
        end_date=end_date,
        volume_24h=vol_24h,
        tags=tags,
        neg_risk=neg_risk,
        tick_size=tick_size,
    )


def _normalise_spread(raw_value: float) -> float:
    """Convert max_spread to decimal if the API returned it in cents.

    Both the CLOB ``rewards.max_spread`` and Gamma ``rewardsMaxSpread``
    return values in *cents* (e.g. 3.5 means 3.5 ¢ = $0.035) whereas the
    quoter does arithmetic in *dollars* (prices are 0-1).  Any value > 1.0
    is unambiguously in cents because a 100 %+ spread is nonsensical.
    """
    if raw_value > 1.0:
        return raw_value / 100.0
    return raw_value


def _safe_json_list(value: Any) -> List[str]:
    """
    Parse a JSON-encoded list string (e.g. ``'["a","b"]'``).
    Returns an empty list on any failure, never raises.
    """
    if isinstance(value, list):
        return [str(v) for v in value]
    if not isinstance(value, str) or not value:
        return []
    try:
        parsed = _json.loads(value)
        if isinstance(parsed, list):
            return [str(v) for v in parsed]
    except (ValueError, TypeError):
        pass
    return []


def parse_clob_simplified_market(raw: Dict[str, Any]) -> MarketCandidate:
    """
    Build a MarketCandidate from a CLOB get_sampling_simplified_markets()
    response item.  These are lighter-weight and lack Gamma metadata like
    end_date/tags, which get enriched later by MarketManager.
    """
    tokens = raw.get("tokens", [])
    token_yes = ""
    token_no = ""
    for tok in tokens:
        outcome = (tok.get("outcome") or "").upper()
        if outcome == "YES":
            token_yes = tok.get("token_id", "")
        elif outcome == "NO":
            token_no = tok.get("token_id", "")

    rewards = raw.get("rewards", {}) or {}
    rates = rewards.get("rates", []) or []
    daily_rate = 0.0
    for r in rates:
        daily_rate += float(r.get("rewards_daily_rate", 0) or 0)

    return MarketCandidate(
        condition_id=raw.get("condition_id", ""),
        token_id_yes=token_yes,
        token_id_no=token_no,
        max_spread=_normalise_spread(float(rewards.get("max_spread", 0) or 0)),
        min_size=float(rewards.get("min_size", 0) or 0),
        reward_rate_daily=daily_rate,
        midpoint=_avg_token_price(tokens),
        neg_risk=bool(raw.get("neg_risk", False)),
    )


def _avg_token_price(tokens: list) -> float:
    """Compute midpoint as average price from YES token (fallback 0.50)."""
    for tok in tokens:
        if (tok.get("outcome") or "").upper() == "YES":
            try:
                return float(tok["price"])
            except (KeyError, TypeError, ValueError):
                pass
    return 0.50
