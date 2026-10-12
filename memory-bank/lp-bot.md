# LP Bot Polymarket — Memory Bank

Document de référence courte pour les sessions agent. Dernière mise à jour : **2026-08-31** (PnL top-up incremental cost).

---

## 1. État actuel du projet

### Stack

- **Langage** : Python 3.12, asyncio (single process).
- **Dépendances clés** : **`py-clob-client-v2>=1.0.0`** (`core/client.py` + scripts), `web3==7.14.1`, `aiohttp==3.13.3`, `websockets==15.0.1`, `loguru==0.7.3`, `pydantic-settings==2.13.1`. Legacy `py-clob-client` retiré du chemin trading après migration V2 (commit **`34aa08a`**).
- **Structure** : `main.py` (~1218 LOC orchestrateur), `core/` (client, quoter, order_manager, inventory, fill_collector, scoring_monitor, rewards_tracker, ws_feed, market_manager, inventory_seeder, **toxicity_analyzer**, **paper_broker**), `config/` (settings, markets_filter), `utils/`, `scripts/`, `tests/`.
- **Entrée** : `python main.py`.

**Changelog agent :** 2026-08-31 \| [core/inventory.py, main.py, tests/test_pnl_topup.py] \| Seed top-up ADD USD: `add_seed_costs` + `absorb_seed_acquisition` (size grew + seeder USD → add, never reset); `seed_cost_gap` delta@mid; write-once first seed kept; `_credit_paper_seed`/went_in/live sync wired. Iran 248→347 avg~0.615/0.405, ASK@0.41 `|realised|<5`. DRY_RUN/$50/.env/tightness/Gamma/ASK-UNMATCH/keep-50 untouched. **416/416**. 2026-08-30 \| [core/inventory.py, main.py, tests/test_pnl_basis.py] \| Cost basis write-once: `apply_seed_costs_once` — paper/seed sync never re-assigns seed USD after fills; missing basis → size×mid + tape `cost_basis_missing`; YES/NO from token_id only (unknown skip). Fixes phantom realised (+152 US-Iran / +179 Gemini). DRY_RUN/$50/.env/tightness/Gamma/ASK-UNMATCH untouched. **413/413**. 2026-08-30 \| [utils/helpers.py, core/quoter.py, core/paper_broker.py, core/inventory_seeder.py, main.py] \| ASK unmatched cap (`max_ask_shares`); ASK=min(planned,inv,cap); min_size≤50 no ASK floor past cap; min_size>50 ASK min_size+ask_over_cap; rebalance sells to unmatched≤$50 (0/N→0/50 not 0/0); no re-seed one-sided leftover. DRY_RUN/$50/.env capital/tightness/Gamma untouched. **408/408**. 2026-08-30 \| [utils/helpers.py, core/quoter.py, core/inventory.py, core/paper_broker.py, core/inventory_seeder.py, core/resolution_guard.py, main.py, core/market_manager.py] \| max_loss → rebalance surplus only (stay SCORING); flatten+INACTIVE only extreme_mid/move_24h or escape if unmatched still >$50; BID cap iff min_size≤limit (else min_size two-sided + tape bid_over_cap); no hard-skip min_size>50. DRY_RUN/$50/.env capital/tightness/Gamma untouched. **400/400**. 2026-08-30 \| [config/settings.py, utils/discord.py] \| Un seul `DISCORD_WEBHOOK_URL` ; username/embeds taggés `qv · DRY_RUN` / `qv · LIVE`. 2026-08-30 \| [core/rewards_tracker.py, config/settings.py, utils/discord.py, main.py] \| Daily Discord: pool=`sum(reward_rate_daily)` selected markets; net+gross exposure labels; titles DRY_RUN/LIVE. 2026-08-29 \| [core/market_manager.py, tests/test_gamma_pagination.py] \| Gamma join by CLOB condition_ids: drop keyset/offset from refresh; chunk ``GET /markets?condition_ids=`` ×100 (repeated param); 422→split 50/20/1; 429/5xx retry; concurrency≤2; CLOB end_date fills; log chunks/hits/misses/splits/elapsed. Rank/filters unchanged. DRY_RUN/capital/exposure/seed/quoter untouched. **374/374**. 2026-08-29 \| [core/market_manager.py, tests/test_gamma_pagination.py] \| Gamma keyset-only: drop ``/markets?offset=`` entirely; only ``GET /markets/keyset?limit=100&after_cursor=``; stop empty/no-cursor/repeat; ``MAX_GAMMA_PAGES=250``; log unique/pages/stop_reason. Fallback 200 + gamma_miss unchanged. DRY_RUN/capital/exposure/seed/quoter untouched. **378/378**. 2026-08-29 \| [core/market_manager.py] \| Gamma: on 422 ``offset too large`` stop offset immediately (no retries) and continue via ``/markets/keyset`` ``after_cursor``/``next_cursor`` (never send offset). Fallback cap 200 unchanged. **377/377**. 2026-08-29 \| [core/order_manager.py, core/inventory.py, core/quoter.py, core/resolution_guard.py, core/inventory_seeder.py, core/market_manager.py, config/markets_filter.py, main.py, utils/helpers.py] \| GUARDS-GAMMA: deployed=inv+BID (ASK excluded); leftover BID under ALLOC×ratio + min_size two-sided floor (no naked ASK / no INACTIVE for floor overshoot); sticky max_loss scrubbed+not persisted (extreme_mid/move_24h only); Gamma page retry+continue; capped condition_id fallback (~200 cheap-filter); gamma_miss vs no end_date; rank matched_seed_usd. DRY_RUN/$50/.env capital unchanged. **376/376**. 2026-08-29 \| [utils/discord.py] \| Discord toggles persist `data/discord_channels.json` (gagne sur `.env` au restart). 2026-08-29 \| [core/inventory_seeder.py, core/paper_broker.py, core/inventory.py, main.py, tests/test_seeder_atomicity.py] \| Seed matched shares: `N=min(ideal_yes,ideal_no)` capped by expensive side (SEED_MAX/budget); max_loss/should_delist preflight before any post; ASK clips to inventory=N; resolution delist flattens YES+NO (paper sell DRY_RUN, GTC live). DRY_RUN / RESOLUTION_MAX_LOSS unchanged. **364/364**. 2026-08-29 \| [terminal/] \| Shell Proxima : footer full-bleed (centré page) ; Settings (écrou) bas-gauche ; collapse nav ‹› ; Discord sous Settings. 2026-08-29 \| [utils/tape_server.py, main.py, terminal/] \| Discord toggles live via webapp : `GET/PATCH /discord/channels` + pane Discord (runtime, restart recharge `.env`). 2026-08-29 \| [config/settings.py, utils/discord.py, .env.example, tests/test_discord.py] \| Discord channel toggles : `DISCORD_CHANNELS` CSV (startup/orders/cycle/fills/skew/errors/daily/alerts) + `set_channels()` hot-swap ; défaut = tout on. **8** tests discord. 2026-08-29 \| [core/quoter.py, core/inventory_seeder.py, core/market_manager.py, config/settings.py, utils/helpers.py] \| Quote size = `alloc/2/price` (plus `min_size×SIZE_MULTIPLIER`); seed target = planned ask, capped SEED_MAX/SEED_BUDGET (pas fail si partiel); N tronqué à `floor(TOTAL/MIN)`; log `n/ranked/alloc_med/seed_budget/cash_reserve`; `MAX_MARKETS_PER_CYCLE` défaut 60; `SEED_BUDGET_PCT` défaut 0.65. DRY_RUN inchangé. **344/344**. 2026-08-29 \| [core/tape.py, core/order_manager.py, main.py] \| Quotes YES/NO : `remember_two_sided` après chaque cancel_and_replace (même si smart-diff keep) — N bid/ask plus vides. **338/338**. 2026-08-29 \| [core/tape.py, core/order_manager.py, core/operator_digest.py, terminal/] \| Table quotes : YES/NO séparés (`y bid/ask`, `n bid/ask`) — plus d’écrasement NO sur mid YES ; `post.outcome` + cache `yes_*`/`no_*`. **337/337 tests** ✅. 2026-08-29 \| [terminal/] \| PnL + tape snapshot : yes/y vert, no/n rouge. 2026-08-29 \| [terminal/] \| Seeder: pills en flex rows, plus de overlap. 2026-08-29 \| [terminal/] \| Table: yes vert `#19c27c`, no rouge `#f05151` ; mid tick vert si hausse / rouge si baisse. 2026-08-29 \| [terminal/] \| Charcoal Proxima (`#0b0d10`/`#12151a`) ; accent orchid seulement ; vue Markets = table selected plein écran. 2026-08-29 \| [terminal/] \| Palette navy (Dark Knight/Orchid) + status `#19c27c` `#f05151` `#f4b943` ; rail en grid `fr` (plus de flex coincé) ; scroll carte PnL + reset scrollTop. **336/336 tests** ✅. 2026-08-29 \| [terminal/] \| Polish: accent `#7892FF` ; rail Quote+PnL sur 2 rangées (plus de crop collateral) ; sidebar focus/expand réel ; live+horloge une seule fois (footer). **336/336 tests** ✅. 2026-08-29 \| [scripts/archive_tape.py, data/archives/, .gitignore] \| Archive tape: one-shot move `data/tape.jsonl` → `data/archives/tape_YYYYMMDD_HHMMSS.jsonl` ; `--watch` renames bare `tape.jsonl` drops in archives (local stamp). 2026-08-29 \| [core/hot_guards.py, main.py, core/rewards_tracker.py, core/operator_digest.py, terminal/] \| Hot skip tape/digest emit on transition only (`note_hot_skip_transition` + `_hot_skip_last`) — still skip compute/post + record_mid ; digest `est_rewards_usd` / `est_share` / `est_note` separate from realised (LP_ESTIMATED_SHARE, fallback time×tightness×reward_rate_daily). **336/336 tests** ✅. 2026-08-29 \| [terminal/, utils/tape_server.py] \| Revamp UI Proxima (sidebar lys qv, cartes métriques, footer live) ; favicon `quietvault_pfp_3.jpg` via `/favicon.jpg` `/favicon.ico`. Read-only. **331/331 tests** ✅. 2026-08-29 \| [main.py, utils/tape_server.py] \| Tape HTTP early-start (avant cancel_all/refresh/seed/inventory) ; `CancelledError` uvicorn (Ctrl+C / SSE disconnect) filtré — bénin. 2026-08-29 \| [core/paper_broker.py, core/order_manager.py, core/inventory_seeder.py, core/ws_feed.py, core/scoring_monitor.py, core/operator_digest.py, main.py, terminal/] \| DRY_RUN paper broker : cash=TOTAL_CAPITAL (label paper), would_seed crédite YES/NO + débit cash, pre-check BID/ASK papier, GTC paper + tape, fills WS last_trade/BBO via apply_fills (pas de watermark), scoring=est sans are_orders_scoring. **331/331 tests** ✅. 2026-08-29 \| [terminal/, core/operator_digest.py, config/markets_filter.py] \| UI polish : Paris PT clock+ticker, charcoal panes, funnel pipeline sentence + sample why/mid/spread, rank/score/tags on selected table, seed/tape no clip. **317/317 tests** ✅. 2026-08-29 \| [core/tape.py, core/operator_digest.py, utils/tape_server.py, terminal/, main.py, core/market_manager.py, core/inventory_seeder.py, core/order_manager.py, config/markets_filter.py] \| Operator terminal qv/Polymarket : events universe/filter/seed/cycle/state ; collateral skip on tape ; snapshots per cold cycle not 1 Hz ; GET `/` `/digest` `/events` `/stream` ; vanilla UI cyan `#22d3ee`. **317/317 tests** ✅. 2026-08-29 \| [core/tape.py, core/hot_guards.py, core/fill_collector.py, core/inventory.py, core/resolution_guard.py, main.py, AUDIT.md] \| Moves 1–3 : JSONL tape + FastAPI read-only ; `hot_skip_reason` + fill ingest 2–5s apply-then-commit ; `max_loss=abs(yes−no)` + sticky delist ; MTM drawdown ; tick marché ; last_trade ignoré ; recon CONDITIONAL ; refresh IN/OUT ; `cancel_all` fail-closed \| audit A1–A12 (user WS wontfix, SDK REST-only). **310/310 tests** ✅. 2026-05-19 \| **P2d Resolution guard livré** : `core/resolution_guard.py` (nouveau, 280 LOC, pure logic — zéro API, zéro import `main.py`). Trois checks : (a) `max_loss_check(cid, yes_size, no_size)` worst-case `max(yes_size, no_size) × $1` vs `RESOLUTION_MAX_LOSS_USD=25.0` ; (b) `evaluate_move_24h(cid)` drift `|mid_now − mid_24h_ago| / mid_24h_ago × 100` vs `RESOLUTION_MAX_MOVE_24H_PCT=15.0` — requiert ≥20 min d'historique, ±30 min de tolérance autour de l'anchor `now-24h` ; (c) filet extrême hard-coded `mid < 0.05 || mid > 0.95` → delist quel que soit (a) et (b). Priorité reasons : `extreme_mid > max_loss > move_24h > None`. Rétention 25h via deque ; pruning amortised à chaque `record_midpoint`. Helper `_utcnow` indirecté pour tests. **Intégration `main.py`** : (i) `ResolutionGuard(settings)` instancié au startup (juste après `ToxicityAnalyzer`) ; (ii) **Step 3c** dans `run_quoting_cycle` AVANT le per-market exposure gate — appelle `record_midpoint` + `should_delist` puis `set_state(INACTIVE)` + `cancel_orders_for_market` + Discord `warning` ; (iii) hot WS path : `record_midpoint` appelé après `_hot_quoter.record_mid` ; (iv) signature `run_quoting_cycle` étendue d'un paramètre `resolution_guard: ResolutionGuard` ; (v) module-level `_hot_resolution_guard` pour le hot path. **+31 nouveaux tests** dans `tests/test_resolution_guard.py` (7 max_loss + 6 record_midpoint + 8 evaluate_move_24h + 8 should_delist + 2 integration). **279/279 tests** ✅. 2026-05-10 \| **6 fixes opérationnels post-V2** (issues 1–6 du runbook) : (1) seeder atomique YES+NO (pre-check coût worst-case + book depth), (2) skew penalty continu `delta = signed × SKEW_PENALTY × base_offset` + Q-score forcé à 0 sur quote one-sided, (3) compteurs cycle `selected/active/scoring` distincts + skip reasons, (4) `MarketState` enum (`UNKNOWN/SEED_FAILED/ONE_SIDED/SCORING/INACTIVE`) + realloc tire de `{SEED_FAILED, INACTIVE}` jamais de `ONE_SIDED`, (5) `MAX_EXPOSURE_RATIO=0.80` + `MAX_MARKET_EXPOSURE_RATIO=0.40` (per-market gate AVANT global), (6) hot-path log INFO seulement si modifié (sinon DEBUG) + `scoring_monitor` log `repr(exc)` quand `checked==0` + table cycle ASCII INFO + table startup. **248/248 tests** ✅ (+25 nouveaux). 2026-05-09 \| Migration **CLOB V2 complète** (commit **`34aa08a`**) : stack runtime `py_clob_client_v2`, fix `get_midpoints`/`get_order_books` sérialisation `BookParams` → dict (`core/client.py`), allowances/wrap `scripts/setup_allowances.py` (pUSD, Uniswap natif→USDC.e, POA middleware Polygon). **Live mainnet** depuis 2026-05-09 : `DRY_RUN=false`, `TOTAL_CAPITAL=350`, `MAX_MARKETS=3` ; marchés actifs *Becerra CA Gov*, *Fed rate cuts 2026* ; ~8 ordres, Q-score ~96 %. **Prochain :** P2d `resolution_guard`.

