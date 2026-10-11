"""Offline contracts for preparation only: no selection, purchase, or persistence."""
import asyncio
from types import SimpleNamespace

import numpy as np
import pytest

from plans.resonance_pc.src.actions import black_moon_local_prepare_pc_actions as prepare


STATES = {
    0: {"page_state": "unknown", "local_selected": None, "batch_enabled": None, "ready": False},
    1: {"page_state": "shop_page", "local_selected": False, "batch_enabled": False, "ready": False},
    2: {"page_state": "local_shop", "local_selected": True, "batch_enabled": False, "ready": False},
    3: {"page_state": "local_shop", "local_selected": True, "batch_enabled": True, "ready": True},
    4: {"page_state": "shop_page", "local_selected": False, "batch_enabled": True, "ready": False},
    5: {"page_state": "local_shop", "local_selected": True, "batch_enabled": None, "ready": False},
}
FRAMES = {key: np.full((720, 1280, 3), key, np.uint8) for key in STATES}


class App:
    def __init__(self, frames):
        self.frames = list(frames)
        self.captures = 0
        self.clicks = []

    def capture(self):
        frame = self.frames[min(self.captures, len(self.frames) - 1)]
        self.captures += 1
        return SimpleNamespace(success=frame is not None, image=frame)

    def click(self, *args, **kwargs):
        self.clicks.append((args, kwargs, self.captures))

    def __getattr__(self, name):
        if name in {"drag", "scroll", "press_key"}:
            raise AssertionError(f"Preparation must not call {name}")
        raise AttributeError(name)


