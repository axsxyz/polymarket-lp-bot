# LP Bot — Active Tasks

## P2a — Dynamic spread (priorité immédiate)
- [ ] **Dynamic spread basé vol réalisée** → `core/quoter.py`
  - Rolling vol window 15-30min sur WS price history par marché
  - Spread élargi si vol élevée, resserré si stable
  - Remplace le spread quasi-statique actuel (`QUOTE_OFFSET_TICKS` fixe)
  - Paramètres configurables : `VOL_WINDOW_SECONDS`, `VOL_SPREAD_MULTIPLIER_MIN/MAX`
  - Ref ROADMAP P5-6 — dynamic per-market tightness

## P2b — Toxicity analyzer (priorité critique)
- [ ] **`core/toxicity_analyzer.py`** — Adverse selection defense
  - **VPIN** : buy/sell imbalance sur fenêtre glissante → widen spread si élevé
  - **Fill-vs-mid** : comparer fill price vs mid à +5s / +30s / +5min → score de toxicité continu
  - Remplace `_fill_rates` blunt instrument actuel dans `core/quoter.py`
  - Seuils configurables : `TOXICITY_VPIN_THRESHOLD`, `TOXICITY_FILL_WINDOW_SECONDS`
  - *(corrélation cross-market déplacée en P4 — complexité élevée, gain marginal)*

## P2c — Backtest framework (validation params)
- [ ] **`backtest/`** — Backtesting framework sur données historiques Gamma
  - Fetch historique via Gamma `/trades` API
  - Replay fills simulés par placement d'ordre
  - Calcul Q-score réalisé vs théorique max, PnL, Sharpe
  - Grid search : `tightness_factor`, `offset_ticks`, `size_multiplier`, `VOL_WINDOW_SECONDS`
  - Objectif : valider empiriquement tous les paramètres actuellement arbitraires
  - Ref ROADMAP P5-1 — levier le plus haut pour optimisation params

## P2d — Resolution guard complet
- [ ] **`core/resolution_guard.py`** — Resolution risk manager
  - Max-loss calculator par marché : `max(yes_size, no_size) × 1.0` → pause si > seuil
  - Auto-delist si mid move > 15% en 24h (distinct du midpoint guard cycle-based)
  - Paramètres : `RESOLUTION_MAX_LOSS_USD`, `RESOLUTION_MAX_MOVE_24H_PCT`
  - *(calendrier événements déplacé en P4 — intégration externe complexe)*

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