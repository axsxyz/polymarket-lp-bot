# LP Bot Polymarket — Memory Bank

Document de référence courte pour les sessions agent. Dernière mise à jour : **2026-05-03**.

---

## 1. État actuel du projet

### Stack

- **Langage** : Python 3.12, asyncio (single process).
- **Dépendances clés** : `py-clob-client>=0.34`, `web3>=7`, `aiohttp`, `websockets`, `loguru`, `pydantic-settings`.
- **Structure** : `main.py` (~1000 LOC orchestrateur), `core/` (client, quoter, order_manager, inventory, fill_collector, scoring_monitor, rewards_tracker, ws_feed, market_manager, inventory_seeder), `config/` (settings, markets_filter), `utils/`, `scripts/`, `tests/`.
- **Entrée** : `python main.py`.

### Capital & mode (exemple Stage 1 — vérifier `.env` local)

| Paramètre | Valeur typique Stage 1 |
|-----------|-------------------------|
| `TOTAL_CAPITAL` | 300 USDC |
| `MAX_MARKETS` | 3 |
| `MAX_CAPITAL_PER_MARKET` | 80 |
| `DRY_RUN` | `false` (live) |
| `MODE` | `safe` |
| `REBALANCE_INTERVAL_SECONDS` | 10 |
| `WS_ENABLED` | `true` |

### Stage produit

- **Stage 1** : validation live minimale (capital réduit, peu de marchés), aligné ROADMAP P5-5.
- **Avant scaling** (> ~1k USDC) : traiter les bugs / risques listés ci-dessous (drawdown journalier, garde-fou midpoint, rate limit, cost basis).

---

## 2. Bugs connues (fichier:ligne → problème)

| Fichier:ligne | Problème |
|---------------|----------|
| `main.py:866-876` | Sync inventaire : `pos.yes_cost` / `pos.no_cost` initialisés avec `bal * mid` au lieu du prix de seed réel → coût moyen et PnL réalisé biaisés. |
| `rewards_tracker.py:274` | Part LP estimée : `estimated_rewards_usd / 10.0` — hypothèse «~1/10 LPs» hardcodée, non fondée sur la concurrence. |
| `config/settings.py:66` | `API_RATE_LIMIT_RPS` peut dépasser la limite documentée CLOB (~1.67 RPS / 100 req/10s) si `.env` monte à 5.0 → risque 429. |
| `main.py:977` | Sommeil entre cycles sans jitter → empreinte temporelle déterministe (fingerprint / Sybil heuristique). |
| *(aucun fichier)* | Pas de **circuit breaker drawdown journalier** sur le PnL ; seuls CB par marché (erreurs post) + garde-fous skew / exposure. |

---

## 3. Décisions d’architecture

- **Python / asyncio**, pas Go : SDK officiel synchrone enveloppé dans `asyncio.to_thread()`, pas de binaire séparé.
- **Règles Cursor** (workspace) : `.cursor/rules/01-core-behavior.mdc`, `02-architecture.mdc`, `03-polymarket-api.mdc`, `04-airdrop-priority.mdc`.
- **Configuration** : une seule classe `Settings` dans `config/settings.py` — ne pas inventer de variables d’environnement absentes de ce fichier.
- **`DRY_RUN`** : ne passer à `false` ou modifier le comportement live **que** sur instruction explicite du propriétaire du wallet.
- **Invariants prod** : conserver `cancel_all_orders` au démarrage (~815 `main.py`), handler SIGTERM / shutdown gracieux, logging via loguru (pas `print()`).

---

## 4. Prochain chantier prioritaire

1. **P0 — Sécurité capital** : circuit breaker **drawdown journalier** (pause + alerte si PnL réalisé ou marqueur de risque dépasse un seuil vs `TOTAL_CAPITAL`).
2. **P0 — Résolution / drift** : garde-fou **midpoint continu** (ex. pause marché si `live_mid` sort d’une bande sûre sur N cycles), en complément des filtres statiques à la sélection (`MIN_END_DATE_DAYS`, `MIDPOINT_MIN` / `MAX`).
3. **P1 — Ops & conformité API** : aligner `API_RATE_LIMIT_RPS` avec la doc Polymarket + **jitter** sur le sleep de cycle (`main.py:977`).
4. **P1 — Comptabilité** : corriger le cost basis au sync startup (`main.py:866-876`) ; remplacer ou documenter clairement le `/10` dans `rewards_tracker.py:274`.

---

## 5. Liens utiles

- ROADMAP détaillé : `ROADMAP.md`
- README opérationnel : `README.md`
- Graphe Memory MCP : entité racine `Polymarket LP Farmer Bot` + relations `has_known_bug`, `follows_architecture_decisions`, `targets_next_priority_work`