class Reader:
    def __init__(self, missing=()):
        self.missing = set(missing)
        self.matches = []
        self.observations = []

    def match(self, frame, name):
        state = int(frame[0, 0, 0])
        self.matches.append((state, name))
        allowed = {
            "shop_entry": state == 0,
            "shop_page": state in {1, 2, 3, 4, 5},
            "local_tab_selected": state in {2, 3, 5},
            "local_tab_unselected": state in {1, 4},
            "batch_on": state in {3, 4},
            "batch_off": state in {1, 2},
        }
        return {"found": name not in self.missing and allowed[name], "score": .98,
                "rect": [700, 150, 120, 40], "center": [760, 170]}

    def observe(self, frame):
        state = int(frame[0, 0, 0])
        self.observations.append(state)
        return dict(STATES[state])


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    clock = SimpleNamespace(now=0.)
    original_sleep = asyncio.sleep

    async def sleep(seconds):
        clock.now += seconds
        await original_sleep(0)

    monkeypatch.setattr(prepare, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(prepare.asyncio, "sleep", sleep)
    monkeypatch.setattr(prepare, "_STEP_TIMEOUT_SECONDS", .01)
    monkeypatch.setattr(prepare, "_POLL_SECONDS", .001)
    monkeypatch.setattr(prepare, "_CLICK_SETTLE_SECONDS", .001)
    return clock


def run_step(step, states, *, reader=None, app=None):
    app = app or App([FRAMES[state] if isinstance(state, int) else state for state in states])
    reader = reader or Reader()
    result = asyncio.run(prepare.execute_black_moon_prepare_step(step=step, app=app, reader=reader))
    return result, app, reader


@pytest.mark.parametrize("step,sequence,expected", [
    ("enter_shop", [0, 1, 1], "shop_page"),
    ("select_local", [1, 1, 2, 2], "local_shop"),
    ("enable_batch", [2, 2, 3, 3], "local_shop"),
    ("verify_ready", [3, 3], "local_shop"),
])
def test_each_normal_step_returns_verified_state(step, sequence, expected):
    result, app, reader = run_step(step, sequence)
    assert result["success"] is True and result["step"] == step
    assert result["page_state"] == expected
    assert result["click_count"] == len(app.clicks) == (0 if step == "verify_ready" else 1)
    assert result["supplemental_clicks"] == 0
    assert len(reader.observations) >= 2
    assert all(key in result for key in ("local_selected", "batch_enabled", "ready", "skipped"))
    if step == "verify_ready":
        assert result["ready"] and result["local_selected"] and result["batch_enabled"]


def test_enter_shop_only_locates_entry_without_initial_page_observation():
    result, app, reader = run_step("enter_shop", [0, 1, 1])
    assert result["success"] and len(app.clicks) == 1
    assert 0 not in reader.observations
    assert reader.matches[0] == (0, "shop_entry")


@pytest.mark.parametrize("step,state", [("select_local", 2), ("select_local", 3), ("enable_batch", 3)])
def test_already_in_target_state_does_not_toggle(step, state):
    result, app, reader = run_step(step, [state])
    assert result["success"] and result["skipped"]
    assert result["click_count"] == 0 and not app.clicks
    assert len(reader.observations) >= 2


@pytest.mark.parametrize("step,source,target", [("select_local", 1, 2), ("enable_batch", 2, 3)])
def test_delayed_transition_waits_without_supplemental_click(step, source, target):
    result, app, _ = run_step(step, [source] * 4 + [target] * 2)
    assert result["success"] and len(app.clicks) == 1
    assert result["supplemental_clicks"] == 0


def test_unsuccessful_click_gets_fresh_source_confirmation_and_relocation():
    app = App([FRAMES[2]])
    reader = Reader()
    original_click = app.click
    original_match = reader.match

    def match(frame, name):
        hit = original_match(frame, name)
        hit["center"] = [760 + 10 * len(app.clicks), 170]
        return hit

    def click(*args, **kwargs):
        original_click(*args, **kwargs)
        if len(app.clicks) == 2:
            app.frames = [FRAMES[3]]

    app.click = click
    reader.match = match
    result, app, reader = run_step("enable_batch", [], app=app, reader=reader)
    assert result["success"] and result["click_count"] == 2
    assert result["supplemental_clicks"] == 1
    assert app.clicks[1][2] > app.clicks[0][2]
    assert app.clicks[0][1]["x"] == 760 and app.clicks[1][1]["x"] == 770
    assert sum(name == "batch_off" for _, name in reader.matches) >= 2


@pytest.mark.parametrize("step,state", [("select_local", 0), ("enable_batch", 0), ("enable_batch", 5)])
def test_unknown_state_is_not_interpreted_as_off(step, state):
    app = App([FRAMES[state]])
    with pytest.raises(prepare.BlackMoonLocalPrepareError) as error:
        run_step(step, [], app=app)
    assert not app.clicks
    assert error.value.code and isinstance(error.value.detail, dict)


def test_unknown_feedback_after_click_does_not_allow_blind_reclick():
    app = App([FRAMES[2], FRAMES[2], FRAMES[0]])
    with pytest.raises(prepare.BlackMoonLocalPrepareError):
        run_step("enable_batch", [], app=app)
    assert len(app.clicks) == 1


@pytest.mark.parametrize("step,source", [("enter_shop", 0), ("select_local", 1), ("enable_batch", 2)])
def test_clicks_are_bounded_to_initial_plus_three_supplemental(step, source):
    app = App([FRAMES[source]])
    with pytest.raises(prepare.BlackMoonLocalPrepareError) as error:
        run_step(step, [], app=app)
    assert len(app.clicks) == 4
    assert error.value.code and isinstance(error.value.detail, dict)
    assert all(current[2] > previous[2] for previous, current in zip(app.clicks, app.clicks[1:]))


def test_missing_entry_raises_without_clicking():
    app = App([FRAMES[0]])
    with pytest.raises(prepare.BlackMoonLocalPrepareError):
        run_step("enter_shop", [], reader=Reader(missing={"shop_entry"}), app=app)
    assert not app.clicks


@pytest.mark.parametrize("frame", [None, np.zeros((1080, 1920, 3), np.uint8)])
def test_capture_failure_or_wrong_resolution_cannot_click(frame):
    app = App([frame])
    with pytest.raises(prepare.BlackMoonLocalPrepareError):
        run_step("enter_shop", [], app=app)
    assert not app.clicks


def test_cancelled_error_propagates_without_click(monkeypatch):
    def cancel():
        raise asyncio.CancelledError()

    monkeypatch.setattr(prepare, "_cancel_check", cancel)
    app = App([FRAMES[0]])
    with pytest.raises(asyncio.CancelledError):
        run_step("enter_shop", [], app=app)
    assert not app.clicks


@pytest.mark.parametrize("states", [[2, 4], [3, 0], [4], [5]])
def test_verify_ready_requires_same_frame_local_and_batch_and_consecutive_confirmation(states):
    app = App([FRAMES[state] for state in states])
    with pytest.raises(prepare.BlackMoonLocalPrepareError):
        run_step("verify_ready", [], app=app)
    assert not app.clicks


def test_transient_ready_frame_is_not_enough_but_later_stable_ready_succeeds():
    result, app, reader = run_step("verify_ready", [3, 0, 3, 3])
    assert result["success"] and result["ready"]
    assert reader.observations == [3, 0, 3, 3]
    assert not app.clicks


@pytest.mark.parametrize("step,state", [("enter_shop", 0), ("select_local", 1), ("enable_batch", 2)])
@pytest.mark.parametrize("center", [None, [True, 20], [1280, 10], [-1, 20], [float("nan"), 20], [float("inf"), 20]])
def test_invalid_template_point_never_reaches_input(center, step, state):
    reader = Reader()
    original = reader.match

    def match(frame, name):
        return {**original(frame, name), "center": center}

    reader.match = match
    app = App([FRAMES[state]])
    with pytest.raises(prepare.BlackMoonLocalPrepareError) as error:
        run_step(step, [], reader=reader, app=app)
    assert error.value.code == "black_moon_prepare_click_missing"
    assert not app.clicks


def test_task_cancellation_during_transition_stops_with_no_reclick(monkeypatch):
    app = App([FRAMES[0], FRAMES[0]])

    def cancel():
        if app.clicks:
            raise asyncio.CancelledError()

    monkeypatch.setattr(prepare, "_cancel_check", cancel)
    with pytest.raises(asyncio.CancelledError):
        run_step("enter_shop", [], app=app)
    assert len(app.clicks) == 1


@pytest.mark.parametrize("step,source", [("select_local", 1), ("enable_batch", 2)])
def test_one_source_frame_followed_by_unknown_never_clicks(step, source):
    app = App([FRAMES[source], FRAMES[0]])
    with pytest.raises(prepare.BlackMoonLocalPrepareError):
        run_step(step, [], app=app)
    assert not app.clicks


def test_target_arriving_in_supplemental_probe_cancels_reclick(fast_poll):
    app = App([FRAMES[2]])
    original_capture = app.capture

    def capture():
        if app.clicks and fast_poll.now >= .011:
            app.frames = [FRAMES[3]]
        return original_capture()

    app.capture = capture
    result, app, _ = run_step("enable_batch", [], app=app)
    assert result["success"] and result["supplemental_clicks"] == 0
    assert len(app.clicks) == 1


@pytest.mark.parametrize("code", ["template_load_failed", "unknown_template", "invalid_image"])
def test_invalid_template_configuration_propagates_without_click(code):
    reader = Reader()

    def match(frame, name):
        raise prepare.BlackMoonLocalShopRecognitionError(code, "unusable test template")

    reader.match = match
    app = App([FRAMES[0]])
    with pytest.raises(prepare.BlackMoonLocalShopRecognitionError) as error:
        run_step("enter_shop", [], app=app, reader=reader)
    assert error.value.code == code and not app.clicks


def test_transient_recognition_failure_is_waited_not_clicked():
    reader = Reader()
    original_observe = reader.observe
    observations = []

    def observe(frame):
        observations.append(None)
        if len(observations) == 1:
            raise prepare.BlackMoonLocalShopRecognitionError("ambiguous_state", "temporary transition")
        return original_observe(frame)

    reader.observe = observe
    result, app, _ = run_step("verify_ready", [3], reader=reader)
    assert result["ready"] and not app.clicks
    assert len(observations) == 3


def test_scheduler_cancellation_is_explicit_error_not_success(monkeypatch):
    monkeypatch.setattr(prepare, "is_current_task_cancel_requested", lambda: True)
    app = App([FRAMES[0]])
    with pytest.raises(prepare.BlackMoonLocalPrepareError) as error:
        run_step("enter_shop", [], app=app)
    assert error.value.code == "black_moon_prepare_cancelled"
    assert not app.clicks


def test_cancellation_arriving_with_final_observation_cannot_return_success(monkeypatch):
    reader = Reader()
    monkeypatch.setattr(prepare, "is_current_task_cancel_requested", lambda: len(reader.observations) >= 2)
    app = App([FRAMES[3]])
    with pytest.raises(prepare.BlackMoonLocalPrepareError) as error:
        run_step("verify_ready", [], app=app, reader=reader)
    assert error.value.code == "black_moon_prepare_cancelled"
    assert len(reader.observations) == 2 and not app.clicks
