"""Observation contracts using synthetic pixels and deterministic recognizers."""
import asyncio
from collections import deque
from types import SimpleNamespace
import threading
import time

import numpy as np
import pytest

from packages.aura_core.observability import runtime_trace
from packages.aura_core.scheduler import cancellation, operation_scope
from packages.aura_core.scheduler.operation_scope import OperationScope, OperationTimeout, TargetOperationBusy
from plans.aura_base.src.services.observation_service import (
    Frame, Observation, ObservationService,
)
from plans.aura_base.src.services.ocr_service import MultiOcrResult, OcrResult
from plans.aura_base.src.services.vision_service import MatchResult


class Clock:
    def __init__(self):
        self.now = 100.

    def __call__(self):
        return self.now


class Target:
    def __init__(self):
        self.identity = {"pid": 1, "hwnd": 2}

    def target_summary(self):
        return dict(self.identity)


class App:
    def __init__(self):
        self.target_runtime = Target()
        self.image = np.arange(90, dtype=np.uint8).reshape(5, 6, 3)
        self.calls = 0
        self.success = True
        self.on_capture = lambda: None

    def capture(self, rect=None):
        self.calls += 1
        self.on_capture()
        return SimpleNamespace(success=self.success, image=self.image,
                               relative_rect=rect, error_message="capture_unavailable")


class Vision:
    def __init__(self):
        self.sources = []
        self.batch_calls = 0
        self.result = MatchResult(True, (0, 0), (1, 1), (0, 0, 2, 2), .95)
        self.before_result = lambda: None

    def find_template(self, **kwargs):
        self.sources.append(kwargs["source_image"].copy())
        self.before_result()
        return self.result

    def find_templates_batch(self, **kwargs):
        self.batch_calls += 1
        self.sources.append(kwargs["source_image"].copy())
        self.before_result()
        return [self.result for _ in kwargs["template_images"]]


class Ocr:
    def __init__(self):
        self.result = OcrResult(True, "确定", (1, 1), (0, 0, 2, 2), .9)
        self.before_result = lambda: None

    def find_text(self, **kwargs):
        self.before_result()
        return self.result

    def recognize_all(self, **kwargs):
        self.before_result()
        return MultiOcrResult(1, [self.result])


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(cancellation, "_cancelled_cids", set())
    monkeypatch.setattr(cancellation, "_sync_actions", {})
    monkeypatch.setattr(runtime_trace, "_events", deque(maxlen=2048))
    monkeypatch.setattr(runtime_trace, "_active", {})
    monkeypatch.setattr(runtime_trace, "_stops", {})
    monkeypatch.setattr(runtime_trace, "_monitor_thread", SimpleNamespace(is_alive=lambda: True))
    monkeypatch.setattr(runtime_trace.logger, "info", lambda *args, **kwargs: None)
    monkeypatch.setattr(operation_scope, "get_config_value", lambda *args: .1)


@pytest.fixture
def setup():
    app, vision, ocr = App(), Vision(), Ocr()
    return ObservationService(app, vision, ocr), app, vision, ocr


@pytest.mark.parametrize("failure", ["reported", "missing", "empty"])
def test_failed_capture_is_unknown_and_cannot_confirm_absence(setup, failure):
    service, app, _, _ = setup
    if failure == "reported":
        app.success = False
    elif failure == "missing":
        app.image = None
    else:
        app.image = np.zeros((0, 0, 3), dtype=np.uint8)
    scope = OperationScope(target=app.target_runtime)
    probe_calls = []

    def probe(frame):
        probe_calls.append(frame.id)
        return Observation("absent")

    result = service.wait(lambda: service.observe(probe), lambda value: value.state == "absent",
                          scope=scope, max_probes=2, interval=0)
    assert not result.matched
    assert result.observation.state == "unknown"
    assert probe_calls == []
    assert app.calls == 2


def test_capture_identity_change_is_unknown(setup):
    service, app, _, _ = setup
    app.on_capture = lambda: app.target_runtime.identity.update(hwnd=3)
    value = service.observe(lambda frame: Observation("absent"))
    assert value.state == "unknown"
    assert "identity changed" in value.reason


@pytest.mark.parametrize("async_mode", [False, True])
def test_capture_time_is_included_in_total_deadline(setup, async_mode):
    service, app, _, _ = setup
    clock = Clock()
    scope = OperationScope(1, target=app.target_runtime, monotonic=clock)
    app.on_capture = lambda: setattr(clock, "now", clock.now + 2)
    with pytest.raises(OperationTimeout):
        if async_mode:
            asyncio.run(service.capture_async(scope=scope))
        else:
            service.capture(scope=scope)
    assert app.calls == 1


