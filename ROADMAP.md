# Polymarket LP Farmer Bot — Code Action Plan (Task Roadmap)

## Project Goal

Maximize total return (LP rewards + holding rewards + $POLY airdrop value) while maintaining near-zero directional risk and capital protection, on ~$10k USDC across as many Polymarket markets as the API budget allows.

**Three revenue sources, two hard constraints:**

| | Revenue source | Key lever |
|---|---|---|
| R1 | LP Rewards (quadratic scoring) | Tighter quotes, two-sided, many markets, always scoring |
| R2 | Holding Rewards (~4% APY) | Hold tokens over time (automatic from inventory) |
| R3 | $POLY Airdrop | Volume, consistency, market breadth, LP provision |
| C1 | ZERO directional risk | Skew management, exposure caps |
| C2 | Capital protection | Sizing, gross exposure guards |

## Executive Summary

The bot has **four P0 blockers** that corrupt inventory tracking (Phases 0). After fixing those, there are two equally important tracks: **defensive** (risk/exposure — Phase 1) and **offensive** (maximize scoring and airdrop metrics — Phase 5). The current roadmap was purely defensive; this revision adds the revenue-generating track.

**The 10 most critical actions, in order:**

1. **Fix fill accounting** (P0-1a/P0-3/P0-1b/P0-2). Fills are inferred from cancelled orders, double-counted across markets, and API errors corrupt inventory. This is the single most damaging bug cluster.

2. **Align API throughput with market count** (P0-4). At 1.3 req/s, the bot can only quote ~6-20 markets per cycle. This fundamentally constrains all three revenue sources.

3. **Determine optimal market count** (P5-1). The strategic question: with our real API budget, how many markets maximize `total_rewards = Σ(score × pool)` + airdrop breadth? Maybe 20 markets with fresh quotes every 45s beats 60 markets with stale quotes every 3 minutes.

4. **Monitor scoring in real-time** (P5-2). The SDK has `are_orders_scoring` — the most direct metric of whether we are earning R1 revenue. Without this, we are flying blind.

5. **Replace cancel_all periodic cleanup with rolling refresh** (P5-3). The current 4-hour `cancel_all` creates ~45s of zero scoring across all markets, 6 times per day. For a bot optimizing consistency, this is an anti-pattern.

6. **Gross exposure cap** (P1-2) and **inventory reconciliation** (P1-1). Prevent the bot from silently overcommitting capital.

7. **Track airdrop-relevant metrics** (P5-4). Volume, number of markets with active orders, LP consistency — monitor what we're optimizing for.

8. **Fix rewards reporting** (P2-1). Show our estimated share, not the total pool.

9. **Graduated live validation** (P5-5). DRY_RUN cannot validate fill accounting, rate limits, or scoring. Run $100 on 3 markets for 24h before scaling.

10. **Dynamic quoting improvements** (P5-6). Per-market tightness based on volatility and competition for higher Q scores.

**Mandatory execution order for Phase 0:**
P0-1a → P0-3 → P0-1b → P0-2 → P0-4

**Recommended order after Phase 0:**
P3-1 → P3-2 → P5-1 → P5-2 → P5-5 (live validation) → then Phases 1, 2, 5 remaining tasks in parallel

---

## Phases

### Phase 0: Fill Accounting (Blocker)

**Objective:** Establish a correct, non-duplicating, error-resilient fill source of truth so that inventory, skew, exposure, and alerts reflect reality.

**Dependencies:** None. Must be completed before production deployment.

---

### Phase 1: Risk & Exposure

**Objective:** Close gaps in capital/exposure management so the bot cannot silently overcommit capital or accumulate untracked gross notional.

**Dependencies:** Phase 0 (inventory must be correct before exposure caps are meaningful).

---

### Phase 2: Rewards, PnL & Reporting

**Objective:** Make the daily summary and PnL numbers accurate and useful rather than misleading.

**Dependencies:** Phase 0 (fill data feeds PnL). Can run in parallel with Phase 1.

---

### Phase 3: API & Performance

**Objective:** Align the bot's API consumption with actual CLOB rate limits so cycles complete within the configured interval. This is a prerequisite for revenue optimisation — every wasted API call is a market not being quoted.

**Dependencies:** Phase 0 (the new trades-API-based fill source changes the per-cycle call count).

---

### Phase 4: UX & Quality

**Objective:** Fix alert spam, missing Discord notifications, log style inconsistencies, session reuse, and add basic test coverage.

**Dependencies:** Phases 0-1 (tests need correct fill/inventory semantics).

---

### Phase 5: Revenue Optimization & Airdrop Farming

**Objective:** Maximize the three revenue streams (LP rewards, holding rewards, $POLY airdrop) now that the foundation is correct and the bot can actually measure what matters.

**Revenue model context:** LP rewards use quadratic scoring `S(v,s) = ((v-s)/v)^2` — tighter spreads yield exponentially higher scores. The airdrop likely weights: total volume, number of distinct markets with activity, LP provision consistency (days active), and possibly order count. Each task in this phase directly targets one or more of these levers.

**Dependencies:** Phase 0 (fill accounting), Phase 3 (API throughput — we need sufficient call budget to act on the optimisations).

---

## Tasks

### Phase 0: Fill Accounting

#### Task P0-1a: Stop inferring fills from missing orders

| Field | Value |
|---|---|
| **Audit ref** | P0 #1 |
| **Files** | `core/order_manager.py` — `_detect_fills()` (lines 183-206), `cancel_and_replace()` (lines 91-129) |
| **Objective** | Remove the broken "order disappeared after cancel → must be a fill" heuristic. After this task, no fills are inferred from order-set differencing at all; the method becomes a no-op stub pending P0-1b. |

**Subtasks:**

1. In `_detect_fills()`, remove the `get_orders()` call and the diff logic entirely.
2. Replace the body with `pass` (or a `logger.debug` noting it is a stub pending trades-API wiring).
3. Remove the `current_ids = set()` fallback in the exception handler (it no longer exists).
4. Keep the `FillEvent` dataclass and `_recent_fills` buffer — they will be populated by P0-1b.

**Acceptance criteria:**

- `_detect_fills` no longer calls `get_orders()`.
- No `FillEvent` is created during cancel-and-replace (until P0-1b wires the trades API).
- `_recent_fills` remains empty after a full cycle (verified in DRY_RUN).
- Inventory positions stay at zero (no phantom fills).
- Existing tests (if any) pass; bot starts and enters the quoting loop.

---

#### Task P0-3: Never treat API errors as fills

| Field | Value |
|---|---|
| **Audit ref** | P0 #3 |
| **Files** | `core/order_manager.py` — `_detect_fills()` (lines 188-192) |
| **Objective** | Ensure that if any API call used for fill detection fails, the method aborts without recording any fills. |

**Subtasks:**

