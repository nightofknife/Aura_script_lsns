"""Input acknowledgement is not a substitute for fresh visual confirmation."""
from types import SimpleNamespace
import asyncio

import numpy as np
import pytest

from plans.resonance_pc.src.actions import _reforming_center_clicks as clicks
from plans.aura_base.src.services.interaction_service import InteractionService
from tests.interaction_clock import advance, install_clock


class App:
    def __init__(self, response=None, error=None):
        self.response, self.error = response, error
        self.clicks, self.captures = [], []
        self.rgb = np.zeros((720, 1280, 3), dtype=np.uint8)
        self.rgb[0, 0] = [200, 100, 10]

    def capture(self, *, rect):
        self.captures.append(rect)
        return SimpleNamespace(success=True, image=self.rgb.copy())

    def click(self, *, x, y):
        self.clicks.append((x, y))
        if self.error:
            raise self.error
        return self.response


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    with install_clock(monkeypatch, clicks) as clock:
        monkeypatch.setattr(clicks, "_sleep", advance)
        yield clock


def test_capture_converts_rgb_once_without_mutating_source():
    app = App()
    frame = clicks.read_frame(app)
    assert frame.shape == (720, 1280, 3)
    assert frame[0, 0].tolist() == [10, 100, 200]
    assert app.rgb[0, 0].tolist() == [200, 100, 10]
    assert app.captures == [(0, 0, 1280, 720)]


@pytest.mark.parametrize("image,success", [(None, True), (np.zeros((10, 10, 3)), True),
                                         (np.zeros((720, 1280, 3)), False)])
def test_bad_capture_aborts_before_input(image, success):
    app = App()
    app.capture = lambda **_: SimpleNamespace(success=success, image=image)
    with pytest.raises(clicks.CheckedClickError):
        clicks.read_frame(app)
    assert app.clicks == []


def test_none_input_return_remains_unacknowledged_and_quantized():
    app = App()
    record = clicks.send_click(app, (399, 438))
    assert len(app.clicks) == 1
    assert all(value % 5 == 0 for value in app.clicks[0])
    assert record["input_attempted"] is True
    assert record["input_acknowledged"] is None


@pytest.mark.parametrize("response", [False, SimpleNamespace(success=False)])
def test_explicit_input_failure_does_not_probe_or_retry(response):
    app = App(response=response)
    probes = []
    with pytest.raises(clicks.CheckedClickError):
        clicks.checked_click(app, (400, 440), lambda frame: probes.append(frame) or True)
    assert app.clicks == [(400, 440)]
    assert not probes


def test_input_exception_does_not_probe_or_retry():
    app = App(error=RuntimeError("send failed"))
    with pytest.raises(clicks.CheckedClickError):
        clicks.checked_click(app, (400, 440), lambda frame: True)
    assert app.clicks == [(400, 440)]
    assert not app.captures


def test_wait_requires_two_consecutive_equal_fresh_observations():
    app = App()
    values = iter(["dialog", False, "dialog", "dialog"])
    result = clicks.wait_confirmed(app, lambda frame: next(values))
    assert result == "dialog"
    assert len(app.captures) == 4


def test_effect_then_destination_readiness_use_separate_frames():
    app = App()
    seen = []
    def effect(frame):
        seen.append("effect")
        return True
    def ready(frame):
        seen.append("ready")
        return True
    record = clicks.checked_click(app, (960, 535), effect, ready=ready)
    assert record["input_acknowledged"] is None
    assert app.clicks == [(960, 535)]
    assert seen == ["effect", "ready", "ready"]
    assert len(app.captures) == 3


def test_visual_confirmation_failure_never_repeats_submit():
    app = App()
    with pytest.raises(clicks.CheckedClickError):
        clicks.checked_click(app, (960, 535), lambda frame: False)
    assert app.clicks == [(960, 535)]


@pytest.mark.parametrize("response", [False, SimpleNamespace(success=False)])
def test_reused_record_cannot_confirm_a_new_rejected_input(response):
    app = App(response=response)
    record = {"input_attempted": True, "call_returned": True, "effect_confirmed": True}
    with pytest.raises(clicks.CheckedClickError, match="click_input_failed"):
        clicks.send_click(app, (400, 440), record)
    assert record["call_returned"] is False and record["effect_confirmed"] is False
    with pytest.raises(clicks.CheckedClickError, match="click_not_sent"):
        clicks.confirm_click(app, record, lambda frame: pytest.fail("rejection cannot authorize a probe"))
    assert app.clicks == [(400, 440)] and app.captures == []


def test_reused_record_cannot_confirm_a_new_exception():
    app = App(error=RuntimeError("send failed"))
    record = {"call_returned": True}
    with pytest.raises(clicks.CheckedClickError):
        clicks.send_click(app, (400, 440), record)
    assert record["input_attempted"] and record["call_returned"] is False
    assert record["input_error"] == {"type": "RuntimeError", "message": "send failed"}


def test_reused_record_is_invalidated_before_point_validation():
    app = App()
    record = {"input_attempted": True, "call_returned": True}
    with pytest.raises(clicks.CheckedClickError, match="invalid_click_point"):
        clicks.send_click(app, (float("nan"), 440), record)
    assert record["input_attempted"] is False and record["call_returned"] is False
    with pytest.raises(clicks.CheckedClickError, match="click_not_sent"):
        clicks.confirm_click(app, record, lambda frame: True)
    assert app.clicks == [] and app.captures == []


