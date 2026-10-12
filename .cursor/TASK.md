# LP Bot — Active Tasks

## Audit 2026-08-29 (blocking vs P2e)
- [x] **Written audit** → `AUDIT.md` (alarming / keep / next-3). No trading refactor.
- [x] **Move 1 — local tape** (fills/spread/pnl/errors on disk; FastAPI read-only optional). See AUDIT.md §3.
- [x] **Move 2 — hot-path pauses + fill ingest off the 45s clock**
- [x] **Move 3 — hedge-aware resolution max_loss + mark-to-mid drawdown**

## P2e — Position sizing dynamique
- [ ] **Capital reallocation basé sur scoring réel** → `core/market_manager.py`
  - Utilise `are_orders_scoring` (déjà présent via `scoring_monitor.py`)
  - Toutes les heures : augmente capital sur marchés scoring > 80%, réduit sur < 50%
  - Bounds : `MIN_CAPITAL_PER_MARKET` / `MAX_CAPITAL_PER_MARKET` inchangés
  - Objectif : concentrer le capital là où le bot performe réellement

## P3a — Market-specific strategy profiles
- [ ] **Tightness adaptée par profil de marché** → `core/quoter.py` + `config/markets_filter.py`
  - High-volume (> $50k/jour) : spread plus large, size réduite, requote rapide
  - Low-volume (< $5k/jour) : spread plus tight, size normale, requote lente
  - Near-resolution (< 30 jours) : spread très large ou exit
  - Paramètres par profil configurables via `.env`

## P3b — Competition monitoring
- [ ] **Densité carnet & ajustement tightness** → `core/ws_feed.py` + `core/quoter.py`
  - Monitore la profondeur du carnet via WS book snapshots
  - Si bot est seul (ou quasi) sur un marché → resserrer spread max pour capturer tout le Q-score
  - Si carnet dense (pros présents) → élargir spread, réduire exposition
  - Mécanisme : `book_depth_score` par marché, mise à jour à chaque snapshot WS

## P3c — Adaptive taker logic
- [ ] **Switch maker→taker quand flow est informed** → `core/taker_signal.py`
  - Si toxicity_analyzer détecte flow informed fort (VPIN > seuil haut)
  - ET position bot est du mauvais côté → trade taker sur le bon côté pour couper la perte
  - Budget limité : `MAX_TAKER_USD_PER_EVENT`, max 1 par marché par heure
  - Distinct du taker signal mensuel airdrop (objectif différent)

## P3d — Multi-level quoting
- [ ] **2-3 niveaux de quotes par token** → `core/quoter.py` + `core/order_manager.py`
  - L1 : spread tight, size petite → score quadratique maximal
  - L2 : spread medium, size moyenne → capture fills quand L1 picked off
  - L3 : spread large, size grande → filet de sécurité
  - Contrainte : batch limit Polymarket = 15 ordres → calibrer MAX_MARKETS en conséquence

## P4 — Optionnel / Long terme
- [ ] Taker signal mensuel airdrop → `core/taker_signal.py` (~$20-50, marché aléatoire, 1x/mois)
- [ ] Corrélation cross-market → widen coordonné sur marchés liés (Trump 2028 ↔ Republican 2028)
- [ ] Intégration calendrier événements → widen avant elections, Fed meetings, deadlines
- [ ] Cross-market hedging → matrice corrélation, offsetter YES marché A avec NO marché B

---

## Complété

### 2026-08-31
- [x] **PnL top-up incremental cost** → `.cursor/TASK-PNL-TOPUP.md`. `apply_seed_costs_once` write-once kept; `add_seed_costs` / `absorb_seed_acquisition` ADD seeder USD on size growth; `seed_cost_gap` init delta@mid; Iran 248→347 avg~0.615/0.405, ASK 200@0.41 `|realised|<5`, keep-50 haircut not +$23. DRY_RUN / $50 / .env / tightness / Gamma / ASK-UNMATCH / keep-50 / write-once untouched. **416 passed**

