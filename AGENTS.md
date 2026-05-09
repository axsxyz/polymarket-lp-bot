# AGENTS.md

This file provides guidance to WARP (warp.dev) when working with code in this repository.

## What This Is

An automated two-sided liquidity provider (market-making) bot for Polymarket. It posts bid/ask GTC limit orders on reward-eligible binary outcome markets to earn LP rewards via Polymarket's quadratic scoring formula. The hard constraint is ZERO directional betting — inventory skew is actively managed to protect capital.

## Build & Run Commands

```bash
# Setup
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run (DRY_RUN=true by default)
python main.py

# Run tests
pytest tests/

# Run a single test file
pytest tests/test_quoter.py

# Run a single test class or method
pytest tests/test_quoter.py::TestComputeOffset::test_offset_is_tick_based

# Scripts
python scripts/setup_allowances.py --all    # one-time wrap + token approvals (see script docstring)
python scripts/sell_positions.py             # liquidate positions
```

There is no linter, type checker, or formatter configured. Tests use pytest with no special plugins.

## Architecture

### Entry Point & Event Loop

`main.py` is a single ~1000-line async orchestrator. It runs an infinite loop with two quoting paths:

- **Cold path** (every `REBALANCE_INTERVAL_SECONDS`, default 45s): full cycle — collect fills, batch-fetch midpoints, check global exposure, per-market quote computation with skew evaluation, smart-diff cancel-and-replace.
- **Hot path** (event-driven via WebSocket): `hot_requote()` fires when a mid-price moves ≥1 tick from the current order center. Lightweight — no fill collection, no skew eval, no exposure checks.

Background tasks scheduled from `main.py`: market refresh (15 min), periodic cleanup (4 h), daily rewards snapshot (midnight UTC). Graceful shutdown on SIGINT/SIGTERM cancels all orders first.

### Core Components (in `core/`)

All components are instantiated in `main.py` and passed references to each other. There is no DI framework or service registry.

- **`client.py` — `AsyncClobClient`**: Async wrapper around the synchronous `py-clob-client` SDK. Every SDK call runs in `asyncio.to_thread()`. Has a token-bucket rate limiter and exponential-backoff retry with non-retryable error detection.
- **`market_manager.py` — `MarketManager`**: Discovers markets from CLOB `get_sampling_simplified_markets()`, enriches with Gamma API metadata (end_date, tags, volume), delegates filtering/ranking to `config/markets_filter.py`, allocates capital (top-15 boosted), caches to `data/selected_markets.json`.
- **`quoter.py` — `Quoter`**: Stateless quote calculator. Takes a `MarketCandidate` + live mid + skew info, returns `TwoSidedQuotes` (YES + NO). Two offset modes: tick-based (`QUOTE_OFFSET_TICKS > 0`) with adverse-fill-rate auto-widening, or legacy tightness-factor. Applies skew adjustments (widen heavy side bid, tighten light side).
- **`order_manager.py` — `OrderManager`**: Smart-diff cancel-and-replace — only replaces orders where price moved beyond `ORDER_UPDATE_THRESHOLD_TICKS`. Tracks live orders locally in `_live_orders` dict. Per-market circuit breaker. Collateral pre-check against USDC balance. Per-market `asyncio.Lock` for concurrent safety between hot/cold paths.
- **`inventory.py` — `InventoryTracker`**: Tracks per-market `MarketPosition` (YES/NO sizes + cost basis). Computes skew ratio and produces `SkewAction` with four escalation tiers (alert → adjust → recovery → hard pause). Global exposure guard across all markets. Realised PnL via average-cost-basis accounting.
- **`inventory_seeder.py` — `InventorySeeder`**: Runs once at startup. Buys minimal YES+NO tokens via GTC limit orders (3 attempts with escalating aggressiveness) so ASK orders can be posted. Markets that fail seeding are excluded from quoting.
- **`fill_collector.py` — `FillCollector`**: Polls CLOB trades API with deduplication (bounded `seen_ids` set) and watermark persistence to `data/trade_watermark.json` for crash recovery.
- **`scoring_monitor.py` — `ScoringMonitor`**: Periodically checks `are_orders_scoring` endpoint. Tracks consecutive non-scoring counts per market. After `SCORING_REMEDIATION_THRESHOLD` failures, returns condition_ids for force-replace.
- **`ws_feed.py` — `WebSocketFeed`**: Connects to `wss://ws-subscriptions-clob.polymarket.com/ws/market`. Maintains `{token_id: mid}` cache. Fires `on_mid_change` callback with velocity guard (widens on 3+ directional ticks in 5s). Auto-reconnects with exponential backoff.
- **`rewards_tracker.py` — `RewardsTracker`**: Daily snapshot collection combining Gamma API reward allocations + realised PnL + airdrop metrics. History persisted to `data/rewards_history.json`.

