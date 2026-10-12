"""Pure hot-path skip predicate (A1). No I/O — unit-tested without asyncio."""

from __future__ import annotations

from typing import MutableMapping, Optional, Set

from core.market_manager import MarketState


def hot_skip_reason(
    cid: str,
    *,
    state: MarketState,
    toxic_paused: bool,
    exposure_paused: bool,
    skip_cids: Set[str],
    drawdown: bool,
    midpoint_paused: bool,
    spike_paused: bool,
) -> Optional[str]:
    """Return a skip reason if hot_requote must not compute/post, else None."""
    if drawdown:
        return "drawdown"
    if cid in skip_cids:
        return "seed_failed"
    if state == MarketState.SEED_FAILED:
        return "seed_failed"
    if state == MarketState.INACTIVE:
        return "inactive"
    if toxic_paused:
        return "toxicity_paused"
    if exposure_paused:
        return "exposure"
    if midpoint_paused:
        return "midpoint_guard"
    if spike_paused:
        return "spike_paused"
    return None


def note_hot_skip_transition(
    cid: str,
    reason: Optional[str],
    last: MutableMapping[str, str],
) -> Optional[str]:
    """Return ``hot_<reason>`` only when the skip reason appears or changes.

    Still-skipping with the same reason → ``None`` (no tape/digest spam).
    ``reason is None`` clears the in-memory last reason for ``cid``.
    """
    if not reason:
        last.pop(cid, None)
        return None
    if last.get(cid) == reason:
        return None
    last[cid] = reason
    return f"hot_{reason}"
