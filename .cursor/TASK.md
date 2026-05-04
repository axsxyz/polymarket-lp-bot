# LP Bot — Active Tasks

## P0 — Cette semaine (quick wins audit)
- [x] API_RATE_LIMIT_RPS=1.5 → `.env` + `config/settings.py:66`
- [x] Jitter cycle → `main.py:977`
- [x] MAX_MARKETS=20 → `.env` (Appendix A ROADMAP : sweet spot 20 marchés à 45s)
- [x] REBALANCE_INTERVAL_SECONDS=45 → `.env` (Appendix A ROADMAP)
- [x] PREFERRED_TAGS enrichis → `.env`
- [x] pip freeze → `requirements.txt`

## P1 — Semaine prochaine

### Drawdown daily circuit breaker → `core/inventory.py` + `main.py`
**Critères d'acceptance :**
- `DAILY_DRAWDOWN_LIMIT_PCT` ajouté à `config/settings.py` (default `0.05` = 5 % du capital)
- Si `realised_pnl_today < -(DAILY_DRAWDOWN_LIMIT_PCT × TOTAL_CAPITAL)` → pause globale + alerte Discord
- Compteur remis à zéro à minuit UTC (aligné sur le snapshot `RewardsTracker`)
- Pause n'empêche pas les annulations (shutdown reste fonctionnel)
- Setting configurable via `.env` — pas de valeur hardcodée
- **Bloquant pour tout scaling > 1 000 USDC**

### Midpoint guard continu [0.18, 0.82] → `core/quoter.py:141`
**Critères d'acceptance :**
- Si `live_mid < 0.18` ou `live_mid > 0.82` pendant 3 cycles consécutifs → pause du marché + alerte Discord (condition_id + mid actuel)
- Seuils configurables (`MIDPOINT_GUARD_MIN`, `MIDPOINT_GUARD_MAX`, `MIDPOINT_GUARD_CYCLES`)
- Reprise automatique quand le mid revient dans la bande
- Pas de régression sur les filtres statiques de sélection (`MIDPOINT_MIN`/`MIDPOINT_MAX`)
- (Ref ANALYSIS.md Tier 1 — Resolution Risk Manager)

### Fix cost basis seed → `main.py:866-876`
**Critères d'acceptance :**
- `pos.yes_cost` / `pos.no_cost` initialisés avec le prix réel du seed order, pas `bal * mid`
- Test : buy YES à 0.40 → sell à 0.60 → `realised_pnl = +$0.20/share`
- Pas de régression sur `inventory.py:248-285` (skew tiers)
- (Ref ROADMAP P2-2)

### Tracker volume quoted notional → `core/order_manager.py` (autour ligne 617)
**Critères d'acceptance :**
- Compteur `total_quoted_notional` incrémenté de `price × size` à chaque `POST /order` réussi
- Persisté dans `data/airdrop_metrics.json` via `RewardsTracker`
- Visible dans le daily Discord summary (section "Airdrop Metrics")
- (Ref ROADMAP P5-4)

## P2 — Dans 2-3 semaines
- [ ] Dynamic spread basé vol réalisée → `core/quoter.py` (ref ROADMAP P5-6 — dynamic per-market tightness)
- [ ] Taker signal mensuel → `core/taker_signal.py` (~1 trade/mois, $20-50, marché aléatoire)

## P3 — Chantiers structurants (ANALYSIS.md)

### Tier 1 — Impact 2-3× sur les retours

- [ ] **`core/toxicity_analyzer.py`** — Adverse selection defense
  - VPIN sur fenêtre glissante : buy/sell imbalance → widen spread si élevé
  - Fill-vs-mid : comparer fill price vs mid à +5s / +30s / +5min → score de toxicité continu
  - Corrélation marchés liés (e.g. Trump 2028 ↔ Republican 2028) → widening coordonné
  - Remplace le `_fill_rates` blunt instrument actuel

- [ ] **`core/resolution_guard.py`** — Resolution risk manager
  - Ongoing midpoint monitoring : auto-delist si mid sort d'une bande configurable (> 15 % en 24 h)
  - Max-loss calculator par marché : `max(yes_size, no_size) × 1.0` — pause si > seuil
  - Intégration calendrier événements (elections, fed meetings) → widen avant dates clés

- [ ] **`backtest/`** — Backtesting framework
  - Replay des snapshots orderbook historiques via Gamma `/trades` API
  - Simulation de fill probability par placement
  - Calcul Q-score réalisé vs théorique max, PnL, Sharpe
  - Grid search sur `tightness_factor`, `offset_ticks`, `size_multiplier`
  - (Cf. ROADMAP P5-1 — le plus haut levier d'optimisation des paramètres)

### Tier 2 — Impact 1.5-2× sur les retours

- [ ] **Market-specific strategy profiles** — tightness adaptée par marché (volume / volatilité / concurrence)
- [ ] **Multi-level quoting** — 2-3 niveaux (L1 tight/small, L2 medium, L3 wide/large) per token
- [ ] **Cross-market hedging** — matrice de corrélation ; offsetter YES marché A avec NO marché B

## Complété
- [x] 2026-05-04 — `API_RATE_LIMIT_RPS=1.5` → `.env:61` + `config/settings.py:66` (default 3.0→1.5) — aligne sur limite CLOB ~1.67 RPS
- [x] 2026-05-04 — Jitter cycle → `main.py:978` + `import random` — anti-fingerprint ±2s
- [x] 2026-05-04 — `MAX_MARKETS=20` → `.env:35` (était 3→15→20) — sweet spot ROADMAP Appendix A
- [x] 2026-05-04 — `REBALANCE_INTERVAL_SECONDS=45` → `.env:25` (était 10) — Appendix A : 20 marchés × 5 calls ≤ 58 calls/cycle à 1.5 RPS
- [x] 2026-05-04 — `PREFERRED_TAGS` enrichis → `.env:36` — ajout sports,crypto,ai,science,entertainment,culture
- [x] 2026-05-04 — `requirements.txt` → pip freeze complet (py_clob_client==0.34.6, web3==7.14.1, etc.)
