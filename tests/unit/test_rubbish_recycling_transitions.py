"""Recycling transitions require usable frames and bounded, cancellable input."""
from collections import defaultdict, deque
from pathlib import Path
from types import SimpleNamespace
import asyncio
import math

import numpy as np
import pytest

from plans.resonance_pc.src.actions import rubbish_recycling_pc_actions as rubbish


def test_nan_wait_keeps_existing_sleep_argument_and_cancellation_contract(monkeypatch):
    events = []
    marker = ValueError("nonfinite wait")

    def sleep(seconds):
        assert math.isnan(seconds)
        raise marker

    monkeypatch.setattr(rubbish, "_check_cancelled", lambda: events.append("check"))
    monkeypatch.setattr(rubbish, "time", SimpleNamespace(monotonic=lambda: 0, sleep=sleep))
    with pytest.raises(ValueError) as caught:
        rubbish._sleep(float("nan"))
    assert caught.value is marker
    assert events == ["check"]


class Clock:
    def __init__(self):
        self.now = 0.0
        self.after_sleep = None

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        if self.after_sleep:
            self.after_sleep()


class App:
    def __init__(self, clock):
        self.clock = clock
        self.clicks = []
        self.captures = 0
        self.capture_hook = None
        self.click_hook = None

    def capture(self, *, rect):
        self.captures += 1
        if self.capture_hook:
            result = self.capture_hook(self.captures, rect)
            if result is not None:
                return result
        return SimpleNamespace(success=True, image=np.zeros((rect[3], rect[2], 3), np.uint8))

    def click(self, *, x, y):
        self.clicks.append((x, y, self.clock.now))
        if self.click_hook:
            self.click_hook()


class Vision:
    def __init__(self):
        self.sequences = defaultdict(deque)
        self.calls = []

    def find_template(self, *, template_image, **kwargs):
        name = Path(template_image).name
        self.calls.append(name)
        found = self.sequences[name].popleft() if self.sequences[name] else False
        if isinstance(found, Exception):
            raise found
        if not isinstance(found, bool):
            return found
        return SimpleNamespace(found=found, confidence=.96 if found else .1,
                               center_point=(20, 20) if found else None, debug_info={})

    def values(self, template, *values):
        self.sequences[Path(template).name].extend(values)


