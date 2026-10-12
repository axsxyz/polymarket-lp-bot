"""Discord channel allowlist (DISCORD_CHANNELS / set_channels)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from config.settings import (
    DISCORD_CHANNEL_KINDS,
    Settings,
    parse_discord_channels,
)
from utils.discord import DiscordNotifier


def _make_settings(**overrides) -> Settings:
    defaults = {
        "PRIVATE_KEY": "0x" + "ab" * 32,
        "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/test/token",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _notifier(tmp_path, **overrides) -> DiscordNotifier:
    """Notifier that never touches the real data/discord_channels.json."""
    return DiscordNotifier(
        _make_settings(**overrides),
        persist_path=tmp_path / "discord_channels.json",
    )


class TestParseDiscordChannels:
    def test_default_all_known(self):
        s = _make_settings()
        assert s.discord_channels == set(DISCORD_CHANNEL_KINDS)

    def test_subset(self):
        assert parse_discord_channels("fills, errors") == {"fills", "errors"}

    def test_empty_means_none(self):
        assert parse_discord_channels("") == set()
        assert parse_discord_channels("   ") == set()

    def test_unknown_tokens_dropped(self):
        assert parse_discord_channels("fills,bogus,orders") == {"fills", "orders"}


class TestDiscordChannelGate:
    def test_send_methods_pass_expected_kind(self, tmp_path):
        async def run() -> None:
            n = _notifier(tmp_path)
            n._post = AsyncMock()  # type: ignore[method-assign]
            await n.send_startup(3, 100.0, "safe")
            await n.send_shutdown("test")
            await n.send_order_posted("cid", "BID", "YES", 0.4, 10.0, "oid")
            await n.send_cycle_summary(1, 2, 1, 0, 1.0)
            await n.send_fill("cid", "BID", 0.5, 10.0)
            await n.send_skew_warning("cid", 0.4, "YES")
            await n.send_skew_pause("cid", 0.8, "YES", 60)
            await n.send_status_dashboard(50, 100, 0.9, 2, 1, 0.0, "ok")
            await n.send_daily_summary("day")
            await n.send_error("boom")
            await n.send_alert("t", "m")
            kinds = [c.kwargs["kind"] for c in n._post.await_args_list]
            assert kinds == [
                "startup",
                "startup",
                "orders",
                "cycle",
                "fills",
                "skew",
                "skew",
                "cycle",
                "daily",
                "errors",
                "alerts",
            ]

        asyncio.run(run())

    def test_channel_off_skips_http(self, tmp_path):
        async def run() -> None:
            n = _notifier(tmp_path, DISCORD_CHANNELS="errors")
            with patch("aiohttp.ClientSession") as sess_cls:
                await n.send_fill("cid", "BID", 0.5, 10.0)
                sess_cls.assert_not_called()

            mock_sess = MagicMock()
            mock_resp = AsyncMock()
            mock_resp.status = 204
            mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_resp.__aexit__ = AsyncMock(return_value=False)
            mock_sess.post = MagicMock(return_value=mock_resp)
            mock_sess.closed = False
            n._session = mock_sess
            await n.send_error("boom")
            mock_sess.post.assert_called_once()

        asyncio.run(run())

    def test_set_channels_hot_swap(self, tmp_path):
        n = _notifier(tmp_path, DISCORD_CHANNELS="fills,errors")
        assert n.channel_enabled("fills")
        n.set_channels(["errors", "bogus"])
        assert not n.channel_enabled("fills")
        assert n.channel_enabled("errors")

    def test_persist_survives_restart(self, tmp_path):
        path = tmp_path / "discord_channels.json"
        n1 = DiscordNotifier(
            _make_settings(DISCORD_CHANNELS="fills,orders,cycle"),
            persist_path=path,
        )
        n1.set_channels(["errors", "skew"])
        assert path.is_file()
        n2 = DiscordNotifier(
            _make_settings(DISCORD_CHANNELS="fills,orders,cycle"),
            persist_path=path,
        )
        assert n2.channels == {"errors", "skew"}
        assert not n2.channel_enabled("fills")

    def test_send_alert_respects_alerts_channel(self, tmp_path):
        async def run() -> None:
            n = _notifier(tmp_path, DISCORD_CHANNELS="errors")
            n._post = AsyncMock()  # type: ignore[method-assign]
            await n.send_alert("t", "m")
            n._post.assert_not_awaited()

            n.set_channels(["alerts"])
            await n.send_alert("t", "m")
            n._post.assert_awaited_once()
            assert n._post.await_args.kwargs["kind"] == "alerts"

        asyncio.run(run())


class TestDiscordHttpToggles:
    def test_get_and_patch_via_tape_app(self, tmp_path):
        from fastapi.testclient import TestClient

        from core.tape import LocalTape
        from utils.tape_server import create_tape_app

        tape = LocalTape(path=tmp_path / "tape.jsonl")
        n = _notifier(tmp_path, DISCORD_CHANNELS="fills,errors")
        app = create_tape_app(tape, discord=n)
        client = TestClient(app)

        g = client.get("/discord/channels")
        assert g.status_code == 200
        body = g.json()
        assert body["webhook"] is True
        assert body["enabled"]["fills"] is True
        assert body["enabled"]["orders"] is False

        p = client.patch("/discord/channels", json={"channels": ["errors", "skew"]})
        assert p.status_code == 200
        out = p.json()
        assert out["channels"] == ["errors", "skew"]
        assert n.channel_enabled("errors")
        assert n.channel_enabled("skew")
        assert not n.channel_enabled("fills")
        assert (tmp_path / "discord_channels.json").is_file()

        assert client.post("/fills").status_code == 405

    def test_patch_without_discord_is_503(self, tmp_path):
        from fastapi.testclient import TestClient

        from core.tape import LocalTape
        from utils.tape_server import create_tape_app

        tape = LocalTape(path=tmp_path / "tape.jsonl")
        client = TestClient(create_tape_app(tape))
        r = client.patch("/discord/channels", json={"channels": ["errors"]})
        assert r.status_code == 503
