# AUDIT — Polymarket LP Farmer Bot (Python)

**Date:** 2026-08-29  
**Repo:** `AXS13/Polymarket_lp` (read on `main` @ `d906d86`)  
**Scope:** Read-only pass of `main.py`, `core/*`, `config/*`, `tests/*`, `requirements.txt`, `.env.example`, and the May 2026 audit (`audit_opus4_7_max_may_2026.txt`). No rewrite. No new strategy. No drive-by refactors.

**What this is not:** the May 2026 audit is stale. Most of its “known bugs” (rate limit 5 RPS, no jitter, no drawdown breaker, no midpoint guard, seed cost-basis, `/10` LP share, missing resolution/toxicity/backtest) were already patched in May. This document is the current code.

**Tests in this environment:** `pytest` is not installed on the audit host. Test *count* from source is **287** functions across 14 files. There is no CI (no `.github/workflows/`). There are **zero** async/integration tests for `main.py`, `hot_requote`, `FillCollector`, `WebSocketFeed`, or `AsyncClobClient`.

---

## 0. Verdict in one page

This is a real market-making *skeleton*, not a toy order-spammer. The quoting math, skew policy, smart-diff, watermark, atomic seeder, and layered guards are worth keeping.

It is still not safe to treat as “we have a live tape and we know if we are blowing up.” Operator visibility is Discord + loguru text. `fastapi` and `uvicorn` are in `requirements.txt` and unused. There is no `dashboard/` package. Inventory is updated by polling fills on the **45s cold cycle**. The hot WS path can re-post into markets the cold path just paused.

**Python is not the binding constraint.** The official CLOB SDK is Python; 3–20 markets at 1.5 RPS fit asyncio. The binding constraints are (1) fill/inventory latency, (2) pause holes on the hot path, (3) a resolution “max-loss” formula that punishes a balanced LP book, and (4) a strategy that farms 1-tick Q-score on 120-day politics names without a measurable tape.

If you are still debating language: keep Python. Spend the next engineering cycles on not-blowing-up and on a local fills/spread/pnl/errors tape.

---

## 1. Alarming issues (verified in code)

Ordered by “can this silently put the wallet in a hole.”

### A1. Hot path re-quotes markets the cold path just paused

`hot_requote` (`main.py:193`) is documented as “no fill collection, no skew eval, no exposure checks.” The skew claim is already stale — it *does* call `inventory.evaluate` (`main.py:257-258`). What it still does **not** check:

| Guard | Cold path | Hot path |
|---|---|---|
| Daily drawdown | yes (`main.py:396`) | yes (`main.py:203`) |
| Midpoint pause | yes (`main.py:574`) | yes (`main.py:210`) |
| Spike pause | yes (`main.py:629`) | yes (`main.py:226`) |
| Toxicity pause | yes (`main.py:637-681`) | **no** |
| Resolution delist | yes (`main.py:451-503`) | records mid only (`main.py:219-224`) |
| Per-market / global exposure | yes (`main.py:505-567`) | **no** |
| `SEED_FAILED` / `MarketState.INACTIVE` | skip via `paused_set` / `skip_cids` | **no** — `get_market_by_token` (`core/market_manager.py:281`) ignores state |

Sequence that is live today: cold cycle trips toxicity / resolution / exposure → `cancel_orders_for_market` → a WS `price_change` 200 ms later → `hot_requote` posts GTC again.

`ToxicityAnalyzer` even documents the intended contract and it is not implemented:

```27:29:core/toxicity_analyzer.py
Cold path only — hot path reads `is_toxic_paused()` for symmetry with
midpoint guard (Quoter.is_midpoint_paused) and spike pause
(Quoter.is_spike_paused).
```

There is no `_hot_toxicity` global. Resolution delist sets `MarketState.INACTIVE` but does not remove the market from `_markets` / `_token_index`, so the hot path still sees it.

**This is the highest-priority race.** The safety layers only exist if the hot path honors them.

### A2. Resolution `max_loss` treats a hedged LP book as naked risk

```152:170:core/resolution_guard.py
    def max_loss_check(...):
        ...
        max_loss = max(float(yes_size), float(no_size)) * 1.0
        return (max_loss > limit, max_loss, limit)
```

Default `RESOLUTION_MAX_LOSS_USD=25`. For a two-sided LP, YES+NO is a complement: matched pairs redeem at $1. Worst case is the **unmatched** size, not `max(yes, no)`.

