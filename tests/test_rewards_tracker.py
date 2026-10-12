"""Daily rewards snapshot + Discord summary formatting."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from config.settings import Settings
from core.inventory import InventoryTracker
from core.rewards_tracker import DailySnapshot, RewardsTracker


def _make_settings(**overrides) -> Settings:
    defaults = {
        "PRIVATE_KEY": "0x" + "ab" * 32,
        "TOTAL_CAPITAL": 10_000.0,
        "DRY_RUN": True,
        "LP_ESTIMATED_SHARE": 0.10,
        "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/test/token",
    }
    defaults.update(overrides)
    return Settings(**defaults)


class TestDiscordRunLabel:
    def test_username_dry_vs_live(self, tmp_path):
        from utils.discord import DiscordNotifier

        dry = DiscordNotifier(
            _make_settings(DRY_RUN=True),
            persist_path=tmp_path / "a.json",
        )
        live = DiscordNotifier(
            _make_settings(DRY_RUN=False),
            persist_path=tmp_path / "b.json",
        )
        assert dry.webhook_username == "qv · DRY_RUN"
        assert live.webhook_username == "qv · LIVE"
        assert dry.run_label == "DRY_RUN"
        assert live.run_label == "LIVE"


class TestRewardsPoolAndSummary:
    def test_pool_from_selected_markets(self):
        markets = [
            SimpleNamespace(condition_id="a", reward_rate_daily=100.0),
            SimpleNamespace(condition_id="b", reward_rate_daily=50.0),
            SimpleNamespace(condition_id="c", reward_rate_daily=0.0),
        ]
        pool = RewardsTracker._pool_from_markets(markets)
        assert pool == {"a": 100.0, "b": 50.0}

    def test_collect_snapshot_uses_market_rates_not_gamma(self):
        s = _make_settings()
        inv = InventoryTracker(s)
        rt = RewardsTracker(client=None, settings=s, inventory=inv)  # type: ignore[arg-type]
        markets = [
            SimpleNamespace(condition_id="m1", reward_rate_daily=200.0),
            SimpleNamespace(condition_id="m2", reward_rate_daily=100.0),
        ]

        async def run() -> None:
            snap = await rt.collect_daily_snapshot(
                markets=markets,
                scored_market_count=2,
                scoring_pct=1.0,
                open_order_notional=0.0,
            )
            assert snap.estimated_rewards_usd == 300.0
            assert snap.markets_active == 2
            assert snap.dry_run is True
            text = rt.format_summary(snap)
            assert "Selected markets pool" in text
            assert "$300.00" in text
            assert "Est. our share" in text
            assert "$30.00" in text  # 10% of 300
            assert "Net exposure" in text
            assert "Gross deployed" in text
            assert "DRY_RUN" in text

        asyncio.run(run())

    def test_format_summary_labels(self):
        s = _make_settings(DRY_RUN=False)
        inv = InventoryTracker(s)
        rt = RewardsTracker(client=None, settings=s, inventory=inv)  # type: ignore[arg-type]
        snap = DailySnapshot(
            date="2026-08-30",
            estimated_rewards_usd=100.0,
            realised_pnl_usd=-1.0,
            total_fills=3,
            markets_active=5,
            global_exposure_usd=4.5,
            gross_exposure_usd=120.0,
            dry_run=False,
        )
        text = rt.format_summary(snap)
        assert "LIVE" in text
        assert "Net exposure (unmatched YES/NO)" in text
        assert "$4.50" in text
        assert "Gross deployed" in text
        assert "$120.00" in text
        assert "Global exposure:" not in text
