"""
Backtest framework (P2c) — standalone replay of bot logic on Polymarket
historical data.

Modules:
  - fetcher:      one-shot fetch CLOB /prices-history + Gamma /markets → SQLite
  - simulator:    replay quoter + toxicity + midpoint guard on mid tape
  - metrics:      Q-score, PnL, Sharpe, adverse-selection
  - grid_search:  iterate parameter grid, write CSV
  - run:          CLI entry point

This package is standalone: NO imports from main.py, NO live CLOB connections.
It reads config/settings.py and config/markets_filter.py read-only.
"""