- 26 YES + 26 NO (healthy, ~$26 notional each side at 0.50) → `max_loss=26` → **delist**.
- 40 YES + 35 NO (still mostly hedged) → delist.
- 24 YES + 0 NO (actually dangerous) → **not** delisted.

The guard that was added to stop resolution blow-ups will fire on successful inventory accumulation and will miss the one-sided book it was meant to catch. Tests in `tests/test_resolution_guard.py` encode this formula; they do not test hedge-aware loss.

Delist also does not persist across refresh: `should_delist` is re-evaluated every cycle; `move_24h` can flicker back in-band and the market is quotable again (and the hot path never left).

### A3. Fills are 45 seconds late; hot path quotes on stale inventory

Fills are collected only at the start of the cold cycle:

```374:376:main.py
    await fill_collector.poll()
    all_fills = fill_collector.drain()
```

There is no user-channel WebSocket. `WebSocketFeed` is market-data only (`core/ws_feed.py:24`, `wss://.../ws/market`). `last_trade_price` is used to mutate the mid cache (see A5), not to detect *our* fills.

Consequence: after an adverse fill, the bot can keep bidding the same side for up to `REBALANCE_INTERVAL_SECONDS` (default 45, plus jitter) before `InventoryTracker.record_fills` runs. Skew / exposure / toxicity all see yesterday’s position on the hot path.

`get_trades_since` (`core/client.py:387-389`) is a single `get_trades(TradeParams(after=after_ts))` — no cursor pagination. A burst of trades can be truncated. **Hypothesis (not load-tested):** fine at Stage-1 volume; will drop fills if the bot ever scales.

### A4. Fill watermark is committed before inventory is updated

`FillCollector.poll` advances and persists `data/trade_watermark.json` (and `seen_ids`) inside `poll()` (`core/fill_collector.py:130-132`). `drain()` is a later in-memory handoff (`:141-145`). `main.py` then filters and `record_fills`.

Crash between `_save_watermark()` and `inventory.record_fills`:

- those trades will not be replayed (watermark past them; IDs in `seen_ids`);
- positions / cost basis / drawdown / toxicity VPIN are wrong until the 30-minute `reconciliation_task` — which is itself broken (A7).

The watermark *design* is good. The commit-before-apply order is not.

Related hole: fills whose `condition_id` is not in the current `markets` list are dropped (`main.py:377-383`). The trade still happened. Capital moved. Inventory did not.

### A5. WS `last_trade_price` overwrites BBO mid

```208:214:core/ws_feed.py
    def _handle_last_trade(self, msg: dict) -> None:
        """last_trade_price — update cache but don't trigger requote."""
        ...
            self._mid_prices[asset_id] = float(price)
            self._last_update[asset_id] = time.monotonic()
```

Cold-path midpoint fetch prefers this cache (`main.py:106-115`). A print is not a mid. It also refreshes the 60s staleness clock, so a stale book plus a recent print looks “fresh.” Quotes can center on last sale.

### A6. P2b fill-vs-mid is structurally dead on the live path

`ToxicityAnalyzer` scores adverse drift at +5s / +30s / +5min with `_MID_LOOKUP_TOLERANCE_S = 2.0` (`core/toxicity_analyzer.py:54-65`). Mid samples for toxicity are ingested only on the cold path (`main.py:440-444`). The hot path feeds `Quoter.record_mid` and `ResolutionGuard.record_midpoint`, **not** `toxicity.record_mid`.

With ~1 mid per 45s and a 2s match window, +5s and +30s almost never resolve. +5min is a 4-second needle in a 45-second haystack.

Unit tests pass because they inject mids at the exact horizons (`tests/test_toxicity_analyzer.py`). Live, the only toxicity signal that can fire is **VPIN on the bot’s own fills** — and two-sided quoting tends to pair BID+ASK, which is the same degeneracy already documented in `backtest/` (`TestVpinSymmetry`). `_VPIN_MIN_FILLS = 10` (`toxicity_analyzer.py:74`) means the first adverse streak is ignored.

### A7. Inventory reconciliation is a no-op

`reconciliation_task` (`main.py:1111`) calls `client.get_balance_allowance()` with **no params** (`main.py:1121`, `core/client.py:429-434`). The working conditional-token path is `get_conditional_balance(token_id)` with `BalanceAllowanceParams(asset_type="CONDITIONAL", token_id=...)` (`client.py:439-462`).

