# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

An automated two-sided liquidity provider (market-making) bot for Polymarket. It posts bid/ask GTC limit orders on reward-eligible binary outcome markets to earn LP rewards via Polymarket's quadratic scoring formula. The hard constraint is ZERO directional betting — inventory skew is actively managed to protect capital.

## Build & Run Commands

```bash
# Setup
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run (DRY_RUN=true by default in .env)
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

No linter, type checker, or formatter is configured. Tests use pytest with no special plugins.

## Architecture

### Entry Point & Event Loop

`main.py` is a single ~1000-line async orchestrator running two quoting paths:

- **Cold path** (every `REBALANCE_INTERVAL_SECONDS`, default 45s): full cycle — collect fills, batch-fetch midpoints, check global exposure, per-market quote computation with skew evaluation, smart-diff cancel-and-replace.
- **Hot path** (event-driven via WebSocket): `hot_requote()` fires when a mid-price moves ≥1 tick from current order center. Lightweight — no fill collection, no skew eval, no exposure checks.

Background tasks: market refresh (15 min), periodic cleanup (4 h), daily rewards snapshot (midnight UTC). Graceful shutdown on SIGINT/SIGTERM cancels all orders first.

### Core Components (`core/`)

All components are instantiated in `main.py` and passed references to each other — no DI framework.

| Component | File | Role |
|-----------|------|------|
| `AsyncClobClient` | `client.py` | Async wrapper around synchronous `py-clob-client` SDK. Every SDK call runs in `asyncio.to_thread()`. Token-bucket rate limiter + exponential-backoff retry. |
| `MarketManager` | `market_manager.py` | Discovers markets from CLOB `get_sampling_simplified_markets()`, enriches with Gamma API metadata, delegates to `config/markets_filter.py`, caches to `data/selected_markets.json`. |
| `Quoter` | `quoter.py` | Stateless quote calculator. Returns `TwoSidedQuotes` (YES + NO). Two offset modes: tick-based (`QUOTE_OFFSET_TICKS > 0`) or legacy tightness-factor. Applies skew adjustments. |
| `OrderManager` | `order_manager.py` | Smart-diff cancel-and-replace — only replaces when price moved beyond `ORDER_UPDATE_THRESHOLD_TICKS`. Per-market `asyncio.Lock` prevents hot/cold path races. Per-market circuit breaker. |
| `InventoryTracker` | `inventory.py` | Tracks per-market YES/NO positions + cost basis. Computes skew ratio, produces `SkewAction` with four escalation tiers (alert → adjust → recovery → hard pause). Global exposure guard. |
| `InventorySeeder` | `inventory_seeder.py` | Runs once at startup. Buys minimal YES+NO tokens so ASK orders can be posted. Markets that fail seeding are excluded. |
| `FillCollector` | `fill_collector.py` | Polls CLOB trades API with deduplication (`seen_ids` set) and watermark persistence to `data/trade_watermark.json`. |
| `ScoringMonitor` | `scoring_monitor.py` | Checks `are_orders_scoring` endpoint. After `SCORING_REMEDIATION_THRESHOLD` consecutive failures, returns condition_ids for force-replace. |
| `WebSocketFeed` | `ws_feed.py` | Connects to Polymarket WS market feed. Maintains `{token_id: mid}` cache. Fires `on_mid_change` callback with velocity guard. Auto-reconnects. |
| `RewardsTracker` | `rewards_tracker.py` | Daily snapshot: Gamma API reward allocations + realised PnL + airdrop metrics → `data/rewards_history.json`. |

### Configuration (`config/`)

- **`settings.py`**: Single `Settings` Pydantic-BaseSettings class. All values from `.env`. `tightness_factor` property switches between safe/aggressive based on `MODE`. Field validators for `PREFERRED_TAGS` (comma-separated) and `MODE` (case-insensitive).
- **`markets_filter.py`**: `MarketCandidate` dataclass, `passes_hard_filters()`, `rank_score()` composite ranking, Gamma/CLOB parsers. `_normalise_spread()` converts cents to dollars (any `max_spread > 1.0` is treated as cents).

### Utilities (`utils/`)

- **`discord.py`**: Rate-limited Discord webhook sender with rich embeds (≤5 msg/sec).
- **`logging.py`**: Loguru — stderr (colored) + rotating file (`logs/bot_*.log`) + structured JSON (`logs/bot_*.jsonl`).
- **`helpers.py`**: Pure functions — `round_to_tick`, `clamp_price`, `estimate_reward_score`, `two_sided_q_score`, `seconds_until_midnight_utc`.

### Concurrency Model

- `_market_lock` and `_inventory_lock` (module-level `main.py`) protect shared state from background tasks.
- `OrderManager` has per-market `asyncio.Lock` to prevent hot and cold paths from racing on the same market.
- All CLOB SDK calls go through `asyncio.to_thread()` since `py-clob-client` is blocking.

## Key Domain Concepts

- **Quadratic scoring**: `S(v, s) = ((v - s) / v)² × size`. Tighter quotes score quadratically better. Protocol takes `min(Q_bid_side, Q_ask_side)` — balanced two-sided quoting is critical.
- **max_spread**: Per-market reward parameter. Quotes must be within max_spread of mid to score. Arrives from API in cents (>1.0) or dollars (<1.0) — always normalised to dollars internally.
- **Tick sizes**: Markets use `0.1`, `0.01`, `0.001`, or `0.0001`. The `_TICK_MAP` dict appears in `quoter.py`, `helpers.py`, and `inventory_seeder.py`.
- **neg_risk markets**: Use different exchange contracts (Neg Risk Exchange / Neg Risk Adapter). The `neg_risk` flag on `MarketCandidate` affects contract routing for allowances and orders.

## Test Patterns

Tests use a `_make_settings(**overrides)` helper to construct `Settings` with a dummy `PRIVATE_KEY` and sensible defaults. Market fixtures use `_market()` or `_candidate()` helpers. Tests are pure synchronous unit tests — no external API mocking, no async fixtures.

## Auto-Generated Data Files (gitignored)

- `data/selected_markets.json` — cached market selection
- `data/trade_watermark.json` — fill collector crash-recovery watermark
- `data/rewards_history.json` — rolling reward snapshots
