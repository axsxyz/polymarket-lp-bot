"""
backtest/fetcher.py — One-shot fetcher: CLOB /prices-history + Gamma /markets → SQLite.

API surface (verified May 2026 via Context7 against /websites/polymarket_api-reference):

  GET https://clob.polymarket.com/prices-history
      ?market={token_id}&startTs={int}&endTs={int}&interval={1m|1h|6h|1d|1w|max|all}
      &fidelity={int}
      → {"history": [{"t": int_unix_s, "p": float}, ...]}
      Smallest interval: 1m. fidelity defaults to 1 minute.

  POST https://clob.polymarket.com/batch-prices-history
      body: {"markets":[token_id, ...], "start_ts", "end_ts", "interval", "fidelity"}
      → {"history": {token_id: [{"t": ..., "p": ...}, ...]}}
      Hard limit: 20 token_ids per request.

  GET https://gamma-api.polymarket.com/markets
      ?condition_ids=cid       → list[market_dict] (one element)
      ?active=true&closed=false&limit=N&offset=M  → paginated
      Parsed via config.markets_filter.parse_gamma_market.

CAVEATS (read before interpreting backtest results):
  - Market metadata (max_spread, min_size, tick_size) is fetched at backtest
    RUN time, NOT at historical trade time. These reward params may differ
    from values active during the trade period. Use results for relative
    parameter comparison only — absolute PnL is biased if reward params
    changed in the interval.
  - /prices-history finest interval = 1m. Sub-minute fills cannot be
    reproduced; ToxicityAnalyzer's 5s/30s adverse-drift horizons collapse
    to 1min in backtest.
  - Closed/null markets from Gamma → conservative fallback
    (max_spread=0.05, tick_size="0.01", min_size=5.0) + WARN log.
  - Coverage check: if a market has <80% sample coverage on the requested
    period (gaps > 5min between consecutive samples), low_coverage=1 is set
    in the markets table. grid_search supports --skip-low-coverage.
"""

from __future__ import annotations

import argparse
import asyncio
import calendar
import json
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple

import aiohttp
from loguru import logger

from config.markets_filter import MarketCandidate, parse_gamma_market

# ──────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────

DB_PATH = Path("data/backtest.db")
CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"

DEFAULT_INTERVAL = "1m"
DEFAULT_FIDELITY = 1
BATCH_HISTORY_MAX = 20

COVERAGE_GAP_THRESHOLD_S = 300       # gaps > 5min count against coverage
COVERAGE_THRESHOLD_PCT = 0.80
COVERAGE_NORMAL_INTERVAL_S = 60      # expected sample interval (1m)

# CLOB documented limit ~1.67 RPS; we stay below at ~1.4 RPS.
HTTP_RATE_LIMIT_SLEEP_S = 0.7
HTTP_TIMEOUT_S = 30
GAMMA_PAGE_SIZE = 100
GAMMA_MAX_PAGES = 20  # safety bound

# Conservative fallback used when Gamma returns null/empty for a closed market.
FALLBACK_MAX_SPREAD = 0.05
FALLBACK_TICK_SIZE = "0.01"
FALLBACK_MIN_SIZE = 5.0


# ──────────────────────────────────────────────────────────────────
# DB schema
# ──────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    condition_id    TEXT PRIMARY KEY,
    question        TEXT,
    token_id_yes    TEXT NOT NULL,
    token_id_no     TEXT NOT NULL,
    max_spread      REAL NOT NULL,
    min_size        REAL NOT NULL,
    tick_size       TEXT NOT NULL,
    end_date_iso    TEXT,
    tags            TEXT,
    neg_risk        INTEGER NOT NULL DEFAULT 0,
    low_coverage    INTEGER NOT NULL DEFAULT 0,
    fetched_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS mid_history (
    condition_id    TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    ts              INTEGER NOT NULL,
    mid             REAL NOT NULL,
    PRIMARY KEY (condition_id, token_id, ts)
);

