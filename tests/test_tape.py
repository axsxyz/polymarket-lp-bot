"""Tests for LocalTape JSONL schema and read-only FastAPI routes."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from core.operator_digest import OperatorDigest, explain_idle
from core.order_manager import FillEvent
from core.tape import LocalTape
from utils.tape_server import create_tape_app


def test_append_and_readback(tmp_path):
    tape = LocalTape(path=tmp_path / "tape.jsonl")
    fill = FillEvent(
        token_id="tok",
        condition_id="cid1",
        side="BID",
        price=0.42,
        filled_size=10.0,
        timestamp=datetime.now(timezone.utc),
        trade_id="t1",
        mid_at_fill=0.50,
    )
    tape.fill(
        condition_id=fill.condition_id,
        token_id=fill.token_id,
        side=fill.side,
        price=fill.price,
        filled_size=fill.filled_size,
        mid_at_fill=fill.mid_at_fill,
        trade_id=fill.trade_id,
    )
    tape.post(condition_id="cid1", token_id="tok", side="BID", price=0.41, size=5, order_id="o1")
    tape.cancel(condition_id="cid1", token_id="tok", side="BID", order_id="o1")
    tape.skip("cid1", "toxicity_paused")
    tape.exception("hot_requote", RuntimeError("boom"))
    tape.snapshot_row(
        cid="cid1", mid=0.50, bid=0.49, ask=0.51,
        yes=10.0, no=10.0, realised=1.25, mark_to_mid=10.0,
    )
    events = tape.read_events(limit=50)
    types = [e["type"] for e in events]
    assert types == ["fill", "post", "cancel", "skip", "exception", "snapshot"]
    snap = events[-1]
    assert snap["cid"] == "cid1"
    assert snap["yes"] == 10.0
    assert snap["realised"] == 1.25
    assert snap["mark_to_mid"] == 10.0
    fill_ev = events[0]
    assert fill_ev["mid_at_fill"] == 0.50
    assert fill_ev["trade_id"] == "t1"
    assert fill_ev["seq"] == 1


def test_yes_no_posts_do_not_overwrite_each_other(tmp_path):
    """YES then NO posts must keep separate bid/ask (terminal mid is YES)."""
    tape = LocalTape(path=tmp_path / "tape.jsonl")
    cid = "cid_vance"
    tape.post(
        condition_id=cid, token_id="yes_tok", side="BID",
        price=0.49, size=200, order_id="y1", outcome="yes",
    )
    tape.post(
        condition_id=cid, token_id="yes_tok", side="ASK",
        price=0.492, size=200, order_id="y2", outcome="yes",
    )
    tape.post(
        condition_id=cid, token_id="no_tok", side="BID",
        price=0.507, size=200, order_id="n1", outcome="no",
    )
    tape.post(
        condition_id=cid, token_id="no_tok", side="ASK",
        price=0.509, size=200, order_id="n2", outcome="no",
    )
    q = tape.last_quote(cid)
    assert q["yes_bid"] == 0.49
    assert q["yes_ask"] == 0.492
    assert q["no_bid"] == 0.507
    assert q["no_ask"] == 0.509
    assert q["bid"] == 0.49
    assert q["ask"] == 0.492
    posts = tape.read_events(types=["post"], limit=10)
    assert {p["outcome"] for p in posts} == {"yes", "no"}
    assert sum(1 for p in posts if p["outcome"] == "yes") == 2
    assert sum(1 for p in posts if p["outcome"] == "no") == 2


def test_remember_two_sided_fills_yes_and_no(tmp_path):
    from types import SimpleNamespace

    tape = LocalTape(path=tmp_path / "tape.jsonl")
    yes_q = SimpleNamespace(bid_price=0.49, bid_size=200, ask_price=0.492, ask_size=200, mid=0.4915)
    no_q = SimpleNamespace(bid_price=0.507, bid_size=200, ask_price=0.509, ask_size=200, mid=0.5085)
    tape.remember_two_sided("cid", mid=0.4915, yes_quote=yes_q, no_quote=no_q)
    q = tape.last_quote("cid")
    assert q["yes_bid"] == 0.49
    assert q["yes_ask"] == 0.492
    assert q["no_bid"] == 0.507
    assert q["no_ask"] == 0.509
    assert q["intended_bid"] == 0.49
    assert q["mid"] == 0.4915


def test_new_event_types(tmp_path):
    tape = LocalTape(path=tmp_path / "tape.jsonl")
    tape.universe(fetched=10, gamma_matched=3, gamma_status=422, pages=2)
    tape.filter(
        rejection_counts={"no end_date": 7},
        samples={"no end_date": [{"cid": "aa", "question": "Q"}]},
        selected=[{"cid": "bb", "question": "Sel", "mid": 0.5, "spread": 0.04,
                   "reward": 1.0, "min_size": 5, "alloc": 80.0, "tags": []}],
        ranked=4,
    )
    tape.seed(cid="bb", yes_cost=12.0, no_cost=11.0, cap=15.0, ok=False, dry_run=True, reason="cost_cap")
    tape.cycle(n=3, selected=5, active=0, posted=0, kept=0, errors=0, elapsed_s=1.2, utilization=0.0)
    tape.state(cid="bb", from_state="UNKNOWN", to_state="SEED_FAILED", reason="seed_failed")
    tape.skip("bb", "collateral", needed_usd=25.5, available_usd=24.0)
    types = [e["type"] for e in tape.read_events(limit=20)]
    assert types == ["universe", "filter", "seed", "cycle", "state", "skip"]
    skip = tape.read_events(types=["skip"], limit=1)[0]
    assert skip["reason"] == "collateral"
    assert skip["needed_usd"] == 25.5
    assert skip["available_usd"] == 24.0
    st = tape.read_events(types=["state"], limit=1)[0]
    assert st["from"] == "UNKNOWN"
    assert st["to"] == "SEED_FAILED"


def test_write_failure_does_not_raise(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    tape = LocalTape(path=blocked)
    tape.skip("c", "x")
    tape.fill(
        condition_id="c", token_id="t", side="BID",
        price=0.5, filled_size=1.0,
    )
    tape.universe(fetched=1, gamma_matched=0, gamma_status=422, pages=0)
    assert tape.ok is False


def test_events_since_ring(tmp_path):
    tape = LocalTape(path=tmp_path / "tape.jsonl")
    tape.skip("a", "one")
    tape.skip("b", "two")
    tape.skip("c", "three")
    rows = tape.events_since(since=1, limit=10)
    assert [r["cid"] for r in rows] == ["c", "b"]
    assert rows[0]["seq"] == 3


def test_fastapi_readonly(tmp_path):
    tape = LocalTape(path=tmp_path / "tape.jsonl")
    tape.fill(
        condition_id="c", token_id="t", side="ASK",
        price=0.6, filled_size=2.0, mid_at_fill=0.55, trade_id="x",
    )
    tape.snapshot_row(
        cid="c", mid=0.55, bid=0.54, ask=0.56,
        yes=2.0, no=0.0, realised=-0.10, mark_to_mid=1.1,
    )
    digest = OperatorDigest()
    digest.configure_from_settings(
        type("S", (), {
            "DRY_RUN": True, "MODE": "safe", "TOTAL_CAPITAL": 560.0,
            "REBALANCE_INTERVAL_SECONDS": 45, "WS_ENABLED": True,
        })(),
        tape_path=str(tape.path),
    )
    app = create_tape_app(tape, digest=digest)
    client = TestClient(app)
    h = client.get("/health")
    assert h.status_code == 200
    assert h.json()["ok"] is True
    fills = client.get("/fills?limit=10")
    assert fills.status_code == 200
    assert fills.json()[0]["type"] == "fill"
    pnl = client.get("/pnl")
    assert pnl.status_code == 200
    assert pnl.json()["realised"] == -0.10
    post = client.post("/fills")
    assert post.status_code == 405
    write = client.post("/control")
    assert write.status_code == 405
    assert write.json()["error"] == "read-only"
    root = client.get("/")
    assert root.status_code == 200
    assert b"qv/Polymarket" in root.content
    assert b"qv / Polymarket" in root.content
    assert b"RHVault" not in root.content
    js = client.get("/terminal.js")
    assert js.status_code == 200
    fav = client.get("/favicon.jpg")
    assert fav.status_code == 200
    assert fav.headers.get("content-type", "").startswith("image/jpeg")
    ico = client.get("/favicon.ico")
    assert ico.status_code == 200
    mark = client.get("/quietvault_pfp_3.jpg")
    assert mark.status_code == 200
    dg = client.get("/digest")
    assert dg.status_code == 200
    body = dg.json()
    assert body["header"]["name"] == "qv/Polymarket"
    assert body["header"]["dry_run"] is True
    assert "funnel" in body and "markets" in body and "pnl" in body
    ev = client.get("/events?since=0&limit=10")
    assert ev.status_code == 200
    assert "events" in ev.json()
    typed = client.get("/events?types=fill&limit=5")
    assert typed.json()["events"][0]["type"] == "fill"


def test_stream_route_exists(tmp_path):
    tape = LocalTape(path=tmp_path / "tape.jsonl")
    app = create_tape_app(tape)
    # Route is registered; consuming SSE blocks under TestClient.
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/stream" in paths


def test_explain_idle_collateral():
    msg = explain_idle(
        state="INACTIVE",
        skip_reason="collateral",
        seed_reason="",
        dry_run=True,
        needed_usd=25.0,
        available_usd=24.0,
    )
    assert "collateral" in msg
    assert "25.00" in msg


def test_digest_shape():
    d = OperatorDigest()
    d.configure_from_settings(
        type("S", (), {
            "DRY_RUN": True, "MODE": "safe", "TOTAL_CAPITAL": 100.0,
            "REBALANCE_INTERVAL_SECONDS": 45, "WS_ENABLED": False,
            "LP_ESTIMATED_SHARE": 0.10,
        })(),
        tape_path="data/tape.jsonl",
    )
    d.set_funnel(
        fetched=10, gamma_matched=2, gamma_status=200, pages=1,
        rejection_counts={"no end_date": 8},
        samples={"no end_date": [{"cid": "x", "question": "q"}]},
        selected=[], ranked=0,
    )
    d.set_est_rewards(1.25, share=0.10)
    blob = d.to_dict()
    assert blob["header"]["name"] == "qv/Polymarket"
    assert blob["funnel"]["fetched"] == 10
    assert blob["pnl"]["realised"] == 0.0
    assert blob["pnl"]["est_rewards_usd"] == 1.25
    assert blob["pnl"]["est_share"] == 0.10
    assert "ESTIM" in blob["pnl"]["est_note"]
    assert blob["header"]["est_rewards_usd"] == 1.25
    assert blob["collateral"]["config_capital"] == 100.0
    assert blob["header"]["cash_source"] == "paper"
    assert blob["collateral"]["cash_source"] == "paper"
    # realised must not include ESTIM
    assert blob["pnl"]["realised"] != blob["pnl"]["est_rewards_usd"] or blob["pnl"]["realised"] == 0.0


def test_estimate_our_share_from_today_snapshot(tmp_path, monkeypatch):
    """ESTIM uses pool × LP_ESTIMATED_SHARE; never touches realised."""
    from unittest.mock import MagicMock

    from config.settings import Settings
    from core.rewards_tracker import DailySnapshot, RewardsTracker

    monkeypatch.chdir(tmp_path)
    settings = Settings(
        PRIVATE_KEY="0x" + "ab" * 32,
        TOTAL_CAPITAL=100.0,
        LP_ESTIMATED_SHARE=0.10,
    )
    rt = RewardsTracker(MagicMock(), settings, MagicMock())
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rt._history = [DailySnapshot(date=today, estimated_rewards_usd=50.0)]
    out = rt.estimate_our_share_usd([])
    assert out["est_rewards_usd"] == 5.0
    assert out["est_share"] == 0.10
    assert "ESTIM, not CLOB Q-score" in out["est_note"]


def test_estimate_our_share_fallback_time_frac(tmp_path, monkeypatch):
    from datetime import timedelta
    from unittest.mock import MagicMock

    from config.settings import Settings
    from core.rewards_tracker import RewardsTracker

    monkeypatch.chdir(tmp_path)
    settings = Settings(
        PRIVATE_KEY="0x" + "ab" * 32,
        TOTAL_CAPITAL=100.0,
        LP_ESTIMATED_SHARE=0.10,
        MODE="safe",
    )
    rt = RewardsTracker(MagicMock(), settings, MagicMock())
    rt._history = []
    m = type("M", (), {"reward_rate_daily": 100.0})()
    started = datetime.now(timezone.utc) - timedelta(hours=12)
    out = rt.estimate_our_share_usd([m], session_started_at=started.isoformat())
    # 100 × 0.10 × tightness_factor(safe) × ~0.5
    assert out["est_rewards_usd"] > 0
    assert out["est_share"] == 0.10
    assert "ESTIM, not CLOB Q-score" == out["est_note"]