1. This is largely addressed by P0-1a (the `get_orders` call is removed). However, if any future fill-detection path (P0-1b) adds API calls, wrap them with `try/except` that logs a warning and returns early (no fills recorded) on error.
2. Add a guard comment in `_detect_fills` documenting this contract: "On any API error, abort without recording fills."

**Acceptance criteria:**

- No code path exists where an exception in an API call leads to `FillEvent` creation.
- A simulated API timeout during fill detection produces a warning log and zero fills.
- No regression in the cancel-and-replace flow.

**Dependencies:** Logically comes after P0-1a (which removes the problematic code). Can be merged in the same PR.

---

#### Task P0-1b: Wire fill detection to the CLOB trades API

| Field | Value |
|---|---|
| **Audit ref** | P0 #1 (resolution) |
| **Files** | `core/order_manager.py` — `_detect_fills()`, new helper `_fetch_recent_trades()`; `core/client.py` — new `get_trades_since()` wrapper |
| **Objective** | Replace the stub from P0-1a with fill detection based on the authenticated `get_trades(TradeParams)` endpoint, which returns actual executed fills with `after`/`before` timestamp filtering. |

**Verification step (must be done first):**

Confirm the SDK's `get_trades(TradeParams(after=unix_ts))` returns a paginated list of trade objects with at least: `id`, `asset_id` (or `market`), `side`, `price`, `size`, `match_time` (or equivalent timestamp). The SDK source at `.venv/.../py_clob_client/client.py:808-827` and `clob_types.py:130-136` confirms `TradeParams` has `after: int` (Unix timestamp), `market: str`, `asset_id: str`.

**Subtasks:**

1. Add to `core/client.py` an `async get_trades_since(after_ts: int) -> List[Dict]` wrapper that calls `get_trades(TradeParams(after=after_ts))` through the standard `_call` / `to_thread` pattern. Decorate with `@_with_retry(max_retries=2)`.
2. Implement a persisted watermark for trade polling:
   - In `OrderManager.__init__`, add `self._trade_watermark_ts: int` initialised by loading a saved value from disk (e.g. `data/trade_watermark.json`). If no saved value exists, fall back to `int(time.time())` on first run only.
   - On each poll, query with a small overlap: `after = self._trade_watermark_ts - 2` (2-second overlap window) to guard against API latency, clock skew, and pagination timing.
   - Rely on trade-ID deduplication (see step 3) to discard the duplicates introduced by the overlap.
   - After processing, advance the watermark to the **maximum trade timestamp actually observed** in the response, not to wall-clock `now`. If the response is empty, do not advance.
   - Persist the updated watermark to disk so it survives restarts.
3. Rewrite `_detect_fills`:
   - Call `client.get_trades_since(self._trade_watermark_ts - 2)`.
   - On API error: log warning, return early (no fills, watermark unchanged).
   - On success: for each returned trade, build a `FillEvent` with the trade's actual `price`, `size`, `side`, `asset_id` → `token_id`, and `market` → `condition_id`.
   - Deduplicate by trade `id` (keep a set of recently-seen trade IDs, bounded to last 1000). Skip any trade whose ID is already in the set.
   - Append new (non-duplicate) fills to `_recent_fills`.
   - Advance watermark to `max(trade_timestamp for trade in response)` (only if response was non-empty).
   - Persist watermark.