CREATE INDEX IF NOT EXISTS idx_mid_cid_ts ON mid_history(condition_id, ts);
"""


@dataclass(frozen=True)
class HistMid:
    """Single mid sample fetched from CLOB /prices-history."""

    condition_id: str
    token_id: str
    ts: int          # unix seconds
    mid: float       # 0..1


# ──────────────────────────────────────────────────────────────────
# DB helpers (sync — sqlite3 is fast enough for one-shot ingest)
# ──────────────────────────────────────────────────────────────────

def init_db(db_path: Path = DB_PATH) -> None:
    """Create schema if absent. Idempotent."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(_SCHEMA)


def _upsert_market(
    conn: sqlite3.Connection, m: MarketCandidate, low_coverage: bool,
) -> None:
    """Insert or replace a market row, preserving cumulative low_coverage flag."""
    conn.execute(
        """
        INSERT INTO markets
          (condition_id, question, token_id_yes, token_id_no, max_spread,
           min_size, tick_size, end_date_iso, tags, neg_risk,
           low_coverage, fetched_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(condition_id) DO UPDATE SET
          question      = excluded.question,
          token_id_yes  = excluded.token_id_yes,
          token_id_no   = excluded.token_id_no,
          max_spread    = excluded.max_spread,
          min_size      = excluded.min_size,
          tick_size     = excluded.tick_size,
          end_date_iso  = excluded.end_date_iso,
          tags          = excluded.tags,
          neg_risk      = excluded.neg_risk,
          low_coverage  = excluded.low_coverage,
          fetched_at    = excluded.fetched_at
        """,
        (
            m.condition_id,
            m.question or "",
            m.token_id_yes,
            m.token_id_no,
            float(m.max_spread),
            float(m.min_size),
            m.tick_size,
            m.end_date.isoformat() if m.end_date else None,
            json.dumps(m.tags or []),
            1 if m.neg_risk else 0,
            1 if low_coverage else 0,
            int(time.time()),
        ),
    )


def _insert_mids(conn: sqlite3.Connection, mids: List[HistMid]) -> int:
    """Bulk insert mids with INSERT OR IGNORE — idempotent on (cid, tid, ts)."""
    if not mids:
        return 0
    cur = conn.executemany(
        """
        INSERT OR IGNORE INTO mid_history (condition_id, token_id, ts, mid)
        VALUES (?, ?, ?, ?)
        """,
        [(m.condition_id, m.token_id, m.ts, m.mid) for m in mids],
    )
    return cur.rowcount or 0


def _row_to_market(row: tuple) -> MarketCandidate:
    (cid, question, tyes, tno, max_sp, min_sz, tick,
     end_iso, tags_json, neg_risk, _low_cov, _fetched_at) = row
    end_date: Optional[datetime] = None
    if end_iso:
        try:
            end_date = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            end_date = None
    try:
        tags = json.loads(tags_json) if tags_json else []
    except (TypeError, ValueError):
        tags = []
    return MarketCandidate(
        condition_id=cid,
        question=question or "",
        token_id_yes=tyes,
        token_id_no=tno,
        max_spread=float(max_sp),
        min_size=float(min_sz),
        tick_size=tick,
        end_date=end_date,
        tags=tags,
        neg_risk=bool(neg_risk),
    )


def load_market(cid: str, db_path: Path = DB_PATH) -> Optional[MarketCandidate]:
    """Read a single market row by condition_id."""
    if not Path(db_path).exists():
        return None
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT condition_id, question, token_id_yes, token_id_no, max_spread, "
            "min_size, tick_size, end_date_iso, tags, neg_risk, low_coverage, fetched_at "
            "FROM markets WHERE condition_id=?",
            (cid,),
        ).fetchone()
    return _row_to_market(row) if row else None