### ⚠️ Modules qui N'EXISTENT PAS (doc stale — ne jamais référencer)
- `dashboard/` — absent. UI opérateur = `terminal/` via FastAPI `GET /` (qv/Polymarket, lecture seule). `ENABLE_DASHBOARD=true` sans effet.
- `core/positions_aggregator.py` — absent.
- `scripts/positions_cli.py` — absent.
- Retiré de CLAUDE.md et AGENTS.md le 2026-05-04.

### Capital & mode (live — vérifier `.env` local)

| Paramètre | Valeur |
|-----------|--------|
| `TOTAL_CAPITAL` | **350** USDC |
| `MAX_MARKETS` | **3** (smoke / rotation ; ROADMAP peut viser plus après garde-fous) |
| `MAX_MARKETS_PER_CYCLE` | aligné config (voir `.env`) |
| `MAX_CAPITAL_PER_MARKET` | 80 |
| `DRY_RUN` | **`false`** — bot **live Polygon mainnet** depuis **2026-05-09** |
| `MODE` | `safe` |
| `REBALANCE_INTERVAL_SECONDS` | **45** |
| `WS_ENABLED` | `true` |
| `API_RATE_LIMIT_RPS` | **1.5** (plafond CLOB ~1.67 RPS) |

### État opérationnel (2026-05-09)