@pytest.mark.parametrize("recognizer", ["template", "ocr"])
@pytest.mark.parametrize("async_mode", [False, True])
def test_recognition_time_is_included_and_late_hit_is_not_success(setup, recognizer, async_mode):
    service, app, vision, ocr = setup
    clock = Clock()
    scope = OperationScope(1, target=app.target_runtime, monotonic=clock)
    engine = vision if recognizer == "template" else ocr
    engine.before_result = lambda: setattr(clock, "now", clock.now + 2)
    template = np.zeros((2, 2, 3), dtype=np.uint8)

    def probe(frame):
        if recognizer == "template":
            return service.match_template(frame, template)
        return service.find_text(frame, "确定")

    with pytest.raises(OperationTimeout):
        if async_mode:
            asyncio.run(service.observe_async(probe, scope=scope))
        else:
            service.observe(probe, scope=scope)


@pytest.mark.parametrize("async_mode", [False, True])
def test_late_comparison_cannot_complete_wait(setup, async_mode):
    service, _, _, _ = setup
    clock = Clock()
    scope = OperationScope(1, monotonic=clock)

    def accept(_observation):
        clock.now += 2
        return True

    if async_mode:
        result = asyncio.run(service.wait_async(lambda: Observation("present", "f1"), accept, scope=scope))
    else:
        result = service.wait(lambda: Observation("present", "f1"), accept, scope=scope)
    assert not result.matched
    assert result.reason == "timeout"


def test_one_frame_shared_probes_failure_is_local_and_scope_is_inherited(setup):
    service, app, _, _ = setup
    frame_ids = []

    def invalid(frame):
        frame_ids.append(frame.id)
        raise RuntimeError("broken recognizer")

    def valid(frame):
        frame_ids.append(frame.id)
        return service.match_template(frame, np.zeros((2, 2, 3), dtype=np.uint8))

    values = service.observe({"bad": invalid, "good": valid})
    assert app.calls == 1
    assert frame_ids[0] == frame_ids[1] == values["good"].frame_id
    assert values["bad"].state == "unknown"
    assert values["good"].state == "present"


def test_frame_is_immutable_bgr_conversion_and_nested_roi_offset_once(setup):
    service, _, vision, _ = setup
    pixels = np.zeros((5, 6, 3), dtype=np.uint8)
    pixels[..., 0], pixels[..., 1], pixels[..., 2] = 10, 20, 30
    frame = Frame.from_image(pixels, color_space="BGR", roi=(10, 20, 6, 5), frame_id="one")
    pixels[:] = 0
    with pytest.raises(ValueError):
        frame.image.setflags(write=True)
    cropped = frame.crop((11, 21, 4, 3))
    value = service.match_template(cropped, np.zeros((2, 2, 3), dtype=np.uint8), roi=(12, 22, 3, 2))
    assert tuple(vision.sources[0][0, 0]) == (30, 20, 10)
    assert value.frame_id == "one"
    assert value.client_center == (13, 23)
    assert value.value.top_left == (12, 22)
    assert value.value.rect == (12, 22, 2, 2)
    assert vision.result.center_point == (1, 1)


def test_batch_matching_uses_one_batch_and_preserves_invalid_member(setup):
    service, _, vision, _ = setup
    frame = Frame.from_image(np.zeros((5, 6, 3), dtype=np.uint8))
    good, bad = np.zeros((2, 2, 3), dtype=np.uint8), np.zeros((20, 20, 3), dtype=np.uint8)
    values = service.match_templates(frame, [good, bad, good])
    assert vision.batch_calls == 1
    assert [item.state for item in values] == ["present", "unknown", "present"]
    assert all(item.frame_id == frame.id for item in values)


@pytest.mark.parametrize("result", [
    MatchResult(debug_info={"error": "engine failed"}),
    MatchResult(False, confidence=float("nan")),
    MatchResult(True, center_point=(float("inf"), 0), confidence=.9),
    MatchResult(True, center_point=(99, 0), confidence=.9),
])
def test_invalid_match_cannot_prove_presence_or_absence(setup, result):
    service, _, vision, _ = setup
    vision.result = result
    frame = Frame.from_image(np.zeros((5, 6, 3), dtype=np.uint8))
    assert service.match_template(frame, np.zeros((2, 2, 3), dtype=np.uint8)).state == "unknown"


