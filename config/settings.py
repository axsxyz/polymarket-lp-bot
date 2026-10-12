"""
Central configuration via pydantic-settings.

Every value is overridable through environment variables or a `.env` file
located in the project root.  Defaults are tuned for the "safe" LP-farming
profile (~10 k USDC across 30-60 long-dated Polymarket markets).
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, List, Optional, Set

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class BotMode(str, Enum):
    SAFE = "safe"
    AGGRESSIVE = "aggressive"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Wallet & Auth ───────────────────────────────────────────────
    PRIVATE_KEY: str = ""
    FUNDER_ADDRESS: str = ""
    SIGNATURE_TYPE: int = Field(default=0, ge=0, le=2)
    CHAIN_ID: int = 137

    # ── Endpoints ───────────────────────────────────────────────────
    CLOB_HOST: str = "https://clob.polymarket.com"
    GAMMA_API_URL: str = "https://gamma-api.polymarket.com"

    # ── Capital & Risk ──────────────────────────────────────────────
    TOTAL_CAPITAL: float = 10_000.0
    MAX_CAPITAL_PER_MARKET: float = 500.0
    MIN_CAPITAL_PER_MARKET: float = 200.0
    GLOBAL_MAX_EXPOSURE_PCT: float = Field(default=0.30, ge=0.0, le=1.0)
    GLOBAL_MAX_GROSS_EXPOSURE_PCT: float = Field(default=0.80, ge=0.0, le=5.0)
    DAILY_DRAWDOWN_LIMIT_PCT: float = Field(default=0.05, ge=0.0, le=1.0)

    # ── Strategy ────────────────────────────────────────────────────
    MODE: BotMode = BotMode.SAFE
    DRY_RUN: bool = True
    # Starting paper cash when DRY_RUN=true. None → TOTAL_CAPITAL.
    PAPER_CASH: Optional[float] = None
    TIGHTNESS_FACTOR_SAFE: float = Field(default=0.48, ge=0.05, le=0.9)
    TIGHTNESS_FACTOR_AGGRESSIVE: float = Field(default=0.35, ge=0.05, le=0.9)
    SIZE_MULTIPLIER: float = Field(
        default=2.0, ge=1.0, le=5.0,
        description="Legacy; quote size is alloc/2/price (not min_size×this).",
    )
    REBALANCE_INTERVAL_SECONDS: int = Field(default=15, ge=3, le=300)
    MAX_ORDER_AGE_SECONDS: int = Field(default=600, ge=60, le=3600)
    ORDER_UPDATE_THRESHOLD_TICKS: int = Field(default=1, ge=1, le=10)
    POST_BEFORE_CANCEL: bool = False
    QUOTE_OFFSET_TICKS: int = Field(default=1, ge=0, le=20)

    # ── WebSocket / Event-Driven Quoting ─────────────────────────────
    WS_ENABLED: bool = True
    HOT_REQUOTE_DEBOUNCE_MS: int = Field(default=1000, ge=200, le=5000)

    # ── Toxicity / Adverse-selection (P2b) ──────────────────────────
    # Replaces the legacy ADVERSE_FILL_RATE_THRESHOLD widening.
    # VPIN: buy/sell imbalance over a rolling window of bot fills drives
    # a spread multiplier; breaching the pause threshold cancels quotes
    # for TOXICITY_PAUSE_SECONDS. Fill-vs-mid scoring adds a continuous
    # adverse-drift signal evaluated at +5s/+30s/+5min after each fill.
    # Inside ToxicityAnalyzer the two signals combine via max(); the
    # resulting multiplier (∈ [1.0, TOXICITY_SPREAD_MULTIPLIER_MAX]) is
    # then multiplied by the volatility multiplier in Quoter._compute_offset.
    # Final offset stays bounded by max_spread * 0.499 (existing clamp).
    VPIN_WINDOW_FILLS: int = Field(default=50, ge=5, le=500)
    VPIN_THRESHOLD: float = Field(default=0.7, ge=0.0, le=1.0)
    VPIN_PAUSE_THRESHOLD: float = Field(default=0.85, ge=0.0, le=1.0)
    TOXICITY_FILL_WINDOW_SECONDS: int = Field(default=300, ge=60, le=3600)
    TOXICITY_PAUSE_SECONDS: int = Field(default=180, ge=30, le=3600)
    TOXICITY_SPREAD_MULTIPLIER_MAX: float = Field(default=2.5, ge=1.0, le=10.0)

    # ── API Rate Limiting ───────────────────────────────────────────
    API_RATE_LIMIT_RPS: float = Field(default=1.5, ge=0.1, le=10.0)
    API_RATE_LIMIT_BURST: float = Field(default=20.0, ge=1.0, le=50.0)
    MAX_MARKETS_PER_CYCLE: int = Field(default=60, ge=1, le=200)

    # ── Market Selection ────────────────────────────────────────────
    MAX_MARKETS: int = Field(default=60, ge=1)
    MIN_MARKETS: int = Field(default=1, ge=1)
    MIN_END_DATE_DAYS: int = Field(default=120, ge=1)
    MIN_REWARDS_MAX_SPREAD: float = Field(default=0.03, ge=0.0)
    MAX_24H_VOLUME: float = Field(default=800_000.0, ge=0.0)
    MIN_24H_VOLUME: float = Field(default=500.0, ge=0.0)
    MIN_REWARD_RATE_DAILY: float = Field(default=5.0, ge=0.0)
    MIDPOINT_MIN: float = Field(default=0.15, ge=0.0, le=1.0)
    MIDPOINT_MAX: float = Field(default=0.85, ge=0.0, le=1.0)
    # Continuous midpoint guard — pauses an individual market when its live mid
    # drifts outside [MIN, MAX] for MIDPOINT_GUARD_CYCLES consecutive cold cycles.
    # Distinct from MIDPOINT_MIN/MAX which only filter at selection time.
    MIDPOINT_GUARD_MIN: float = Field(default=0.18, ge=0.0, le=0.5)
    MIDPOINT_GUARD_MAX: float = Field(default=0.82, ge=0.5, le=1.0)
    MIDPOINT_GUARD_CYCLES: int = Field(default=3, ge=1, le=20)

    # ── Volatility & Spike Detection ────────────────────────────────
    # Rolling-window realised volatility drives a multiplier on
    # QUOTE_OFFSET_TICKS so the spread tightens in calm books and widens
    # in volatile ones. Spike detector triggers a hard pause when the
    # mid moves >= SPIKE_TICKS_THRESHOLD ticks inside the detection
    # window (range, not net move — captures pumps and reversals).
    VOL_WINDOW_SECONDS: int = Field(default=900, ge=60, le=3600)
    VOL_SPREAD_MULTIPLIER_MIN: float = Field(default=0.7, ge=0.1, le=2.0)
    VOL_SPREAD_MULTIPLIER_MAX: float = Field(default=3.0, ge=1.0, le=10.0)
    SPIKE_TICKS_THRESHOLD: int = Field(default=5, ge=1, le=50)
    SPIKE_DETECTION_WINDOW_SECONDS: int = Field(default=30, ge=5, le=300)
    SPIKE_PAUSE_SECONDS: int = Field(default=120, ge=10, le=3600)

    PREFERRED_TAGS: Annotated[List[str], NoDecode] = Field(
        default=[
            "politics",
            "election",
            "fed",
            "geopolitics",
            "midterms",
            "2026",
            "2028",
        ]
    )

    # ── Skew Management ─────────────────────────────────────────────
    SKEW_ALERT_THRESHOLD: float = Field(default=0.25, ge=0.0, le=1.0)
    SKEW_ACTION_THRESHOLD: float = Field(default=0.30, ge=0.0, le=1.0)
    SKEW_PAUSE_THRESHOLD: float = Field(default=0.45, ge=0.0, le=1.0)
    SKEW_HARD_PAUSE_THRESHOLD: float = Field(default=0.70, ge=0.0, le=1.0)
    SKEW_PAUSE_DURATION_SECONDS: int = Field(default=600, ge=0)
    SKEW_ALERT_COOLDOWN_SECONDS: int = Field(default=900, ge=0)
    SKEW_RECOVERY_ASK_SIZE_PCT: float = Field(default=0.50, ge=0.05, le=1.0)
    # Continuous skew penalty (Issue 2): linear price shift on the heavy
    # side — `delta_price = signed_skew * SKEW_PENALTY * base_offset`.
    # Larger value → more aggressive rebalancing through the quote prices.
    # Replaces the previous hard-coded 1.4×widen / 0.85×tighten coefficients.
    SKEW_PENALTY: float = Field(default=0.5, ge=0.0, le=2.0)

    # ── Exposure (deployed-capital-relative) ────────────────────────
    # Issue 5: gross exposure must stay below MAX_EXPOSURE_RATIO of the
    # *deployed* capital (sum of per-market allocations, falling back
    # to TOTAL_CAPITAL when no markets are loaded). Per-market sub-cap
    # via MAX_MARKET_EXPOSURE_RATIO triggers cancel-and-pause for that
    # single market BEFORE the global limit can be reached.
    MAX_EXPOSURE_RATIO: float = Field(default=0.80, ge=0.0, le=2.0)
    MAX_MARKET_EXPOSURE_RATIO: float = Field(default=0.40, ge=0.0, le=2.0)

    # ── Resolution Guard (P2d) ──────────────────────────────────────
    # Pre-resolution risk + 24h drift delisting (core/resolution_guard.py).
    # Independent of the skew / spike / toxicity guards: those protect against
    # bad fills, this protects against terminal binary-payoff risk as a
    # market approaches its resolution event.
    #
    #  - RESOLUTION_MAX_LOSS_USD:    worst-case USD loss tolerated per market,
    #    computed as abs(yes_size - no_size) (unmatched tokens × $1).
    #  - RESOLUTION_MAX_MOVE_24H_PCT: max permitted relative midpoint move
    #    over the last 24h (|now − 24h_ago| / 24h_ago * 100). Requires
    #    ≥ 20 min of recorded history; lookup tolerance ±30 min around the
    #    24h anchor. Hard-coded module-level safety net (mid < 0.05 or > 0.95
    #    forces delist regardless of the two values above).
    RESOLUTION_MAX_LOSS_USD: float = Field(default=25.0, ge=1.0)
    RESOLUTION_MAX_MOVE_24H_PCT: float = Field(default=15.0, ge=1.0, le=100.0)

    # ── Inventory Seeding ─────────────────────────────────────────────
    SEED_ENABLED: bool = True
    SEED_SHARES: float = Field(default=1.0, ge=1.0, le=100.0)
    SEED_MAX_USD_PER_SIDE: float = Field(default=150.0, ge=0.5, le=500.0)
    SEED_AGGRESSIVE_TICKS: int = Field(default=3, ge=1, le=10)
    SEED_BUDGET_PCT: float = Field(default=0.65, ge=0.05, le=0.80)

    # ── Scoring ─────────────────────────────────────────────────────
    SCORING_CHECK_INTERVAL_SECONDS: int = Field(default=300, ge=60, le=3600)
    SCORING_ALERT_THRESHOLD: float = Field(default=0.70, ge=0.0, le=1.0)
    SCORING_REMEDIATION_THRESHOLD: int = Field(default=3, ge=1, le=20)

    # ── Rewards Estimation ──────────────────────────────────────────
    LP_ESTIMATED_SHARE: float = Field(default=0.10, ge=0.001, le=1.0)

    # ── Dynamic Reallocation ─────────────────────────────────────
    DYNAMIC_REALLOC_ENABLED: bool = True
    DYNAMIC_REALLOC_INTERVAL_SECONDS: int = Field(default=300, ge=60, le=1800)
    DYNAMIC_REALLOC_MAX_SHIFT_PCT: float = Field(default=0.20, ge=0.0, le=0.50)

    # ── Circuit Breaker ───────────────────────────────────────────
    CIRCUIT_BREAKER_MAX_FAILURES: int = Field(default=3, ge=1, le=20)
    CIRCUIT_BREAKER_COOLDOWN_SECONDS: int = Field(default=300, ge=30, le=3600)

    # ── Local tape (fills / quotes / skips / errors / snapshots) ──
    TAPE_HTTP_ENABLED: bool = True
    TAPE_HTTP_PORT: int = Field(default=8765, ge=1024, le=65535)
    FILL_POLL_INTERVAL_SECONDS: int = Field(default=3, ge=2, le=15)

    # ── Discord ─────────────────────────────────────────────────────
    # One webhook; embeds / username tagged DRY_RUN vs LIVE from settings.DRY_RUN.
    DISCORD_WEBHOOK_URL: str = ""
    # Comma-separated kinds that may fire. Empty = none. Unknown tokens ignored.
    # Kinds: startup, orders, cycle, fills, skew, errors, daily, alerts
    DISCORD_CHANNELS: str = (
        "startup,orders,cycle,fills,skew,errors,daily,alerts"
    )

    # ── Logging ─────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"

    # ── Derived helpers (not env vars) ──────────────────────────────

    @property
    def tightness_factor(self) -> float:
        """Return the tightness factor for the current mode."""
        if self.MODE == BotMode.AGGRESSIVE:
            return self.TIGHTNESS_FACTOR_AGGRESSIVE
        return self.TIGHTNESS_FACTOR_SAFE

    @property
    def paper_cash_start(self) -> float:
        """Paper broker starting cash (DRY_RUN only). Defaults to TOTAL_CAPITAL."""
        if self.PAPER_CASH is not None:
            return float(self.PAPER_CASH)
        return float(self.TOTAL_CAPITAL)

    @property
    def discord_channels(self) -> Set[str]:
        """Parsed allowlist of Discord notification kinds."""
        return parse_discord_channels(self.DISCORD_CHANNELS)

    @field_validator("PREFERRED_TAGS", mode="before")
    @classmethod
    def _parse_tags(cls, v):
        """Accept comma-separated string from .env."""
        if isinstance(v, str):
            return [t.strip().lower() for t in v.split(",") if t.strip()]
        return [t.lower() for t in v]

    @field_validator("MODE", mode="before")
    @classmethod
    def _parse_mode(cls, v):
        if isinstance(v, str):
            return v.lower()
        return v


# Known DiscordNotifier channel kinds (also documented on DISCORD_CHANNELS).
DISCORD_CHANNEL_KINDS = frozenset({
    "startup",
    "orders",
    "cycle",
    "fills",
    "skew",
    "errors",
    "daily",
    "alerts",
})


def parse_discord_channels(raw: str) -> Set[str]:
    """Parse CSV channel allowlist; drop unknown tokens."""
    if not raw or not str(raw).strip():
        return set()
    out: Set[str] = set()
    for part in str(raw).split(","):
        kind = part.strip().lower()
        if kind in DISCORD_CHANNEL_KINDS:
            out.add(kind)
    return out
