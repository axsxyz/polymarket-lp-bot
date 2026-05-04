# LP Bot — Active Tasks

## P0 — Cette semaine (quick wins audit)
- [x] API_RATE_LIMIT_RPS=1.5 → `.env` + `config/settings.py:66`
- [x] Jitter cycle → `main.py:977`
- [x] MAX_MARKETS=15 → `.env`
- [x] PREFERRED_TAGS enrichis → `.env`
- [x] pip freeze → `requirements.txt`

## P1 — Semaine prochaine
- [ ] Drawdown daily circuit breaker → `core/inventory.py` + `main.py`
- [ ] Midpoint guard continu [0.18, 0.82] → `core/quoter.py:141`
- [ ] Fix cost basis seed → `main.py:866-876`
- [ ] Tracker volume quoted notional → `core/order_manager.py:617`

## P2 — Dans 2-3 semaines
- [ ] Backtest framework Gamma `/trades` API
- [ ] Dynamic spread basé vol réalisée → `core/quoter.py`
- [ ] Taker signal mensuel → `core/taker_signal.py`

## Complété
- [x] 2026-05-04 — `API_RATE_LIMIT_RPS=1.5` → `.env:61` + `config/settings.py:66` (default 3.0→1.5) — aligne sur limite CLOB ~1.67 RPS
- [x] 2026-05-04 — Jitter cycle → `main.py:978` + `import random` — anti-fingerprint ±2s
- [x] 2026-05-04 — `MAX_MARKETS=15` → `.env:35` (était 3) — breadth airdrop
- [x] 2026-05-04 — `PREFERRED_TAGS` enrichis → `.env:36` — ajout sports,crypto,ai,science,entertainment,culture
- [x] 2026-05-04 — `requirements.txt` → pip freeze complet (py_clob_client==0.34.6, web3==7.14.1, etc.)