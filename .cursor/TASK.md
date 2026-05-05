# LP Bot — Active Tasks

## P0 — Cette semaine (quick wins audit)
- [x] API_RATE_LIMIT_RPS=1.5 → `.env` + `config/settings.py:66`
- [x] Jitter cycle → `main.py:977`
- [x] MAX_MARKETS=20 → `.env` (Appendix A ROADMAP : sweet spot 20 marchés à 45s)
- [x] REBALANCE_INTERVAL_SECONDS=45 → `.env` (Appendix A ROADMAP)
- [x] PREFERRED_TAGS enrichis → `.env`
- [x] pip freeze → `requirements.txt`

## P1 — Semaine prochaine
- [x] Tous les items P1 sont terminés le 2026-05-05 (détails dans `## Complété`)

## P2 — Prochain chantier (2-3 semaines)
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
- [x] 2026-05-05 — Tracker volume quoted notional → `core/order_manager.py:_session_quoted_notional+drain_quoted_notional` + `main.py:639,1072` + `core/rewards_tracker.py:DailySnapshot+record_cycle_stats+_save/_load_airdrop_metrics+format_summary` — `_session_quoted_notional` (price×size par POST réussi, dry-run inclus), drainé à chaque cycle froid ; `record_cycle_stats` reçoit maintenant `volume_usd=quoted` + `filled_usd=volume` ; `airdrop_metrics.json` persiste `total_quoted_notional` + `total_filled_usd` cumulatifs ; Discord daily summary affiche "Volume quoted today" + "Volume filled today" séparément (87/87 pytest OK)
- [x] 2026-05-05 — Fix cost basis seed → `core/inventory_seeder.py:_seed_market+seed_all` + `main.py:959-972` — `seed_all` retourne maintenant `(failed_cids, seed_costs)` avec le vrai USD dépensé par side ; inventory sync utilise `seed_costs[cid]["yes/no"]` si disponible, fallback `bal * mid` uniquement pour tokens pré-existants (run précédent). 3 tests `TestSeedCostBasis` (29/29 pytest OK)
- [x] 2026-05-05 — Midpoint guard continu `[0.18, 0.82]` → `core/quoter.py:108-145` + `main.py:201-203,413-466` + `config/settings.py:84-89` — pause par marché si `live_mid` hors bande pendant `MIDPOINT_GUARD_CYCLES=3` cold cycles, reprise auto au 1er retour in-band, alerte Discord pause+resume, gate hot path, 8 tests `TestMidpointGuard` (84/84 pytest OK), `.env` + `.env.example` mis à jour
- [x] 2026-05-05 — Drawdown daily circuit breaker → `core/inventory.py:344` + `main.py:328` (commit `76ec51f`) — pause globale + alerte Discord si `realised_pnl_today < -DAILY_DRAWDOWN_LIMIT_PCT × TOTAL_CAPITAL` (default 5%), reset à minuit UTC, gate hot+cold path, `cancel_all_orders` au déclenchement, 5 tests unitaires (`TestDailyDrawdown`)
- [x] 2026-05-05 — `fix(settings)` `NoDecode` + `extra="ignore"` → `config/settings.py:12,28,81` (commit `f531129`) — débloque pytest : `PREFERRED_TAGS` CSV n'était plus parsé en JSON par pydantic-settings 2.x, et les `DASHBOARD_*` legacy du `.env` ne cassent plus l'init (dashboard fantôme, cf. `.cursor/rules/02-architecture.mdc`)
- [x] 2026-05-04 — `API_RATE_LIMIT_RPS=1.5` → `.env:61` + `config/settings.py:66` (default 3.0→1.5) — aligne sur limite CLOB ~1.67 RPS
- [x] 2026-05-04 — Jitter cycle → `main.py:978` + `import random` — anti-fingerprint ±2s
- [x] 2026-05-04 — `MAX_MARKETS=20` → `.env:35` (était 3→15→20) — sweet spot ROADMAP Appendix A
- [x] 2026-05-04 — `REBALANCE_INTERVAL_SECONDS=45` → `.env:25` (était 10) — Appendix A : 20 marchés × 5 calls ≤ 58 calls/cycle à 1.5 RPS
- [x] 2026-05-04 — `PREFERRED_TAGS` enrichis → `.env:36` — ajout sports,crypto,ai,science,entertainment,culture
- [x] 2026-05-04 — `requirements.txt` → pip freeze complet (py_clob_client==0.34.6, web3==7.14.1, etc.)