A bare `get_balance_allowance()` is collateral-shaped (or an error, swallowed to `None`). `_parse_exchange_positions` then looks for `data` / `positions` / `asset_id` (`main.py:1143-1182`) and typically returns `{}`. `InventoryTracker.reconcile` never runs.

So: watermark crash (A4) has no backup. Local inventory is the only truth, and it is not persisted.

### A8. Daily drawdown ignores mark-to-mid

`check_daily_drawdown` (`core/inventory.py:398-436`) trips on `realised_pnl_today < -DAILY_DRAWDOWN_LIMIT_PCT * TOTAL_CAPITAL`. Realised PnL only moves on ASK fills (`inventory.py:228-240`). A book that is long YES at 0.50 while mid goes to 0.12 does not pause the bot. The resolution/midpoint guards are supposed to cover that — except the hot path can ignore them (A1) and `max_loss` is wrong (A2).

Default 5% of `$350` live capital (memory-bank) is `$17.50`. Easy to miss; also easy to trip on a single round-trip if you only count realised.

### A9. Smart-diff tick is hardcoded to 0.01

```35:36:core/order_manager.py
TICK_SIZE = 0.01
```

Used as the replace threshold (`order_manager.py:268`) and in the log (`:322`). Quoter and seeder use the per-market `_TICK_MAP` (`0.1` / `0.01` / `0.001` / `0.0001`).

- Tick `0.001` / `0.0001` markets: orders stay put for 10–100 ticks of mid move.
- Tick `0.1` markets: a 0.01 move (0.1 tick) forces replace churn.

No test covers non-`0.01` ticks (`tests/test_order_manager.py` is 3 tests).

### A10. Market refresh does not close the old book or seed the new one

`market_refresh_task` (`main.py:976`) calls `market_mgr.refresh()` then re-subscribes WS. `refresh()` logs IN/OUT (`core/market_manager.py:229-241`) and does **not**:

- cancel live orders on OUT markets (they sit until `MAX_ORDER_AGE_SECONDS=600` or ghost recon — ghost recon only cancels IDs *missing* from `_live_orders`, so tracked OUT orders stay);
- run `InventorySeeder` on IN markets (`seed_all` is startup-only, `main.py:1262-1264`);
- add new unseeded cids to `skip_cids` (that set is captured once).

New names get BID-only quotes (`quoter.py:505-508` zeros ASK when `position_size <= 0`) → `ONE_SIDED` → Q-score 0, while still consuming rate limit and inventory risk on the bid.

### A11. There is no live tape

What exists:

- Loguru stderr + `logs/bot_*.log` + `logs/bot_*.jsonl` (`utils/logging.py`) — log lines, not a queryable fills/spread/pnl schema.
- Discord: fills, cycle summary, 15-minute “dashboard” (`main.py:1053-1086`, `utils/discord.py:send_status_dashboard`). Rate-limited webhook. Not a tape.
- `data/airdrop_metrics.json` / `data/rewards_history.json` — daily aggregates, LP share is `pool * LP_ESTIMATED_SHARE` (default 10%, `rewards_tracker.py:303-305`).
- `data/trade_watermark.json` — cursor, not fills.

What does **not** exist:

- `dashboard/` package (docs that mention it are stale; `ENABLE_DASHBOARD` is not in `Settings`).
- Any FastAPI/Starlette app. `fastapi==0.131.0` and `uvicorn==0.41.0` are unused. So is `APScheduler`.
- Persistence of `InventoryTracker` positions / cost basis.
- A CSV/JSONL of `{ts, cid, side, price, size, mid, spread, realised_pnl, error}`.

You cannot answer “what was my edge last week?” from this repo. Discord is not a substitute.

### A12. Smaller but real holes

