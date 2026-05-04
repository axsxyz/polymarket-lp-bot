# LP Bot Polymarket — Memory Bank

Document de référence courte pour les sessions agent. Dernière mise à jour : **2026-05-04**.

---

## 1. État actuel du projet

### Stack

- **Langage** : Python 3.12, asyncio (single process).
- **Dépendances clés** : `py-clob-client==0.34.6`, `web3==7.14.1`, `aiohttp==3.13.3`, `websockets==15.0.1`, `loguru==0.7.3`, `pydantic-settings==2.13.1`.
- **Structure** : `main.py` (~1003 LOC orchestrateur), `core/` (client, quoter, order_manager, inventory, fill_collector, scoring_monitor, rewards_tracker, ws_feed, market_manager, inventory_seeder), `config/` (settings, markets_filter), `utils/`, `scripts/`, `tests/`.
- **Entrée** : `python main.py`.

### Capital & mode (Stage 1 — vérifier `.env` local)

| Paramètre | Valeur |
|-----------|--------|
| `TOTAL_CAPITAL` | 300 USDC |
| `MAX_MARKETS` | **15** (était 3 avant 2026-05-04) |
| `MAX_CAPITAL_PER_MARKET` | 80 |
| `DRY_RUN` | `false` (live) |
| `MODE` | `safe` |
| `REBALANCE_INTERVAL_SECONDS` | 10 |
| `WS_ENABLED` | `true` |
| `API_RATE_LIMIT_RPS` | **1.5** (était 5.0 avant 2026-05-04) |

### Stage produit

- **Stage 1** : validation live minimale (capital réduit, marchés élargis à 15), aligné ROADMAP P5-5.
- **Avant scaling** (> ~1k USDC) : implémenter le drawdown daily circuit breaker en **P1 prioritaire**.

---

## 2. Bugs connus (fichier:ligne → problème)

| Fichier:ligne | Problème | Statut |
|---------------|----------|--------|
| `main.py:866-876` | `pos.yes_cost` / `pos.no_cost` initialisés avec `bal * mid` au lieu du prix seed réel → PnL biaisé | NON FIXÉ |
| `rewards_tracker.py:274` | `estimated_rewards_usd / 10.0` — hardcodé, non fondé sur la concurrence réelle | NON FIXÉ |
| `config/settings.py:66` | `API_RATE_LIMIT_RPS` dépassait le plafond CLOB (~1.67 RPS) | **FIXÉ 2026-05-04** : 5.0 → 1.5 |
| `main.py:977` | Sleep cycle sans jitter → fingerprint déterministe | **FIXÉ 2026-05-04** : `+random.uniform(-2.0, 2.0)` |
| *(aucun fichier)* | Pas de **circuit breaker drawdown journalier** | NON FIXÉ — **PROCHAIN P1** |

---

## 3. Changements effectués (session 2026-05-04)

| Fichier | Changement |
|---------|------------|
| `.env:61` + `config/settings.py:66` | `API_RATE_LIMIT_RPS` : `5.0` → `1.5` |
| `main.py:20` | `import random` ajouté |
| `main.py:978` | `sleep_time += random.uniform(-2.0, 2.0)` — jitter anti-fingerprint |
| `.env:35` | `MAX_MARKETS` : `3` → `15` — breadth airdrop |
| `.env:36` | `PREFERRED_TAGS` ajouté : politics + election + fed + geopolitics + midterms + 2026 + 2028 + **sports + crypto + ai + science + entertainment + culture** |
| `requirements.txt` | `pip freeze` complet depuis `.venv` (versions épinglées) |

---

## 4. Décisions d'architecture

- **Python / asyncio**, pas Go : SDK officiel synchrone enveloppé dans `asyncio.to_thread()`, pas de binaire séparé.
- **Règles Cursor** (workspace) : `.cursor/rules/01-core-behavior.mdc`, `02-architecture.mdc`, `03-polymarket-api.mdc`, `04-airdrop-priority.mdc`.
- **Configuration** : une seule classe `Settings` dans `config/settings.py` — ne pas inventer de variables d'environnement absentes de ce fichier.
- **`DRY_RUN`** : ne passer à `false` ou modifier le comportement live **que** sur instruction explicite du propriétaire du wallet.
- **Invariants prod** : conserver `cancel_all_orders` au démarrage (~815 `main.py`), handler SIGTERM / shutdown gracieux, logging via loguru (pas `print()`).

---

## 5. Prochain chantier prioritaire (P1)

1. **URGENT — Drawdown daily circuit breaker** (`core/inventory.py` + `main.py`) : pause globale + alerte Discord si PnL journalier dépasse un seuil configurable (ex. `DAILY_DRAWDOWN_LIMIT_PCT=5`) vs `TOTAL_CAPITAL`. **Blocker pour scaler > 1000 USDC.**
2. **Midpoint guard continu [0.18, 0.82]** (`core/quoter.py:141`) : pause marché si `live_mid` sort de la bande sur N cycles consécutifs.
3. **Fix cost basis seed** (`main.py:866-876`) : utiliser le prix réel du seed order, pas `bal * mid`.
4. **Tracker volume quoted notional** (`core/order_manager.py:617`) : incrémenter `price * size` sur chaque POST `/order` réussi, persister dans `rewards_tracker.py`.

---

## 6. Liens utiles

- ROADMAP détail : `ROADMAP.md`
- README opérationnel : `README.md`
- Graph Memory MCP : entités `Polymarket LP Farmer Bot`, `P0 Quick Wins May 2026`, `Known Bugs`, `Next Priority Work`