def is_low_coverage(cid: str, db_path: Path = DB_PATH) -> bool:
    if not Path(db_path).exists():
        return False
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT low_coverage FROM markets WHERE condition_id=?", (cid,),
        ).fetchone()
    return bool(row[0]) if row else False


def _existing_mid_range(
    cid: str, db_path: Path = DB_PATH,
) -> Tuple[Optional[int], Optional[int]]:
    """Return (min_ts, max_ts) of existing mids for cid, or (None, None) if empty."""
    if not Path(db_path).exists():
        return None, None
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT MIN(ts), MAX(ts) FROM mid_history WHERE condition_id=?",
            (cid,),
        ).fetchone()
    if not row or row[0] is None:
        return None, None
    return int(row[0]), int(row[1])


def load_mid_history(
    cid: str, token_side: str = "YES", db_path: Path = DB_PATH,
) -> List[HistMid]:
    """Read sorted mid history for one side of a market."""
    market = load_market(cid, db_path)
    if not market:
        return []
    token_id = market.token_id_yes if token_side.upper() == "YES" else market.token_id_no
    if not token_id:
        return []
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT condition_id, token_id, ts, mid FROM mid_history "
            "WHERE condition_id=? AND token_id=? ORDER BY ts ASC",
            (cid, token_id),
        ).fetchall()
    return [
        HistMid(condition_id=r[0], token_id=r[1], ts=int(r[2]), mid=float(r[3]))
        for r in rows
    ]


def list_markets_in_period(
    year: int, month: int, db_path: Path = DB_PATH,
) -> List[str]:
    """Return condition_ids whose end_date_iso is in {year}-{month:02d}."""
    if not Path(db_path).exists():
        return []
    prefix = f"{year:04d}-{month:02d}-"
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT condition_id FROM markets "
            "WHERE end_date_iso LIKE ? ORDER BY condition_id",
            (f"{prefix}%",),
        ).fetchall()
    return [r[0] for r in rows]


def list_all_markets(
    db_path: Path = DB_PATH, exclude_low_coverage: bool = False,
) -> List[str]:
    """Return every condition_id in the markets table.

    If ``exclude_low_coverage`` is True, markets flagged with low_coverage=1
    by the fetcher's coverage check are filtered out.
    """
    if not Path(db_path).exists():
        return []
    sql = "SELECT condition_id FROM markets"
    if exclude_low_coverage:
        sql += " WHERE low_coverage=0"
    sql += " ORDER BY condition_id"
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(sql).fetchall()
    return [r[0] for r in rows]


# ──────────────────────────────────────────────────────────────────
# Coverage evaluation
# ──────────────────────────────────────────────────────────────────

def _evaluate_coverage(mids: List[HistMid]) -> float:
    """Coverage ratio in [0.0, 1.0]: 1 - (sum of gaps>5min) / total_period.

    A "gap" subtracts only the excess beyond the normal sample interval (60s).
    Empty / single-sample tapes return 0.0 (treated as low coverage).
    """
    if len(mids) < 2:
        return 0.0
    sorted_mids = sorted(mids, key=lambda m: m.ts)
    total_period = sorted_mids[-1].ts - sorted_mids[0].ts
    if total_period <= 0:
        return 0.0
    gap_time = 0
    for i in range(1, len(sorted_mids)):
        delta = sorted_mids[i].ts - sorted_mids[i - 1].ts
        if delta > COVERAGE_GAP_THRESHOLD_S:
            gap_time += delta - COVERAGE_NORMAL_INTERVAL_S
    return max(0.0, 1.0 - gap_time / total_period)


# ──────────────────────────────────────────────────────────────────
# Period helpers
# ──────────────────────────────────────────────────────────────────

def _month_bounds(year: int, month: int) -> Tuple[datetime, datetime]:
    """Return (first_day_00:00:00_UTC, last_day_23:59:59_UTC) for (year, month)."""
    first = datetime(year, month, 1, 0, 0, 0, tzinfo=timezone.utc)
    last_day_num = calendar.monthrange(year, month)[1]
    last = datetime(year, month, last_day_num, 23, 59, 59, tzinfo=timezone.utc)
    return first, last