def test_probe_exception_is_not_converted_to_unknown_or_timeout():
    app = App()
    error = RuntimeError("broken domain reader")

    def probe(frame):
        raise error

    with pytest.raises(RuntimeError) as caught:
        clicks.wait_confirmed(app, probe)
    assert caught.value is error and len(app.captures) == 1 and not app.clicks


def test_ready_exception_preserves_completed_effect_without_additional_input():
    app = App()
    record = {}

    def ready(frame):
        raise ValueError("invalid room evidence")

    with pytest.raises(ValueError, match="invalid room evidence"):
        clicks.checked_click(app, (400, 440), lambda frame: "effect", ready, record)
    assert record["effect_confirmed"] and not record["transition_ready"]
    assert record["effect_evidence"] == "effect" and len(app.captures) == 2
    assert app.clicks == [(400, 440)]


def test_late_probe_cannot_succeed_after_total_deadline(no_sleep):
    app = App()

    def probe(frame):
        no_sleep.advance(5)
        return True

    with pytest.raises(clicks.CheckedClickError, match="confirmation_timeout"):
        clicks.wait_confirmed(app, probe)
    assert len(app.captures) == 1 and not app.clicks


def test_parent_deadline_is_not_restarted_by_helper():
    app = App()
    scope = clicks.OperationScope(1)
    with scope.activate():
        with pytest.raises(clicks.CheckedClickError, match="confirmation_timeout"):
            clicks.wait_confirmed(app, lambda frame: True, timeout_sec=5)
    assert len(app.captures) == 1


def test_zero_is_valid_confirmation_evidence():
    app = App()
    assert clicks.wait_confirmed(app, lambda frame: 0) == 0
    assert len(app.captures) == 2


class Runtime:
    def __init__(self):
        self.generation = 1

    def target_summary(self):
        return {"pid": 100, "generation": self.generation}


def test_bound_input_consumes_source_frame_until_fresh_capture():
    app = App()
    app.target_runtime = Runtime()
    service = InteractionService(app=app)
    scope = clicks.OperationScope(5, target=app.target_runtime)
    with clicks.interaction_context(service, scope):
        clicks.read_frame(app)
        first = clicks.send_click(app, (400, 440))
        assert first["call_returned"]
        with pytest.raises(clicks.CheckedClickError) as caught:
            clicks.send_click(app, (400, 440))
        assert caught.value.__cause__.code == "stale_input_frame"
        assert caught.value.data["click"]["input_attempted"] is False
        clicks.read_frame(app)
        clicks.send_click(app, (400, 440))
    assert len(app.captures) == 2 and app.clicks == [(400, 440), (400, 440)]
    assert clicks._INTERACTION.get() is None


def test_bound_frame_cannot_authorize_changed_target_identity():
    app = App()
    app.target_runtime = Runtime()
    with clicks.interaction_context(InteractionService(app=app), clicks.OperationScope(5, target=app.target_runtime)):
        clicks.read_frame(app)
        app.target_runtime.generation += 1
        with pytest.raises(clicks.CheckedClickError) as caught:
            clicks.send_click(app, (400, 440))
        assert caught.value.__cause__.code == "input_target_changed"
    assert len(app.captures) == 1 and not app.clicks


def test_capture_target_change_is_not_accepted_as_fresh_source():
    app = App()
    app.target_runtime = Runtime()
    original = app.capture

    def capture(**kwargs):
        result = original(**kwargs)
        app.target_runtime.generation += 1
        return result

    app.capture = capture
    with clicks.interaction_context(InteractionService(app=app), clicks.OperationScope(5, target=app.target_runtime)):
        with pytest.raises(clicks.CheckedClickError, match="capture_failed") as caught:
            clicks.read_frame(app)
        assert "identity changed" in caught.value.data["error"]
        assert clicks._INTERACTION.get()[2]["frame"] is None
    assert len(app.captures) == 1 and not app.clicks


def test_input_time_consumes_confirmation_total_budget(no_sleep):
    app = App()
    record = {}

    def click(*, x, y):
        app.clicks.append((x, y))
        no_sleep.advance(9)

    app.click = click
    with pytest.raises(clicks.CheckedClickError, match="click_transition_not_confirmed"):
        clicks.checked_click(app, (400, 440), lambda frame: True, record=record)
    assert record["effect_confirmed"] and not record["transition_ready"]
    assert len(app.captures) == 1 and app.clicks == [(400, 440)]


def test_late_input_keeps_submitted_receipt_and_never_retries(no_sleep):
    app = App()
    record = {}

    def click(*, x, y):
        app.clicks.append((x, y))
        no_sleep.advance(11)

    app.click = click
    with pytest.raises(clicks.CheckedClickError, match="click_input_failed"):
        clicks.checked_click(app, (400, 440), lambda frame: True, record=record)
    assert record["input_attempted"] and record["call_returned"]
    assert record["input_acknowledged"] is None and not record["effect_confirmed"]
    assert app.clicks == [(400, 440)] and not app.captures


def test_cancel_after_submitted_input_preserves_receipt_without_confirmation():
    app = App()
    state = {"cancelled": False}
    record = {}

    def cancel_check():
        if state["cancelled"]:
            raise asyncio.CancelledError("stop")

    def click(*, x, y):
        app.clicks.append((x, y))
        state["cancelled"] = True

    app.click = click
    with clicks.cancellation_checks(cancel_check):
        with pytest.raises(asyncio.CancelledError):
            clicks.send_click(app, (400, 440), record)
    assert record["input_attempted"] and record["call_returned"]
    assert record["input_acknowledged"] is None and not record["effect_confirmed"]
    assert app.clicks == [(400, 440)] and not app.captures