### 2026-08-30
- [x] **PnL cost basis write-once** → `.cursor/TASK-PNL-BASIS.md`. Kill paper/seed sync re-assign of `yes_cost`/`no_cost`; only `record_fills`/`reduce_position` mutate after seed. Missing basis → size×mid once + tape `cost_basis_missing`. Token map YES/NO only (no guess). US-Iran-ish `|realised|<15` after BID/ASK+keep-50. DRY_RUN / $50 / .env / tightness / Gamma / ASK-UNMATCH untouched. **413 passed**
- [x] **ASK unmatched cap + rebalance keep-$50** → `.cursor/TASK-ASK-UNMATCH.md`. `max_ask_shares` caps ASK like BID; `min_size≤50` never floors ASK past cap; `min_size>50` posts min_size + `ask_over_cap`. `rebalance_unmatched` sells to unmatched≤$50 (0/311→0/50), never 0/0; no re-seed on one-sided leftover. DRY_RUN / $50 / .env capital / tightness / Gamma untouched. **408 passed**
- [x] **max_loss rebalance (not nuke seed)** → `.cursor/TASK-MAXLOSS-REBALANCE.md`. `max_loss` sells unmatched surplus only, stays SCORING; `extreme_mid`/`move_24h` still full flatten+INACTIVE; escape hatch if unmatched still >$50. BID cap only when `min_size ≤ $50`; `min_size > 50` still posts two-sided + tape `bid_over_cap`. No hard-skip min_size>50. DRY_RUN / $50 / .env capital / tightness / Gamma untouched. **400 passed**

### 2026-08-29
- [x] **Gamma join by CLOB condition_ids** → `.cursor/TASK-GAMMA-JOIN.md`. Drop keyset/offset walk from refresh; chunk `GET /markets?condition_ids=` ×100; 422 split 50/20/1; 429 retry; CLOB end_date fills. Rank/filters unchanged. DRY_RUN / capital / exposure/seed/quoter untouched. **374 passed**
- [x] **Gamma keyset-only pagination** → `.cursor/TASK-GAMMA-KEYSET.md`. Drop `/markets?offset=` entirely; only `GET /markets/keyset?limit=100&after_cursor=`; stop empty/no-cursor/repeat; `MAX_GAMMA_PAGES=250`. Fallback cap 200 + gamma_miss unchanged. DRY_RUN / capital / exposure/sticky/seed/quoter untouched. **378 passed**
- [x] **Guards + Gamma (exposure BID-only, sticky delist, universe)** → `.cursor/TASK-GUARDS-GAMMA.md`. Deployed=inv+BID (ASK excluded); leftover BID + min_size two-sided floor (no naked ASK); sticky max_loss scrubbed/not persisted; Gamma page retry + capped fallback (~200); rank matched-pair seed. DRY_RUN / $50 / .env capital unchanged. **376 passed**
- [x] **Seed matched shares (not matched dollars)** → `.cursor/TASK-SEED.md`. `N=min(ideal_yes,ideal_no)` capped by expensive side; max_loss preflight before post; ASK clips via inventory=N; resolution delist flattens inventory (paper+live path). DRY_RUN unchanged. **364 passed**
- [x] **TOTAL_CAPITAL → quote+seed size** → `.cursor/TASK-SIZE.md`. Alloc-based shares (`alloc/2/price`); seed target = planned ask (capped by SEED_MAX/SEED_BUDGET); N capped at `floor(TOTAL/MIN)`; `MAX_MARKETS_PER_CYCLE` default 60; `SEED_BUDGET_PCT` default 0.65. DRY_RUN unchanged. **344 passed**
- [x] **Tape clean (hot skip dedupe + rewards ESTIM)** → `.cursor/TASK-TAPE-CLEAN.md`. Transition-only `hot_*` tape/digest skips; digest/terminal `est_rewards_usd` separate from realised. DRY_RUN unchanged. **336 passed**
- [x] **Operator terminal (qv/Polymarket)** → `terminal/` + tape events `universe/filter/seed/cycle/state` + `GET /` `/digest` `/events` `/stream` on `127.0.0.1:TAPE_HTTP_PORT`. Read-only. No strategy/`.env` change. See `.cursor/TASK-TERMINAL.md`.
  - `pytest tests/` **317 passed**
  - `pytest tests/` **310 passed**
  - A1–A12 status in `AUDIT.md` (user WS **wontfix** — SDK REST-only)

### 2026-05-19
- [x] **P2d — Resolution guard** → `core/resolution_guard.py` + `config/settings.py` + `main.py` + `tests/test_resolution_guard.py`
  - `max_loss_check` (worst-case `max(yes,no)×$1` vs `RESOLUTION_MAX_LOSS_USD=25.0`, strict `>`)
  - `evaluate_move_24h` (`|mid_now−mid_24h| / ref × 100` vs `RESOLUTION_MAX_MOVE_24H_PCT=15.0`, ≥20 min history, ±30 min tolerance, closest sample)
  - `should_delist` : priorité `extreme_mid > max_loss > move_24h` ; filet hard-coded `mid<0.05 || >0.95`
  - Intégration : **Step 3c** cold path (AVANT exposure gate) + hot WS path `record_midpoint` ; Discord `warning` + `cancel_orders` + `set_state(INACTIVE)` sur delist
  - 248 → 279 tests ✅ (+31 : 7+6+8+8+2)