def _gamma_start_date(raw: dict) -> Optional[datetime]:
    """Parse start_date from a raw Gamma /markets payload (None if absent).

    Mirrors parse_gamma_market's tolerance to camelCase + snake_case shapes.
    """
    s = (
        raw.get("start_date_iso")
        or raw.get("startDateIso")
        or raw.get("start_date")
        or raw.get("startDate")
        or ""
    )
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


# ──────────────────────────────────────────────────────────────────
# HTTP wrappers (defensive — never raise to caller)
# ──────────────────────────────────────────────────────────────────

async def _http_get_json(
    session: aiohttp.ClientSession, url: str, params: dict,
) -> Optional[Any]:
    try:
        async with session.get(
            url, params=params,
            timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S),
        ) as resp:
            if resp.status != 200:
                logger.warning("GET {} {} → {}", url, params, resp.status)
                return None
            return await resp.json()
    except Exception as exc:
        logger.warning("GET {} failed: {}", url, exc)
        return None


async def _http_post_json(
    session: aiohttp.ClientSession, url: str, body: dict,
) -> Optional[Any]:
    try:
        async with session.post(
            url, json=body,
            timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S),
        ) as resp:
            if resp.status != 200:
                logger.warning("POST {} → {}", url, resp.status)
                return None
            return await resp.json()
    except Exception as exc:
        logger.warning("POST {} failed: {}", url, exc)
        return None


# ──────────────────────────────────────────────────────────────────
# Gamma — market metadata
# ──────────────────────────────────────────────────────────────────

async def _fetch_gamma_market(
    session: aiohttp.ClientSession, condition_id: str,
) -> Optional[MarketCandidate]:
    """Fetch a single market's metadata via Gamma /markets?condition_ids=cid."""
    payload = await _http_get_json(
        session, f"{GAMMA_BASE}/markets",
        {"condition_ids": condition_id, "limit": 1},
    )
    if not payload:
        return None
    items = payload if isinstance(payload, list) else payload.get("data", [])
    if not items:
        return None
    return parse_gamma_market(items[0])


def _fallback_market(
    condition_id: str, token_yes: str = "", token_no: str = "",
) -> MarketCandidate:
    """Conservative defaults when Gamma returns nothing for a closed market."""
    logger.warning(
        "Using conservative fallback metadata for {} (max_spread={}, tick={}, min_size={})",
        condition_id[:16], FALLBACK_MAX_SPREAD, FALLBACK_TICK_SIZE, FALLBACK_MIN_SIZE,
    )
    # end_date set to a benign +90d so passes_hard_filters() does not reject
    # the candidate elsewhere — the simulator does not enforce that filter.
    return MarketCandidate(
        condition_id=condition_id,
        token_id_yes=token_yes,
        token_id_no=token_no,
        max_spread=FALLBACK_MAX_SPREAD,
        min_size=FALLBACK_MIN_SIZE,
        tick_size=FALLBACK_TICK_SIZE,
        end_date=datetime.now(timezone.utc) + timedelta(days=90),
    )