- **`net_exposure_usd` uses YES average cost only** (`inventory.py:75-78`). Heavy-NO books are marked at `$0.50` or at YES cost. Global net gate (`GLOBAL_MAX_EXPOSURE_PCT`) is therefore biased.
- **Scoring remediation races the hot path** (`main.py:789-817`): `cancel_and_replace(..., force_replace=True)` without `order_mgr.market_lock`. Ghost recon (`:1010`) and stale cleanup (`:1000`) also take no per-market lock.
- **Startup `cancel_all_orders` failure is non-fatal** (`main.py:1240-1244`). Previous-session GTCs stay live; the new run posts on top. Comment at `:1237-1239` describes exactly why that is bad.
- **`signal.signal` instead of `loop.add_signal_handler`** (`main.py:1373-1374`). Works on the main thread; fragile under some asyncio deployments. Shutdown *intent* (cancel-all in `finally`, `:1436-1447`) is correct.
- **`ALLOW_NAKED_ASKS`** is in `Settings` / `.env.example` and never read. ASK is always capped to held size (`quoter.py:505-508`). Dead config.
- **`.env.example` has `MIN_MARKETS=30` and `MAX_MARKETS=20`.** `refresh()` will permanently warn that the universe is below minimum (`market_manager.py:167-173`).
- **Dead code in `OrderManager`:** `_recent_fills` / `drain_new_fills` (`:81-103`), `_detect_fills` stub (`:530`), `periodic_cleanup` (“every 4h cancel-all”, `:426`) — **not scheduled**. `main.py` module docstring still says “periodic cleanup (4 h)”.
- **Hot-path docstring vs code** (`main.py:196-198`): claims no skew eval; evaluates skew.
- **No inventory persistence.** Restart rebuilds size from chain (`main.py:1293-1321`) and cost from `seed_costs` or `bal * mid`. Pre-existing tokens get mid as cost basis (the May bug is only fixed for *this* boot’s seeder).

---

## 2. What is good — keep this

Do not throw these away in a rewrite.

| Piece | Why it stays |
|---|---|
| **Tick-based quoter + Q-score honesty** (`core/quoter.py`) | Offset = ticks × vol_mult × tox_mult, clamped to `max_spread * 0.499`. One-sided books force Q-score 0 (`:535-540`). `is_two_sided` is the right LP invariant. |
| **Continuous skew penalty** (`quoter.py:457-470`, `SKEW_PENALTY`) | Sign-symmetric; replaced the old 1.4× / 0.85× magic. Combined with 4-tier `InventoryTracker.evaluate` (`inventory.py:246-298`): alert / adjust / recovery ASK cut / hard pause. |
| **ASK ≤ position** (`quoter.py:505-508`) | No naked short of tokens the wallet does not hold. (`ALLOW_NAKED_ASKS` is a lie; the code is conservative.) |
| **Smart-diff cancel-replace** (`order_manager.py:245`) | Concept is correct for quadratic scoring (uptime). Cancel-first on ASK so tokens unlock. Keep this; fix `TICK_SIZE` (A9). |
| **Atomic seeder** (`inventory_seeder.py:170+`) | Worst-case price pre-check on both sides, book depth, abort NO if YES is 0. Tests in `tests/test_seeder_atomicity.py`. |
| **Fill watermark + `seen_ids`** (`fill_collector.py`) | Overlap window, persist for crash *replay* — right idea. Fix commit-before-apply (A4). |
| **`MarketState` + realloc donors** (`market_manager.py:77-128`, `reallocate_capital`) | `SEED_FAILED` sticky; donors are `{SEED_FAILED, INACTIVE}`; `ONE_SIDED` is not drained. Tests in `tests/test_market_states.py`. |
| **Exposure: per-market then global** (`inventory.py:338-392`, `main.py:505-567`) | `MAX_MARKET_EXPOSURE_RATIO` before `MAX_EXPOSURE_RATIO` on *deployed* capital. |
| **Startup + shutdown `cancel_all_orders`** (`main.py:1236`, `:1436`) | Non-negotiable. Keep; do not continue silently on startup failure (A12). |
| **Settings as single source** (`config/settings.py`) | Pydantic bounds, `extra="ignore"`, `DRY_RUN=true` default. Do not invent env vars. |
| **Gamma double-shape parser + pagination fix** (`markets_filter.py`, `market_manager.py` `GAMMA_PAGE_LIMIT=100`) | The May 19 boot-loop (limit=200 vs API cap 100) is fixed and tested (`tests/test_gamma_pagination.py`). |
| **Rate limit 1.5 RPS + jitter** (`settings.py:84`, `main.py:1425`) | May audit items — done. |
| **Pure-logic tests** | Quoter, inventory, resolution, toxicity, seeder, market states, exposure, filter: this is the valuable test surface. Keep adding here; do not start with a rewrite. |
| **Loguru JSONL** (`utils/logging.py:54-61`) | Foundation for a real tape. Not a tape yet. |
| **`backtest/` honesty** | Naive fill model and VPIN symmetry are documented, not hidden. Ranking (offset=1 tick) is the only claim they make. |

Guards that are *real* once A1 is closed: midpoint band `[0.18, 0.82]` over 3 cycles, spike pause, drawdown *as a realised-loss brake*, circuit breaker on post failures, ghost recon.

