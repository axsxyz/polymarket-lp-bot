# LP Bot Polymarket — Memory Bank

Document de référence courte pour les sessions agent. Dernière mise à jour : **2026-05-05**.

---

## 1. État actuel du projet

### Stack

- **Langage** : Python 3.12, asyncio (single process).
- **Dépendances clés** : `py-clob-client==0.34.6`, `web3==7.14.1`, `aiohttp==3.13.3`, `websockets==15.0.1`, `loguru==0.7.3`, `pydantic-settings==2.13.1`.
- **Structure** : `main.py` (~1003 LOC orchestrateur), `core/` (client, quoter, order_manager, inventory, fill_collector, scoring_monitor, rewards_tracker, ws_feed, market_manager, inventory_seeder), `config/` (settings, markets_filter), `utils/`, `scripts/`, `tests/`.
- **Entrée** : `python main.py`.

### ⚠️ Modules qui N'EXISTENT PAS (doc stale — ne jamais référencer)
- `dashboard/` — absent. `ENABLE_DASHBOARD=true` sans effet.
- `core/positions_aggregator.py` — absent.
- `scripts/positions_cli.py` — absent.
- Retiré de CLAUDE.md et AGENTS.md le 2026-05-04.

### Capital & mode (Stage 1 — vérifier `.env` local)

| Paramètre | Valeur |
|-----------|--------|
| `TOTAL_CAPITAL` | 300 USDC |
| `MAX_MARKETS` | **20** (3→15→20 le 2026-05-04, ROADMAP Appendix A) |
| `MAX_MARKETS_PER_CYCLE` | 20 |
| `MAX_CAPITAL_PER_MARKET` | 80 |
| `DRY_RUN` | `false` (live) |
| `MODE` | `safe` |
| `REBALANCE_INTERVAL_SECONDS` | **45** (était 10, ROADMAP Appendix A sweet spot) |
| `WS_ENABLED` | `true` |
| `API_RATE_LIMIT_RPS` | **1.5** (était 5.0, plafond CLOB ~1.67 RPS) |

### Stage produit

- **Stage 1** : validation live minimale (capital réduit, 20 marchés, cycle 45s).
- **Circuit breaker drawdown daily** : implémenté le 2026-05-05 (commit `76ec51f`) avec pause globale et alerte Discord critique.

---

## 2. Bugs connus (fichier:ligne → problème)

| Fichier:ligne | Problème | Statut |
|---------------|----------|--------|
| `main.py:866-876` | `pos.yes_cost/no_cost` initialisés avec `bal * mid` au lieu du prix seed réel → PnL biaisé | **FIXÉ 2026-05-05** : seed cost basis réel depuis `core/inventory_seeder.py` |
| `rewards_tracker.py:274` | `estimated_rewards_usd / 10.0` hardcodé | NON FIXÉ |
| `config/settings.py:66` | `API_RATE_LIMIT_RPS` dépassait le plafond CLOB | **FIXÉ 2026-05-04** : 5.0 → 1.5 |
| `main.py:977` | Sleep cycle sans jitter | **FIXÉ 2026-05-04** : `+random.uniform(-2.0, 2.0)` |
| `core/inventory.py` + `main.py` | Circuit breaker drawdown journalier absent | **FIXÉ 2026-05-05** : `DAILY_DRAWDOWN_LIMIT_PCT=0.05`, pause globale + alerte Discord `critical`, reset minuit UTC |

---

## 3. Tous les changements récents (2026-05-05 + 2026-05-04)

| Fichier | Changement |
|---------|------------|
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

### P2 — Dynamic spread + taker signal
- **Fichiers** : `core/quoter.py`, `core/taker_signal.py` (à créer)
- **Acceptance** : spread dynamique basé vol réalisée + signal mensuel de pression taker intégré à la logique de quotation.

---

## 7. Roadmap P3 (chantiers structurants, ANALYSIS.md)

### Tier 1 — Impact 2-3× sur les retours
1. **`core/toxicity_analyzer.py`** — VPIN + fill-vs-mid scoring + corrélation marchés liés
2. **`core/resolution_guard.py`** — midpoint ongoing + max-loss calculator + auto-delist >15 % en 24h
3. **`backtest/`** — replay orderbook historique Gamma `/trades`, grid search params, Q-score réalisé vs max

### Tier 2 — Impact 1.5-2× sur les retours
4. Market-specific strategy profiles (tightness adaptée par marché)
5. Multi-level quoting L1/L2/L3 par token
6. Cross-market hedging (matrice corrélation, YES A ↔ NO B)

---

## 8. Liens utiles

- ROADMAP détail : `ROADMAP.md`
- README opérationnel : `README.md`
- Analyse codebase : `docs/ANALYSIS.md`
- Graph Memory MCP : entités `Polymarket LP Farmer Bot`, `P0 Quick Wins May 2026`, `Known Bugs`, `Next Priority Work`
