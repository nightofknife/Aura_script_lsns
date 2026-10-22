"""Public click action contracts and differential checks against the batch snapshot."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import replace
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pytest

from packages.aura_core.observability import runtime_trace
from packages.aura_core.observability.logging.core_logger import current_cid, reset_cid, set_cid
from packages.aura_core.scheduler import cancellation
from packages.aura_core.scheduler.operation_scope import OperationScope
from packages.aura_core.utils.exceptions import StopTaskException
from plans.aura_base.src.actions import interaction_actions as actions
from plans.aura_base.src.services.interaction_service import InteractionError, InteractionService
from plans.aura_base.src.services.observation_service import ObservationService
from plans.aura_base.src.services.ocr_service import MultiOcrResult, OcrResult
from plans.aura_base.src.services.vision_service import MatchResult


class Clock:
    def __init__(self):
        self.now = 100.

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Target:
    def target_summary(self):
        return {"pid": 7, "hwnd": 8}


class App:
    def __init__(self):
        self.target_runtime = Target()
        self.events = []
        self.on_capture = self.on_move = self.on_click = lambda: None
        self.ack = None

    def capture(self, rect=None):
        self.events.append(("capture", rect))
        self.on_capture()
        width, height = rect[2:] if rect else (64, 64)
        return SimpleNamespace(success=True, image=np.zeros((height, width, 3), dtype=np.uint8), relative_rect=rect)

    def move_to(self, x, y, duration=0.):
        self.events.append(("move", x, y, duration))
        self.on_move()

    def click(self, x=None, y=None, button="left"):
        self.events.append(("click", x, y, button))
        self.on_click()
        return self.ack


class Vision:
    def __init__(self, results=None):
        self.results = deque(results or [MatchResult(True, (1, 2), (2, 3), (1, 2, 2, 2), .95)])
        self.calls = self.resolves = 0
        self.options = []
        self.on_resolve = self.on_match = lambda: None

    def resolve_template(self, *_args):
        self.resolves += 1
        self.on_resolve()
        return "synthetic-button.png"

    def load_template_image(self, _path):
        return np.arange(12, dtype=np.uint8).reshape(2, 2, 3)

    def find_template(self, **kwargs):
        self.calls += 1
        self.options.append({key: value for key, value in kwargs.items() if key not in {"source_image", "template_image"}})
        self.on_match()
        result = self.results.popleft() if len(self.results) > 1 else self.results[0]
        return replace(result, debug_info=dict(result.debug_info))


class Ocr:
    def __init__(self, results=None):
        self.results = results if results is not None else [OcrResult(True, "开始", (2, 3), (1, 2, 2, 2), .95)]
        self.calls = []

    def find_text(self, **kwargs):
        self.calls.append(("find", {key: value for key, value in kwargs.items() if key != "source_image"}))
        value = self.results[0] if self.results else OcrResult()
        return replace(value, debug_info=dict(value.debug_info))

    def recognize_all(self, **kwargs):
        self.calls.append(("all", {key: value for key, value in kwargs.items() if key != "source_image"}))
        return MultiOcrResult(len(self.results), [replace(value, debug_info=dict(value.debug_info)) for value in self.results])


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(cancellation, "_cancelled_cids", set())
    monkeypatch.setattr(cancellation, "_sync_actions", {})
    monkeypatch.setattr(runtime_trace, "_events", deque(maxlen=2048))
    monkeypatch.setattr(runtime_trace, "_active", {})
    monkeypatch.setattr(runtime_trace, "_stops", {})
    monkeypatch.setattr(runtime_trace, "_monitor_thread", SimpleNamespace(is_alive=lambda: True))
    monkeypatch.setattr(runtime_trace.logger, "info", lambda *args, **kwargs: None)
    token = set_cid(uuid4().hex)
    yield
    reset_cid(token)


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()

    class Scope(OperationScope):
        def __init__(self, timeout=None, **options):
            super().__init__(timeout, monotonic=clock, **options)

        def sleep(self, seconds):
            self.checkpoint()
            clock.advance(seconds if self.remaining is None else min(seconds, self.remaining))
            self.checkpoint()

    monkeypatch.setattr(actions, "OperationScope", Scope)
    return clock


@pytest.fixture
def legacy(clock):
    path = Path(__file__).resolve().parents[2] / ".analysis_tmp/interaction-batch2-20261022/plans/aura_base/src/actions/interaction_actions.py"
    if not path.is_file():
        pytest.skip("Local batch 2 baseline is required for the offline old/new comparison")
    name = "plans.aura_base.src.actions._legacy_interaction_action_diff"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    module.time = SimpleNamespace(monotonic=clock, sleep=clock.advance)
    yield module
    sys.modules.pop(name, None)


def invoke(kind, *, module=actions, app=None, vision=None, ocr=None, **options):
    app, vision, ocr = app or App(), vision or Vision(), ocr or Ocr()
    engine = SimpleNamespace(root_context=SimpleNamespace(data={}),
                             orchestrator=SimpleNamespace(current_plan_path=Path("."), plan_name="synthetic"))
    if module is actions:
        options["interaction"] = InteractionService(app, ObservationService(app, vision, ocr))
    if kind == "image":
        return module.find_image_and_click(app, vision, engine, options.pop("template", "button.png"), **options)
    return module.find_text_and_click(app, ocr, engine, options.pop("text_to_find", "开始"), **options)


@pytest.mark.parametrize("kind", ["image", "text"])
def test_timeout_zero_is_one_probe_even_when_stability_needs_more(kind, clock):
    app = App()
    assert invoke(kind, app=app, stable_scans=2, timeout=0.) is False
    assert app.events == [("capture", None)]


@pytest.mark.parametrize("kind", ["image", "text"])
@pytest.mark.parametrize("custom", [None, "精确失败原因"])
def test_required_failure_preserves_messages_and_failure_flag(kind, custom, clock):
    options = {"vision": Vision([MatchResult()])} if kind == "image" else {"ocr": Ocr([])}
    with pytest.raises(StopTaskException) as caught:
        invoke(kind, required=True, failure_message=custom, **options)
    expected = custom or ("未能在指定区域找到图像 'button.png'。" if kind == "image" else "未能在指定区域找到文本 '开始'。")
    assert str(caught.value) == expected
    assert caught.value.success is False


@pytest.mark.parametrize("kind", ["image", "text"])
def test_timeout_zero_inherits_expired_parent_and_does_not_resolve_or_capture(kind, clock):
    app, vision = App(), Vision()
    parent = OperationScope(1, target=app.target_runtime, monotonic=clock)
    with parent.activate():
        clock.advance(2)
        assert invoke(kind, app=app, vision=vision, timeout=0.) is False
    assert app.events == []
    assert vision.resolves == 0


@pytest.mark.parametrize("kind", ["image", "text"])
def test_movement_cancellation_prevents_click(kind, clock):
    app = App()
    app.on_move = lambda: cancellation.request_task_cancel(current_cid())
    with pytest.raises(asyncio.CancelledError):
        invoke(kind, app=app)
    assert [event[0] for event in app.events] == ["capture", "move"]


@pytest.mark.parametrize("kind", ["image", "text"])
@pytest.mark.parametrize("required", [False, True])
def test_input_failure_is_technical_error_never_false_or_required_not_found(kind, required, clock):
    app = App()

    def fail():
        raise RuntimeError("backend unavailable")

    app.on_click = fail
    with pytest.raises(InteractionError) as caught:
        invoke(kind, app=app, required=required)
    assert caught.value.code == "input_failed"
    assert caught.value.receipts[0].attempted
    assert not caught.value.receipts[0].call_returned


@pytest.mark.parametrize("kind", ["image", "text"])
def test_after_click_wait_consumes_total_budget_without_reclick(kind, clock):
    app = App()
    assert invoke(kind, app=app, timeout=1., after_click_sec=2.) is False
    assert [event[0] for event in app.events] == ["capture", "move", "click"]
    assert clock.now == 101.


@pytest.mark.parametrize("normalized", [False, True])
def test_all_ocr_options_reach_backend_and_coordinates_are_client_coordinates(clock, normalized):
    app, ocr = App(), Ocr()
    targets = ["开始", "结束"] if normalized else "开始"
    assert invoke("text", app=app, ocr=ocr, text_to_find=targets, region=(10, 20, 8, 8),
                  match_mode="exact", normalize=normalized, min_confidence=.8 if normalized else 0.,
                  recognition_only=True, det_resize_long=960, use_textline_orientation=False,
                  rotate_tall_crops=True, button="right", move_duration=.4)
    mode, options = ocr.calls[0]
    assert mode == ("all" if normalized else "find")
    assert options["recognition_only"] is True
    assert options["det_resize_long"] == 960
    assert options["use_textline_orientation"] is False
    assert options["rotate_tall_crops"] is True
    assert app.events[-2:] == [("move", 12, 23, .4), ("click", 12, 23, "right")]


@pytest.mark.parametrize("kind", ["image", "text"])
def test_old_new_default_hit_roi_coordinates_and_input_order_agree(kind, clock, legacy):
    old, new = App(), App()
    options = dict(region=(10, 20, 8, 8), button="right", move_duration=.4)
    assert invoke(kind, module=legacy, app=old, **options) is True
    assert invoke(kind, app=new, **options) is True
    assert old.events == new.events == [("capture", (10, 20, 8, 8)),
                                       ("move", 12, 23, .4), ("click", 12, 23, "right")]


@pytest.mark.parametrize("kind", ["image", "text"])
def test_old_new_missing_source_is_false_without_input(kind, clock, legacy):
    for module in (legacy, actions):
        app = App()
        options = {"vision": Vision([MatchResult()])} if kind == "image" else {"ocr": Ocr([])}
        assert invoke(kind, module=module, app=app, **options) is False
        assert app.events == [("capture", None)]


def test_old_new_image_stability_preserves_final_input_and_resolves_once(clock, legacy):
    results = [MatchResult(True, (0, 0), center, (0, 0, 2, 2), .95)
               for center in [(10, 10), (20, 10), (21, 11)]]
    apps, providers = [], []
    for module in (legacy, actions):
        app, vision = App(), Vision(results)
        assert invoke("image", module=module, app=app, vision=vision, timeout=1., interval=.01,
                      stable_scans=2, stable_center_tolerance_px=2)
        apps.append(app)
        providers.append(vision)
    assert apps[0].events == apps[1].events
    assert apps[1].events[-2:] == [("move", 21, 11, .2), ("click", 21, 11, "left")]
    assert [provider.calls for provider in providers] == [3, 3]
    assert [provider.resolves for provider in providers] == [3, 1]


def test_intentional_difference_late_hit_no_longer_authorizes_input(clock, legacy):
    for module, expected in [(legacy, True), (actions, False)]:
        app, vision = App(), Vision()
        vision.on_match = lambda: clock.advance(2.)
        assert invoke("image", module=module, app=app, vision=vision, timeout=1.) is expected
        assert sum(event[0] == "click" for event in app.events) == int(expected)


def test_resource_resolution_is_inside_budget_before_first_capture(clock):
    app, vision = App(), Vision()
    vision.on_resolve = lambda: clock.advance(2.)
    assert invoke("image", app=app, vision=vision, timeout=1.) is False
    assert app.events == []


@pytest.mark.parametrize("kind", ["image", "text"])
def test_none_acknowledgement_still_means_request_returned_without_effect_claim(kind, clock, legacy):
    for module in (legacy, actions):
        app = App()
        app.ack = None
        assert invoke(kind, module=module, app=app) is True


def test_intentional_difference_explicit_backend_rejection_is_not_success(clock, legacy):
    old, new = App(), App()
    old.ack = new.ack = False
    assert invoke("image", module=legacy, app=old) is True
    with pytest.raises(InteractionError) as caught:
        invoke("image", app=new)
    assert caught.value.code == "input_rejected"


def test_old_new_normalized_ocr_selection_and_all_backend_options_agree(clock, legacy):
    observed = []
    for module in (legacy, actions):
        app = App()
        ocr = Ocr([OcrResult(True, "开始", (1, 1), (0, 0, 2, 2), .5),
                   OcrResult(True, " START  BATTLE！ ", (4, 5), (3, 4, 2, 2), .96)])
        assert invoke("text", module=module, app=app, ocr=ocr, text_to_find=["开始", "start battle"],
                      normalize=True, min_confidence=.7, region=(10, 20, 8, 8),
                      recognition_only=True, det_resize_long=960, use_textline_orientation=False,
                      rotate_tall_crops=True)
        observed.append((app.events, ocr.calls))
    assert observed[0] == observed[1]
    assert observed[1][0][-2:] == [("move", 14, 25, .2), ("click", 14, 25, "left")]


def test_intentional_difference_after_click_wait_is_inside_total_budget(clock, legacy):
    for module, expected in [(legacy, True), (actions, False)]:
        app = App()
        assert invoke("image", module=module, app=app, timeout=1., after_click_sec=2.) is expected
        assert [event[0] for event in app.events] == ["capture", "move", "click"]