async def _fetch_gamma_markets_for_period(
    session: aiohttp.ClientSession, year: int, month: int, n_markets: int,
) -> List[MarketCandidate]:
    """Paginate Gamma /markets, keep those ACTIVE during {year}-{month}.

    Active-during-month criterion (overlap with the calendar month, not exact match):
      - end_date >= first_day_of_month (market hasn't resolved before the month)
      - AND (start_date is None OR start_date <= last_day_of_month)
        (market had started, or no start_date provided → optimistic include)

    Markets without an end_date are rejected (cannot know if active in period).
    Markets without both YES/NO token_ids are rejected (simulator cannot replay).
    """
    out: List[MarketCandidate] = []
    first_day, last_day = _month_bounds(year, month)
    offset = 0
    pages = 0
    while len(out) < n_markets and pages < GAMMA_MAX_PAGES:
        payload = await _http_get_json(
            session, f"{GAMMA_BASE}/markets",
            {
                "active": "true", "closed": "false",
                "limit": GAMMA_PAGE_SIZE, "offset": offset,
            },
        )
        if not payload:
            break
        items = payload if isinstance(payload, list) else payload.get("data", [])
        if not items:
            break
        for raw in items:
            mc = parse_gamma_market(raw)
            if not mc.token_id_yes or not mc.token_id_no:
                continue
            if mc.end_date is None:
                continue
            if mc.end_date < first_day:
                continue
            start_date = _gamma_start_date(raw)
            if start_date is not None and start_date > last_day:
                continue
            out.append(mc)
            if len(out) >= n_markets:
                break
        offset += GAMMA_PAGE_SIZE
        pages += 1
        await asyncio.sleep(HTTP_RATE_LIMIT_SLEEP_S)
    logger.info(
        "Gamma scan {}-{:02d} (active-during): kept {} markets ({} pages)",
        year, month, len(out), pages,
    )
    return out


# ──────────────────────────────────────────────────────────────────
# CLOB — /batch-prices-history
# ──────────────────────────────────────────────────────────────────

async def _fetch_batch_prices_history(
    session: aiohttp.ClientSession,
    token_ids: List[str],
    start_ts: int,
    end_ts: int,
    interval: str = DEFAULT_INTERVAL,
    fidelity: int = DEFAULT_FIDELITY,
) -> dict:
    """POST /batch-prices-history. Returns {token_id: [(ts, mid), ...]}.

    Enforces the documented 20-token limit by chunking when the input is larger.
    Returns an empty dict on full failure; partial failures are logged but
    surfaced as missing keys.
    """
    out: dict = {tid: [] for tid in token_ids}
    if not token_ids:
        return out
    for i in range(0, len(token_ids), BATCH_HISTORY_MAX):
        chunk = token_ids[i: i + BATCH_HISTORY_MAX]
        payload = await _http_post_json(
            session, f"{CLOB_BASE}/batch-prices-history",
            {
                "markets": chunk,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "interval": interval,
                "fidelity": fidelity,
            },
        )
        if not payload:
            await asyncio.sleep(HTTP_RATE_LIMIT_SLEEP_S)
            continue
        history = payload.get("history", {}) or {}
        for tid, points in history.items():
            if not isinstance(points, list):
                continue
            parsed: List[Tuple[int, float]] = []
            for pt in points:
                if not isinstance(pt, dict):
                    continue
                try:
                    parsed.append((int(pt["t"]), float(pt["p"])))
                except (KeyError, TypeError, ValueError):
                    continue
            out[str(tid)] = parsed
        await asyncio.sleep(HTTP_RATE_LIMIT_SLEEP_S)
    return out


def _to_hist_mids(
    condition_id: str, token_id: str, points: List[Tuple[int, float]],
) -> List[HistMid]:
    """Filter (0 < mid < 1), drop garbage rows."""
    return [
        HistMid(condition_id=condition_id, token_id=token_id, ts=ts, mid=mid)
        for ts, mid in points
        if 0.0 < mid < 1.0 and ts > 0
    ]


# ──────────────────────────────────────────────────────────────────
# Public fetch API
# ──────────────────────────────────────────────────────────────────

