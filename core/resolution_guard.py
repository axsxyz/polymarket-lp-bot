"""
Resolution guard — pre-resolution risk + 24h drift delisting (P2d).

Two independent checks per market, plus a hard-coded extreme-mid safety
net. All pure logic — no API calls, no I/O, no main.py imports — so the
module is trivially unit-testable.

  1. Max-loss check
     ``max_loss_usd = max(yes_size, no_size) * 1.0``
     Worst case: the heavy side resolves to 0, costing us its full token
     count in USD. Breached when above ``RESOLUTION_MAX_LOSS_USD``.

  2. 24h move check
     ``move_pct = |mid_now − mid_24h_ago| / mid_24h_ago * 100``
     Reference point is the recorded sample whose timestamp is closest to
     (now − 24h) within a ±30-minute tolerance window. Requires at least
     20 minutes of recorded history — below that the answer is ``False``
     (insufficient data, fail-open by design).

  3. Extreme-mid safety net (hard-coded, not configurable)
     ``mid < 0.05 or mid > 0.95`` ⇒ delist regardless of (1) / (2).
     Quoting this close to resolution is unsafe — payoff is asymmetric,
     the book thins out, and resolution can flip the mid to {0, 1}
     in a single block.

Distinct from the midpoint guard (``Quoter.evaluate_midpoint_guard``)
which protects the band ``[0.18, 0.82]`` over a 3-cycle counter to skip
ranged markets — this module protects against terminal binary-payoff
risk as a market approaches resolution.

Cold path uses :py:meth:`should_delist` once per cycle per market.
Hot path only feeds the rolling history via :py:meth:`record_midpoint`.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Deque, Dict, Optional, Tuple

from config.settings import Settings


# ──────────────────────────────────────────────────────────────────────
# Module constants — fixed by design (not env-tunable)
# ──────────────────────────────────────────────────────────────────────

# Rolling midpoint history retention. A bit over 24h so the 24h move
# check always has a ~one-hour cushion either side of its anchor lookup.
_HISTORY_RETENTION = timedelta(hours=25)

# Minimum span of recorded history before evaluate_move_24h returns a
# real answer — guards against firing on near-empty deques and on the
# first cycle of a freshly-discovered market.
_MIN_HISTORY_FOR_MOVE = timedelta(minutes=20)

# Tolerance for matching a stored sample to the (now − 24h) anchor.
# Sparse cold-cycle ingestion (one sample / cycle) on a quiet market
# would miss the anchor entirely without this slack.
_HISTORY_LOOKUP_TOLERANCE = timedelta(minutes=30)

# Anchor distance for the 24h move check.
_HISTORY_LOOKUP_TARGET = timedelta(hours=24)

# Hard cap on per-market deque length — defensive bound against
# pathological feeders. At 1 sample/sec for 25h we'd see ~90 k entries;
# the retention prune drops most. Cap at ~100 k just in case.
_HISTORY_MAXLEN = 100_000

# Extreme-mid safety net — markets with live mid outside this band
# are delisted on sight (asymmetric payoff + thin book + imminent
# resolution). Strict comparison: 0.05 and 0.95 themselves are still
# in-band, only values strictly below / above trigger.
_EXTREME_MID_LOW = 0.05
_EXTREME_MID_HIGH = 0.95


# ──────────────────────────────────────────────────────────────────────
# Time helper (indirected for testability — see test_resolution_guard.py)
# ──────────────────────────────────────────────────────────────────────


def _utcnow() -> datetime:
    """Current UTC time. Indirected so tests can monkeypatch
    :py:mod:`core.resolution_guard._utcnow` without touching the stdlib."""
    return datetime.now(timezone.utc)


# ──────────────────────────────────────────────────────────────────────
# Public class
# ──────────────────────────────────────────────────────────────────────


class ResolutionGuard:
    """Per-market max-loss + 24h drift evaluator with extreme-mid net.

    Lifecycle (cold cycle, once per market per cycle):

      1. ``main.py`` → :py:meth:`record_midpoint` *(also called from
         the hot WS path on every mid update)*
      2. ``main.py`` → :py:meth:`should_delist`  *(aggregated decision)*
      3. ``main.py`` → :py:meth:`max_loss_check` + :py:meth:`evaluate_move_24h`
         *(only re-called for logging the actual numbers when a delist
         fires; they are otherwise transparent inside should_delist)*

    No mutation outside :py:meth:`record_midpoint`. The two ``*_check``
    /``evaluate_*`` methods are pure reads on the rolling history plus
    the caller-provided sizes/mid.
    """

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        # cid → deque of (mid, ts_utc). Caller feeds monotonically
        # increasing timestamps in production; tests may inject ordered
        # synthetic timestamps via record_midpoint.
        self._history: Dict[str, Deque[Tuple[float, datetime]]] = {}

    # ── Ingestion ──────────────────────────────────────────────────

    def record_midpoint(
        self, cid: str, mid: float, timestamp_utc: datetime,
    ) -> None:
        """Append a ``(mid, ts_utc)`` sample and prune entries older than
        :data:`_HISTORY_RETENTION` (25h).

        Behaviour:
          - Empty ``cid`` is ignored.
          - Mids outside ``(0, 1)`` are dropped (mirrors
            ``Quoter.record_mid`` and ``ToxicityAnalyzer.record_mid``
            so the same WS feed can drive all three).
          - Naive datetimes are interpreted as UTC; tz-aware values are
            converted to UTC. Both forms come out comparable.
          - Pruning is amortised O(1): only entries older than
            ``ts − 25h`` (relative to the just-inserted sample's ts) are
            popped from the front. Out-of-order inserts are tolerated
            but won't trigger a full re-sort.
        """
        if not cid:
            return
        if mid <= 0.0 or mid >= 1.0:
            return
        ts = self._normalise_ts(timestamp_utc)
        history = self._history.get(cid)
        if history is None:
            history = deque(maxlen=_HISTORY_MAXLEN)
            self._history[cid] = history
        history.append((float(mid), ts))
        self._prune(history, ts)

    # ── Checks ─────────────────────────────────────────────────────

    def max_loss_check(
        self, cid: str, yes_size: float, no_size: float,
    ) -> Tuple[bool, float, float]:
        """Worst-case USD loss if the heavy side resolves to 0.

        Computed as ``max(yes_size, no_size) * 1.0`` — the side we hold
        more of pays out 0 in the bad case, costing its full token count
        (each token nominally worth $1).

        Returns ``(breached, max_loss_usd, limit)`` where ``breached``
        is ``max_loss_usd > settings.RESOLUTION_MAX_LOSS_USD`` (strict ``>``
        so a market sitting exactly at the limit is *not* delisted —
        consistent with the rest of the bot's threshold semantics).
        The ``cid`` argument is accepted for symmetry with the other
        check; the value is not stateful for this method.
        """
        limit = float(self._s.RESOLUTION_MAX_LOSS_USD)
        max_loss = max(float(yes_size), float(no_size)) * 1.0
        return (max_loss > limit, max_loss, limit)

    def evaluate_move_24h(
        self, cid: str,
    ) -> Tuple[bool, float, float]:
        """24h relative midpoint drift.

        Returns ``(False, 0.0, limit)`` in any of:
          - no history for ``cid`` (or fewer than 2 samples),
          - oldest sample is less than 20 minutes old (insufficient span),
          - no sample sits within ±30 min of ``(now − 24h)`` (gap in feed),
          - the reference mid is non-positive (defensive — should never
            happen given the ``(0, 1)`` filter in :py:meth:`record_midpoint`).

        Otherwise returns ``(breached, move_pct, limit)`` with
        ``move_pct = |mid_now − mid_24h_ago| / mid_24h_ago * 100``.
        The "now" reading is the most recently appended sample (the
        deque is append-only ascending in production usage); the
        reference is the closest sample to ``(now − 24h)`` inside the
        ±30 min tolerance window.

        Breach uses strict ``>`` against ``settings.RESOLUTION_MAX_MOVE_24H_PCT``.
        """
        limit = float(self._s.RESOLUTION_MAX_MOVE_24H_PCT)
        history = self._history.get(cid)
        if not history or len(history) < 2:
            return (False, 0.0, limit)

        now = _utcnow()
        oldest_ts = history[0][1]
        if now - oldest_ts < _MIN_HISTORY_FOR_MOVE:
            return (False, 0.0, limit)

        target_ts = now - _HISTORY_LOOKUP_TARGET
        best_mid: Optional[float] = None
        best_dt: Optional[timedelta] = None
        for mid, ts in history:
            dt = ts - target_ts if ts >= target_ts else target_ts - ts
            if dt > _HISTORY_LOOKUP_TOLERANCE:
                continue
            if best_dt is None or dt < best_dt:
                best_dt = dt
                best_mid = mid

        if best_mid is None or best_mid <= 0.0:
            return (False, 0.0, limit)

        mid_now = history[-1][0]
        move_pct = abs(mid_now - best_mid) / best_mid * 100.0
        return (move_pct > limit, move_pct, limit)

    def should_delist(
        self,
        cid: str,
        yes_size: float,
        no_size: float,
        mid: float,
    ) -> Tuple[bool, Optional[str]]:
        """Aggregate the two checks plus the extreme-mid safety net.

        Resolution-order (highest priority first):

          1. ``"extreme_mid"`` — ``mid < 0.05`` or ``mid > 0.95``. Hard-
             coded, ignores both configurable limits. Wins over the
             other checks when multiple would trigger — it's the
             strongest signal of imminent resolution.
          2. ``"max_loss"``    — max-loss USD breach (see
             :py:meth:`max_loss_check`).
          3. ``"move_24h"``    — 24h move breach (see
             :py:meth:`evaluate_move_24h`).

        Returns ``(delist, reason)`` with ``reason`` one of
        ``"extreme_mid" | "max_loss" | "move_24h" | None``.

        Note: although the original P2d spec listed only the two
        configurable reasons plus ``None``, ``"extreme_mid"`` is added
        as a third distinct identifier so the cold-cycle log + Discord
        alert can name the exact trigger (silent delisting without
        reason traceability is a debugging hazard).
        """
        if mid < _EXTREME_MID_LOW or mid > _EXTREME_MID_HIGH:
            return (True, "extreme_mid")
        loss_breached, _, _ = self.max_loss_check(cid, yes_size, no_size)
        if loss_breached:
            return (True, "max_loss")
        move_breached, _, _ = self.evaluate_move_24h(cid)
        if move_breached:
            return (True, "move_24h")
        return (False, None)

    # ── Diagnostics (tests + cold-cycle logging) ───────────────────

    def history_len(self, cid: str) -> int:
        """Number of samples currently retained for ``cid``. Returns 0
        for unknown cid. Useful for tests and for the cycle-summary
        diagnostic logging."""
        history = self._history.get(cid)
        return len(history) if history else 0

    def history_span(self, cid: str) -> timedelta:
        """Span between the oldest and newest stored sample for ``cid``.
        ``timedelta(0)`` for empty or unknown history. Read-only helper
        for tests; production code never relies on this."""
        history = self._history.get(cid)
        if not history or len(history) < 2:
            return timedelta(0)
        return history[-1][1] - history[0][1]

    # ── Internals ──────────────────────────────────────────────────

    @staticmethod
    def _normalise_ts(ts: datetime) -> datetime:
        """Coerce ``ts`` to a UTC-aware datetime.

        Naive datetimes are assumed to already be UTC (matches the
        semantics of the legacy ``datetime.utcnow()`` API the rest of
        the codebase has migrated away from). tz-aware datetimes are
        converted to UTC.
        """
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)

    @staticmethod
    def _prune(
        history: Deque[Tuple[float, datetime]], now: datetime,
    ) -> None:
        """Drop entries older than :data:`_HISTORY_RETENTION` before
        ``now``. Operates from the left of the deque only — assumes
        ascending insertion order in production. Tolerates out-of-order
        inserts by simply leaving the tail untouched."""
        cutoff = now - _HISTORY_RETENTION
        while history and history[0][1] < cutoff:
            history.popleft()
