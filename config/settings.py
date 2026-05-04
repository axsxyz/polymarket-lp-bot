"""
Central configuration via pydantic-settings.

Every value is overridable through environment variables or a `.env` file
located in the project root.  Defaults are tuned for the "safe" LP-farming
profile (~10 k USDC across 30-60 long-dated Polymarket markets).
"""

from __future__ import annotations

from enum import Enum
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BotMode(str, Enum):
    SAFE = "safe"
    AGGRESSIVE = "aggressive"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
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

    # ── Strategy ────────────────────────────────────────────────────
    MODE: BotMode = BotMode.SAFE
    DRY_RUN: bool = True
    TIGHTNESS_FACTOR_SAFE: float = Field(default=0.48, ge=0.05, le=0.9)
    TIGHTNESS_FACTOR_AGGRESSIVE: float = Field(default=0.35, ge=0.05, le=0.9)
    SIZE_MULTIPLIER: float = Field(default=2.0, ge=1.0, le=5.0)
    ALLOW_NAKED_ASKS: bool = True
    REBALANCE_INTERVAL_SECONDS: int = Field(default=15, ge=3, le=300)
    MAX_ORDER_AGE_SECONDS: int = Field(default=600, ge=60, le=3600)
    ORDER_UPDATE_THRESHOLD_TICKS: int = Field(default=1, ge=1, le=10)
    POST_BEFORE_CANCEL: bool = False
    QUOTE_OFFSET_TICKS: int = Field(default=1, ge=0, le=20)

    # ── WebSocket / Event-Driven Quoting ─────────────────────────────
    WS_ENABLED: bool = True
    HOT_REQUOTE_DEBOUNCE_MS: int = Field(default=1000, ge=200, le=5000)
    ADVERSE_FILL_RATE_THRESHOLD: int = Field(default=3, ge=1, le=20)

    # ── API Rate Limiting ───────────────────────────────────────────
    API_RATE_LIMIT_RPS: float = Field(default=1.5, ge=0.1, le=10.0)
    API_RATE_LIMIT_BURST: float = Field(default=20.0, ge=1.0, le=50.0)
    MAX_MARKETS_PER_CYCLE: int = Field(default=20, ge=1, le=200)

    # ── Market Selection ────────────────────────────────────────────
    MAX_MARKETS: int = Field(default=60, ge=1)
    MIN_MARKETS: int = Field(default=30, ge=1)
    MIN_END_DATE_DAYS: int = Field(default=120, ge=1)
    MIN_REWARDS_MAX_SPREAD: float = Field(default=0.03, ge=0.0)
    MAX_24H_VOLUME: float = Field(default=800_000.0, ge=0.0)
    MIN_24H_VOLUME: float = Field(default=500.0, ge=0.0)
    MIN_REWARD_RATE_DAILY: float = Field(default=5.0, ge=0.0)
    MIDPOINT_MIN: float = Field(default=0.15, ge=0.0, le=1.0)
    MIDPOINT_MAX: float = Field(default=0.85, ge=0.0, le=1.0)
    PREFERRED_TAGS: List[str] = Field(
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

    # ── Inventory Seeding ─────────────────────────────────────────────
    SEED_ENABLED: bool = True
    SEED_SHARES: float = Field(default=1.0, ge=1.0, le=100.0)
    SEED_MAX_USD_PER_SIDE: float = Field(default=150.0, ge=0.5, le=500.0)
    SEED_AGGRESSIVE_TICKS: int = Field(default=3, ge=1, le=10)
    SEED_BUDGET_PCT: float = Field(default=0.30, ge=0.05, le=0.80)

    # ── Scoring ─────────────────────────────────────────────────────
    SCORING_CHECK_INTERVAL_SECONDS: int = Field(default=300, ge=60, le=3600)
    SCORING_ALERT_THRESHOLD: float = Field(default=0.70, ge=0.0, le=1.0)
    SCORING_REMEDIATION_THRESHOLD: int = Field(default=3, ge=1, le=20)

    # ── Dynamic Reallocation ─────────────────────────────────────
    DYNAMIC_REALLOC_ENABLED: bool = True
    DYNAMIC_REALLOC_INTERVAL_SECONDS: int = Field(default=300, ge=60, le=1800)
    DYNAMIC_REALLOC_MAX_SHIFT_PCT: float = Field(default=0.20, ge=0.0, le=0.50)

    # ── Circuit Breaker ───────────────────────────────────────────
    CIRCUIT_BREAKER_MAX_FAILURES: int = Field(default=3, ge=1, le=20)
    CIRCUIT_BREAKER_COOLDOWN_SECONDS: int = Field(default=300, ge=30, le=3600)

    # ── Discord ─────────────────────────────────────────────────────
    DISCORD_WEBHOOK_URL: str = ""

    # ── Logging ─────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"

    # ── Derived helpers (not env vars) ──────────────────────────────

    @property
    def tightness_factor(self) -> float:
        """Return the tightness factor for the current mode."""
        if self.MODE == BotMode.AGGRESSIVE:
            return self.TIGHTNESS_FACTOR_AGGRESSIVE
        return self.TIGHTNESS_FACTOR_SAFE

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