async def fetch_for_condition_ids(
    condition_ids: List[str], days: int,
    db_path: Path = DB_PATH, refresh: bool = False,
    interval: str = DEFAULT_INTERVAL,
) -> List[MarketCandidate]:
    """Fetch metadata + mid history for each condition_id, persist to SQLite.

    Cache semantics:
      - With ``refresh=True``: full re-fetch over [now - days*86400, now]; mids
        existing in the requested window are kept (INSERT OR IGNORE) but new
        rows are layered on top. Metadata is upserted.
      - Without ``refresh``: incremental DELTA-BACKFILL — we extend the
        existing mid_history backwards if needed.
          * No metadata row → full fetch over [now - days*86400, now].
          * Metadata row exists but no mids → full fetch.
          * Existing min(ts) <= desired_start_ts → cache covers requested
            depth → skip mid fetch entirely (metadata kept as-is).
          * Existing min(ts) > desired_start_ts → backfill ONLY the missing
            upstream window [desired_start_ts, existing_min_ts].
      - Forward-stale cache (recent gap between max(ts) and now) is NOT
        auto-refreshed in this mode; use ``--refresh`` to pick up new data.
    """
    init_db(db_path)
    out: List[MarketCandidate] = []
    now_ts = int(time.time())
    desired_start_ts = now_ts - days * 86400

    async with aiohttp.ClientSession() as session:
        for cid in condition_ids:
            cached = None if refresh else load_market(cid, db_path)
            existing_min, _existing_max = (
                _existing_mid_range(cid, db_path) if cached else (None, None)
            )

            # Determine fetch window. None means "skip fetch entirely".
            fetch_start_ts: Optional[int]
            fetch_end_ts: Optional[int]

            if refresh or cached is None:
                fetch_start_ts, fetch_end_ts = desired_start_ts, now_ts
            elif existing_min is None:
                fetch_start_ts, fetch_end_ts = desired_start_ts, now_ts
            elif existing_min <= desired_start_ts:
                logger.info(
                    "Cache hit (depth ok, oldest={} <= desired_start={}): {}",
                    existing_min, desired_start_ts, cid[:16],
                )
                out.append(cached)
                continue
            else:
                missing_s = existing_min - desired_start_ts
                logger.info(
                    "Delta backfill {}: filling {}s upstream of existing min",
                    cid[:16], missing_s,
                )
                fetch_start_ts, fetch_end_ts = desired_start_ts, existing_min

            # Resolve metadata: prefer cached, else hit Gamma, else fallback.
            if cached is not None and cached.token_id_yes and cached.token_id_no:
                market = cached
            else:
                market = await _fetch_gamma_market(session, cid)
                await asyncio.sleep(HTTP_RATE_LIMIT_SLEEP_S)
                if not market or not market.token_id_yes or not market.token_id_no:
                    market = _fallback_market(cid)
                    if not market.token_id_yes or not market.token_id_no:
                        logger.error(
                            "No tokens for {} (Gamma + fallback empty), skipping",
                            cid[:16],
                        )
                        continue

            yes_mids, no_mids = await _fetch_market_history(
                session, market, fetch_start_ts, fetch_end_ts, interval,
            )

            with sqlite3.connect(db_path) as conn:
                _insert_mids(conn, yes_mids)
                _insert_mids(conn, no_mids)

                # Coverage uses the FULL stored history for this cid (post-insert),
                # not just the freshly fetched delta — so a backfill that closes
                # a gap correctly upgrades the flag. Query on SAME connection so
                # the still-uncommitted INSERTs above are visible.
                yes_tid = market.token_id_yes
                rows = conn.execute(
                    "SELECT ts, mid FROM mid_history "
                    "WHERE condition_id=? AND token_id=? ORDER BY ts ASC",
                    (cid, yes_tid),
                ).fetchall()
                full_yes = [
                    HistMid(condition_id=cid, token_id=yes_tid,
                            ts=int(r[0]), mid=float(r[1]))
                    for r in rows
                ]
                coverage = _evaluate_coverage(full_yes) if full_yes else _evaluate_coverage(yes_mids)
                low_cov = coverage < COVERAGE_THRESHOLD_PCT
                if low_cov:
                    logger.warning(
                        "Market {} low coverage: {:.0%} (gaps >5min) — flagged",
                        cid[:16], coverage,
                    )
                else:
                    logger.info(
                        "Market {} coverage {:.0%} (+{} YES, +{} NO mids fetched)",
                        cid[:16], coverage, len(yes_mids), len(no_mids),
                    )
                _upsert_market(conn, market, low_cov)

            out.append(market)
            await asyncio.sleep(HTTP_RATE_LIMIT_SLEEP_S)

    return out


