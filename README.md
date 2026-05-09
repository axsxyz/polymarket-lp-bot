# Polymarket LP Farmer Bot

A 24/7 automated liquidity provider (market-making) bot for [Polymarket](https://polymarket.com).

**Goals:** maximise daily LP rewards (quadratic scoring), holding rewards (~4% APY), and $POLY airdrop metrics (volume, consistency, market count).

**Hard constraint:** ZERO directional betting — the bot provides two-sided liquidity only and actively manages inventory skew to protect capital.

---

## Quick Start

```bash
# 1. Clone and enter the project
cd polymarket-lp-farmer-bot

# 2. Create a virtual environment
python3.12 -m venv .venv && source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure
cp .env.example .env
# Edit .env with your private key, funder address, and Discord webhook

# 5. Set up token allowances (required once per wallet for EOA/MetaMask)
python scripts/setup_allowances.py --all    # wrap USDC→pUSD if needed + approve spenders
python scripts/setup_allowances.py --check-only  # just check status

# 6. Run (dry-run mode by default — no real orders)
python main.py

# 7. When ready for live trading, set DRY_RUN=false in .env
```

---

## Token Allowances (EOA / MetaMask Wallets)

Before the bot can trade, your wallet must approve the Polymarket exchange contracts to spend **pUSD** (collateral) and Conditional Tokens on Polygon. Wrap USDC / USDC.e → pUSD via the script if needed. This is a **one-time setup** per wallet.

**What gets approved:**

| Token | Spender | Purpose |
|-------|---------|---------|
| pUSD | CTF Exchange | Main order execution |
| pUSD | Neg Risk CTF Exchange | Neg-risk market orders |
| pUSD | Neg Risk Adapter | Neg-risk position management |
| Conditional Tokens | (same 3 contracts) | Trading outcome tokens |

**Setup:**

```bash
# Check current allowance status
python scripts/setup_allowances.py --check-only

# Full setup: auto-wrap native USDC + USDC.e balances, then approve (needs MATIC for gas)
python scripts/setup_allowances.py --all

# Interactive wrap only (same as running `python scripts/setup_allowances.py` with no arguments)
python scripts/setup_allowances.py --wrap

# Revoke all approvals (if needed)
python scripts/setup_allowances.py --revoke
```

**Prerequisites:**
- Your `PRIVATE_KEY` must be set in `.env`
- Your wallet needs **~0.1 MATIC** on Polygon for gas fees (6 approval transactions)
- If using a proxy/funder wallet, set `FUNDER_ADDRESS` in `.env`

> If you use an email/Magic wallet (`SIGNATURE_TYPE=1`), allowances are set automatically and you can skip this step.

---

## How It Works

### Architecture

```
main.py (async event loop)
 ├── MarketManager      — discovers & filters reward-eligible markets (Gamma API enrichment)
 ├── Quoter             — computes optimal bid/ask inside max_spread (quadratic scoring)
 ├── OrderManager       — posts GTC limit orders, smart-diff cancel-and-replace
 ├── InventoryTracker   — monitors skew, global exposure guard, per-market pause tiers
 ├── InventorySeeder    — bootstraps YES/NO token positions via GTC orders at startup
 ├── FillCollector      — polls CLOB trades API, deduplicates, advances watermark
 ├── ScoringMonitor     — tracks LP scoring rates, triggers remediation on low scores
 ├── RewardsTracker     — daily reward snapshots & P&L tracking
 └── DiscordNotifier    — webhook alerts (fills, skew, errors, daily dashboard)
```

### Quoting Strategy

Polymarket rewards LPs via a **quadratic scoring** formula:

```
S(v, s) = ((v - s) / v)² × size
```

Where `v` is the market's `max_spread` and `s` is your quote's distance from the midpoint.

- **Tighter quotes score quadratically better** — placing at 50% of max_spread scores 4× better than at 75%.
- **Two-sided bonus** — the protocol takes `min(Q_bid_side, Q_ask_side)`, so balanced quoting is critical.
- The bot targets `mid ± (max_spread × tightness_factor)` where tightness_factor defaults to **0.48** (safe mode) or **0.35** (aggressive mode).

### Cycle Flow (every 45 seconds by default)

1. **Collect fills** — poll the CLOB trades API; filter to active markets only (prevents phantom exposure from orphaned orders).
2. **Batch-fetch midpoints** — one API call for all active markets.
3. **Check global exposure** — net and gross exposure scoped to active markets; if breached, pause the worst offenders (never all markets) and cancel their open orders.
4. **Per-market quoting** — evaluate skew, compute bid/ask, smart-diff against existing orders (only replace if price moves > `ORDER_UPDATE_THRESHOLD_TICKS`).
5. **Scoring remediation** — if scoring rate drops, force-replace orders with tighter spreads.

### Startup Sequence

1. Cancel all pre-existing orders (clears orphaned orders from previous sessions).
2. Discover and filter reward-eligible markets via Gamma API.
3. Seed token positions (GTC limit orders with escalating aggressiveness).
4. Sync on-chain balances into inventory with proper cost basis.
5. Enter main quoting loop.

### Market Selection

Markets are fetched from `get_sampling_simplified_markets()` (only reward-eligible markets) and enriched with Gamma API metadata. Selection criteria:

| Filter | Default |
|--------|---------|
| Min remaining time | 120 days |
| Min reward max_spread | 3 cents |
| Max 24h volume | $800,000 |
| Midpoint range | 0.15 – 0.85 |
| Preferred tags | politics, election, fed, geopolitics, midterms, 2026, 2028 |

Markets are ranked by `reward_rate / (1 + estimated_volatility)` with a tag-match bonus, and the top N are selected.

### Capital Allocation

Capital is distributed with a **weighted scheme**:
- Top 15 markets by reward score get **25% more** capital
- Remaining markets split the rest evenly
- Each market is capped at `MAX_CAPITAL_PER_MARKET`
- Dynamic reallocation shifts capital toward higher-scoring markets every 5 minutes

### Skew / Inventory Management

The bot NEVER takes directional views. Four escalation tiers:

| Skew Level | Threshold | Action |
|-----------|-----------|--------|
| Alert | 25% | Discord warning |
| Adjust | 30% | Widen quotes on heavy side, tighten on light side |
| Recovery | 45% | Reduced ASK on heavy side to attract rebalancing fills |
| Hard Pause | 70% | Stop quoting the heavy side entirely for 10 minutes |

**Global guard:** if total net directional exposure across active markets exceeds `GLOBAL_MAX_EXPOSURE_PCT × TOTAL_CAPITAL`, the most imbalanced markets are paused (always keeping at least one active) and their open orders cancelled to stop further adverse fills.

### Inventory Seeding

The CLOB requires you to **hold** conditional tokens to post ASK orders. At startup, the seeder acquires minimal YES/NO positions for each market using GTC limit orders with a 5-second timeout and 3 escalating price attempts. Markets that fail seeding are skipped (BID-only, won't score LP).

---

## Configuration

All settings are in `.env` (see `.env.example`). Key parameters:

### Wallet & Auth

| Variable | Description | Default |
|----------|-------------|---------|
| `PRIVATE_KEY` | Your EOA private key (hex with 0x prefix) | required |
| `FUNDER_ADDRESS` | Proxy wallet address (leave empty for EOA) | "" |
| `SIGNATURE_TYPE` | 0=EOA, 1=Magic/email, 2=browser proxy | 0 |

### Strategy

| Variable | Description | Default |
|----------|-------------|---------|
| `MODE` | `safe` or `aggressive` | safe |
| `DRY_RUN` | `true` = log orders but don't post them | true |
| `TIGHTNESS_FACTOR_SAFE` | Spread multiplier for safe mode | 0.48 |
| `TIGHTNESS_FACTOR_AGGRESSIVE` | Spread multiplier for aggressive mode | 0.35 |
| `SIZE_MULTIPLIER` | Order size as multiple of min_incentive_size | 2.0 |
| `REBALANCE_INTERVAL_SECONDS` | Seconds between quoting cycles | 45 |
| `ORDER_UPDATE_THRESHOLD_TICKS` | Min price change (in ticks) before replacing an order | 2 |

### Risk

| Variable | Description | Default |
|----------|-------------|---------|
| `TOTAL_CAPITAL` | Total USDC budget | 10000 |
| `MAX_CAPITAL_PER_MARKET` | Max allocation per market | 500 |
| `GLOBAL_MAX_EXPOSURE_PCT` | Max net directional exposure / capital | 0.30 |
| `GLOBAL_MAX_GROSS_EXPOSURE_PCT` | Max gross exposure (positions + open orders) / capital | 0.80 |
| `SKEW_ALERT_THRESHOLD` | Skew ratio for Discord warning | 0.25 |
| `SKEW_ACTION_THRESHOLD` | Skew ratio for quote adjustment | 0.30 |
| `SKEW_PAUSE_THRESHOLD` | Skew ratio to enter recovery mode | 0.45 |
| `SKEW_HARD_PAUSE_THRESHOLD` | Skew ratio to hard-pause the heavy side | 0.70 |
| `SKEW_PAUSE_DURATION_SECONDS` | How long to pause quoting on heavy side | 600 |

### Inventory Seeding

| Variable | Description | Default |
|----------|-------------|---------|
| `SEED_ENABLED` | Enable initial token seeding at startup | true |
| `SEED_MAX_USD_PER_SIDE` | Max USD to spend seeding one side of a market | 150 |
| `SEED_AGGRESSIVE_TICKS` | Extra ticks above mid for seeding limit orders | 3 |
| `SEED_BUDGET_PCT` | Fraction of TOTAL_CAPITAL reserved for seeding | 0.30 |

### API & Performance

| Variable | Description | Default |
|----------|-------------|---------|
| `API_RATE_LIMIT_RPS` | Max API requests per second | 1.5 |
| `API_RATE_LIMIT_BURST` | Token bucket burst capacity | 20 |
| `MAX_MARKETS_PER_CYCLE` | Max markets processed per cycle | 20 |

### Discord

| Variable | Description |
|----------|-------------|
| `DISCORD_WEBHOOK_URL` | Full Discord webhook URL for alerts |

---

## Safe Mode vs Aggressive Mode

| Parameter | Safe | Aggressive |
|-----------|------|------------|
| Tightness factor | 0.48 | 0.35 |
| Expected Q score (relative) | ~27% of max | ~42% of max |
| Adverse selection risk | Lower | Higher |
| Recommended for | Starting out, volatile periods | Established, stable markets |

Switch between modes by setting `MODE=safe` or `MODE=aggressive` in `.env`. The bot reads the setting on startup.

---

## Scripts

### Sell Positions

Liquidate all or specific conditional token positions:

```bash
python scripts/sell_positions.py
```

Uses GTC limit orders with escalating price aggressiveness to sell positions across all markets where the wallet holds tokens.

### Setup Allowances

```bash
python scripts/setup_allowances.py --all      # wrap + approve spenders
python scripts/setup_allowances.py --check-only  # just check
python scripts/setup_allowances.py --revoke      # revoke all
```

---

## Monitoring

### Discord Alerts

The bot sends real-time Discord notifications for:
- Bot start / stop
- Order fills (with price and size)
- Skew warnings (> 25%) and pause events
- Global exposure breaches (with per-market breakdown)
- Scoring rate changes
- Errors and exceptions
- Periodic dashboard with capital utilization and P&L

### Log Files

Logs are written to `logs/`:
- `bot_YYYY-MM-DD.log` — human-readable, rotated at 50 MB, 30 days retention
- `bot_YYYY-MM-DD.jsonl` — structured JSON for log aggregation

### Data Files

- `data/selected_markets.json` — cached market selection (auto-refreshed every 15 min)
- `data/trade_watermark.json` — fill collector watermark for crash recovery
- `data/rewards_history.json` — rolling 90-day reward snapshots

---

## Running on a VPS

### systemd Service

Create `/etc/systemd/system/polymarket-lp.service`:

```ini
[Unit]
Description=Polymarket LP Farmer Bot
After=network-online.target

[Service]
Type=simple
User=polybot
WorkingDirectory=/home/polybot/polymarket-lp-farmer-bot
Environment=PATH=/home/polybot/polymarket-lp-farmer-bot/.venv/bin:/usr/bin
ExecStart=/home/polybot/polymarket-lp-farmer-bot/.venv/bin/python main.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable polymarket-lp
sudo systemctl start polymarket-lp

# Check status
sudo systemctl status polymarket-lp
journalctl -u polymarket-lp -f
```

### Docker (alternative)

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "main.py"]
```

```bash
docker build -t polymarket-lp .
docker run -d --name polymarket-lp --env-file .env --restart unless-stopped polymarket-lp
```

---

## Project Structure

```
polymarket-lp-farmer-bot/
├── main.py                        # Entry point — async quoting loop
├── config/
│   ├── settings.py                # Pydantic BaseSettings + .env loading
│   └── markets_filter.py          # Market selection rules & scoring
├── core/
│   ├── client.py                  # Async wrapper around py-clob-client (rate-limited)
│   ├── market_manager.py          # Market discovery, Gamma API enrichment, caching
│   ├── quoter.py                  # Bid/ask calculation engine (quadratic scoring)
│   ├── order_manager.py           # Order lifecycle (post/cancel/smart-diff)
│   ├── inventory.py               # Skew tracking, exposure guard, reconciliation
│   ├── inventory_seeder.py        # GTC-based token position bootstrapping
│   ├── fill_collector.py          # Trade API polling with watermark persistence
│   ├── scoring_monitor.py         # LP scoring rate tracking & remediation
│   └── rewards_tracker.py         # Daily reward snapshots & P&L
├── utils/
│   ├── discord.py                 # Discord webhook notifications (embeds)
│   ├── logging.py                 # Loguru configuration
│   ├── helpers.py                 # Price rounding, Q-score estimation
│   └── allowance_check.py         # On-chain allowance verification
├── scripts/
│   ├── setup_allowances.py        # One-time token approval setup
│   └── sell_positions.py          # Position liquidation tool
├── tests/
│   ├── test_quoter.py
│   ├── test_inventory.py
│   ├── test_order_manager.py
│   └── test_markets_filter.py
├── data/                          # Auto-generated runtime data
├── logs/                          # Auto-generated log files
├── .env.example                   # Template for environment variables
├── .gitignore
├── requirements.txt
└── ROADMAP.md
```

---

## Prerequisites

- **Python 3.11+** (3.12 recommended)
- **Polygon wallet** with USDC funded (the bot trades on Polygon mainnet)
- **Token allowances** set for the exchange contracts (required for EOA wallets — see above)
- **Discord webhook** (optional but recommended for monitoring)
- **~0.1 MATIC** on Polygon for gas (allowance approvals and token transfers)

---

## FAQ

**Q: Will this bot lose money?**
A: The bot is designed for capital preservation through strict two-sided quoting and skew management. However, adverse selection (being filled at stale prices when markets move) is always a risk. Long-dated, low-volatility markets minimise this. Start with `DRY_RUN=true` to observe before going live.

**Q: How much can I earn?**
A: Rewards depend on competition (other LPs), the reward pool per market, and how tight your quotes are. Reasonable expectations vary with capital deployed and market conditions.

**Q: What if the bot crashes?**
A: On startup the bot cancels all pre-existing orders on the exchange to clear orphaned orders. The fill collector resumes from its persisted watermark. The systemd service (or Docker `--restart`) will auto-restart it.

**Q: Can I run multiple instances?**
A: Not recommended with the same wallet — you'd have duplicate orders and confused inventory tracking. Use one instance per wallet.

**Q: Why are ASK orders rejected with "not enough balance"?**
A: The CLOB requires you to **hold** conditional tokens to post ASK orders (no naked sells). The bot seeds tokens at startup via `InventorySeeder`. If seeding fails for a market, it becomes BID-only and won't score LP. Ensure sufficient capital for `SEED_BUDGET_PCT × TOTAL_CAPITAL`.

**Q: What is the "Global exposure breached" warning?**
A: The exposure guard sums net directional exposure across all active markets. When it exceeds `GLOBAL_MAX_EXPOSURE_PCT × TOTAL_CAPITAL`, the most imbalanced markets are paused (at least one always stays active) and their open orders are cancelled. This prevents runaway one-sided accumulation.

---

## License

Private / internal use. Not open source.