### Configuration (`config/`)

- **`settings.py`**: Single `Settings` class using `pydantic-settings` `BaseSettings`. All values come from `.env`. Includes field validators for `PREFERRED_TAGS` (comma-separated string) and `MODE` (case-insensitive). The `tightness_factor` property switches between safe/aggressive based on `MODE`.
- **`markets_filter.py`**: `MarketCandidate` dataclass, hard filters (`passes_hard_filters`), composite ranking score (`rank_score`), and Gamma/CLOB response parsers. The `_normalise_spread` function converts cents to dollars (any `max_spread > 1.0` is treated as cents).

### Utilities (`utils/`)

- **`discord.py` — `DiscordNotifier`**: Rate-limited webhook sender with rich embeds. Self-rate-limits to avoid Discord's 5 msg/sec throttle.
- **`logging.py`**: Loguru setup — stderr (colored), rotating file (`logs/bot_*.log`), structured JSON (`logs/bot_*.jsonl`).
- **`helpers.py`**: Pure functions — `round_to_tick`, `clamp_price`, `estimate_reward_score`, `two_sided_q_score`, `seconds_until_midnight_utc`.

### Concurrency Model

- `_market_lock` and `_inventory_lock` (module-level in `main.py`) protect shared state from background tasks.
- `OrderManager` has per-market `asyncio.Lock` to prevent hot and cold paths from racing on the same market.
- All CLOB SDK calls go through `asyncio.to_thread()` since `py-clob-client` uses blocking `requests`.

## Key Domain Concepts

- **Quadratic scoring**: `S(v, s) = ((v - s) / v)² × size`. Tighter quotes score quadratically better. Protocol takes `min(Q_bid_side, Q_ask_side)`, so balanced two-sided quoting is critical.
- **max_spread**: Per-market reward parameter from Polymarket. Quotes must be within max_spread of mid to score. Can arrive from API in cents (>1.0) or dollars (<1.0) — always normalised to dollars internally.
- **Tick sizes**: Markets use one of `0.1`, `0.01`, `0.001`, `0.0001`. The `_TICK_MAP` dict appears in multiple files (`quoter.py`, `helpers.py`, `inventory_seeder.py`).
- **neg_risk markets**: Use different exchange contracts (Neg Risk Exchange / Neg Risk Adapter). The `neg_risk` flag on `MarketCandidate` affects which contracts are used for allowances and order routing.

## Test Patterns

Tests use a `_make_settings(**overrides)` helper to construct `Settings` with a dummy `PRIVATE_KEY` and sensible defaults. Market fixtures use `_market()` or `_candidate()` helpers. Tests are pure unit tests — no mocking of external APIs, no async tests. All test files are in `tests/` with `test_` prefix.

## Data Files (auto-generated, gitignored)

- `data/selected_markets.json` — cached market selection
- `data/trade_watermark.json` — fill collector watermark for crash recovery
- `data/rewards_history.json` — rolling reward snapshots