4. **Decouple fill collection from `cancel_and_replace`:** Extract fill polling into a standalone cycle-level step (called once in `main.py`'s `run_quoting_cycle` before the per-market loop, or as a dedicated `FillCollector` / `FillTracker` component). `cancel_and_replace()` must no longer call `_detect_fills` — it should only cancel and post orders. This keeps fill accounting centralised and avoids re-coupling it with per-market execution. Use a single global `get_trades_since` call per cycle and group results by `market`/`asset_id` in memory.
5. Ensure `filled_size` uses the trade's actual size, not the original order size (fixes P2-3 partial fills).

**Acceptance criteria:**

- Fill collection calls `get_trades` (or `get_trades_since`), not `get_orders`.
- Fill collection runs once per cycle (cycle-level), not inside `cancel_and_replace`.
- Watermark advances to the max observed trade timestamp, not wall-clock `now`.
- Watermark is persisted to disk and survives restarts without missing fills.
- Overlap window + trade-ID dedup ensures no fills are lost or double-counted across polls.
- On API error, zero fills are recorded and the watermark is not advanced.
- `FillEvent.filled_size` matches the trade's `size` field (not the original order size).
- In DRY_RUN with a funded account that has historical trades, the bot correctly picks up past fills without double-counting across cycles.

**Dependencies:** P0-1a, P0-3.

---

#### Task P0-2: Eliminate double-counting of fills in the main loop

| Field | Value |
|---|---|
| **Audit ref** | P0 #2 |
| **Files** | `main.py` — `run_quoting_cycle()` (lines 124-132) |
| **Objective** | Ensure each fill is applied to inventory exactly once per cycle. |

**Subtasks:**

1. Move the fill-fetch-and-record block **outside** the per-market `for` loop.
2. Collect new fills **once** at the start (or end) of the cycle:
   - `new_fills = order_mgr.drain_new_fills()` — a new method that returns fills accumulated since the last drain and clears the internal buffer.
3. Call `inventory.record_fills(new_fills)` once.
4. Send Discord fill alerts from `new_fills` (filter for large fills).
5. Remove the per-market `fills = order_mgr.recent_fills` / `inventory.record_fills(fills)` block inside the loop.
6. Add `drain_new_fills()` to `OrderManager`: returns `self._recent_fills`, replaces it with an empty list, and returns the snapshot. This gives exactly-once delivery semantics.

**Acceptance criteria:**

- `inventory.record_fills()` is called exactly once per cycle, not once per market.
- `drain_new_fills()` returns each fill exactly once; subsequent calls return empty until new fills arrive.
- With 60 markets and 5 fills per cycle, inventory increments by 5 (not 300).
- Discord fill alerts fire at most once per fill.

**Dependencies:** P0-1a, P0-1b (fills must be correct before dedup matters; but the structural fix can be merged alongside P0-1b).

---

#### Task P0-4: Align API throughput with market count

| Field | Value |
|---|---|
| **Audit ref** | P0 #4 |
| **Files** | `core/client.py` — `TokenBucketRateLimiter` (lines 38-65); `main.py` — `run_quoting_cycle()`; `core/order_manager.py` — `cancel_and_replace()` |
| **Objective** | Ensure 60 markets can complete a full cancel-replace cycle within the 45s interval without exceeding the CLOB rate limit (~100 req/min documented, ~1.67 req/s). |

**Subtasks:**

1. **Audit per-market API calls** after P0-1b changes:
   - `get_midpoint` (1 call) — in `main.py`
   - `cancel_market_orders` (1 call) — in `order_manager`
   - `get_trades_since` or scoped `get_trades` (1 call, if per-market) — in `order_manager`
   - `create_and_post_order` × up to 4 (bid/ask YES + bid/ask NO) — each is `create_order` + `post_order` = 2 calls → 8 calls
   - **Total per market: ~11 calls** (or ~10 if trades are fetched globally once)
   - **60 markets × 11 = 660 calls per cycle** — way over 100/min.

2. **Reduce calls per market:**
   - **Batch midpoints:** Replace per-market `get_midpoint` with `get_midpoints` (batch endpoint) — one call for all token IDs. The SDK has `get_midpoints(params: List[BookParams])`.
   - **Batch order posting:** Consider if the SDK supports batch order creation. If not, this is the irreducible bottleneck.
   - **Global trades fetch:** Fetch `get_trades_since(ts)` once per cycle (not per market) and group by `market`/`asset_id` in memory. Saves 59 calls.
   - **Combine create + post:** Already done (`create_and_post_order` does 2 sequential calls). No further reduction possible without SDK changes.

3. **Make the rate limiter configurable.** Add `API_RATE_LIMIT_RPS` to `config/settings.py` (default: a conservative `1.3` req/s). Do **not** hardcode a higher value. Before raising the default, add a verification step: during a test run, log 429 counts and cycle durations at different rates. The documented ~100 req/min (~1.67 req/s) is a global figure; individual endpoints may have stricter limits. The operator can increase `API_RATE_LIMIT_RPS` via `.env` once verified. Also make bucket `capacity` configurable (default: `10`).

4. **Reduce market count to fit budget.** After optimisations, per-market calls ≈ 1 (cancel) + 4-8 (posts) = 5-9. Global overhead: ~2 (midpoints batch + trades batch). Budget at `API_RATE_LIMIT_RPS × REBALANCE_INTERVAL_SECONDS` calls (e.g., 1.3 × 45 ≈ 58 at the conservative default). That supports **6-11 markets per cycle if all 4 orders per market require 2 calls each**, or **~20 markets if create_and_post_order is 1 call** (it's 2). Options:
   - Stagger markets across cycles (e.g., refresh 30 markets per cycle, alternating halves).
   - Increase `REBALANCE_INTERVAL_SECONDS` to 90s (doubles budget to ~150 calls).
   - Accept 30 markets max at 45s cadence.

5. **Add a setting** `MAX_MARKETS_PER_CYCLE` and implement round-robin or priority-based batching in `run_quoting_cycle`.

6. **Log cycle timing** prominently (already done at DEBUG; elevate to INFO) with call count per cycle for monitoring.

**Acceptance criteria:**

- Cycle completes within `REBALANCE_INTERVAL_SECONDS` for the configured market count (verified via log timing).
- No 429 errors from the CLOB API during normal operation.
- Rate limiter `rate` and `capacity` are configurable via `API_RATE_LIMIT_RPS` / `API_RATE_LIMIT_BURST` in `.env`; defaults are conservative.
- A verification step (test run with 429 logging) is documented before any rate increase.
- `MAX_MARKETS_PER_CYCLE` is configurable and defaults to a safe value.
- Batch midpoint fetching is used (single call replaces N calls).

**Dependencies:** P0-1b (changes per-market call count).

---

### Phase 1: Risk & Exposure

#### Task P1-1: Add periodic inventory reconciliation

| Field | Value |
|---|---|
| **Audit ref** | P1 #3 |
| **Files** | `core/inventory.py` — new method `reconcile()`; `main.py` — new background task |
| **Objective** | Periodically compare incremental inventory against the exchange's view of the user's positions to detect and correct drift. |

**Subtasks:**

1. Investigate if the CLOB SDK or a Polygon RPC call can return the user's conditional token balances per `asset_id`. If the SDK has no direct method, use the Gamma API or a direct contract call to the CTF token contract.
2. Add `InventoryTracker.reconcile(exchange_positions: Dict[str, float])` that compares `yes_size`/`no_size` against the exchange view and logs/corrects discrepancies beyond a tolerance (e.g., 1%).
3. Add a background task in `main.py` that runs reconciliation every 30 minutes.
4. On large discrepancy: send Discord alert, overwrite local position with exchange truth.

**Acceptance criteria:**

- Reconciliation runs on a schedule without blocking the quoting loop.
- Discrepancies > 1% are logged at WARNING and corrected.
- Discrepancies > 10% trigger a Discord alert.
- If the reconciliation API call fails, the task logs a warning and retries next interval.

**Dependencies:** Phase 0 (fills must be correct; reconciliation catches residual drift).

---

#### Task P1-2: Add gross exposure cap

| Field | Value |
|---|---|
| **Audit ref** | P1 #5 |
| **Files** | `core/inventory.py` — new `gross_exposure_usd()` method, update `is_global_exposure_breached()`; `config/settings.py` — new `GLOBAL_MAX_GROSS_EXPOSURE_PCT` |
| **Objective** | Cap total gross risk notional (filled inventory + open order exposure, marked to current price) across all markets to prevent overcommitment. This is a risk guard, not just an accounting metric — average cost alone can underestimate current exposure. |

**Subtasks:**

1. Add `MarketPosition.gross_exposure_usd` property that values positions at **mark-to-mid (current midpoint)** rather than average cost. Use the latest midpoint from the quoting cycle (already fetched). Formula: `yes_size * yes_mid + no_size * no_mid`. Average cost may be used as a fallback when midpoint is unavailable, but the primary valuation should reflect current market prices.
2. Add open-order notional to the gross figure: sum `price * size` for all orders tracked in `OrderManager._live_orders`. Pass order notional into the exposure check, or have `InventoryTracker` accept it as an argument.
3. Add `InventoryTracker.gross_exposure_usd(open_order_notional: float = 0.0)` summing filled-position mark-to-mid + open order notional.
4. Add `GLOBAL_MAX_GROSS_EXPOSURE_PCT` to `Settings` (default 0.80 = 80% of capital).
5. In `is_global_exposure_breached()` or a new `is_gross_exposure_breached()`, check the gross cap.
6. Wire into `run_quoting_cycle` alongside the existing net exposure check.
7. *(Future enhancement, not required now):* Add a worst-case payout approximation that accounts for the YES/NO binary structure — e.g., max loss on a position is `max(yes_size, no_size) * 1.0` minus hedge value. Document this as a potential P1-2b follow-up.

**Acceptance criteria:**

- Gross exposure is computed using mark-to-mid pricing (not average cost) and logged each cycle.
- Open order notional is included in the gross figure.
- If gross > limit, the most-exposed markets are paused.
- Setting is configurable via `.env`.
- Average-cost fallback is documented as secondary to mid-price valuation.

**Dependencies:** Phase 0 (inventory must be correct).

---

#### Task P1-3: Audit capital overcommitment in quoting

| Field | Value |
|---|---|
| **Audit ref** | P1 #4 |
| **Files** | `core/quoter.py` — `_build_quote()` (lines 196-200) |
| **Objective** | Verify that 4 orders per market (bid/ask on YES + bid/ask on NO) don't commit more than `allocated_capital`. Currently each token book uses `allocated_capital / 2` per side, but bid and ask on the same book both consume capital. |

**Subtasks:**

1. Analyse: for one market with `allocated_capital = $400`, `_build_quote` sets `half_capital = $200` for each of the 2 token books (YES and NO). Each book places a bid and an ask, each sized up to `half_capital / price`. In the worst case (both filled), the bid costs `bid_price * bid_size` and the ask requires holding `ask_size` shares. For a YES/NO pair the bid on YES and bid on NO are economically offsetting (buying $0.40 YES and $0.60 NO = $1.00 combined), so this is partially self-hedging. Document the exact worst-case capital consumption.
2. If worst case exceeds `allocated_capital`, adjust the sizing formula. Options:
   - Split `allocated_capital / 4` per order (4 orders per market).
   - Account for the YES/NO hedge: `allocated_capital / 2` is safe if bids on both sides are simultaneously filled (they hedge each other).
3. Add a comment in `_build_quote` documenting the capital analysis.

**Acceptance criteria:**

- A written analysis (comment or doc) of worst-case capital per market.
- If overcommitment is possible, sizing is adjusted so that `sum(order_notional) <= allocated_capital` per market under any fill scenario.
- No change to the quoting logic if analysis shows the current sizing is safe.

**Dependencies:** None (analysis task). Should inform Phase 3 tuning.

---

### Phase 2: Rewards, PnL & Reporting

#### Task P2-1: Fix "estimated_rewards_usd" to reflect user's share

| Field | Value |
|---|---|
| **Audit ref** | P1 #1 |
| **Files** | `core/rewards_tracker.py` — `_fetch_reward_allocations()` (lines 163-196), `format_summary()` |
| **Objective** | Currently `estimated_rewards_usd` sums public per-market reward pools, not the user's share. Either compute an estimate of the user's share or clearly label the number as "total market pool" to avoid confusion. |

**Subtasks:**

1. **Option A (preferred):** Estimate user's share. For each market, if we know our `Q_min` score (from `estimate_q_score` in `utils/helpers.py`) and the market's total reward pool, our share ≈ `(our_Q / estimated_total_Q) * pool`. `estimated_total_Q` is unknown, so use a configurable assumption (e.g., "we are 1 of ~10 LPs" → our share ≈ pool / 10). Label as "Est. rewards (assuming ~N LPs)".
2. **Option B (simpler):** Rename the field and Discord label to "Total reward pool (all LPs)" and add a note that individual share depends on relative Q score.
3. Investigate if `are_orders_scoring` (SDK method) can help quantify our scoring contribution.
4. Update `DailySnapshot` field name if needed and `format_summary`.

**Acceptance criteria:**

- The Discord daily summary does not present the total reward pool as the user's personal earnings.
- Either an estimated share is shown (with caveats) or the label is unambiguous.
- `format_summary` output is reviewed and makes sense to a non-developer.

---

#### Task P2-2: Fix realised PnL to use proper cost-basis matching

| Field | Value |
|---|---|
| **Audit ref** | P1 #2 |
| **Files** | `core/rewards_tracker.py` — `_compute_fill_pnl()` (lines 119-157) |
| **Objective** | Replace the raw cashflow heuristic (`BUY → -price*size`, `SELL → +price*size`) with proper FIFO or average-cost PnL that tracks cost basis per token. |

**Subtasks:**

1. Leverage the fill data from Phase 0 (now correct via trades API). Maintain per-token FIFO queues or average cost basis in `InventoryTracker`.
2. Realised PnL for a round trip = sell_price * size - cost_basis * size.
3. For an LP, most fills are small and two-sided; a simpler "average cost" approach is sufficient.
4. Update `_compute_fill_pnl` to query `InventoryTracker.realised_pnl()` rather than re-fetching trades.
5. Rename to "Realised PnL" (not "fill P&L") in the summary.

**Acceptance criteria:**

- PnL is computed from cost-basis-matched round trips, not raw cashflow.
- A buy at 0.40 followed by a sell at 0.60 of the same token shows +$0.20/share profit.
- Unrealised PnL (open inventory marked to mid) is shown separately.

**Dependencies:** Phase 0 (correct fill data).

---

#### Task P2-3: Fix partial fill detection

| Field | Value |
|---|---|
| **Audit ref** | P2 #3 |
| **Files** | `core/order_manager.py` — `FillEvent.filled_size` |
| **Objective** | Currently `filled_size` is set to the original order `rec.size` (full size). With the trades API (P0-1b), the trade's actual `size` field is used, inherently supporting partial fills. |

**Subtasks:**

1. Verify that after P0-1b, `FillEvent.filled_size` is sourced from the trade response's `size` field.
2. If the trade response reports partial fills as separate trade records (likely), no further work is needed.
3. If a single trade record can have `size < order_size`, ensure we handle it.

**Acceptance criteria:**

- A partial fill of 50 shares on a 100-share order creates a `FillEvent` with `filled_size=50`, not 100.
- Inventory is incremented by the actual filled amount.

**Dependencies:** P0-1b.

---

### Phase 3: API & Performance

#### Task P3-1: Batch midpoint fetching

| Field | Value |
|---|---|
| **Audit ref** | P0 #4 (sub-item) |
| **Files** | `main.py` — `run_quoting_cycle()`; `core/client.py` — `get_midpoints()` |
| **Objective** | Replace N individual `get_midpoint(token_id)` calls with a single `get_midpoints(List[BookParams])` batch call. |

**Subtasks:**

1. Add `async get_midpoints(params: List[BookParams]) -> Dict` to `AsyncClobClient` wrapping `client.get_midpoints`.
2. At the top of `run_quoting_cycle`, collect all YES token IDs, call `get_midpoints` once, store results in a `Dict[token_id, float]`.
3. Replace the per-market `client.get_midpoint` call with a dict lookup.

**Acceptance criteria:**

- Only one `get_midpoints` call per cycle (not 60).
- Midpoint fallback to `market.midpoint` still works if a token ID is missing from the response.
- Cycle time reduced measurably (logged).

---

#### Task P3-2: Implement per-cycle market batching / round-robin

| Field | Value |
|---|---|
| **Audit ref** | P0 #4 (capacity) |
| **Files** | `main.py` — `run_quoting_cycle()`; `config/settings.py` — new `MAX_MARKETS_PER_CYCLE` |
| **Objective** | If the total API budget cannot serve all markets in one cycle, rotate through subsets. |

**Subtasks:**

1. Add `MAX_MARKETS_PER_CYCLE` to Settings (default: 20).
2. In `run_quoting_cycle`, maintain a rotating index. Each cycle processes the next `MAX_MARKETS_PER_CYCLE` markets, wrapping around.
3. Higher-priority markets (top-15 by reward score) can be serviced every cycle; lower-priority markets every 2nd or 3rd cycle.
4. Log which market slice is being processed.

**Acceptance criteria:**

- All markets are serviced within `ceil(total / batch_size)` cycles.
- No market goes unserviced for more than 3 consecutive cycles.
- API call count per cycle stays within the rate budget.

---

### Phase 4: UX & Quality

#### Task P4-1: Add cooldown to skew alerts

| Field | Value |
|---|---|
| **Audit ref** | P2 #1 |
| **Files** | `main.py` — `run_quoting_cycle()` (lines 97-102); `utils/discord.py` or `core/inventory.py` |
| **Objective** | Prevent Discord spam when skew persists above threshold across multiple cycles. |

**Subtasks:**

1. Add a `_last_alert_time: Dict[str, datetime]` to `InventoryTracker` (or `DiscordNotifier`).
2. In `evaluate()`, set `should_alert = True` only if the last alert for this market was > 15 minutes ago (configurable via `SKEW_ALERT_COOLDOWN_SECONDS`).
3. Add the setting to `config/settings.py`.

**Acceptance criteria:**

- A market at 30% skew generates at most one alert per 15 minutes.
- First alert is immediate; subsequent alerts are suppressed until cooldown expires.
- Cooldown resets when skew drops below alert threshold.

---

#### Task P4-2: Wire `send_skew_pause` to the skew pause trigger

| Field | Value |
|---|---|
| **Audit ref** | P2 #2 |
| **Files** | `main.py` — `run_quoting_cycle()` (around line 97) |
| **Objective** | `discord.send_skew_pause()` exists but is never called. Wire it to fire when `SkewAction.paused_side` is set for the first time (i.e., when `InventoryTracker._set_pause` is called). |

**Subtasks:**

1. Return a `pause_triggered: bool` flag from `SkewAction` (set only on the transition to paused, not while already paused).
2. In `run_quoting_cycle`, if `skew_action.pause_triggered`, call `discord.send_skew_pause(...)`.

**Acceptance criteria:**

- `send_skew_pause` is called exactly once when a market enters pause.
- It is not called on every cycle while the market remains paused.
- The Discord message includes condition_id, skew ratio, heavy side, and pause duration.

---

#### Task P4-3: Fix `%s` log placeholders

| Field | Value |
|---|---|
| **Audit ref** | P2 #4 |
| **Files** | `core/market_manager.py`, `core/client.py`, `core/quoter.py` |
| **Objective** | Replace `%s` (printf-style) with `{}` (loguru-style) for consistency and lazy evaluation. |

**Subtasks:**

1. Search all `.py` files for `logger.*(.*%s.*)` patterns.
2. Replace with `{}` and positional args.

**Acceptance criteria:**

- Zero `%s` placeholders in any `logger.*` call.
- All log messages render correctly.

---

#### Task P4-4: Reuse aiohttp session in Discord notifier

| Field | Value |
|---|---|
| **Audit ref** | P2 #5 |
| **Files** | `utils/discord.py` — `_post()` (lines 151-180) |
| **Objective** | Currently creates a new `aiohttp.ClientSession` per message. Reuse a single session for the lifetime of the notifier. |

**Subtasks:**

1. Create `self._session: Optional[aiohttp.ClientSession] = None` in `__init__`.
2. Lazily initialise in `_post` (or add an `async def start()` / `async def close()` lifecycle).
3. Close the session in the shutdown sequence (`main.py`).

**Acceptance criteria:**

- Only one `ClientSession` is created for the notifier's lifetime.
- Session is properly closed on shutdown.
- No change in Discord message delivery behavior.

---

#### Task P4-5: Add unit tests for core accounting

| Field | Value |
|---|---|
| **Audit ref** | P3 #3 |
| **Files** | New `tests/` directory; `tests/test_inventory.py`, `tests/test_order_manager.py`, `tests/test_quoter.py`, `tests/test_markets_filter.py` |
| **Objective** | Cover the most critical paths: fill recording, skew calculation, quote sizing, and market filtering. |

**Subtasks:**

1. `test_inventory.py`: record_fills applies correctly; skew_ratio computed; double-recording prevented; reconcile corrects drift.
2. `test_order_manager.py`: drain_new_fills returns each fill once; FillEvent fields match trade data.
3. `test_quoter.py`: bid < mid < ask; prices clamped; sizes respect capital; skew adjustments directionally correct.
4. `test_markets_filter.py`: hard filters reject bad candidates; ranking is deterministic; both Gamma shapes parse.

**Acceptance criteria:**

- At least 15 test cases covering the above.
- All tests pass in CI / `pytest`.
- No external API calls in tests (mock `AsyncClobClient`).

---

### Phase 5: Revenue Optimization & Airdrop Farming

#### Task P5-1: Determine optimal market count for revenue maximisation

| Field | Value |
|---|---|
| **Priority** | High (strategic blocker) |
| **Files** | Analysis doc (new); `config/settings.py` — update defaults; `main.py` — log scoring % |
| **Objective** | Answer the key strategic question: given our real API budget, what market count maximizes `total_rewards = Σ(our_Q_score × pool_reward) + airdrop_breadth_bonus`? |

**Context:**

At conservative 1.3 req/s × 45s = ~58 calls/cycle. Each market costs ~9-11 API calls (cancel + 4 order posts × 2 calls each + midpoint if not batched). After P3-1 (batch midpoints) and global trades fetch, per-market cost drops to ~5-9. This means:
- 58 / 9 ≈ **6 markets at 45s** (no batching)
- 58 / 5 ≈ **11 markets at 45s** (with batching)
- At 90s interval: double these numbers
- Round-robin across 2-3 cycles: 20-33 markets refreshed every 90-135s

But: stale quotes score 0 (they get filled or crossed). **20 markets with always-fresh quotes may earn more than 60 markets with quotes refreshed every 3 minutes.** Airdrop breadth pushes toward more markets, but scoring consistency pushes toward fewer.

**Subtasks:**

1. Build a simple model: `expected_daily_revenue(n_markets, cycle_time, avg_reward_pool, avg_q_score, staleness_penalty)`. Staleness penalty: if quotes are refreshed every `T` seconds, estimate what fraction of time they remain competitive vs other LPs who refresh at 30s.
2. Estimate airdrop value per additional market (unknown, but can parameterize). Output: a curve of `revenue(n)` showing the tradeoff.
3. Based on the model, set `TARGET_MARKETS` and `REBALANCE_INTERVAL_SECONDS` defaults. Document the reasoning.
4. Optionally implement a tiered approach: top-N markets refreshed every cycle, remaining markets refreshed every Kth cycle. Already partially captured by P3-2 but the target numbers should come from this analysis.

**Acceptance criteria:**

- Written analysis (README section or doc) with recommended market count and interval.
- `TARGET_MARKETS` / `MAX_MARKETS` defaults updated based on analysis.
- The reasoning is documented so it can be revisited as API limits or market conditions change.

---

#### Task P5-2: Monitor scoring via `are_orders_scoring`

| Field | Value |
|---|---|
| **Priority** | High |
| **Files** | `core/client.py` — new wrapper; `core/order_manager.py` or new `core/scoring_monitor.py`; `main.py` — background task; `utils/discord.py` — new alert |
| **Objective** | Use the SDK's `are_orders_scoring` endpoint to know in real time whether our orders are actually earning LP rewards. This is the single most important operational metric. |

**Context:**

The CLOB has an `are_orders_scoring` endpoint (check SDK for exact method signature). It returns whether the user's current orders on a given market meet the reward criteria (spread within `rewardsMaxSpread`, size >= `rewardsMinSize`, two-sided). If we are not scoring, we are earning zero R1 revenue on that market — immediate action is needed.

**Subtasks:**

1. Add `async are_orders_scoring(condition_id: str, token_id: str) -> bool` wrapper in `core/client.py`. Decorate with `@_with_retry(max_retries=1)`.
2. Add a periodic scoring check (every 5 minutes or every Nth cycle):
   - For each active market, call `are_orders_scoring`.
   - Log a summary: `"Scoring: 45/50 markets (90%)"`.
   - Track a `scoring_ratio` metric over time.
3. If scoring ratio drops below a configurable threshold (e.g., `SCORING_ALERT_THRESHOLD = 0.70`), fire a Discord alert.
4. If a specific market is not scoring for 3+ consecutive checks, log a warning with the reason if inferable (spread too wide? size too small? one-sided?).
5. Add `SCORING_CHECK_INTERVAL_SECONDS` (default: 300) to settings.

**Acceptance criteria:**

- `are_orders_scoring` is called periodically and the ratio is logged at INFO.
- Discord alert fires if scoring ratio drops below threshold.
- Per-market non-scoring is logged with enough context to debug.
- API calls for scoring checks are accounted for in the rate budget (P0-4).
- Does not block the quoting loop.

---

#### Task P5-3: Replace periodic `cancel_all` with rolling refresh

| Field | Value |
|---|---|
| **Priority** | High |
| **Files** | `main.py` — `periodic_cleanup_task()`; `core/order_manager.py` — `cancel_and_replace()` |
| **Objective** | Eliminate the 4-hourly `cancel_all()` that creates scoring gaps across all markets. |

**Context:**

The current `periodic_cleanup_task` calls `cancel_all()` every 4 hours. This cancels every order on every market simultaneously, creating a window of ~30-90s where no orders exist (time to rebuild all quotes). During this window, scoring = 0 on all markets, fill opportunities are lost, and airdrop consistency metrics take a hit. Over 24h this is 6 windows of zero revenue.

**Subtasks:**

1. Remove the blanket `cancel_all()` periodic task from `main.py`.
2. Instead, track per-market order age. In `cancel_and_replace()`, the cancel-then-post cycle already replaces orders every `REBALANCE_INTERVAL_SECONDS`. This is sufficient for staleness cleanup.
3. Add a safety net: if an order has been live for longer than `MAX_ORDER_AGE_SECONDS` (default: 600s = 10 minutes), flag it for forced refresh next cycle. This catches orders that survive (e.g., if the bot skipped that market due to round-robin).
4. Keep `cancel_all()` as a shutdown-only operation (graceful shutdown already uses it) and as a manual override via Discord command or admin flag.
5. For stuck-order protection: if `are_orders_scoring` (P5-2) shows a market not scoring, trigger an immediate cancel+repost for that market.

**Acceptance criteria:**

- No periodic `cancel_all()` in normal operation.
- Orders older than `MAX_ORDER_AGE_SECONDS` are refreshed within the next cycle.
- Scoring gaps from blanket cancellation are eliminated.
- Shutdown still cancels all orders.
- No orphaned orders accumulate indefinitely.

---

#### Task P5-4: Track airdrop-relevant metrics

| Field | Value |
|---|---|
| **Priority** | Medium |
| **Files** | `core/rewards_tracker.py` — extend `DailySnapshot`; `utils/discord.py` — daily summary update; new `data/airdrop_metrics.json` |
| **Objective** | Monitor and log the metrics that the $POLY airdrop likely rewards, so we can optimize for them. |

**Context:**

Based on public airdrop criteria from other protocols and Polymarket's stated preferences, the airdrop likely weights:
- **Total trading volume** (USDC)
- **Number of distinct markets** with activity (breadth)
- **Consistency** (number of days with LP activity)
- **LP provision** (time-weighted liquidity)
- **Order count** (possibly)

We cannot know the exact formula, but tracking these metrics lets us identify weaknesses.

**Subtasks:**

1. In `DailySnapshot`, add fields:
   - `total_volume_usd`: sum of `fill_price * fill_size` for all fills today
   - `distinct_markets_active`: count of condition_ids where we had at least one order posted
   - `distinct_markets_scored`: count where `are_orders_scoring` returned true at least once
   - `scoring_uptime_pct`: average fraction of checks where orders were scoring
   - `total_orders_posted`: count of orders posted today
   - `consecutive_active_days`: running counter
2. Persist `consecutive_active_days` across restarts (in `data/airdrop_metrics.json`).
3. In the daily Discord summary, add a "Airdrop Metrics" section showing these numbers.
4. If `distinct_markets_active` drops below `TARGET_MARKETS * 0.5`, fire an alert.

**Acceptance criteria:**

- Daily summary includes airdrop-relevant metrics.
- `consecutive_active_days` counter is accurate across restarts.
- Alert fires if market coverage drops significantly.
- Metrics are persisted for historical analysis.

---

#### Task P5-5: Graduated live validation before full deployment

| Field | Value |
|---|---|
| **Priority** | High (gate before scaling) |
| **Files** | `README.md` — deployment guide update; `config/settings.py` — validation mode |
| **Objective** | DRY_RUN cannot validate fill accounting, rate limits, scoring, or real P&L. Define and execute a graduated go-live plan. |

**Subtasks:**

1. **Stage 1 — Minimal live test ($100, 3 markets, 24h):**
   - Set `TOTAL_CAPITAL = 100`, `TARGET_MARKETS = 3`, `MODE = safe`.
   - Monitor: fills recorded correctly (cross-check with Polymarket UI), no phantom fills, inventory matches UI positions, scoring ratio near 100% on those 3 markets.
   - Acceptance: zero accounting discrepancies, scoring confirmed via `are_orders_scoring`.
2. **Stage 2 — Scale test ($500, 10 markets, 48h):**
   - Increase capital and market count.
   - Monitor: cycle timing, 429 errors, skew management under real fills, daily reward estimate vs actual.
   - Acceptance: cycles complete on time, no 429 bursts, inventory reconciliation passes.
3. **Stage 3 — Full deployment ($10k, 20-60 markets based on P5-1 analysis):**
   - Scale to target capital and market count.
   - Monitor continuously for 1 week before considering it "production".

**Acceptance criteria:**

- Each stage has explicit pass/fail criteria documented in README.
- Stage gate: do not proceed to Stage N+1 until Stage N passes all criteria for its full duration.
- A deployment checklist in README covers each stage.

---

#### Task P5-6: Dynamic per-market quoting tightness

| Field | Value |
|---|---|
| **Priority** | Medium |
| **Files** | `core/quoter.py` — `compute()`; `core/market_manager.py` — per-market metadata |
| **Objective** | Instead of a global `tightness_factor`, adapt tightness per market based on conditions for higher Q scores where it's safe and wider spreads where it's risky. |

**Context:**

The quadratic scoring formula `S = ((v-s)/v)^2` means going from 48% to 35% tightness roughly doubles the score (from ~0.27 to ~0.42). But tighter quotes on volatile markets increase adverse selection risk. The optimal strategy is: be tight on stable markets, wider on volatile ones.

**Subtasks:**

1. For each market, compute `effective_tightness = base_tightness * volatility_multiplier`:
   - If `estimated_volatility < 0.05`: multiply by `0.75` (tighter, more aggressive)
   - If `estimated_volatility > 0.15`: multiply by `1.3` (wider, more conservative)
   - Clamp to `[0.25, 0.70]`
2. Optionally factor in competition: if the market has high existing LP activity (inferable from order book depth at tight spreads), slightly widen to avoid being constantly crossed.
3. Store `effective_tightness` per `MarketCandidate` and use it in `Quoter.compute()` instead of the global setting.
4. Log per-market tightness at DEBUG for tuning.

**Acceptance criteria:**

- Tightness varies per market based on volatility.
- Stable long-dated markets get tighter quotes (higher Q scores).
- Volatile markets get wider quotes (more protection).
- Global safe/aggressive mode still sets the baseline; per-market adjustment is a multiplier on top.

---

## Prioritization and Execution Order

| Order | Task ID | Title | Revenue impact | Blocker? | Dependencies | Status |
|-------|---------|-------|---------------|----------|-------------|--------|
| 1 | P0-1a | Stop inferring fills from missing orders | — (correctness) | Yes | — | **DONE** |
| 2 | P0-3 | Never treat API errors as fills | — (correctness) | Yes | P0-1a (same PR) | **DONE** |
| 3 | P0-1b | Wire fill detection to CLOB trades API | — (correctness) | Yes | P0-1a, P0-3 | **DONE** |
| 4 | P0-2 | Eliminate double-counting in main loop | — (correctness) | Yes | P0-1b | **DONE** |
| 5 | P0-4 | Align API throughput with market count | R1+R3 (enables scaling) | Yes | P0-1b | **DONE** |
| 6 | P3-1 | Batch midpoint fetching | R1+R3 (frees API budget) | — | P0-4 | **DONE** |
| 7 | P3-2 | Per-cycle market batching / round-robin | R1+R3 (covers more markets) | — | P0-4 | **DONE** |
| 8 | P5-1 | Optimal market count analysis | R1+R3 (strategic) | — | P0-4 | **DONE** |
| 9 | P5-2 | Scoring monitoring (`are_orders_scoring`) | R1 (know if earning) | — | Phase 0 | **DONE** |
| 10 | P5-3 | Replace `cancel_all` with rolling refresh | R1+R3 (eliminate scoring gaps) | — | — | **DONE** |
| 11 | P5-5 | Graduated live validation ($100 to $10k) | All (gate to revenue) | Gate | Phases 0, 3 | |
| 12 | P1-1 | Periodic inventory reconciliation | C1+C2 (safety) | — | Phase 0 | **DONE** |
| 13 | P1-2 | Gross exposure cap | C2 (safety) | — | Phase 0 | **DONE** |
| 14 | P1-3 | Audit capital overcommitment in quoting | C2 (safety) | — | — | **DONE** |
| 15 | P2-1 | Fix estimated_rewards_usd labeling | — (visibility) | — | — | **DONE** |
| 16 | P5-4 | Airdrop metrics tracking | R3 (visibility) | — | P5-2 | **DONE** |
| 17 | P5-6 | Dynamic per-market quoting tightness | R1 (higher Q scores) | — | P5-2 | **DONE** |
| 18 | P2-3 | Partial fill detection | — (correctness) | — | P0-1b | **DONE** |
| 19 | P4-1 | Skew alert cooldown | — (UX) | — | — | **DONE** |
| 20 | P4-2 | Wire send_skew_pause | — (UX) | — | — | **DONE** |
| 21 | P4-3 | Fix %s log placeholders | — (UX) | — | — | **DONE** |
| 22 | P4-4 | Reuse aiohttp session in Discord | — (perf) | — | — | **DONE** |
| 23 | P4-5 | Unit tests for core accounting | — (quality) | — | Phases 0-1 | **DONE** |
| 24 | P2-2 | Realised PnL cost-basis matching (low ROI) | — (visibility) | — | Phase 0 | **DONE** |

**Execution strategy:**

1. **Phase 0 (tasks 1-5):** Must be completed first. No exceptions.
2. **Throughput + strategy (tasks 6-8):** Immediately after Phase 0. These determine how many markets we can profitably quote. Without them, everything downstream is guessing.
3. **Scoring + quick wins (tasks 9-10):** P5-2 tells us if we are earning; P5-3 eliminates free losses. Both can be done in parallel with task 8.
4. **Live validation (task 11):** Gate before scaling capital. Run $100 on 3 markets, verify everything works, then scale.
5. **Safety (tasks 12-14):** Phase 1 runs in parallel with or right after live validation.
6. **Revenue optimization (tasks 15-17):** Fine-tuning once the bot is live and we have real data.
7. **P2-2 (task 24):** Demoted. FIFO cost-basis PnL is nice for reporting but does not affect revenue or risk. The raw cashflow heuristic is directionally correct for an LP that is net-flat. Implement only if time permits.
8. **Phase 4 UX (tasks 19-23):** Independent, do as needed.

**Note on P2-2 demotion:** For an LP bot that is net-flat by design (zero directional exposure), FIFO vs raw cashflow PnL differences are small. The effort is better spent on tasks that directly increase scoring or airdrop metrics.

---

## Risks and Uncertainties

### Assumptions

1. **`get_trades(TradeParams(after=ts))` returns the user's own fills**, not all market trades. The SDK docstring says "Fetches the trade history for a user" and requires L2 auth — this strongly suggests user-scoped trades.
2. **Trade records include enough fields** (`id`, `asset_id`/`market`, `side`, `price`, `size`, timestamp) to build `FillEvent` objects. The `TradeParams` dataclass and SDK code confirm `market`, `asset_id`, `before`, `after` filtering.
3. **`get_midpoints` batch endpoint works** for arbitrary lists of token IDs. The SDK method exists (confirmed in public methods docs).
4. **CLOB rate limit is ~100 req/min** as documented. Actual per-endpoint limits may be stricter; the bot should handle 429s gracefully (already does via retry) and the rate limiter default must remain conservative until verified empirically.
5. **No on-chain reconciliation API in the SDK.** Inventory reconciliation may require direct contract calls or the Gamma data API.

### Risks

1. **Trade response shape differs from expectations.** Mitigation: P0-1b includes a verification step to inspect the actual response before writing parsing code.
2. **Rate limit is stricter than documented** (some endpoints may have lower limits). Mitigation: conservative rate limiter + per-cycle market batching.
3. **`cancel_market_orders` may not exist in the SDK** or may behave differently. Mitigation: the existing fallback (individual cancels) handles this.
4. **Inventory reconciliation has no clean API.** Mitigation: defer reconciliation (P1-1) if no endpoint found; rely on correct fill accounting + periodic `cancel_all` cleanup.
5. **`are_orders_scoring` may have undocumented rate limits or lag.** Mitigation: call infrequently (every 5 min, not every cycle), cache results, and do not block quoting on the response.
6. **Airdrop formula is unknown.** Mitigation: optimize for all plausible metrics (volume, breadth, consistency, LP provision) rather than guessing weights. P5-4 tracks all of them so we can pivot if public info emerges.
7. **Optimal market count is wrong.** Mitigation: P5-5 graduated deployment catches this early at $100. The P5-1 analysis produces a range, not a single number — start at the conservative end.

### Fallbacks

- **If `get_trades` does not support `after` filtering:** Poll `get_trades` with cursor pagination, deduplicate by trade ID against a seen-set. More expensive but still correct.
- **If `get_trades` is unavailable or returns no data:** Last resort: track order state via `get_order(order_id)` for each previously-posted order — if status is "filled" or "partially_filled", record the fill. This is expensive (1 call per tracked order) but correct.
- **If batch `get_midpoints` fails or is unsupported:** Fall back to individual calls but reduce `MAX_MARKETS_PER_CYCLE` proportionally.
- **If `are_orders_scoring` is unavailable or unreliable:** Infer scoring from quote parameters: if our spread < `rewardsMaxSpread` and size >= `rewardsMinSize` and we have both sides, we are almost certainly scoring. Less reliable but functional.

---

## Pre-Go-Live Checklist

### Correctness (must pass before any live capital)

- [x] Fill accounting uses the CLOB `get_trades` API (or documented fallback), not order-set differencing
- [x] No fill is inferred when `get_orders` or any API call fails (error = abort, not "all filled")
- [x] Each fill is applied to inventory exactly once per cycle (`drain_new_fills` pattern)
- [x] `FillEvent.filled_size` reflects actual trade size, not original order size
- [x] Trade polling watermark is persisted to disk, advances by max observed trade timestamp (not wall-clock), and uses an overlap window with dedup
- [x] Fill collection is a cycle-level step, not coupled to `cancel_and_replace`
- [ ] DRY_RUN=true test passes with zero phantom fills and correct inventory

### Throughput (must pass before scaling beyond 3 markets)

- [x] Rate limiter `rate` is configurable (not hardcoded); default is conservative; actual limits verified via test run before increasing
- [x] `MAX_MARKETS_PER_CYCLE` is set so that total API calls/cycle fits within `rate × interval`
- [ ] Cycle completes within `REBALANCE_INTERVAL_SECONDS` for the configured market count (verified in logs)
- [ ] No 429 errors observed during 1-hour test run
- [x] Batch midpoint fetching is operational (single call replaces N)

### Safety (must pass before scaling beyond $500)

- [x] Gross exposure cap uses mark-to-mid pricing and includes open order notional (or explicitly deferred)
- [x] Inventory reconciliation is implemented (or explicitly deferred with documented justification)
- [x] `send_skew_pause` is wired and fires on pause transitions
- [x] Skew alerts have a cooldown (no Discord spam)

### Revenue (must pass before considering deployment "production")

- [x] `are_orders_scoring` confirms >80% of active markets are scoring
- [x] No periodic `cancel_all` — rolling refresh is in place
- [x] Daily rewards summary shows estimated user share, not pool totals
- [x] Airdrop metrics (volume, breadth, consistency) are tracked and logged daily
- [x] Optimal market count analysis is documented and defaults reflect the analysis
- [x] Unit tests cover fill recording, skew calculation, and quote sizing

### Graduated deployment gates

- [ ] **Stage 1:** $100 / 3 markets / 24h — zero accounting discrepancies, scoring confirmed
- [ ] **Stage 2:** $500 / 10 markets / 48h — cycles on time, no 429 bursts, reconciliation passes
- [ ] **Stage 3:** $10k / target market count / 1 week — all metrics stable

---

## Appendix A: Optimal Market Count Analysis (P5-1)

### API Budget

At conservative 1.3 req/s × 45s cycle = ~58 API calls/cycle.

Per-market cost after optimizations:
- **With batch midpoints (P3-1) + global trades fetch (P0-1b):** ~5 calls/market (cancel_market + 4 order posts)
- **Without batching:** ~9-11 calls/market

### Capacity Model

| Interval | Markets/cycle (batched) | Round-robin across 2 cycles | 3 cycles |
|----------|------------------------|---------------------------|----------|
| 45s      | 11                     | 22                        | 33       |
| 60s      | 15                     | 30                        | 45       |
| 90s      | 23                     | 46                        | 69       |

### Revenue Tradeoff

- **Scoring consistency vs breadth:** 20 markets with always-fresh quotes (45s refresh) likely earn more than 60 markets with stale quotes (135s refresh). Stale quotes get crossed or filled adversely, scoring 0.
- **Airdrop breadth:** More distinct markets is better. Round-robin lets us cover more markets while keeping quotes reasonably fresh.
- **Sweet spot:** **20 markets at 45s with round-robin** (top-15 always refreshed, remaining 5 rotate). This gives:
  - 100% scoring on core 15 markets
  - ~67% scoring uptime on rotated markets
  - Sufficient breadth for airdrop (20+ distinct markets/day)

### Recommended Defaults

```
MAX_MARKETS_PER_CYCLE = 20
REBALANCE_INTERVAL_SECONDS = 45
TARGET_MARKETS = 20-30  (via market filter tuning)
```

These should be revisited after Stage 2 testing with real scoring data from `are_orders_scoring`.
