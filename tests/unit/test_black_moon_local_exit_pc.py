"""Offline purchase completion and exit contracts; never operate the live game."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from plans.resonance_pc.src.actions import black_moon_local_exit_pc_actions as exits
from plans.resonance_pc.src.actions import black_moon_local_prepare_pc_actions as prepare
from plans.aura_base.src.services.interaction_service import InteractionError


STATES = {
    0: {"page_state": "unknown", "shop_ready": False, "total_zero": False},
    1: {"page_state": "local_shop", "shop_ready": True, "total_zero": True},
    2: {"page_state": "local_shop", "shop_ready": True, "total_zero": False},
    3: {"page_state": "local_shop", "shop_ready": False, "total_zero": True},
    4: {"page_state": "rest_menu", "shop_ready": False, "total_zero": False},
    5: {"page_state": "main", "shop_ready": False, "total_zero": False},
}
SUBMITTED = {"success": True, "purchase_submitted": True}
VERIFIED = {"success": True, "ready_to_exit": True, "purchase_verified": True}
SKIPPED = {"success": True, "purchase_submitted": False, "status": "skipped"}


def frame(state, shade=0):
    image = np.full((720, 1280, 3), shade, dtype=np.uint8)
    image[0, 0] = state
    return image


class App:
    def __init__(self, states, *, transition=None, transition_click=1):
        self.frames = [frame(state) if isinstance(state, int) else state for state in states]
        self.captures = 0
        self.clicks = []
        self.transition = transition
        self.transition_click = transition_click

    def capture(self):
        image = self.frames[min(self.captures, len(self.frames) - 1)]
        self.captures += 1
        return SimpleNamespace(success=image is not None, image=image)

    def click(self, *args, **kwargs):
        self.clicks.append((args, kwargs, self.captures))
        if self.transition is not None and len(self.clicks) >= self.transition_click:
            self.frames = [frame(self.transition)]

    def __getattr__(self, name):
        if name in {"drag", "scroll", "press_key"}:
            raise AssertionError(f"Exit must never invoke {name}")
        raise AttributeError(name)


class Reader:
    VIEWPORT = (594, 143, 684, 500)

    def __init__(self, *, missing=False, center=None):
        self.missing = missing
        self.center = [90, 36] if center is None else center
        self.observations = []
        self.buttons = []

    def observe(self, image):
        state = int(image[0, 0, 0])
        self.observations.append(state)
        return dict(STATES[state])

    def button(self, image, target):
        state = int(image[0, 0, 0])
        self.buttons.append((state, target))
        return {"found": state == 1 and not self.missing, "center": self.center}


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    clock = SimpleNamespace(now=0.)
    original_sleep = asyncio.sleep

    async def sleep(seconds):
        clock.now += seconds
        await original_sleep(0)

    monkeypatch.setattr(exits, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(exits.asyncio, "sleep", sleep)
    monkeypatch.setattr(exits, "_WAIT_SECONDS", 1.)
    monkeypatch.setattr(exits, "_PURCHASE_WAIT_SECONDS", 1.)
    monkeypatch.setattr(exits, "_POLL_SECONDS", .1)
    monkeypatch.setattr(exits, "_SETTLE_SECONDS", .1)
    return clock


def wait(app, submission=None, reader=None):
    return asyncio.run(exits.execute_black_moon_wait_purchase(
        submission=SUBMITTED if submission is None else submission,
        app=app, reader=reader or Reader()))


def leave(app, target="rest_menu", purchase_result=None, reader=None):
    return asyncio.run(exits.execute_black_moon_exit(
        purchase_result=VERIFIED if purchase_result is None else purchase_result,
        exit_target=target, app=app, reader=reader or Reader()))


def test_submission_skipped_does_not_capture_or_click():
    app = App([0])
    result = wait(app, SKIPPED)
    assert result["success"] is True and result["status"] == "skipped"
    assert result["ready_to_exit"] is False and result["purchase_verified"] is False
    assert app.captures == 0 and not app.clicks


def no_target_scan():
    return {"scan_complete": True, "end_reason": "unchanged_view", "bottom_confirmed": False,
            "scroll_count": 2, "unchanged_drag_count": 2,
            "has_selected_goods": False, "matched_item_ids": []}


def test_completed_no_target_scan_allows_exit_without_waiting_for_payment():
    app = App([2])
    result = wait(app, {**SKIPPED, "scan": no_target_scan()})
    assert result["ready_to_exit"] is True and result["purchase_verified"] is False
    assert result["reason"] == "no_selected_goods"
    assert not app.captures and not app.clicks


def test_no_target_visit_exits_directly_even_with_nonzero_initial_total():
    app = App([2], transition=4)
    reader = Reader()
    original_button = reader.button

    def button(image, target):
        hit = original_button(image, target)
        hit["found"] = int(image[0, 0, 0]) == 2
        return hit

    reader.button = button
    result = wait(app, {**SKIPPED, "scan": no_target_scan()})
    exited = leave(app, purchase_result=result, reader=reader)
    assert exited["exited"] and exited["page_state"] == "rest_menu"
    assert len(app.clicks) == 1


@pytest.mark.parametrize("change", [{"scan_complete": False}, {"unchanged_drag_count": 1},
                                    {"has_selected_goods": True, "matched_item_ids": ["a"]}])
def test_invalid_or_nonempty_scan_cannot_authorize_no_target_exit(change):
    app = App([1])
    with pytest.raises(ValueError):
        wait(app, {**SKIPPED, "scan": {**no_target_scan(), **change}})
    assert not app.captures and not app.clicks


@pytest.mark.parametrize("submission", [None, {}, {"success": False}, {"success": 1},
                                        {"success": True, "purchase_submitted": False}])
def test_invalid_submission_does_not_touch_ui(submission):
    app = App([1])
    with pytest.raises(ValueError):
        asyncio.run(exits.execute_black_moon_wait_purchase(submission, app, Reader()))
    assert app.captures == 0 and not app.clicks


def test_purchase_waits_for_loading_and_zero_then_two_stable_frames():
    app = App([0, 3, 2, 1, 1])
    reader = Reader()
    result = wait(app, reader=reader)
    assert result["success"] is True and result["ready_to_exit"] is True
    assert result["purchase_verified"] is True and result["page_state"] == "local_shop"
    assert reader.observations == [0, 3, 2, 1, 1]
    assert not app.clicks


@pytest.mark.parametrize("states", [[0], [2], [3], [4], [5], [1, 0]])
def test_purchase_not_completed_never_clicks_exit_or_purchase(states):
    app = App(states)
    with pytest.raises(exits.BlackMoonLocalExitError):
        wait(app)
    assert not app.clicks


def test_purchase_transient_ready_is_not_enough():
    app = App([1, 0, 1, 1])
    reader = Reader()
    assert wait(app, reader=reader)["purchase_verified"] is True
    assert reader.observations == [1, 0, 1, 1]


def test_purchase_requires_stable_goods_viewport():
    app = App([frame(1, 0), frame(1, 240), frame(1, 0), frame(1, 0)])
    result = wait(app)
    assert result["purchase_verified"] is True
    assert app.captures == 4


@pytest.mark.parametrize("target,state", [("rest_menu", 4), ("main", 5)])
def test_normal_exit_reaches_selected_target(target, state):
    app = App([1], transition=state)
    result = leave(app, target)
    assert result["success"] is True and result["page_state"] == target
    assert result["click_count"] == len(app.clicks) == 1
    assert result["supplemental_clicks"] == 0


@pytest.mark.parametrize("target,state", [("rest_menu", 4), ("main", 5)])
def test_already_at_target_requires_confirmation_but_no_click(target, state):
    app = App([state])
    result = leave(app, target)
    assert result["success"] is True and result["page_state"] == target
    assert result["click_count"] == 0 and app.captures >= 2
    assert not app.clicks


@pytest.mark.parametrize("target", ["rest_menu", "main"])
def test_unsubmitted_result_skips_exit_without_capture(target):
    app = App([0])
    result = leave(app, target, purchase_result={"success": True, "ready_to_exit": False})
    assert result["success"] is True and result["status"] == "skipped"
    assert app.captures == 0 and not app.clicks


@pytest.mark.parametrize("target", [None, "", "city", "REST_MENU", 1])
def test_invalid_target_rejected_even_when_exit_would_be_skipped(target):
    app = App([1])
    with pytest.raises(ValueError):
        leave(app, target, purchase_result={"success": True, "ready_to_exit": False})
    assert app.captures == 0 and not app.clicks


@pytest.mark.parametrize("purchase_result", [None, {}, {"success": False}, {"success": 1}])
def test_failed_purchase_result_cannot_exit(purchase_result):
    app = App([1])
    with pytest.raises(ValueError):
        asyncio.run(exits.execute_black_moon_exit(purchase_result, "main", app, Reader()))
    assert app.captures == 0 and not app.clicks


@pytest.mark.parametrize("state", [0, 2, 3, 5])
def test_unknown_wrong_target_or_nonzero_total_never_clicks(state):
    app = App([state])
    with pytest.raises(exits.BlackMoonLocalExitError):
        leave(app)
    assert not app.clicks


def test_unknown_feedback_after_click_does_not_blindly_supplement():
    app = App([1], transition=0)
    with pytest.raises(exits.BlackMoonLocalExitError):
        leave(app)
    assert len(app.clicks) == 1


def test_target_arriving_after_unknown_feedback_succeeds_without_supplement():
    app = App([1])
    original_click = app.click

    def click(*args, **kwargs):
        original_click(*args, **kwargs)
        app.frames = [frame(0)] * (app.captures + 3) + [frame(4)] * 2

    app.click = click
    result = leave(app)
    assert result["success"] is True and len(app.clicks) == 1


def test_supplement_requires_fresh_source_and_fresh_button_location():
    app = App([1], transition=4, transition_click=2)
    reader = Reader()
    original_button = reader.button

    def button(image, target):
        hit = original_button(image, target)
        hit["center"] = [90 + 10 * len(app.clicks), 36]
        return hit

    reader.button = button
    result = leave(app, reader=reader)
    assert result["click_count"] == len(app.clicks) == 2
    assert result["supplemental_clicks"] == 1
    assert app.clicks[1][2] > app.clicks[0][2]
    assert app.clicks[0][1]["x"] == 90 and app.clicks[1][1]["x"] == 100
    assert len(reader.buttons) >= 4


def test_initial_click_plus_three_supplements_is_hard_limit():
    app = App([1])
    with pytest.raises(exits.BlackMoonLocalExitError):
        leave(app)
    assert len(app.clicks) == 4
    assert all(current[2] > prior[2] for prior, current in zip(app.clicks, app.clicks[1:]))


def test_missing_button_cannot_exit():
    app = App([1])
    with pytest.raises(exits.BlackMoonLocalExitError):
        leave(app, reader=Reader(missing=True))
    assert not app.clicks


def test_source_viewport_change_requires_new_stable_pair_before_click():
    app = App([frame(1, 0), frame(1, 240), frame(1, 0), frame(1, 0)], transition=4)
    result = leave(app)
    assert result["success"] is True
    assert app.clicks[0][2] == 4


def test_moving_button_requires_stable_point_before_input():
    app = App([1], transition=4)
    reader = Reader()
    original_button = reader.button

    def button(image, target):
        hit = original_button(image, target)
        hit["center"] = [90 if app.captures in {1, 3, 4} else 100, 36]
        return hit

    reader.button = button
    assert leave(app, reader=reader)["success"] is True
    assert app.clicks[0][2] == 4


def test_single_target_frame_does_not_end_exit_confirmation():
    app = App([4, 0, 4, 4])
    reader = Reader()
    result = leave(app, reader=reader)
    assert result["success"] is True
    assert reader.observations == [4, 0, 4, 4]
    assert not app.clicks


@pytest.mark.parametrize("ready", [None, 0, 1, "true"])
def test_exit_requires_explicit_boolean_ready_flag(ready):
    app = App([1])
    with pytest.raises(ValueError):
        leave(app, purchase_result={"success": True, "ready_to_exit": ready})
    assert not app.clicks and app.captures == 0


@pytest.mark.parametrize("operation", ["wait", "exit"])
@pytest.mark.parametrize("image", [None, np.zeros((1080, 1920, 3), dtype=np.uint8)])
def test_capture_failure_or_wrong_resolution_never_sends_input(operation, image):
    app = App([image])
    with pytest.raises(prepare.BlackMoonLocalPrepareError):
        (wait if operation == "wait" else leave)(app)
    assert not app.clicks


@pytest.mark.parametrize("center", [[True, 20], [-1, 20], [1280, 20],
                                    [float("nan"), 20], [float("inf"), 20], [10]])
def test_invalid_button_point_never_reaches_input(center):
    app = App([1])
    with pytest.raises(prepare.BlackMoonLocalPrepareError):
        leave(app, reader=Reader(center=center))
    assert not app.clicks


@pytest.mark.parametrize("operation", ["wait", "exit"])
def test_cancellation_prevents_input(monkeypatch, operation):
    def cancel():
        raise asyncio.CancelledError()

    monkeypatch.setattr(exits, "_cancel_check", cancel)
    app = App([1])
    with pytest.raises(asyncio.CancelledError):
        (wait if operation == "wait" else leave)(app)
    assert not app.clicks


def test_cancel_after_exit_click_does_not_send_additional_input(monkeypatch):
    app = App([1], transition=4)

    def cancel():
        if app.clicks:
            raise asyncio.CancelledError()

    monkeypatch.setattr(exits, "_cancel_check", cancel)
    with pytest.raises(asyncio.CancelledError):
        leave(app)
    assert len(app.clicks) == 1


def test_cancellation_during_loading_is_not_hidden_until_timeout(monkeypatch):
    app = App([0])

    def cancel():
        if app.captures >= 2:
            raise asyncio.CancelledError()

    monkeypatch.setattr(prepare, "_cancel_check", cancel)
    with pytest.raises(asyncio.CancelledError):
        wait(app)
    assert app.captures == 2 and not app.clicks


def test_task_exposes_two_exit_targets_and_gates_both_exit_nodes_on_submission():
    root = Path(__file__).resolve().parents[2]
    task = yaml.safe_load((root / "plans/resonance_pc/tasks/black_moon_local_select_pc.yaml")
                          .read_text(encoding="utf-8"))["black_moon_local_select_pc"]
    inputs = {item["name"]: item for item in task["meta"]["inputs"]}
    assert inputs["exit_target"]["default"] == "rest_menu"
    assert inputs["exit_target"]["enum"] == ["rest_menu", "main"]
    assert inputs["submit_purchase"]["default"] is False
    steps = task["steps"]
    assert steps["validate_table"]["params"]["exit_target"] == "{{ inputs.exit_target }}"
    assert steps["require_resolution"]["depends_on"] == "validate_table"
    for key in ("submit", "wait_purchase", "exit_shop"):
        assert steps[key]["when"] == "{{ inputs.submit_purchase }}"
    assert steps["wait_purchase"]["depends_on"] == {"submit": "success|skipped"}
    assert steps["wait_purchase"]["params"]["submission"] == "{{ nodes.submit.output }}"
    assert steps["exit_shop"]["depends_on"] == {"wait_purchase": "success|skipped"}
    assert steps["exit_shop"]["params"]["purchase_result"] == "{{ nodes.wait_purchase.output }}"
    assert steps["exit_shop"]["params"]["exit_target"] == "{{ inputs.exit_target }}"


@pytest.mark.parametrize("returned", [False, SimpleNamespace(success=False)])
def test_rejected_exit_input_retains_receipt_without_another_click(returned):
    app = App([1])
    original = app.click

    def click(*args, **kwargs):
        original(*args, **kwargs)
        return returned

    app.click = click
    with pytest.raises(InteractionError) as error:
        leave(app)
    assert error.value.code == "input_rejected"
    assert len(app.clicks) == 1
    receipt, = error.value.receipts
    assert receipt.attempted and receipt.call_returned
    assert receipt.acknowledged is False and receipt.sent is False