async def _fetch_market_history(
    session: aiohttp.ClientSession,
    market: MarketCandidate,
    start_ts: int,
    end_ts: int,
    interval: str = DEFAULT_INTERVAL,
) -> Tuple[List[HistMid], List[HistMid]]:
    """Fetch YES + NO histories in a single batch call for [start_ts, end_ts]."""
    token_ids = [tid for tid in (market.token_id_yes, market.token_id_no) if tid]
    if not token_ids:
        return [], []
    by_tid = await _fetch_batch_prices_history(
        session, token_ids, start_ts, end_ts, interval,
    )
    yes_mids = _to_hist_mids(
        market.condition_id, market.token_id_yes,
        by_tid.get(market.token_id_yes, []),
    )
    no_mids = _to_hist_mids(
        market.condition_id, market.token_id_no,
        by_tid.get(market.token_id_no, []),
    )
    return yes_mids, no_mids


async def fetch_markets_for_period(
    year: int, month: int, n_markets: int = 50, days: int = 30,
    db_path: Path = DB_PATH, refresh: bool = False,
    interval: str = DEFAULT_INTERVAL,
) -> List[MarketCandidate]:
    """Discover up to n_markets via Gamma for {year}-{month} then fetch history."""
    init_db(db_path)
    async with aiohttp.ClientSession() as session:
        markets = await _fetch_gamma_markets_for_period(
            session, year, month, n_markets,
        )
    if not markets:
        logger.warning("No markets discovered for {}-{:02d}", year, month)
        return []
    cids = [m.condition_id for m in markets]
    return await fetch_for_condition_ids(cids, days, db_path, refresh, interval)


# ──────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backtest.fetcher",
        description="Fetch CLOB /prices-history mids + Gamma metadata into SQLite.",
    )
    parser.add_argument("--year", type=int,
                        help="Filter Gamma /markets by end_date year")
    parser.add_argument("--month", type=int,
                        help="Filter Gamma /markets by end_date month")
    parser.add_argument("--markets", type=int, default=50,
                        help="Max markets when using --year/--month (default 50)")
    parser.add_argument("--days", type=int, default=30,
                        help="History depth in days (default 30)")
    parser.add_argument("--condition-ids", type=str, dest="condition_ids",
                        help="Comma-separated cids (bypasses --year/--month)")
    parser.add_argument("--refresh", action="store_true",
                        help="Re-fetch even if rows already in DB")
    parser.add_argument("--interval", type=str, default=DEFAULT_INTERVAL,
                        choices=["1m", "1h", "6h", "1d", "1w", "max", "all"],
                        help="CLOB /prices-history interval (default 1m)")
    parser.add_argument("--db", type=Path, default=DB_PATH,
                        help="SQLite path (default data/backtest.db)")
    args = parser.parse_args()

    if args.condition_ids:
        cids = [c.strip() for c in args.condition_ids.split(",") if c.strip()]
        if not cids:
            parser.error("--condition-ids was empty after splitting on comma")
            return 2
        result = asyncio.run(fetch_for_condition_ids(
            cids, args.days, args.db, args.refresh, args.interval,
        ))
    elif args.year is not None and args.month is not None:
        result = asyncio.run(fetch_markets_for_period(
            args.year, args.month, args.markets, args.days,
            args.db, args.refresh, args.interval,
        ))
    else:
        parser.error("must provide --condition-ids or both --year and --month")
        return 2

    logger.info("Fetched {} markets into {}", len(result), args.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