- [x] **scripts/sell_positions.py — filtre dust CLI** (mineur)
  - Prompt interactif `[y/N]` (default N) si positions estimées < $1.00 détectées
  - `_is_dust` / `_current_value` : valeur inconnue (`cur_price=0`) conservée par défaut
  - `EOFError` géré pour usage non-interactif (pipe → N)

### 2026-05-08
- [x] **P2c — Backtest framework standalone** → `backtest/` + `tests/test_simulator.py` + `tests/test_metrics.py`
  - Fetcher SQLite idempotent (CLOB `/prices-history` + Gamma `/markets`) avec delta-backfill et flag `low_coverage`
  - Simulator replay (`Quoter` + `ToxicityAnalyzer` + midpoint guard), metrics (Q-score/PnL/Sharpe), grid search (full + `--quick`) et CLI `python -m backtest.run`
  - 174/174 tests ✅

- [x] **P2b — Toxicity analyzer** → `core/toxicity_analyzer.py` + `core/quoter.py` + `main.py` + tests
  - VPIN + fill-vs-mid score, spread multiplier défensif, pause toxicité et intégration cold-cycle
  - 132/132 tests ✅

- [x] **fix(rewards) — LP_ESTIMATED_SHARE configurable** → `core/rewards_tracker.py` + `config/settings.py` + `.env.example`
  - Bug audit #2 CLOSED : `/ 10.0` hardcodé → `* LP_ESTIMATED_SHARE` (défaut 0.10, `ge=0.001, le=1.0`)
  - `format_summary` accepte `q_score_ratio: Optional[float] = None` (hook point Q-score futur)
  - Discord label : `"Est. rewards (~{share:.0%} share): ${amount:.2f}"`
  - 100/100 tests ✅

- [x] **P2a — Dynamic spread + spike detector** → `core/quoter.py` + `main.py` + `config/settings.py` + `tests/test_quoter.py` + `.env.example`
  - Rolling-vol window (`VOL_WINDOW_SECONDS=900`) → multiplier `[0.7, 3.0]` × `QUOTE_OFFSET_TICKS` (legacy mode intact)
  - Spike detector range-based (`SPIKE_TICKS_THRESHOLD=5` / `SPIKE_DETECTION_WINDOW_SECONDS=30`) → cancel + pause `SPIKE_PAUSE_SECONDS=120` + Discord alert
  - Reprise progressive automatique via vol_mult élevé post-spike (pas de logique additionnelle)
  - 3 échelles disjointes préservées : velocity guard (5s/3 ticks → log) | spike (30s/5 ticks → pause) | vol (15min/stdev → multiplier)
  - 100/100 tests ✅ (87 baseline + 13 nouveaux : 6 vol + 7 spike)

### 2026-05-05
- [x] Tracker volume quoted notional → `core/order_manager.py` + `main.py` + `core/rewards_tracker.py`
  `_session_quoted_notional` (price×size par POST réussi), drainé à chaque cycle froid
  Discord : "Volume quoted today" + "Volume filled today" séparément — 87/87 ✅

- [x] Fix cost basis seed → `core/inventory_seeder.py` + `main.py:959-972`
  `seed_all` retourne `(failed_cids, seed_costs)` avec vrai USD dépensé par side
  Fallback `bal * mid` uniquement pour tokens pré-existants — 29/29 ✅

- [x] Midpoint guard continu `[0.18, 0.82]` → `core/quoter.py:108-145` + `main.py` + `config/settings.py`
  Pause par marché après 3 cold cycles hors bande, reprise auto, Discord alerts — 84/84 ✅

- [x] Drawdown daily circuit breaker → `core/inventory.py` + `main.py` (commit `76ec51f`)
  Pause globale si `realised_pnl_today < -5% × TOTAL_CAPITAL`, reset minuit UTC — 76/76 ✅

- [x] fix(settings) NoDecode + extra="ignore" → `config/settings.py` (commit `f531129`)
  PREFERRED_TAGS CSV + DASHBOARD_* legacy ne cassent plus l'init

### 2026-05-04
- [x] API_RATE_LIMIT_RPS → 1.5 (`.env` + `config/settings.py:66`)
- [x] Jitter cycle → `main.py:978` ±2s anti-fingerprint
- [x] MAX_MARKETS → 20 (sweet spot ROADMAP Appendix A)
- [x] REBALANCE_INTERVAL_SECONDS → 45s
- [x] PREFERRED_TAGS enrichis → sports, crypto, ai, science, entertainment, culture
- [x] requirements.txt → pip freeze complet