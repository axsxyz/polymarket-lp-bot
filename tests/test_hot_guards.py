"""Hot-path skip predicate — paused / inactive / toxic markets must not requote."""

from __future__ import annotations

from core.hot_guards import hot_skip_reason, note_hot_skip_transition
from core.market_manager import MarketState


def _clean(**overrides) -> dict:
    base = dict(
        state=MarketState.SCORING,
        toxic_paused=False,
        exposure_paused=False,
        skip_cids=set(),
        drawdown=False,
        midpoint_paused=False,
        spike_paused=False,
    )
    base.update(overrides)
    return base


class TestHotSkipReason:
    def test_clean_market_quotes(self):
        assert hot_skip_reason("c1", **_clean()) is None

    def test_toxic_paused(self):
        assert hot_skip_reason("c1", **_clean(toxic_paused=True)) == "toxicity_paused"

    def test_inactive_state(self):
        assert hot_skip_reason("c1", **_clean(state=MarketState.INACTIVE)) == "inactive"

    def test_seed_failed_state(self):
        assert hot_skip_reason("c1", **_clean(state=MarketState.SEED_FAILED)) == "seed_failed"

    def test_skip_cids(self):
        assert hot_skip_reason("c1", **_clean(skip_cids={"c1"})) == "seed_failed"

    def test_exposure_paused(self):
        assert hot_skip_reason("c1", **_clean(exposure_paused=True)) == "exposure"

    def test_drawdown(self):
        assert hot_skip_reason("c1", **_clean(drawdown=True)) == "drawdown"

    def test_midpoint_and_spike(self):
        assert hot_skip_reason("c1", **_clean(midpoint_paused=True)) == "midpoint_guard"
        assert hot_skip_reason("c1", **_clean(spike_paused=True)) == "spike_paused"


class TestNoteHotSkipTransition:
    """Tape/digest skip emits only on reason appear/change — not every WS tick."""

    def test_100_inactive_ticks_one_emit(self):
        last: dict = {}
        emits = []
        for _ in range(100):
            reason = hot_skip_reason("cid_inactive", **_clean(state=MarketState.INACTIVE))
            assert reason == "inactive"
            tagged = note_hot_skip_transition("cid_inactive", reason, last)
            if tagged:
                emits.append(tagged)
        assert emits == ["hot_inactive"]
        assert last["cid_inactive"] == "inactive"

    def test_clear_on_none_then_reappear(self):
        last: dict = {}
        assert note_hot_skip_transition("c1", "inactive", last) == "hot_inactive"
        assert note_hot_skip_transition("c1", None, last) is None
        assert "c1" not in last
        assert note_hot_skip_transition("c1", "inactive", last) == "hot_inactive"

    def test_reason_change_emits_again(self):
        last: dict = {}
        assert note_hot_skip_transition("c1", "inactive", last) == "hot_inactive"
        assert note_hot_skip_transition("c1", "toxicity_paused", last) == "hot_toxicity_paused"
        assert note_hot_skip_transition("c1", "toxicity_paused", last) is None
