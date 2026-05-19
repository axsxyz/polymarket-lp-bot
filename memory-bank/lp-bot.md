# LP Bot Polymarket — Memory Bank

Document de référence courte pour les sessions agent. Dernière mise à jour : **2026-05-10** (6 fixes opérationnels post-migration V2 : seed atomique, skew continu, table cycle, MarketState, exposure ratios, logs).

---

## 1. État actuel du projet

### Stack

- **Langage** : Python 3.12, asyncio (single process).
- **Dépendances clés** : **`py-clob-client-v2>=1.0.0`** (`core/client.py` + scripts), `web3==7.14.1`, `aiohttp==3.13.3`, `websockets==15.0.1`, `loguru==0.7.3`, `pydantic-settings==2.13.1`. Legacy `py-clob-client` retiré du chemin trading après migration V2 (commit **`34aa08a`**).
- **Structure** : `main.py` (~1218 LOC orchestrateur), `core/` (client, quoter, order_manager, inventory, fill_collector, scoring_monitor, rewards_tracker, ws_feed, market_manager, inventory_seeder, **toxicity_analyzer**), `config/` (settings, markets_filter), `utils/`, `scripts/`, `tests/`.
- **Entrée** : `python main.py`.

**Changelog agent :** 2026-05-10 \| **6 fixes opérationnels post-V2** (issues 1–6 du runbook) : (1) seeder atomique YES+NO (pre-check coût worst-case + book depth), (2) skew penalty continu `delta = signed × SKEW_PENALTY × base_offset` + Q-score forcé à 0 sur quote one-sided, (3) compteurs cycle `selected/active/scoring` distincts + skip reasons, (4) `MarketState` enum (`UNKNOWN/SEED_FAILED/ONE_SIDED/SCORING/INACTIVE`) + realloc tire de `{SEED_FAILED, INACTIVE}` jamais de `ONE_SIDED`, (5) `MAX_EXPOSURE_RATIO=0.80` + `MAX_MARKET_EXPOSURE_RATIO=0.40` (per-market gate AVANT global), (6) hot-path log INFO seulement si modifié (sinon DEBUG) + `scoring_monitor` log `repr(exc)` quand `checked==0` + table cycle ASCII INFO + table startup. **248/248 tests** ✅ (+25 nouveaux). 2026-05-09 \| Migration **CLOB V2 complète** (commit **`34aa08a`**) : stack runtime `py_clob_client_v2`, fix `get_midpoints`/`get_order_books` sérialisation `BookParams` → dict (`core/client.py`), allowances/wrap `scripts/setup_allowances.py` (pUSD, Uniswap natif→USDC.e, POA middleware Polygon). **Live mainnet** depuis 2026-05-09 : `DRY_RUN=false`, `TOTAL_CAPITAL=350`, `MAX_MARKETS=3` ; marchés actifs *Becerra CA Gov*, *Fed rate cuts 2026* ; ~8 ordres, Q-score ~96 %. **Prochain :** P2d `resolution_guard`.

### ⚠️ Modules qui N'EXISTENT PAS (doc stale — ne jamais référencer)
- `dashboard/` — absent. `ENABLE_DASHBOARD=true` sans effet.
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

---

## 3. Tous les changements récents (2026-05-10 + 2026-05-08 + 2026-05-05 + 2026-05-04)

| Fichier | Changement |
|---------|------------|
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

- **Python / asyncio**, pas Go : SDK officiel synchrone enveloppé dans `asyncio.to_thread()`.
- **Règles Cursor** : `.cursor/rules/01-core-behavior.mdc`, `02-architecture.mdc`, `03-polymarket-api.mdc`, `04-airdrop-priority.mdc`.
- **Configuration** : une seule classe `Settings` dans `config/settings.py` — ne pas inventer de variables absentes.
- **`DRY_RUN`** : ne passer à `false` que sur instruction explicite du propriétaire du wallet.
- **Invariants prod** : conserver `cancel_all_orders` au démarrage (~815 `main.py`), handler SIGTERM, logging via loguru (pas `print()`).
- **`dashboard/` n'existe pas** : ne jamais générer de code pour ce module.

---

## 6. Prochain chantier prioritaire

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

### Prochain (P2d) — Resolution guard complet *(priorité engineering post–migration V2, 2026-05-09)*
- **`core/resolution_guard.py`** à créer.
- Max-loss calculator par marché : `max(yes_size, no_size) × 1.0` → pause si > `RESOLUTION_MAX_LOSS_USD`.
- Auto-delist si mid move > 15% en 24h → paramètre `RESOLUTION_MAX_MOVE_24H_PCT`.
- Nouveaux env vars dans `config/settings.py` : `RESOLUTION_MAX_LOSS_USD`, `RESOLUTION_MAX_MOVE_24H_PCT`.

---

## 7. Roadmap P3 (chantiers structurants, ANALYSIS.md)

### Tier 1 — Impact 2-3× sur les retours
1. ~~**`core/toxicity_analyzer.py`**~~ **FAIT P2b** — VPIN + fill-vs-mid scoring
2. **`core/resolution_guard.py`** — midpoint ongoing + max-loss calculator + auto-delist >15 % en 24h
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