---

## 3. Next 3 engineering moves

Ranked by impact on **not blowing up** and on having a **real live tape** (fills / spread / pnl / errors). Not Discord. Not a new strategy. Not Rust.

### Move 1 — Local tape (fills, quotes, mids, errors, mark-to-mid PnL)

**Why first:** you cannot debug A1–A6, and you cannot tell whether LP rewards beat adverse selection, without a clocked record. Discord 15-minute embeds (`periodic_dashboard_task`) are not that record. FastAPI is already a pinned dependency and there is still no HTTP surface.

Minimum viable tape (no product, no charts):

- Append-only JSONL (or sqlite) written on the same process: every fill (`FillEvent` + mid at fill), every post/cancel, every skip reason, every exception, a 1 Hz or per-cycle snapshot of `{cid, mid, bid, ask, yes, no, realised, mtm}`.
- Optional: a 50-line FastAPI process that **only reads** that file (`GET /health`, `GET /fills?limit=`, `GET /pnl`). Read-only. No trading controls in v1.

This is also the only way to validate that fill-vs-mid (A6) and VPIN ever fire in production.

### Move 2 — Close the hot-path pause hole + ingest fills off the quote clock

**Why second:** this is the actual blow-up path.

1. Honor on `hot_requote`, before compute/post: `is_toxic_paused`, `MarketState` in `{SEED_FAILED, INACTIVE}`, exposure pause set, `skip_cids`. Feed `toxicity.record_mid` from the same WS tick that already feeds the quoter (so A6 can live).
2. Do **not** wait 45s to learn about fills. Either:
   - subscribe to the CLOB **user** WS (trades/orders) if you accept that dependency, or
   - run `FillCollector.poll` on its own 2–5s task, apply fills under `_inventory_lock`, **then** persist the watermark.
3. Persist inventory (size + cost basis) next to the watermark so a crash does not reset PnL to `bal * mid`.

Do not add a third quoting path. Fix the two you have.

### Move 3 — Make the capital brakes match two-sided LP reality

**Why third:** the guards are pointed at the wrong risk.

1. Resolution `max_loss` = unmatched size (or `cost − min(yes,no)`), not `max(yes,no) * $1`. Keep the hard extreme-mid net (`<0.05` / `>0.95`). Persist delist across refresh (do not rely on the hot path to “just not see it”).
2. Drawdown: realised **and** mark-to-mid vs UTC-midnight snapshot. Unrealised 0.50→0.12 is the resolution event.
3. Fix `TICK_SIZE` to the market tick (A9). Fix `get_balance_allowance()` reconciliation to per-token CONDITIONAL (A7). Cancel OUT markets on refresh; seed or skip IN markets (A10). Fail startup if `cancel_all` fails.

After 1–3 you still have a Python LP bot, but you can see it and it will not immediately re-arm a paused market.

**Explicitly not next:** P2e dynamic sizing, P3 profiles, multi-level book, taker logic, Rust, a public dashboard product. TASK.md P2e/P3 can wait until the tape exists.

---

## 4. Strategy vs language — be honest

Farming Polymarket quadratic LP rewards on **long-dated politics** (`MIN_END_DATE_DAYS=120`, midpoint `[0.15, 0.85]`, 1-tick offset, 45s cold cycle, poll fills) is a harder problem than “we wrote it in Python.”

- **Rewards:** `RewardsTracker` still cannot see your share. It multiplies the Gamma pool by `LP_ESTIMATED_SHARE` (10%). That is a hope, not a P&L line.
- **Adverse selection:** 1-tick GTC on a public book is the snack tray for anyone with a faster signal. Toxicity/VPIN on *your* fills, sampled every 45s, will not save you (A3, A6). The backtest says offset=1 wins *under a naive paired fill model* — the same model that makes VPIN ≈ 0. That is not evidence of live edge.
- **Capital lock:** 120-day names lock inventory until you sell or resolve. Resolution risk is binary. A2’s formula fights the strategy (accumulating both sides).
- **Latency:** hot path ~1s on mid is fine for these names. Fill awareness at 45s is not. Language will not fix that.
- **Competition:** ANALYSIS.md (March 2026) already said this is hobby-scale vs shops with capital and latency. That still reads true.

Python + `py-clob-client-v2` + asyncio is the correct stack for this wallet and this SDK. A Rust port would re-implement the same holes, months later, with no tape.

