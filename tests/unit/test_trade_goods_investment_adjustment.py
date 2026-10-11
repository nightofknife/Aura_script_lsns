"""Batch adjustment follows observed preview levels, including missed/delayed clicks."""

from types import SimpleNamespace

import numpy as np
import pytest

from plans.resonance_pc.src.actions import trade_goods_investment_pc_actions as investment


@pytest.fixture
def batch_rig(monkeypatch):
    state = SimpleNamespace(now=0., current=0, preview=1, visible=1, cap=20,
                            ready_at=0., delay=0., dropped=set(), clicks=[], events=[], pauses=[],
                            cancel_after=None, overshoot=False, partial_feedback=False, partial_until=None)

    def pause(seconds):
        state.pauses.append(seconds)
        state.now += seconds

    def cancel_check():
        if state.cancel_after is not None and len(state.clicks) >= state.cancel_after:
            raise investment.TradeGoodsInvestmentError("trade_cancelled", "Cancelled in a batch")

    class App:
        def capture(self):
            if state.now >= state.ready_at:
                state.visible = state.preview
            if state.partial_until is not None and state.now < state.partial_until:
                state.visible = 4
            frame = np.zeros((720, 1280, 3), np.uint8)
            frame[0, 0, :2] = state.current, state.visible
            return SimpleNamespace(success=True, image=frame)

        def click(self, *, x, y):
            name = {11: "plus", 22: "minus", 33: "confirm"}[x]
            state.clicks.append(name)
            state.events.append(name)
            if len(state.clicks) not in state.dropped:
                if name == "plus":
                    state.preview = min(state.preview + 1, state.cap)
                elif name == "minus":
                    state.preview = max(state.current + 1, state.preview - 1)
            if state.overshoot and name == "plus" and len(state.clicks) == 9:
                state.preview += 2
            if state.partial_feedback and len(state.clicks) == 9:
                state.partial_until = state.now + .35
            state.ready_at = state.now + state.delay

    class Reader:
        def read_levels(self, frame):
            state.events.append("read")
            return {"current": int(frame[0, 0, 0]), "preview": int(frame[0, 0, 1]), "max_level": False}

        def match(self, frame, name):
            if name == "success":
                return {"found": False}
            points = {"plus": 11, "minus": 22, "confirm_enabled": 33}
            return {"found": True, "center": [points[name], 1]}

    state.app, state.reader = App(), Reader()
    monkeypatch.setattr(investment.time, "monotonic", lambda: state.now)
    monkeypatch.setattr(investment, "_pause", pause)
    monkeypatch.setattr(investment, "_cancel_check", cancel_check)
    state.prepare = lambda target=10: investment._prepare_upgrade(
        state.app, state.reader, {"current": state.current, "preview": state.visible},
        target, lambda **data: None)
    return state


@pytest.mark.parametrize("current,preview,target,count", [
    (0, 1, 10, 9), (7, 8, 10, 2), (0, 1, 20, 19), (0, 0, 20, 20),
])
def test_batch_uses_preview_difference_without_per_click_recognition(batch_rig, current, preview, target, count):
    state = batch_rig
    state.current, state.preview, state.visible = current, preview, preview
    operation = state.prepare(target)
    assert operation["upgrade_level"] == target and not operation["stop_after_commit"]
    assert state.clicks == ["plus"] * count
    assert state.events == ["read"] + ["plus"] * count + ["read", "read"]
    assert .35 not in state.pauses


def test_partial_feedback_recomputes_missing_clicks(batch_rig):
    state = batch_rig
    state.dropped = {2, 5}
    operation = state.prepare()
    assert operation["upgrade_level"] == 10 and not operation["stop_after_commit"]
    assert state.events == (["read"] + ["plus"] * 9 + ["read", "read"]
                            + ["read", "plus", "plus", "read", "read"])