- **Migration CLOB V2** : complète (`AsyncClobClient` → `py_clob_client_v2`, commit **`34aa08a`**).
- **Marchés actifs (2)** : *Becerra CA Gov* ; *Fed rate cuts 2026*.
- **Ordres / scoring** : ~**8** ordres live, **Q-score ~96 %** (ordre de grandeur observé).

### Stage produit

- **Stage 1** : live avec capital 350 USDC, **3** marchés max, cycle 45s.
- **Circuit breaker drawdown daily** : implémenté le 2026-05-05 (commit `76ec51f`) avec pause globale et alerte Discord critique.

---

## 2. Bugs connus (fichier:ligne → problème)

| Fichier:ligne | Problème | Statut |
|---------------|----------|--------|
| `main.py:866-876` | `pos.yes_cost/no_cost` initialisés avec `bal * mid` au lieu du prix seed réel → PnL biaisé | **FIXÉ 2026-05-05** : seed cost basis réel depuis `core/inventory_seeder.py` |
| `rewards_tracker.py:298` | `estimated_rewards_usd / 10.0` hardcodé | **FIXÉ 2026-05-08** — remplacé par `* LP_ESTIMATED_SHARE` (configurable, défaut 0.10) ; `format_summary` accepte `q_score_ratio: Optional[float]` pour override futur |
| `config/settings.py:66` | `API_RATE_LIMIT_RPS` dépassait le plafond CLOB | **FIXÉ 2026-05-04** : 5.0 → 1.5 |
| `main.py:977` | Sleep cycle sans jitter | **FIXÉ 2026-05-04** : `+random.uniform(-2.0, 2.0)` |
| `core/inventory.py` + `main.py` | Circuit breaker drawdown journalier absent | **FIXÉ 2026-05-05** : `DAILY_DRAWDOWN_LIMIT_PCT=0.05`, pause globale + alerte Discord `critical`, reset minuit UTC |
| `core/inventory_seeder.py` | Seed unilatéral si NO skipped après YES posé (Netanyahu 0x7586a96 : YES 50 sh @ $0.43, NO=0) | **FIXÉ 2026-05-10** : pre-check atomique worst-case price + book depth + exécution séquentielle abort (Issue 1) |
| `core/quoter.py` | Skew 100 % → BID seul à zéro spread, ASK=0, Q-score réel ~0 mais reporté 93 % | **FIXÉ 2026-05-10** : penalty continu `delta = signed × SKEW_PENALTY × base_offset` + Q-score forcé à 0 + tag `ONE_SIDED` (Issue 2) |
| `main.py` (`run_quoting_cycle`) | "Cycle #N : 3/3 markets" comptait `selected_markets`, pas les actifs | **FIXÉ 2026-05-10** : compteurs `selected/active/scoring` distincts + skip reasons (Issue 3) |
| `core/market_manager.py` (`reallocate_capital`) | `non_scoring` mélangeait seed-failed et one-sided | **FIXÉ 2026-05-10** : `MarketState` enum + donor-set `{SEED_FAILED, INACTIVE}` (Issue 4) |
| `core/inventory.py` + `main.py` | Exposure guard global trop permissif (~120 % du capital), pas de gate per-market | **FIXÉ 2026-05-10** : `MAX_EXPOSURE_RATIO=0.80` (deployed) + `MAX_MARKET_EXPOSURE_RATIO=0.40` (per-market gate AVANT global) (Issue 5) |
| `main.py` (hot path) + `core/scoring_monitor.py` | Hot path log INFO toutes les 1-3 s même si smart-diff kept ; "0 markets checked" sans HTTP error | **FIXÉ 2026-05-10** : INFO seulement si `posted`, sinon DEBUG ; `last_error` capturé + log `{!r}` ; table cycle ASCII INFO ; table startup (Issue 6) |
| `core/market_manager.py:_fetch_gamma_markets` | `limit=200` + `if len(items) < limit: break` → exit dès page 1 (Gamma plafonne à 100), bot voyait ~100 marchés sur 5458 et démarrait avec 0 marché après filtering | **FIXÉ 2026-05-19** : `limit=100`, exit `if not items: break`, `MAX_GAMMA_PAGES=50`, `asyncio.sleep(0.7)` entre pages, +8 tests |