@pytest.fixture
def rig(monkeypatch):
    clock = Clock()
    cancelled = [False]
    monkeypatch.setattr(rubbish.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(rubbish.time, "sleep", clock.sleep)
    monkeypatch.setattr(rubbish, "is_current_task_cancel_requested", lambda: cancelled[0])
    return SimpleNamespace(clock=clock, app=App(clock), vision=Vision(), cancelled=cancelled)


def bad_capture(kind, rect):
    if kind == "exception":
        raise OSError("capture unavailable")
    image = {"failed": np.zeros((rect[3], rect[2], 3), np.uint8),
             "missing": None, "wrong_shape": np.zeros((1, 1, 3), np.uint8),
             "wrong_dtype": np.zeros((rect[3], rect[2], 3), np.float32)}[kind]
    return SimpleNamespace(success=kind != "failed", image=image)


@pytest.mark.parametrize("kind", ["failed", "missing", "wrong_shape", "wrong_dtype", "exception"])
@pytest.mark.parametrize("operation", ["entry", "recycle"])
def test_failed_recheck_is_never_absence_or_permission_to_continue(rig, kind, operation):
    rig.vision.values(rubbish._ENTRY_TEMPLATE, True)
    fail_at = 2 if operation == "entry" else 1
    rig.app.capture_hook = lambda count, rect: bad_capture(kind, rect) if count == fail_at else None
    with pytest.raises(rubbish.RubbishRecyclingError, match="capture") as error:
        if operation == "entry":
            rubbish._enter_rubbish_page(rig.app, rig.vision)
        else:
            rubbish._click_recycle_all_until_absent(rig.app, rig.vision,
                                                  initial_match={"center": [100, 100]})
    assert error.value.code == "rubbish_capture_failed"
    assert len(rig.app.clicks) == 1


@pytest.mark.parametrize("operation,limit,template", [
    ("entry", 3, rubbish._ENTRY_TEMPLATE), ("recycle", 5, rubbish._RECYCLE_ALL_TEMPLATE),
])
def test_existing_click_budgets_remain_bounded(rig, operation, limit, template):
    rig.vision.values(template, *([True] * 20))
    with pytest.raises(rubbish.RubbishRecyclingError) as error:
        if operation == "entry":
            rubbish._enter_rubbish_page(rig.app, rig.vision)
        else:
            rubbish._click_recycle_all_until_absent(rig.app, rig.vision,
                                                  initial_match={"center": [100, 100]})
    assert error.value.code.endswith("click_not_effective")
    assert len(rig.app.clicks) == limit


@pytest.mark.parametrize("operation", ["entry", "recycle"])
def test_valid_absence_still_confirms_transition(rig, operation):
    if operation == "entry":
        rig.vision.values(rubbish._ENTRY_TEMPLATE, True, False)
        result = rubbish._enter_rubbish_page(rig.app, rig.vision)
    else:
        result = rubbish._click_recycle_all_until_absent(rig.app, rig.vision,
                                                      initial_match={"center": [100, 100]})
    assert result["success"] is True
    assert len(rig.app.clicks) == 1


@pytest.mark.parametrize("bad", [None, float("nan"), "error", "raises"])
def test_match_technical_failure_never_confirms_absence(rig, bad):
    result = (None if bad is None else RuntimeError("matching failed") if bad == "raises"
              else SimpleNamespace(found=False, confidence=bad if bad != "error" else 0,
                                   debug_info={"error": "bad input"} if bad == "error" else {}))
    rig.vision.values(rubbish._RECYCLE_ALL_TEMPLATE, result)
    with pytest.raises(rubbish.RubbishRecyclingError) as error:
        rubbish._click_recycle_all_until_absent(rig.app, rig.vision,
                                              initial_match={"center": [100, 100]})
    assert error.value.code == "rubbish_template_matching_failed"
    assert len(rig.app.clicks) == 1


@pytest.mark.parametrize("invalid_found", [None, "false"])
def test_malformed_found_flag_is_not_a_valid_absence(rig, invalid_found):
    rig.vision.values(rubbish._RECYCLE_ALL_TEMPLATE,
                      SimpleNamespace(found=invalid_found, confidence=0, debug_info={}))
    with pytest.raises(rubbish.RubbishRecyclingError) as error:
        rubbish._click_recycle_all_until_absent(rig.app, rig.vision,
                                              initial_match={"center": [100, 100]})
    assert error.value.code == "rubbish_template_matching_failed"
    assert len(rig.app.clicks) == 1


def test_missing_found_flag_is_not_a_valid_absence(rig):
    rig.vision.values(rubbish._RECYCLE_ALL_TEMPLATE, SimpleNamespace(confidence=0, debug_info={}))
    with pytest.raises(rubbish.RubbishRecyclingError) as error:
        rubbish._click_recycle_all_until_absent(rig.app, rig.vision,
                                              initial_match={"center": [100, 100]})
    assert error.value.code == "rubbish_template_matching_failed"
    assert len(rig.app.clicks) == 1


@pytest.mark.parametrize("debug", [[]])
def test_malformed_debug_output_is_not_a_valid_absence(rig, debug):
    rig.vision.values(rubbish._RECYCLE_ALL_TEMPLATE,
                      SimpleNamespace(found=False, confidence=0, debug_info=debug))
    with pytest.raises(rubbish.RubbishRecyclingError) as error:
        rubbish._click_recycle_all_until_absent(rig.app, rig.vision,
                                              initial_match={"center": [100, 100]})
    assert error.value.code == "rubbish_template_matching_failed"


@pytest.mark.parametrize("flag", [False, np.bool_(False)])
def test_boolean_absence_flag_with_optional_debug_remains_valid(rig, flag):
    rig.vision.values(rubbish._RECYCLE_ALL_TEMPLATE,
                      SimpleNamespace(found=flag, confidence=0, debug_info=None))
    result = rubbish._click_recycle_all_until_absent(rig.app, rig.vision,
                                                  initial_match={"center": [100, 100]})
    assert result["success"] is True
    assert len(rig.app.clicks) == 1


@pytest.mark.parametrize("operation", ["capture", "match"])
def test_concurrent_cancellation_preserves_cancelled_error_over_technical_error(rig, operation):
    def fail(*args, **kwargs):
        rig.cancelled[0] = True
        raise OSError("failed while cancelled")
    if operation == "capture":
        rig.app.capture_hook = fail
    else:
        rig.vision.find_template = fail
    with pytest.raises(asyncio.CancelledError):
        rubbish._match_template(rig.app, rig.vision, template=rubbish._ENTRY_TEMPLATE,
                                region=rubbish._ENTRY_REGION)
    assert not rig.app.clicks


def test_state_capture_failure_is_not_empty(rig):
    rig.app.capture_hook = lambda count, rect: bad_capture("failed", rect)
    with pytest.raises(rubbish.RubbishRecyclingError) as error:
        rubbish._wait_rubbish_state(rig.app, rig.vision)
    assert error.value.code == "rubbish_capture_failed"
    assert not rig.app.clicks


def test_reward_never_empty_stops_at_attempt_limit(rig):
    with pytest.raises(rubbish.RubbishRecyclingError) as error:
        rubbish._dismiss_reward_until_empty(rig.app, rig.vision)
    assert error.value.code == "rubbish_reward_dismiss_timeout"
    assert len(rig.app.clicks) == rubbish._REWARD_DISMISS_MAX_ATTEMPTS
    assert rig.clock.now == pytest.approx(rubbish._REWARD_DISMISS_MAX_ATTEMPTS)
    assert [row[2] for row in rig.app.clicks] == pytest.approx(range(1, 11))


def test_reward_deadline_prevents_next_click(rig, monkeypatch):
    monkeypatch.setattr(rubbish, "_REWARD_DISMISS_TIMEOUT_SEC", 2.5)
    with pytest.raises(rubbish.RubbishRecyclingError) as error:
        rubbish._dismiss_reward_until_empty(rig.app, rig.vision)
    assert error.value.code == "rubbish_reward_dismiss_timeout"
    assert len(rig.app.clicks) == 2
    assert all(row[2] < 2.5 for row in rig.app.clicks)
    assert rig.clock.now == pytest.approx(2.5)


def test_late_reward_frame_cannot_succeed_or_authorize_another_click(rig, monkeypatch):
    monkeypatch.setattr(rubbish, "_REWARD_DISMISS_TIMEOUT_SEC", 2.0)
    rig.vision.values(rubbish._NO_RUBBISH_TEMPLATE, True)
    def slow_capture(count, rect):
        rig.clock.now += 2
    rig.app.capture_hook = slow_capture
    with pytest.raises(rubbish.RubbishRecyclingError) as error:
        rubbish._dismiss_reward_until_empty(rig.app, rig.vision)
    assert error.value.code == "rubbish_reward_dismiss_timeout"
    assert len(rig.app.clicks) == 1


def test_valid_empty_reward_preserves_success_fields(rig):
    rig.vision.values(rubbish._NO_RUBBISH_TEMPLATE, False, True)
    result = rubbish._dismiss_reward_until_empty(rig.app, rig.vision)
    assert result["success"] is True
    assert result["empty_match"]["found"] is True
    assert len(result["attempts"]) == len(rig.app.clicks) == 2


@pytest.mark.parametrize("operation", ["entry", "recycle", "reward", "wait", "state"])
def test_cancelled_before_loop_does_not_capture_or_click(rig, operation):
    rig.cancelled[0] = True
    with pytest.raises(asyncio.CancelledError):
        if operation == "entry":
            rubbish._enter_rubbish_page(rig.app, rig.vision)
        elif operation == "recycle":
            rubbish._click_recycle_all_until_absent(rig.app, rig.vision, initial_match={"center": [1, 1]})
        elif operation == "reward":
            rubbish._dismiss_reward_until_empty(rig.app, rig.vision)
        elif operation == "state":
            rubbish._wait_rubbish_state(rig.app, rig.vision)
        else:
            rubbish._wait_template(rig.app, rig.vision, template=rubbish._ENTRY_TEMPLATE,
                                   region=rubbish._ENTRY_REGION)
    assert not rig.app.clicks
    assert rig.app.captures == 0


@pytest.mark.parametrize("operation", ["recycle", "reward", "wait", "state"])
def test_cancellation_during_wait_prevents_further_capture_and_input(rig, operation):
    rig.clock.after_sleep = lambda: rig.cancelled.__setitem__(0, True)
    with pytest.raises(asyncio.CancelledError):
        if operation == "recycle":
            rubbish._click_recycle_all_until_absent(rig.app, rig.vision, initial_match={"center": [1, 1]})
        elif operation == "reward":
            rubbish._dismiss_reward_until_empty(rig.app, rig.vision)
        elif operation == "state":
            rubbish._wait_rubbish_state(rig.app, rig.vision)
        else:
            rubbish._wait_template(rig.app, rig.vision, template=rubbish._ENTRY_TEMPLATE,
                                   region=rubbish._ENTRY_REGION)
    assert len(rig.app.clicks) == (1 if operation == "recycle" else 0)
    assert rig.app.captures == (1 if operation in {"wait", "state"} else 0)


def test_cancellation_after_entry_capture_never_clicks(rig):
    rig.vision.values(rubbish._ENTRY_TEMPLATE, True)
    rig.app.capture_hook = lambda count, rect: rig.cancelled.__setitem__(0, True)
    with pytest.raises(asyncio.CancelledError):
        rubbish._enter_rubbish_page(rig.app, rig.vision)
    assert not rig.app.clicks
    assert not rig.vision.calls


def configure_public_flow(rig, monkeypatch, *, has_rubbish=False):
    monkeypatch.setattr(rubbish, "resolve_rubbish_recycling_city", lambda **kw: {
        "city_id": "15", "city_name": "test city", "shop_name": "station"})
    shop = SimpleNamespace(resolve_shop_point=lambda **kw: {"x": 10, "y": 20})
    rig.vision.values(rubbish._ENTRY_TEMPLATE, True, False)
    rig.vision.values(rubbish._RECYCLE_ALL_TEMPLATE, has_rubbish, False)
    rig.vision.values(rubbish._NO_RUBBISH_TEMPLATE, not has_rubbish, True)
    rig.vision.values(rubbish._BACK_TEMPLATE, True, True)
    return shop


def test_public_empty_flow_keeps_normal_result_contract(rig, monkeypatch):
    shop = configure_public_flow(rig, monkeypatch)
    result = rubbish.resonance_pc_execute_rubbish_recycling_from_city_panel(
        "test city", app=rig.app, vision=rig.vision, resonance_pc_city_shop_data=shop)
    assert result["success"] is True
    assert result["status"] == "empty"
    assert result["reason"] == "no_rubbish"
    assert result["initial_state"] == result["final_state"] == "empty"
    assert result["reward"] is None
    assert result["recycle_click"] is None
    assert result["page_state"] == "city_panel"
    assert len(result["back_clicks"]) == 2
    assert len(rig.app.clicks) == 4


def test_public_sold_flow_keeps_normal_result_contract(rig, monkeypatch):
    shop = configure_public_flow(rig, monkeypatch, has_rubbish=True)
    result = rubbish.resonance_pc_execute_rubbish_recycling_from_city_panel(
        "test city", app=rig.app, vision=rig.vision, resonance_pc_city_shop_data=shop)
    assert result["success"] is True
    assert result["status"] == "sold"
    assert result["reason"] is None
    assert result["initial_state"] == "has_rubbish"
    assert result["final_state"] == "empty"
    assert result["reward_overlay_seen"] is True
    assert result["reward"]["success"] is True
    assert result["recycle_click"]["clicked"] is True
    assert result["returned_city_panel"]["page_state"] == "city_panel"
    assert len(result["back_clicks"]) == 2
    assert len(rig.app.clicks) == 6


@pytest.mark.parametrize("fail_at,click_count", [(2, 2), (4, 3), (5, 4)])
def test_public_capture_failure_never_dismisses_or_goes_back_blindly(rig, monkeypatch, fail_at, click_count):
    shop = configure_public_flow(rig, monkeypatch, has_rubbish=True)
    rig.app.capture_hook = lambda count, rect: bad_capture("failed", rect) if count == fail_at else None
    with pytest.raises(rubbish.RubbishRecyclingError) as error:
        rubbish.resonance_pc_execute_rubbish_recycling_from_city_panel(
            "test city", app=rig.app, vision=rig.vision, resonance_pc_city_shop_data=shop)
    assert error.value.code == "rubbish_capture_failed"
    assert len(rig.app.clicks) == click_count
    assert Path(rubbish._BACK_TEMPLATE).name not in rig.vision.calls


def test_public_reward_timeout_never_goes_back_or_returns_empty(rig, monkeypatch):
    shop = configure_public_flow(rig, monkeypatch, has_rubbish=True)
    rig.vision.sequences[Path(rubbish._NO_RUBBISH_TEMPLATE).name] = deque([False] * 20)
    with pytest.raises(rubbish.RubbishRecyclingError) as error:
        rubbish.resonance_pc_execute_rubbish_recycling_from_city_panel(
            "test city", app=rig.app, vision=rig.vision, resonance_pc_city_shop_data=shop)
    assert error.value.code == "rubbish_reward_dismiss_timeout"
    assert len(rig.app.clicks) == 3 + rubbish._REWARD_DISMISS_MAX_ATTEMPTS
    assert Path(rubbish._BACK_TEMPLATE).name not in rig.vision.calls


@pytest.mark.parametrize("operation", ["entry", "state"])
def test_late_success_frame_never_authorizes_next_input(rig, operation):
    rig.vision.values(rubbish._ENTRY_TEMPLATE, True)
    rig.vision.values(rubbish._RECYCLE_ALL_TEMPLATE, True)
    def slow_capture(count, rect):
        rig.clock.now += rubbish._MATCH_TIMEOUT_SEC + 1
    rig.app.capture_hook = slow_capture
    with pytest.raises(rubbish.RubbishRecyclingError) as error:
        if operation == "entry":
            rubbish._enter_rubbish_page(rig.app, rig.vision)
        else:
            rubbish._wait_rubbish_state(rig.app, rig.vision)
    assert error.value.code in {"rubbish_recycle_entry_not_found", "rubbish_state_detection_timeout"}
    assert not rig.app.clicks


def test_cancelled_after_public_station_click_does_not_enter_or_go_back(rig, monkeypatch):
    shop = configure_public_flow(rig, monkeypatch)
    rig.app.click_hook = lambda: rig.cancelled.__setitem__(0, True)
    with pytest.raises(asyncio.CancelledError):
        rubbish.resonance_pc_execute_rubbish_recycling_from_city_panel(
            "test city", app=rig.app, vision=rig.vision, resonance_pc_city_shop_data=shop)
    assert len(rig.app.clicks) == 1
    assert rig.app.captures == 0
