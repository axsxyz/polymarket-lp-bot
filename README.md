# Polymarket LP Farmer Bot

A 24/7 automated liquidity provider (market-making) bot for [Polymarket](https://polymarket.com).  
Designed for **~$10k USDC** capital across **30-60 markets** simultaneously.

**Goals:** maximise daily LP rewards (quadratic scoring), holding rewards (~4% APY), and $POLY airdrop metrics (volume, consistency, market count).

**Hard constraint:** ZERO directional betting — the bot provides two-sided liquidity only and actively manages inventory skew to protect capital.

---

## Quick Start

```bash
# 1. Clone and enter the project
cd polymarket-lp-farmer-bot

# 2. Create a virtual environment
python3.11 -m venv .venv && source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure
cp .env.example .env
# Edit .env with your private key, funder address, and Discord webhook

# 5. Run (dry-run mode by default — no real orders)
python main.py

# 6. When ready for live trading, set DRY_RUN=false in .env
```

---

## How It Works

### Architecture

```
main.py (async event loop)
 ├── MarketManager     — discovers & filters reward-eligible markets
 ├── Quoter            — computes optimal bid/ask inside max_spread
 ├── OrderManager      — posts GTC limit orders, detects fills
 ├── InventoryTracker  — monitors skew, triggers alerts/pauses
 ├── RewardsTracker    — daily reward snapshots & P&L
 └── DiscordNotifier   — webhook alerts for fills, skew, errors
```

### Quoting Strategy

Polymarket rewards LPs via a **quadratic scoring** formula:

```
S(v, s) = ((v - s) / v)^2 * size
```

Where `v` is the market's `max_spread` and `s` is your quote's distance from the midpoint.

- **Tighter quotes score quadratically better** — placing at 50% of max_spread scores 4x better than at 75%.
- **Two-sided bonus** — the protocol takes `min(Q_bid_side, Q_ask_side)`, so balanced quoting is critical.
- The bot targets `mid ± (max_spread × tightness_factor)` where tightness_factor defaults to **0.48** (safe mode) or **0.35** (aggressive mode).

### Cycle Flow (every 45 seconds)

1. For each active market:
   - Fetch live midpoint from the CLOB orderbook
   - Evaluate inventory skew (see below)
   - Compute optimal bid/ask prices and sizes
   - Cancel stale orders
   - Post fresh GTC limit orders
2. Detect fills between cycles and update inventory
3. Fire Discord alerts for large fills or skew warnings

### Market Selection

Markets are fetched from `get_sampling_simplified_markets()` (only reward-eligible markets) and enriched with Gamma API metadata. Selection criteria:

| Filter | Default |
|--------|---------|
| Min remaining time | 120 days (4 months) |
| Min reward max_spread | 3 cents |
| Max 24h volume | $800,000 |
| Midpoint range | 0.15 – 0.85 |
| Preferred tags | politics, election, fed, geopolitics, midterms, 2026, 2028 |

Markets are ranked by `reward_rate / (1 + estimated_volatility)` with a tag-match bonus, and the top 60 are selected.

### Capital Allocation

Capital is distributed with a **weighted scheme**:
- Top 15 markets by reward score get **25% more** capital
- Remaining markets split the rest evenly
- Each market is capped at `MAX_CAPITAL_PER_MARKET` ($500 default)
- Total never exceeds `TOTAL_CAPITAL` ($10,000 default)

### Skew / Inventory Management

The bot NEVER takes directional views. Three escalation tiers:

| Skew Level | Threshold | Action |
|-----------|-----------|--------|
| Alert | 25% | Discord warning |
| Adjust | 30% | Widen quotes on heavy side, tighten on light side |
| Pause | 45% | Stop quoting the heavy side entirely for 10 minutes |

**Global guard:** if total net directional exposure exceeds 30% of capital, the 5 most imbalanced markets are paused.

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
| `TIGHTNESS_FACTOR_SAFE` | Spread multiplier for safe mode (lower = tighter = higher score but more risk) | 0.48 |
| `TIGHTNESS_FACTOR_AGGRESSIVE` | Spread multiplier for aggressive mode | 0.35 |
| `SIZE_MULTIPLIER` | Order size as multiple of min_incentive_size | 2.0 |
| `REBALANCE_INTERVAL_SECONDS` | Seconds between quoting cycles | 45 |

### Risk

| Variable | Description | Default |
|----------|-------------|---------|
| `TOTAL_CAPITAL` | Total USDC budget | 10000 |
| `MAX_CAPITAL_PER_MARKET` | Max allocation per market | 500 |
| `GLOBAL_MAX_EXPOSURE_PCT` | Max net directional exposure / capital | 0.30 |
| `SKEW_ALERT_THRESHOLD` | Skew ratio for Discord warning | 0.25 |
| `SKEW_ACTION_THRESHOLD` | Skew ratio for quote adjustment | 0.30 |
| `SKEW_PAUSE_THRESHOLD` | Skew ratio to pause the heavy side | 0.45 |
| `SKEW_PAUSE_DURATION_SECONDS` | How long to pause quoting on heavy side | 600 |

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

## Monitoring

### Discord Alerts

The bot sends real-time Discord notifications for:
- Bot start / stop
- Large fills (> $50 notional)
- Skew warnings (> 25%)
- Skew pause events (> 45%)
- Global exposure breaches
- Errors and exceptions
- Daily reward summaries (midnight UTC + 1 min)

### Log Files

Logs are written to `logs/`:
- `bot_YYYY-MM-DD.log` — human-readable, rotated at 50 MB, 30 days retention
- `bot_YYYY-MM-DD.jsonl` — structured JSON for log aggregation

### Data Files

- `data/selected_markets.json` — cached market selection (auto-refreshed every 15 min)
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
FROM python:3.11-slim
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
├── main.py                      # Entry point — async event loop
├── config/
│   ├── __init__.py
│   ├── settings.py              # Pydantic BaseSettings + .env loading
│   └── markets_filter.py        # Market selection rules & scoring
├── core/
│   ├── __init__.py
│   ├── client.py                # Async wrapper around py-clob-client
│   ├── market_manager.py        # Market discovery, caching, capital allocation
│   ├── quoter.py                # Bid/ask calculation engine
│   ├── order_manager.py         # Order lifecycle (post/cancel/fill detection)
│   ├── inventory.py             # Skew tracking & exposure management
│   └── rewards_tracker.py       # Daily reward snapshots & P&L
├── utils/
│   ├── __init__.py
│   ├── discord.py               # Discord webhook notifications
│   ├── logging.py               # Loguru configuration
│   └── helpers.py               # Price rounding, Q-score estimation, etc.
├── data/
│   └── selected_markets.json    # Auto-generated market cache
├── logs/                        # Auto-generated log files
├── .env.example                 # Template for environment variables
├── .gitignore
├── requirements.txt
└── README.md
```

---

## Prerequisites

- **Python 3.11+**
- **Polygon wallet** with USDC funded (the bot trades on Polygon mainnet)
- **Token allowances** set for the exchange contracts (required for EOA wallets — see [Polymarket docs](https://github.com/Polymarket/py-clob-client#important-token-allowances-for-metamaskeoa-users))
- **Discord webhook** (optional but recommended for monitoring)

### Setting Token Allowances (EOA only)

If using a standalone EOA wallet (`SIGNATURE_TYPE=0`), you must approve the exchange contracts to spend your USDC and conditional tokens before the bot can post orders. See the [py-clob-client README](https://github.com/Polymarket/py-clob-client#important-token-allowances-for-metamaskeoa-users) for the exact contract addresses and a Python example.

---

## FAQ

**Q: Will this bot lose money?**  
A: The bot is designed for capital preservation through strict two-sided quoting and skew management. However, adverse selection (being filled at stale prices when markets move) is always a risk. Long-dated, low-volatility markets minimise this. Start with `DRY_RUN=true` to observe before going live.

**Q: How much can I earn?**  
A: Rewards depend on competition (other LPs), the reward pool per market, and how tight your quotes are. With $10k across 30-60 markets, reasonable expectations are $5-50/day in LP rewards depending on market conditions, plus holding rewards (~4% APY), plus $POLY airdrop eligibility.

**Q: What if the bot crashes?**  
A: On startup the bot attempts to load the market cache from `data/selected_markets.json`. The systemd service (or Docker `--restart`) will auto-restart it. The first action after restart is `cancel_all()` to clean up any stale orders.

**Q: Can I run multiple instances?**  
A: Not recommended with the same wallet — you'd have duplicate orders and confused inventory tracking. Use one instance per wallet.

---

## License

Private / internal use. Not open source.