@pytest.mark.parametrize("delay", [.3, .95])
def test_delayed_feedback_is_reread_without_more_clicks(batch_rig, delay):
    state = batch_rig
    state.delay = delay
    operation = state.prepare()
    assert operation["upgrade_level"] == 10 and not operation["stop_after_commit"]
    assert state.clicks == ["plus"] * 9
    assert len([pause for pause in state.pauses if pause >= .35]) == 1


def test_resource_cap_stops_after_the_remaining_batch_makes_no_progress(batch_rig):
    state = batch_rig
    state.cap = 6
    operation = state.prepare()
    assert operation["upgrade_level"] == 6 and operation["stop_after_commit"]
    assert state.clicks == ["plus"] * 13
    assert len([pause for pause in state.pauses if pause >= .35]) == 1
    assert "confirm" not in state.clicks


def test_no_plus_progress_still_prepares_default_one_level_once(batch_rig):
    state = batch_rig
    state.cap = 1
    operation = state.prepare()
    assert operation["upgrade_level"] == 1 and operation["stop_after_commit"]
    assert state.clicks == ["plus"] * 9
    assert len([pause for pause in state.pauses if pause >= .35]) == 1
    assert state.now == pytest.approx(8 * investment._ADJUST_CLICK_INTERVAL + 1.1)


def test_oversized_initial_preview_uses_one_minus_batch(batch_rig):
    state = batch_rig
    state.preview = state.visible = 12
    operation = state.prepare()
    assert operation["upgrade_level"] == 10 and not operation["stop_after_commit"]
    assert state.clicks == ["minus", "minus"]


def test_batch_overshoot_is_corrected_before_preparing_submission(batch_rig):
    state = batch_rig
    state.overshoot = True
    operation = state.prepare()
    assert operation["upgrade_level"] == 10 and not operation["stop_after_commit"]
    assert state.clicks == ["plus"] * 9 + ["minus"] * 2


def test_late_feedback_before_batch_recalculates_without_clicking_old_count(batch_rig):
    state = batch_rig
    state.preview = 8  # Captured feedback is newer than the caller's preview=1.
    operation = state.prepare()
    assert operation["upgrade_level"] == 10
    assert state.clicks == ["plus", "plus"]


@pytest.mark.parametrize("previous,late,target,name,count", [
    (12, 14, 10, "minus", 4), (8, 7, 10, "plus", 3),
])
def test_late_feedback_in_either_direction_is_replanned(batch_rig, previous, late, target, name, count):
    state = batch_rig
    state.visible, state.preview = previous, late
    operation = state.prepare(target)
    assert operation["upgrade_level"] == target
    assert state.clicks == [name] * count


def test_staged_late_feedback_can_be_corrected_after_partial_supplement(batch_rig):
    state = batch_rig
    state.partial_feedback = True
    operation = state.prepare()
    assert operation["upgrade_level"] == 10 and not operation["stop_after_commit"]
    assert state.clicks == ["plus"] * 15 + ["minus"] * 6
    assert state.current == 0 and "confirm" not in state.clicks


def test_actual_level_change_blocks_batch_before_input(batch_rig):
    state = batch_rig
    state.current = 4
    with pytest.raises(investment.TradeGoodsInvestmentError) as error:
        investment._adjust(state.app, state.reader, "plus", 0, 1, clicks=9)
    assert error.value.code == "investment_actual_level_changed" and not state.clicks


def test_cancellation_interrupts_between_batch_clicks(batch_rig):
    state = batch_rig
    state.cancel_after = 3
    with pytest.raises(investment.TradeGoodsInvestmentError) as error:
        state.prepare()
    assert error.value.code == "trade_cancelled" and state.clicks == ["plus"] * 3


@pytest.mark.parametrize("clicks", [0, 21, -1, True, 2.0])
def test_invalid_batch_count_rejected_before_input(batch_rig, clicks):
    with pytest.raises(investment.TradeGoodsInvestmentError) as error:
        investment._adjust(batch_rig.app, batch_rig.reader, "plus", 0, 1, clicks=clicks)
    assert error.value.code == "investment_adjustment_count_invalid" and not batch_rig.clicks