def test_successful_empty_recognition_is_valid_absence(setup):
    service, _, vision, ocr = setup
    vision.result, ocr.result = MatchResult(), OcrResult()
    frame = Frame.from_image(np.zeros((5, 6, 3), dtype=np.uint8))
    assert service.match_template(frame, np.zeros((2, 2, 3), dtype=np.uint8)).state == "absent"
    assert service.find_text(frame, "确定").state == "absent"


@pytest.mark.parametrize("async_mode", [False, True])
def test_stability_counts_distinct_frames_and_unknown_resets(setup, async_mode):
    service, _, _, _ = setup
    sequence = iter([
        Observation("present", "a", semantic_key="button"),
        Observation("present", "a", semantic_key="button"),
        Observation("unknown", "b"),
        Observation("present", "c", semantic_key="button"),
        Observation("present", "d", semantic_key="button"),
    ])
    kwargs = dict(scope=OperationScope(), stable_scans=2, max_probes=5, interval=0)
    if async_mode:
        result = asyncio.run(service.wait_async(lambda: next(sequence), lambda value: True, **kwargs))
    else:
        result = service.wait(lambda: next(sequence), lambda value: True, **kwargs)
    assert result.matched
    assert result.probes == 5
    assert result.observation.frame_id == "d"


def test_semantic_and_position_change_restart_stability(setup):
    service, _, _, _ = setup
    sequence = iter([
        Observation("present", "1", semantic_key="old", client_center=(1, 1)),
        Observation("present", "2", semantic_key="new", client_center=(1, 1)),
        Observation("present", "3", semantic_key="new", client_center=(9, 9)),
        Observation("present", "4", semantic_key="new", client_center=(9, 9)),
    ])
    result = service.wait(lambda: next(sequence), lambda value: True, scope=OperationScope(),
                          stable_scans=2, center_tolerance=1, max_probes=4, interval=0)
    assert result.matched and result.probes == 4


def test_cancellation_is_not_converted_to_unknown_or_timeout(setup):
    service, app, _, _ = setup
    scope = OperationScope(cid="cancel-probe", target=app.target_runtime)

    def probe(_frame):
        cancellation.request_task_cancel(scope.cid)
        raise RuntimeError("failed at cancellation")

    with pytest.raises(asyncio.CancelledError):
        service.observe(probe, scope=scope)


def test_sync_async_observation_outputs_agree(setup):
    service, _, _, _ = setup
    frame = Frame.from_image(np.zeros((5, 6, 3), dtype=np.uint8), frame_id="shared")
    template = np.zeros((2, 2, 3), dtype=np.uint8)
    probe = lambda current: service.match_template(current, template)
    sync = service.observe(probe, frame=frame)
    asynchronous = asyncio.run(service.observe_async(probe, frame=frame))
    assert sync == asynchronous


def test_implicit_probe_scope_inherits_outer_deadline(setup):
    service, app, _, _ = setup
    clock = Clock()
    parent = OperationScope(1, target=app.target_runtime, monotonic=clock)
    app.on_capture = lambda: setattr(clock, "now", clock.now + 2)
    with parent.activate():
        with pytest.raises(OperationTimeout):
            service.observe(lambda frame: Observation("present"))


def test_async_capture_cancellation_blocks_fresh_target_use_until_real_capture_finishes(setup, monkeypatch):
    service, app, _, _ = setup
    monkeypatch.setattr(operation_scope, "get_config_value", lambda *args: 0.)

    async def scenario():
        entered, release = threading.Event(), threading.Event()
        scope = OperationScope(cid="capture-cancel", target=app.target_runtime)

        def blocking_capture():
            entered.set()
            assert release.wait(2)

        async def until(predicate):
            deadline = time.monotonic() + 2
            while not predicate():
                assert time.monotonic() < deadline
                await asyncio.sleep(.001)

        app.on_capture = blocking_capture
        task = asyncio.create_task(service.capture_async(scope=scope))
        try:
            await until(entered.is_set)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            with pytest.raises(TargetOperationBusy) as error:
                service.capture(scope=OperationScope(target=app.target_runtime))
            assert error.value.quarantined
            release.set()
            await until(lambda: not cancellation.has_pending_sync_actions(scope.cid))
            await until(lambda: not runtime_trace.pending_activities(scope.cid))
            app.on_capture = lambda: None
            assert service.capture().image.shape == app.image.shape
        finally:
            release.set()

    asyncio.run(scenario())
