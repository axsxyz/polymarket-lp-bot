# LP Bot — Active Tasks

## P2 — Prochain chantier (2-3 semaines)
- [ ] Dynamic spread basé vol réalisée → `core/quoter.py` (ref ROADMAP P5-6)
- [ ] Taker signal mensuel → `core/taker_signal.py` (~1 trade/mois, $20-50, marché aléatoire)

## P3 — Chantiers structurants

### Tier 1 — Impact 2-3× sur les retours
- [ ] **`core/toxicity_analyzer.py`** — Adverse selection defense
  - VPIN sur fenêtre glissante : buy/sell imbalance → widen spread si élevé
  - Fill-vs-mid : comparer fill price vs mid à +5s / +30s / +5min → score de toxicité continu
  - Corrélation marchés liés (e.g. Trump 2028 ↔ Republican 2028) → widening coordonné
  - Remplace le `_fill_rates` blunt instrument actuel

- [ ] **`core/resolution_guard.py`** — Resolution risk manager
  - Auto-delist si mid sort d'une bande configurable (> 15% en 24h)
  - Max-loss calculator par marché : `max(yes_size, no_size) × 1.0` — pause si > seuil
  - Intégration calendrier événements (elections, fed meetings) → widen avant dates clés

- [ ] **`backtest/`** — Backtesting framework
  - Replay des snapshots orderbook historiques via Gamma `/trades` API
  - Simulation de fill probability par placement
  - Calcul Q-score réalisé vs théorique max, PnL, Sharpe
  - Grid search sur `tightness_factor`, `offset_ticks`, `size_multiplier`

### Tier 2 — Impact 1.5-2× sur les retours
- [ ] **Market-specific strategy profiles** — tightness adaptée par marché
- [ ] **Multi-level quoting** — L1 tight/small, L2 medium, L3 wide/large per token
- [ ] **Cross-market hedging** — matrice de corrélation ; offsetter YES marché A avec NO marché B

## Complété

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