If the live book is `$350` / 3 markets / `DRY_RUN=false` (memory-bank, 2026-05-09): that is a smoke budget. Do not scale `TOTAL_CAPITAL` on Discord screenshots. Scale after Move 1 shows fills, spread captured, mark-to-mid, and errors on disk.

---

## 5. Dead / misleading surfaces (do not generate)

| Claim | Reality |
|---|---|
| `dashboard/` | Does not exist. FastAPI unused. |
| `ENABLE_DASHBOARD` | Not a `Settings` field. |
| `core/positions_aggregator.py`, `scripts/positions_cli.py` | Do not exist (docs cleaned in May; `docs/ANALYSIS.md` still mentions the CLI). |
| `ALLOW_NAKED_ASKS=true` | Ignored. |
| `periodic_cleanup` every 4h | Method exists; nothing schedules it. |
| May 2026 audit “no backtest / no drawdown / 5 RPS / no jitter” | Fixed. Do not re-litigate. |
| Architecture rule “`backtest/` does not exist” | Stale. `backtest/` exists. |

---

## 6. Map of what was actually read

- `main.py` (hot path, `run_quoting_cycle` steps 1–7, background tasks, startup/shutdown)
- `core/quoter.py`, `inventory.py`, `order_manager.py`, `fill_collector.py`, `ws_feed.py`, `client.py`, `market_manager.py`, `inventory_seeder.py`, `scoring_monitor.py`, `rewards_tracker.py`, `toxicity_analyzer.py`, `resolution_guard.py`
- `config/settings.py`, `config/markets_filter.py`
- `utils/logging.py` (tape-adjacent)
- `requirements.txt`, `.env.example`
- `tests/` (14 files, 287 `def test_*`)
- `audit_opus4_7_max_may_2026.txt` (historical; not authoritative)
- `memory-bank/lp-bot.md`, `.cursor/TASK.md` (session protocol; P2e not started)

No trading code was changed in this pass.

---

## Status after this pass (2026-08-29 implementation)

Python kept. No new strategy. `pytest tests/` → **310 passed**. User-channel WS is **wontfix** (`py-clob-client-v2` is REST-only; fills are a 2–5s REST task). May 2026 5 RPS / jitter / realised drawdown were already fixed before this pass.

| ID | Status | Why |
|---|---|---|
| A1 | **fixed** | `hot_skip_reason` before compute/post: toxicity, INACTIVE, SEED_FAILED, `skip_cids`, exposure, drawdown, midpoint, spike. Hot path also `toxicity.record_mid`. |
| A2 | **fixed** | `max_loss = abs(yes − no)`. Extreme-mid `<0.05` / `>0.95` unchanged. Sticky delist file + `mark_resolution_inactive`. |
| A3 | **fixed** / user-WS **wontfix** | `fill_ingest_task` polls every `FILL_POLL_INTERVAL_SECONDS` (default 3). SDK has no user WS. |
| A4 | **fixed** | `fetch()` does not persist; `commit(fills)` after inventory apply. |
| A5 | **fixed** | `_handle_last_trade` is a no-op; BBO `_process_mid_update` still writes mid + freshness. |
| A6 | **fixed** (ingest) | Hot path feeds `toxicity.record_mid` on the same tick as the quoter. VPIN/fill-vs-mid still need live tape to prove they fire. |
| A7 | **fixed** | Reconciliation uses per-token `get_conditional_balance`. |
| A8 | **fixed** | Drawdown trips on realised **or** `mark_to_mid_equity − midnight_mtm`. |
| A9 | **fixed** | `should_replace_price(old, new, tick, threshold)` uses market tick via `TwoSidedQuotes.tick_size`. |
| A10 | **fixed** | Refresh cancels OUT, seeds or skip-lists IN, re-subscribes WS excluding `skip_cids`. |
| A11 | **fixed** | `LocalTape` JSONL + localhost FastAPI `GET /health` `/fills` `/pnl`. Not a product dashboard. |
| A12 | **fixed** (itemised) | Unmatched `net_exposure_usd`; scoring remedi / ghost / stale cleanup take per-market lock; startup `cancel_all` fail-closed unless `DRY_RUN`; `add_signal_handler` with Windows `signal.signal` fallback; `ALLOW_NAKED_ASKS` removed; `MIN_MARKETS` ≤ `MAX_MARKETS`; dead OrderManager fill/cleanup methods gone; inventory persist `data/inventory_state.json`. |