---

## 3. Tous les changements récents (2026-08-29 + 2026-05-19 + 2026-05-10 + 2026-05-08 + 2026-05-05 + 2026-05-04)

| Fichier | Changement |
|---------|------------|
| **2026-08-29 (paper broker DRY_RUN)** | |
| `core/paper_broker.py` (nouveau) | Cash papier, seed atomique YES/NO, GTC restants, `try_fill` maker (partiels) sur print/BBO |
| `core/order_manager.py`, `core/inventory_seeder.py`, `main.py`, `core/ws_feed.py` | DRY_RUN : pre-check papier, posts dans `_live_orders`, seed crédite inventory, WS last_trade/BBO → apply_fills sans watermark |
| `core/scoring_monitor.py`, `core/operator_digest.py`, `terminal/` | Pas d'`are_orders_scoring` fake ; `scoring=est` ; header `paper $` vs `cfg $` |
| `tests/test_paper_broker.py` | Seed, collateral, fills, DRY_RUN=false n'appelle pas paper. **331/331 ✅** |
| **2026-08-29 (qv/Polymarket operator terminal)** | |
| `core/tape.py`, `core/operator_digest.py`, `utils/tape_server.py`, `terminal/` | Events `universe`/`filter`/`seed`/`cycle`/`state` ; ring+SSE ; snapshots per cold cycle (+fill) not 1 Hz JSONL ; `GET /` `/digest` `/events` `/stream` ; UI qv/Polymarket cyan `#22d3ee` |
| `core/market_manager.py`, `core/inventory_seeder.py`, `core/order_manager.py`, `main.py`, `config/markets_filter.py` | Emitters + collateral `skip` with USD ; digest 1 Hz in-memory only |
| **2026-08-29 (Moves 1–3 — tape, hot pauses, capital brakes)** | |
| `core/tape.py`, `utils/tape_server.py`, `main.py` | JSONL `data/tape.jsonl` (fill/post/cancel/skip/exception/snapshot) + FastAPI GET-only `127.0.0.1:TAPE_HTTP_PORT` |
| `core/hot_guards.py`, `main.py:hot_requote` | Skip toxicity / INACTIVE / SEED_FAILED / exposure / skip_cids before post ; `toxicity.record_mid` on WS tick |
| `core/fill_collector.py` | `fetch()` no disk ; `commit()` after inventory apply ; ingest task 2–5s |
| `core/resolution_guard.py`, `core/inventory.py`, `core/order_manager.py`, `core/ws_feed.py`, `core/market_manager.py` | unmatched max_loss + sticky delist ; MTM drawdown ; market tick ; last_trade no-op ; CONDITIONAL recon ; refresh IN/OUT |
| `AUDIT.md` | Status A1–A12 ; user WS **wontfix** |
| **2026-08-29 (audit pass — Python kept, no rewrite)** | |
| `AUDIT.md` (nouveau, racine) | Audit lu sur le code réel (`main.py`, `core/*`, `config/*`, `tests/*`). L'audit May 2026 (`audit_opus4_7_max_may_2026.txt`) est **stale** (rate limit / jitter / drawdown / midpoint / seeder / P2a–d déjà livrés). Alarming : (A1) hot path re-poste après toxicity/resolution/exposure pause ; (A2) `max_loss = max(yes,no)×$1` déliste un livre hedgé ; (A3–A4) fills à 45s + watermark avant `record_fills` ; (A5) WS `last_trade` écrase le mid ; (A6) fill-vs-mid mort (tolérance 2s, mids toxicity cold-only) ; (A7) reconcile `get_balance_allowance()` sans token ; (A8) drawdown realised-only ; (A9) `TICK_SIZE=0.01` hardcodé ; (A10) refresh sans cancel OUT / seed IN ; (A11) pas de tape (FastAPI unused, pas de `dashboard/`). Keep : quoter Q-score, skew 4-tier, smart-diff, watermark design, seeder atomique, MarketState, settings. Next 3 : tape locale → honorer pauses hot + fills hors cycle → max_loss hedge-aware + MTM drawdown. Verdict : la stratégie (politics 120j, 1 tick, poll 45s) est le vrai plafond, pas Python. |
| **2026-05-19 (Hotfix pagination Gamma)** | |
| `core/market_manager.py:41-55` (nouvelles constantes module) + `:362-435` (`_fetch_gamma_markets`) | **Bug critique fixé** : bot live échouait au boot avec "After filtering: 0 markets" → "No markets available — exiting". Cause = `_fetch_gamma_markets` demandait `limit=200` mais Gamma plafonne à 100 items/réponse ; la condition `if len(items) < limit: break` sortait dès la 1re page (100 < 200), laissant les 3 marchés actifs (Becerra, Fed, Netanyahu) hors du lookup. Fix : (a) `GAMMA_PAGE_LIMIT=100` (= plafond réel API) ; (b) exit sur `if not items: break` (vraie fin de pagination) ; (c) `MAX_GAMMA_PAGES=50` safety bound (5000 marchés max) + log WARNING si bound atteint ; (d) `asyncio.sleep(0.7)` entre pages — ~1.4 RPS aligné `backtest/fetcher.py:HTTP_RATE_LIMIT_SLEEP_S`, jamais avant la 1re requête ; (e) log final enrichi `pages {}` count. |
| `tests/test_gamma_pagination.py` (nouveau, 8 tests) | Mock manuel `aiohttp.ClientSession` via `_FakeSession`/`_FakeResp` (pas de dépendance `aioresponses`) + patch `core.market_manager.asyncio.sleep` pour vitesse. Couvre : sub-ceiling page (80 items → lookup 80, terminaison propre) ; multi-page (100+100+50 → 250 entries) ; full pages + empty probe (300) ; safety bound (queue infinie stoppée à MAX_GAMMA_PAGES) ; empty page #2 (1 page collected) ; HTTP 500 (warning + lookup partiel sans crash) ; sleep entre pages (3 sleeps pour 4 requêtes) ; no-sleep-before-first (probe immédiat). **287/287 ✅** (279 baseline + 8 nouveaux). |
| **2026-05-19 (P2d resolution guard)** | |
| `core/resolution_guard.py` (nouveau, ~280 LOC) | `ResolutionGuard(settings)` : `record_midpoint(cid, mid, ts_utc)` (deque/cid, rétention 25h, auto-prune, mids hors `(0,1)` droppés, naive datetimes traités UTC) ; `max_loss_check(cid, yes, no) → (breached, max_loss, limit)` (strict `>`) ; `evaluate_move_24h(cid) → (breached, move_pct, limit)` (≥20 min d'historique, ±30 min de tolérance sur anchor `now-24h`, picks closest sample) ; `should_delist(cid, yes, no, mid)` agrège avec priorité `extreme_mid > max_loss > move_24h > None`. Module pur — zéro `main.py` import, zéro API call. Helper `_utcnow()` indirecté pour monkeypatch tests. |
| `config/settings.py` (bloc "Resolution Guard") | `RESOLUTION_MAX_LOSS_USD: float = Field(default=25.0, ge=1.0)` + `RESOLUTION_MAX_MOVE_24H_PCT: float = Field(default=15.0, ge=1.0, le=100.0)`. Filet extrême `mid < 0.05 || > 0.95` reste hard-coded module-level (sécurité non-débrayable). |
| `.env.example` (bloc "Resolution Guard (P2d)") | Vars `RESOLUTION_MAX_LOSS_USD=25.0` + `RESOLUTION_MAX_MOVE_24H_PCT=15.0` avec commentaires (algo + tolérance + safety net). |
| `main.py` (import L37, refs L188-191, hot path L218-224, signature `run_quoting_cycle` L348, Step 3c L451-505, instanciation L1170, hot globals L1287, call L1349) | (a) Import `ResolutionGuard` ; (b) module-level `_hot_resolution_guard: Optional[ResolutionGuard] = None` ; (c) hot `record_midpoint(cid, yes_mid_for_history, datetime.now(timezone.utc))` après `_hot_quoter.record_mid` ; (d) **Step 3c** dans `run_quoting_cycle` AVANT le per-market exposure gate (Step 4) : append cold mid → `should_delist` → si delist : `set_state(INACTIVE)` + `paused_set.add(cid)` + `skip_reasons[cid] = f"resolution_{reason}"` + `cancel_orders_for_market` + log `WARNING \| resolution_guard \| {cid[:10]} delisted — {reason} (max_loss=$X, move=Y%)` + Discord `warning` ; (e) init de `active_cids`/`paused_set`/`skip_reasons` remonté de Step 4 vers Step 3c (single init, lecture/append en Step 4) ; (f) `resolution_guard=resolution_guard` ajouté à l'appel `run_quoting_cycle` dans la boucle principale. |
| `tests/test_resolution_guard.py` (nouveau, 31 tests) | `TestMaxLossCheck` (7) : not breached / heavy_yes / heavy_no / strict `>` at threshold / yes==no (max≠sum) / zero / configurable limit. `TestRecordMidpoint` (6) : order / purge >25h / keep ≤25h / drop out-of-band / drop empty cid / naive datetime as UTC. `TestEvaluateMove24h` (8) : no data / <20 min / no point in tolerance (22 min data) / below threshold / above threshold / 29 min in tolerance / 31 min out / picks closest. `TestShouldDelist` (8) : extreme low/high / 0.05+0.95 boundary in-band / max_loss reason / move_24h reason / priority extreme>max_loss / priority max_loss>move_24h / no trigger. `TestIntegration` (2) : timeline 25h synthétique + per-market isolation. **279/279 ✅** (248 baseline + 31 nouveaux). |
| `scripts/sell_positions.py` (mineur) | Filtre dust CLI : prompt `[y/N]` (default N) affiché uniquement si positions estimées `size × cur_price < $1.00` existent. `_is_dust` conserve les positions `cur_price=0` (valeur inconnue). `EOFError` → N automatique pour usage non-interactif. Aucun changement à la logique de vente. |
| **2026-05-10 (post-V2 ops fixes — 6 issues runbook)** | |
| `config/settings.py:142,150-151` | **Issue 5** : `SKEW_PENALTY: float = 0.5` (range `[0,2]`) + `MAX_EXPOSURE_RATIO: float = 0.80` + `MAX_MARKET_EXPOSURE_RATIO: float = 0.40` |
| `core/inventory_seeder.py:179-320` | **Issue 1** : seed atomique YES+NO. Pre-check sur les 2 côtés AVANT tout `create_and_post_order` : (a) `worst_extra_ticks = SEED_AGGRESSIVE_TICKS + 2×(SEED_MAX_ATTEMPTS−1)` pour estimer le prix de la dernière retry — sinon le runtime cost-check ré-introduisait le bug ; (b) `_check_book_depth` sur YES ET NO (asks ou bids opposés via mint complément) ; (c) exécution séquentielle YES→NO, abort NO si YES = 0 shares acquise. Log `WARNING | seeder | Market {cid} skipped — atomic seed failed (...). No position taken.` |
| `core/quoter.py:64-77,457-470,535-546` | **Issue 2** : (a) `TwoSidedQuotes.is_two_sided` property (true ssi un livre a `bid_size>0 AND ask_size>0`) ; (b) skew penalty continu `delta = signed × SKEW_PENALTY × base_offset` (sign-symmetric : heavy → BID↑/ASK↓ pour liquider, light → BID↓/ASK↑ pour acheter), remplace les coefficients hard-codés `1.4×/0.85×` ; (c) Q-score forcé à 0 + tag `ONE_SIDED` si `bid_size==0 OR ask_size==0` (LP rewards = `min(Q_bid, Q_ask)`) ; (d) log `Quote {token_id}` rétrogradé à DEBUG |
| `core/market_manager.py:46-72,103-128,471-600+` | **Issue 4** : (a) `MarketState` enum `{UNKNOWN, SEED_FAILED, ONE_SIDED, SCORING, INACTIVE}` ; (b) `set_state` rend `SEED_FAILED` **sticky** (jamais downgradé) ; (c) `get_state`, `get_states_snapshot`, `cids_in_state` ; (d) `reallocate_capital` : `donor_states = {SEED_FAILED, INACTIVE}` — `ONE_SIDED` jamais drainé (le bot rebalance activement) ; (e) `_log_selection_details` reformaté en table fixed-width au démarrage (Issue 6) |
| `core/inventory.py:338-392` | **Issue 5** : (a) `is_gross_exposure_breached(deployed_capital=...)` — limit = `deployed_capital × MAX_EXPOSURE_RATIO` quand fourni, sinon fallback legacy `GLOBAL_MAX_GROSS_EXPOSURE_PCT × TOTAL_CAPITAL` ; (b) `market_exposure_usd(cid, open_order_notional_for_market)` ; (c) `is_market_exposure_breached(cid, allocated_capital, open_order_notional_for_market) → (breached, exposure, limit)` |
| `core/order_manager.py:110-121` | **Issue 5** : `open_order_notional_for_market(condition_id) -> float` — somme `price × size` pour les ordres tracked d'un marché, pour le gate per-market |
| `core/scoring_monitor.py:89-141` | **Issue 6** : capture `last_error: Optional[BaseException]` + `last_error_cid` ; log `{!r}` (status_code + message HTTP) quand `checked == 0` au lieu d'un compte sec |
| `main.py:33,265-281,442-479,771-882,1205-1213` | **Issues 3+4+5+6** : (a) import `MarketState` ; (b) hot path log INFO seulement si `result.posted`, sinon DEBUG (Issue 6) ; (c) per-market exposure gate AVANT global, cancel automatique si breach ; (d) global gate utilise `deployed_capital = sum(allocated)` (Issue 5) ; (e) `run_quoting_cycle` collecte `market_results` puis classifie en `MarketState`, log table ASCII `┌─ Cycle #N ─┐` avec colonnes Market/State/Bid/Ask/Skew/Net($) + summary `selected=X active=Y scoring=Z | posted=A kept=B errors=C | Es` (Issues 3+6) ; (f) skip lines `SKIP {cid}… reason={seed_failed,skew_one_sided,exposure_per_market,cycle_error}` ; (g) marquage post-seeder via `set_state(SEED_FAILED|UNKNOWN)` (Issue 4) |
| `tests/test_seeder_atomicity.py` (5) + `tests/test_market_states.py` (8) + `tests/test_market_exposure.py` (4) + `tests/test_quote_two_sided.py` (6) + `tests/test_quoter.py` (+2) | **+25 tests** validant : pre-check coût/book-yes-vide/book-no-vide/no-mid/already-seeded ; sticky `SEED_FAILED` + realloc tire de `{SEED_FAILED, INACTIVE}` jamais `ONE_SIDED` ; per-market exposure gate avec `deployed_capital` override ; `is_two_sided` 6 cas ; `SKEW_PENALTY=0` désactive l'ajustement. **248/248** ✅ |
| `scripts/sell_positions.py` | **Inchangé** — vérifié : importe sans erreur, signatures `config.settings` + `core.client` non touchées sur la surface qu'il consomme. Aucune dépendance à `MarketState`/`InventoryTracker` (lit positions directement depuis Polymarket Data API). |
| **2026-05-08 (P2c)** | |
| `backtest/__init__.py` + `backtest/fetcher.py` + `backtest/simulator.py` + `backtest/metrics.py` + `backtest/grid_search.py` + `backtest/run.py` + `tests/test_simulator.py` + `tests/test_metrics.py` + `.gitignore` | **P2c done** : framework backtest standalone (zéro import `main.py`, zéro CLOB live). **fetcher** = CLOB `/batch-prices-history` + Gamma `/markets` → SQLite (`data/backtest.db`), idempotent INSERT OR IGNORE, delta-backfill cache, filtre "active during month" `start_date <= last_day AND end_date >= first_day`, coverage flag (gap > 5min → `low_coverage=1`). **simulator** = replay Quoter+ToxicityAnalyzer+midpoint guard sur tape mid YES, modèle naïf maker (BID YES si `min_yes <= bid_price`, etc.). Fidélité documentée : Quoter.compute 1:1, midpoint guard 1:1 (cycle-counter), VPIN 1:1 (deque-count). Dégradations : fill-vs-mid neutre (monotonic n'avance pas), vol multiplier collapse (window= deque maxlen=3000), spike non-déclenché. `allocated_capital` auto-dérivé `Settings.TOTAL_CAPITAL / MAX_MARKETS` clampé `[MIN, MAX]_CAPITAL_PER_MARKET`. Lookahead horizons strict `±tolerance` (T1MIN=30s, T5MIN=60s). **metrics** = Q-score (((v−s)/v)²×size), théorique max `2 × ((v−tick)/v)² × min_size × n_quoting_cycles` (per user spec), adverse selection `mid_t5min vs fill_price` per side, signed PnL close-at-mid_t5min, daily PnLs UTC, Sharpe portfolio-level (sum daily across markets BEFORE recompute, `× sqrt(30)`). **grid** = 36 combos (4 × 3 × 3 ; `VPIN_THRESHOLD` DROPPED — modèle de fill paired BID+ASK structurellement, VPIN imbalance impossible — documenté `TestVpinSymmetry`). `--quick` mode = 4 combos. `simulate_combo` isolé prêt pour `ProcessPoolExecutor`. CSV append-mode crash-safe. **run** CLI `python -m backtest.run [--quick|--all|--year/--month|--condition-ids ...] [--days N] [--skip-low-coverage]`. **Perf** : 45s pour 36 combos × 20 marchés (177× via ProcessPoolExecutor+pstdev_fast). **Résultat** : offset=1 tick domine ; PnL absolu biaisé par fill naïf (artefact documenté). **Commits** : `72f04b1` + `00f5041` + `c11f962`. **222/222 tests** ✅ (132 baseline + 18 simulator + 24 metrics + 48 fetcher) |
|| `backtest/fetcher.py` + `tests/test_fetcher.py` | **Fetcher quality filters** (même session P2c) : `FetchFilter` dataclass + `_FilterSettings` duck-type (sans `.env`) + `_market_passes_filter()` délègue à `passes_hard_filters()`. CLI flags : `--min-reward-rate`, `--midpoint-min/max`, `--min/max-volume`, `--min-end-date-days`, `--exclude-tags`, `--condition-ids-from PATH`. Filtre appliqué après fetch Gamma, avant écriture SQLite. Defaults = valeurs `.env` live. 48 nouveaux tests. |
| **2026-05-08 (P2b)** | |
| `core/toxicity_analyzer.py` (nouveau, 348 LOC) + `core/quoter.py` + `main.py` + `config/settings.py` + `tests/test_toxicity_analyzer.py` (32 tests) + `tests/test_quoter.py` + `.env.example` | **P2b done** (commit `678d6b5`) : ToxicityAnalyzer VPIN + fill-vs-mid. VPIN = `|buys−sells|/N` rolling fenêtre `VPIN_WINDOW_FILLS=50` → multiplicateur linéaire `[1.0, 2.5]` au-dessus de `VPIN_THRESHOLD=0.70` ; pause hard `TOXICITY_PAUSE_SECONDS=180` au-dessus de `VPIN_PAUSE_THRESHOLD=0.85`. Fill-vs-mid = drift adverse (en ticks, clamp ≥0) aux horizons +5s/+30s/+5min pondérés 0.2/0.3/0.5. Les deux signaux composés via `max()` → `tox_mult` multiplié par `vol_mult` dans `Quoter._compute_offset`. Cold-cycle uniquement, zéro appel API. `ADVERSE_FILL_RATE_THRESHOLD` retiré. Step 4.7 toxicity guard dans `main.py` (pause-on-transition + cancel orders + Discord `warning`). **132/132 tests** ✅ |
| `config/settings.py` + `core/quoter.py` + `main.py` + `tests/test_quoter.py` + `.env.example` | **P2a done** : dynamic spread basé vol réalisée + spike detector. `Quoter._price_history` (rolling deque par cid) alimenté par hot WS path ET cold cycle ; `_compute_vol_multiplier()` mappe stdev (en ticks) sur `[VOL_SPREAD_MULTIPLIER_MIN=0.7, MAX=3.0]` appliqué à `QUOTE_OFFSET_TICKS` (legacy mode `QUOTE_OFFSET_TICKS=0` intact). Spike detector (`evaluate_spike` range-based sur 30s/5 ticks) → `trigger_spike_pause` 120s + cancel + Discord alert `warning`. Reprise progressive automatique via vol_mult restant élevé après spike. Velocity guard `ws_feed.py:_check_velocity` préservé tel quel (3 échelles disjointes : 5s/30s/15min). 100/100 tests ✅ |
| **2026-05-05** | |
| `core/quoter.py:108-145` | `feat(quoter)` midpoint guard continu : `evaluate_midpoint_guard()` + `is_midpoint_paused()`, pause après 3 cycles hors bande `[0.18, 0.82]`, seuils configurables via `.env` |
| `core/inventory.py` + `main.py` | `feat(inventory)` daily drawdown circuit breaker (commit `76ec51f`) : `DAILY_DRAWDOWN_LIMIT_PCT=0.05`, gate global quote path, alert Discord `critical`, reset minuit UTC, hot path gardé (`main.py:199-200`) |
| `tests/` | Validation `87 tests` passent (inclut baseline + drawdown + midpoint guard + seed cost basis + métriques airdrop) |
| `core/order_manager.py` + `main.py` + `core/rewards_tracker.py` | **P1 done (2026-05-05)** : tracker quoted notional (`price × size` par POST réussi) + tracking séparé du filled volume (`price × filled_size`) ; snapshot daily Discord affiche maintenant les deux lignes ("Volume quoted today" / "Volume filled today") ; persistence cumulative dans `data/airdrop_metrics.json` |
| `config/settings.py` | `fix(settings)` (commit `f531129`) : `NoDecode` pour `PREFERRED_TAGS` + `extra="ignore"` pour variables `.env` inconnues |
| `.env:61` + `config/settings.py:66` | `API_RATE_LIMIT_RPS` : `5.0` → `1.5` |
| `main.py:20` | `import random` ajouté |
| `main.py:978` | `sleep_time += random.uniform(-2.0, 2.0)` — jitter anti-fingerprint |
| `.env:25` | `REBALANCE_INTERVAL_SECONDS` : `10` → `45` (ROADMAP Appendix A) |
| `.env:35` | `MAX_MARKETS` : `3` → `15` → `20` (ROADMAP Appendix A sweet spot) |
| `.env:36` | `PREFERRED_TAGS` ajouté : +sports,crypto,ai,science,entertainment,culture |
| `.env.example` | `API_RATE_LIMIT_RPS`: 5.0→1.5, `BURST`: 25→20, `REBALANCE`: 10→45, `MAX_MARKETS`: 60→20 |
| `requirements.txt` | `pip freeze` complet depuis `.venv` (versions épinglées) |
| `CLAUDE.md` + `AGENTS.md` | Retrait `dashboard/`, `positions_aggregator.py`, `positions_cli.py` + "15s" → "45s" |
| `README.md` | `API_RATE_LIMIT_RPS` default 3.0→1.5, `REBALANCE_INTERVAL_SECONDS` 15→45 |
| `.cursor/TASK.md` | Critères d'acceptance P1 + section P3 (Tier 1/2 ANALYSIS.md) |

---

## 4. Tooling / Ops

### Hook post-commit (`scripts/post-commit.sh`)
- **Versionné** dans `scripts/post-commit.sh` (rwx).
- **Installé** dans `.git/hooks/post-commit`.
- **Comportement** : affiche un rappel "⚡ MEMORY UPDATE REQUIRED" avec le hash + message du commit après chaque `git commit`.
- **Sur un nouveau clone** : `cp scripts/post-commit.sh .git/hooks/post-commit`

---

## 5. Décisions d'architecture

- **Python / asyncio**, pas Go **ni Rust** (décision owner 2026-08-29, voir `AUDIT.md`) : SDK officiel synchrone enveloppé dans `asyncio.to_thread()`. Le plafond n'est pas le langage.
- **Règles Cursor** : `.cursor/rules/01-core-behavior.mdc`, `02-architecture.mdc`, `03-polymarket-api.mdc`, `04-airdrop-priority.mdc`.
- **Configuration** : une seule classe `Settings` dans `config/settings.py` — ne pas inventer de variables absentes.
- **`DRY_RUN`** : ne passer à `false` que sur instruction explicite du propriétaire du wallet.
- **Invariants prod** : conserver `cancel_all_orders` au démarrage (~815 `main.py`), handler SIGTERM, logging via loguru (pas `print()`).
- **`dashboard/` n'existe pas** : ne jamais générer de code pour ce module.

---

## 6. Prochain chantier prioritaire

### Audit 2026-08-29 — à faire avant P2e
Voir `AUDIT.md`. Ne pas enchaîner P2e (sizing dynamique) tant que : (1) tape locale fills/spread/pnl/errors, (2) hot path honore les pauses + fills hors clock 45s, (3) `max_loss` hedge-aware + drawdown mark-to-mid. Pas de rewrite Rust.

### P1 — Statut
- **Terminé** : tous les items P1 sont complétés au 2026-05-05.
- **Livré** : fix cost basis seed + tracker quoted notional + dual métriques Discord (quoted vs filled).

### P2a — Dynamic spread + spike detector
- **Terminé** au 2026-05-08. Vol multiplier rolling 15min + spike pause 30s/5 ticks + 6 nouveaux env vars + 13 nouveaux tests.

### P2b — Toxicity analyzer
- **Terminé** au 2026-05-08 (commit `678d6b5`).
- `core/toxicity_analyzer.py` créé (348 LOC). VPIN + fill-vs-mid. `ADVERSE_FILL_RATE_THRESHOLD` retiré. 32 nouveaux tests. 132/132 total.

### P2c — Backtest framework
- **DONE** au 2026-05-08 (commits `72f04b1` + `00f5041` + `c11f962`).
- `backtest/` créé (6 modules). **222/222 tests** ✅ (174 baseline + 48 `tests/test_fetcher.py`).
- Source de données : CLOB `/prices-history` (mid 1m) + Gamma `/markets` → SQLite, pas de `/trades` (data-api privé/user-scoped).
- Limite identifiée : modèle de fill naïf produit fills BID+ASK paired → VPIN imbalance ≈ 0 → `VPIN_THRESHOLD` retiré du grid (36 combos au lieu de 108). Documenté `TestVpinSymmetry`.
- Résultat : ranking relatif valide (offset=1 tick domine) ; PnL absolu biaisé par fill naïf (artefact documenté, pas bug).
- Perf : 45s pour 36 combos × 20 marchés (177× speedup via ProcessPoolExecutor + pstdev_fast).
- Workflow : `python -m backtest.fetcher --year YYYY --month MM [--min-reward-rate N ...]` → DB ; `python -m backtest.run [--quick|--all]` → CSV `data/backtest_results.csv`.
- `FetchFilter` CLI réplique exactement les hard filters du bot live ; `--condition-ids-from data/selected_markets.json` pour tester les marchés actifs.

### Ops post-V2 (2026-05-10) — Stabilité opérationnelle
- **DONE 2026-05-10** : 6 issues du runbook fixées en un pass (seed atomique, skew continu, table cycle, `MarketState`, `MAX_EXPOSURE_RATIO` + per-market sublimit, logs hot-path/scoring/startup). 25 tests ajoutés, 248/248 ✅. Pas de changement de stratégie (spread/gamma/ranking inchangés). Voir section 3 pour le diff exact.

### P2d — Resolution guard
- **DONE 2026-05-19.** `core/resolution_guard.py` (~280 LOC) : `max_loss_check` (worst-case `max(yes,no)×$1`), `evaluate_move_24h` (≥20 min history, ±30 min tolerance, closest sample), `should_delist` (priorité `extreme_mid > max_loss > move_24h`), filet extrême hard-coded `mid<0.05 || >0.95`.
- Env vars : `RESOLUTION_MAX_LOSS_USD=25.0`, `RESOLUTION_MAX_MOVE_24H_PCT=15.0`.
- Intégration `main.py` : instanciation startup, Step 3c cold path AVANT exposure gate, hot WS path `record_midpoint` après `_hot_quoter.record_mid`. Discord `warning` + `cancel_orders_for_market` + `set_state(INACTIVE)` sur delist.
- **+31 tests** (`tests/test_resolution_guard.py`). **279/279 ✅**.

---

## 7. Roadmap P3 (chantiers structurants, ANALYSIS.md)

### Tier 1 — Impact 2-3× sur les retours
1. ~~**`core/toxicity_analyzer.py`**~~ **FAIT P2b** — VPIN + fill-vs-mid scoring
2. ~~**`core/resolution_guard.py`**~~ **FAIT P2d (2026-05-19)** — max-loss + 24h move + extreme-mid net
3. ~~**`backtest/`**~~ **FAIT P2c** — CLOB `/prices-history` + Gamma `/markets` → SQLite ; grid search 36 combos, Q-score réalisé vs max théorique, PnL, Sharpe portfolio-level

### Tier 2 — Impact 1.5-2× sur les retours
4. Market-specific strategy profiles (tightness adaptée par marché)
5. Multi-level quoting L1/L2/L3 par token
6. Cross-market hedging (matrice corrélation, YES A ↔ NO B)

---

## 8. Liens utiles

- ROADMAP détail : `ROADMAP.md`
- README opérationnel : `README.md`
- Analyse codebase : `docs/ANALYSIS.md`
- Graph Memory MCP : entité projet **`Polymarket LP Farmer Bot`** (état live + migration V2 mis à jour 2026-05-09)
