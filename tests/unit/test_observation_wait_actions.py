"""Public wait regression checks with tiny in-memory capture/recognition fakes."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from packages.aura_core.scheduler.operation_scope import OperationScope
from plans.aura_base.src.actions import ocr_actions, wait_actions
from plans.aura_base.src.services.ocr_service import MultiOcrResult, OcrResult
from plans.aura_base.src.services.vision_service import MatchResult


class Clock:
    def __init__(self):
        self.value = 0.

    def now(self):
        return self.value


class App:
    def __init__(self, success=True):
        self.success = success
        self.captures = []

    def capture(self, rect=None):
        self.captures.append(rect)
        width, height = rect[2:] if rect else (64, 48)
        image = np.empty((height, width, 3), dtype=np.uint8)
        image[:] = (1, 2, 3)
        return SimpleNamespace(success=self.success, image=image, relative_rect=rect)


class Ocr:
    def __init__(self, results, callback=None):
        self.results = iter(results)
        self.callback = callback
        self.calls = []

    def _next(self, options):
        self.calls.append(options)
        if self.callback:
            self.callback()
        value = next(self.results)
        if isinstance(value, Exception):
            raise value
        return value

    def find_text(self, **options):
        return self._next(options)

    def recognize_all(self, **options):
        values = self._next(options)
        return MultiOcrResult(len(values), list(values))


class Vision:
    def __init__(self, results, paths=("a.png", "b.png"), callback=None):
        self.results = iter(results)
        self.paths = tuple(map(Path, paths))
        self.callback = callback
        self.calls = []

    def resolve_template(self, _plan, path, _root):
        return Path(path)

    def expand_templates(self, _plan, _ref, _root):
        return list(self.paths)

    def load_template_image(self, _path):
        image = np.empty((2, 2, 3), dtype=np.uint8)
        image[:] = (10, 20, 30)
        return image

    def _next(self, options):
        self.calls.append(options)
        if self.callback:
            self.callback()
        value = next(self.results)
        if isinstance(value, Exception):
            raise value
        return value

    def find_template(self, **options):
        return self._next(options)

    def find_templates_batch(self, **options):
        return self._next(options)


def engine(inspect=False):
    return SimpleNamespace(
        root_context=SimpleNamespace(data={"initial": {"__is_inspect_mode__": inspect}}),
        orchestrator=SimpleNamespace(current_plan_path=Path("."), plan_name="fake"),
    )


def text(value="开始", center=(5, 6)):
    return OcrResult(True, value, center, (1, 2, 8, 8), .9)


def image(center=(5, 6), confidence=.9):
    return MatchResult(True, (1, 2), center, (1, 2, 8, 8), confidence)


@pytest.mark.parametrize("kind", ["text", "set"])
def test_capture_failure_never_proves_disappearance(kind):
    app = App(success=False)
    backend = Ocr([]) if kind == "text" else Vision([])
    if kind == "text":
        result = asyncio.run(wait_actions.wait_for_text_to_disappear(
            app, backend, engine(), "开始", timeout=0))
    else:
        result = asyncio.run(wait_actions.wait_for_templates_in_set_to_disappear(
            app, backend, engine(), "buttons/*", timeout=0))
    assert result is False
    assert len(app.captures) == 1
    assert backend.calls == []


@pytest.mark.parametrize("failure", [RuntimeError("ocr unavailable"),
                                     OcrResult(debug_info={"error": "recognition_failed"})])
def test_ocr_failure_never_proves_disappearance(failure):
    ocr = Ocr([failure])
    assert asyncio.run(wait_actions.wait_for_text_to_disappear(
        App(), ocr, engine(), "开始", timeout=0)) is False
    assert len(ocr.calls) == 1


@pytest.mark.parametrize("entry", ["wait_for_any_template_in_set", "wait_for_templates_in_set_to_disappear"])
def test_empty_template_set_is_configuration_error(entry):
    app = App()
    with pytest.raises(ValueError, match="empty"):
        asyncio.run(getattr(wait_actions, entry)(app, Vision([], paths=()), engine(), "missing/*", timeout=0))
    assert app.captures == []


def test_unknown_template_among_absent_templates_cannot_prove_disappearance():
    vision = Vision([[MatchResult(), MatchResult(debug_info={"error": "template_failed"})]])
    assert asyncio.run(wait_actions.wait_for_templates_in_set_to_disappear(
        App(), vision, engine(), "buttons/*", timeout=0)) is False
    assert len(vision.calls) == 1
    assert vision.calls[0]["template_images"] == ["a.png", "b.png"]


def test_positive_template_evidence_can_succeed_despite_an_unrelated_unknown():
    vision = Vision([[image(), MatchResult(debug_info={"error": "template_failed"})]])
    result = asyncio.run(wait_actions.wait_for_any_template_in_set(
        App(), vision, engine(), "buttons/*", timeout=0))
    assert result["template"] == "a.png"
    assert result["match"].center_point == (5, 6)
    assert len(vision.calls) == 1


@pytest.mark.parametrize("kind", ["text", "image", "set"])
def test_zero_timeout_is_exactly_one_probe_even_when_stability_needs_more(kind):
    app = App()
    if kind == "text":
        backend = Ocr([text()])
        result = asyncio.run(wait_actions.wait_for_text(
            app, backend, engine(), "开始", timeout=0, stable_scans=2))
    elif kind == "image":
        backend = Vision([image()])
        result = asyncio.run(wait_actions.wait_for_image(
            app, backend, engine(), "a.png", timeout=0, stable_scans=2))
    else:
        backend = Vision([[image(), MatchResult()]])
        result = asyncio.run(wait_actions.wait_for_any_template_in_set(
            app, backend, engine(), "buttons/*", timeout=0, stable_scans=2))["match"]
    assert result.found is False
    assert len(app.captures) == len(backend.calls) == 1


@pytest.mark.parametrize("kind", ["text", "image", "set", "text_absent", "set_absent"])
def test_late_recognition_cannot_complete_a_public_wait(kind):
    clock = Clock()
    callback = lambda: setattr(clock, "value", 2.)
    parent = OperationScope(1., monotonic=clock.now)

    async def run():
        with parent.activate():
            if kind == "text":
                return (await wait_actions.wait_for_text(App(), Ocr([text()], callback), engine(), "开始", timeout=0)).found
            if kind == "image":
                return (await wait_actions.wait_for_image(App(), Vision([image()], callback=callback), engine(), "a.png", timeout=0)).found
            if kind == "set":
                return (await wait_actions.wait_for_any_template_in_set(
                    App(), Vision([[image(), MatchResult()]], callback=callback), engine(), "buttons/*", timeout=0))["match"].found
            if kind == "text_absent":
                return await wait_actions.wait_for_text_to_disappear(
                    App(), Ocr([OcrResult()], callback), engine(), "开始", timeout=0)
            return await wait_actions.wait_for_templates_in_set_to_disappear(
                App(), Vision([[MatchResult(), MatchResult()]], callback=callback), engine(), "buttons/*", timeout=0)

    assert asyncio.run(run()) is False


def test_expired_parent_prevents_even_the_legacy_one_probe_capture():
    app, ocr, clock = App(), Ocr([text()]), Clock()
    parent = OperationScope(1., monotonic=clock.now)

    async def run():
        with parent.activate():
            clock.value = 2.
            return await wait_actions.wait_for_text(app, ocr, engine(), "开始", timeout=0)

    assert asyncio.run(run()).found is False
    assert app.captures == ocr.calls == []


@pytest.mark.parametrize("kind", ["image", "set", "set_absent"])
def test_template_path_resolution_spends_the_same_total_wait_budget(monkeypatch, kind):
    clock, app = Clock(), App()
    monkeypatch.setattr(wait_actions, "OperationScope", lambda timeout=None, **kwargs: OperationScope(
        timeout, monotonic=clock.now, **kwargs))
    vision = Vision([image()] if kind == "image" else [[MatchResult(), MatchResult()]])

    def resolve(*_args):
        clock.value = 2.
        return Path("a.png")

    def expand(*_args):
        clock.value = 2.
        return [Path("a.png"), Path("b.png")]

    monkeypatch.setattr(vision, "resolve_template", resolve)
    monkeypatch.setattr(vision, "expand_templates", expand)
    if kind == "image":
        result = asyncio.run(wait_actions.wait_for_image(app, vision, engine(), "a.png", timeout=1)).found
    elif kind == "set":
        vision.results = iter([[image(), MatchResult()]])
        result = asyncio.run(wait_actions.wait_for_any_template_in_set(
            app, vision, engine(), "buttons/*", timeout=1))["match"].found
    else:
        result = asyncio.run(wait_actions.wait_for_templates_in_set_to_disappear(
            app, vision, engine(), "buttons/*", timeout=1))
    assert result is False
    assert app.captures == vision.calls == []


def test_outer_scope_cancellation_propagates_after_recognition():
    stopped = []
    parent = OperationScope(cancel_check=lambda: bool(stopped), cid="wait-cancel-regression")
    ocr = Ocr([text()], lambda: stopped.append(True))

    async def run():
        with parent.activate():
            return await wait_actions.wait_for_text(App(), ocr, engine(), "开始", timeout=0)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run())
    assert len(ocr.calls) == 1


def test_candidates_must_match_the_same_actual_target_on_consecutive_frames():
    ocr = Ocr([[text("开始")], [text("作战")], [text("作战")]])
    result = asyncio.run(wait_actions.wait_for_text(
        App(), ocr, engine(), ["开始", "作战"], timeout=1, interval=0, stable_scans=2))
    assert result.text == result.debug_info["matched_target"] == "作战"
    assert len(ocr.calls) == 3


def test_image_center_tolerance_is_per_axis_between_neighboring_valid_frames():
    vision = Vision([image((10, 10)), image((12, 12)), image((14, 14))])
    result = asyncio.run(wait_actions.wait_for_image(
        App(), vision, engine(), "a.png", timeout=1, interval=0,
        stable_scans=3, stable_center_tolerance_px=2))
    assert result.center_point == (14, 14)
    assert len(vision.calls) == 3


@pytest.mark.parametrize("kind", ["text", "set"])
def test_unknown_resets_stable_absence_in_public_wait(kind):
    if kind == "text":
        backend = Ocr([OcrResult(), RuntimeError("unknown"), OcrResult(), OcrResult()])
        result = asyncio.run(wait_actions.wait_for_text_to_disappear(
            App(), backend, engine(), "开始", timeout=1, interval=0, stable_scans=2))
    else:
        absent = [MatchResult(), MatchResult()]
        backend = Vision([absent, [MatchResult(), MatchResult(debug_info={"error": "unknown"})], absent, absent])
        result = asyncio.run(wait_actions.wait_for_templates_in_set_to_disappear(
            App(), backend, engine(), "buttons/*", timeout=1, interval=0, stable_scans=2))
    assert result is True
    assert len(backend.calls) == 4


def test_text_wait_keeps_roi_orientation_and_inspect_contract():
    app, ocr = App(), Ocr([text()])
    result = asyncio.run(wait_actions.wait_for_text(
        app, ocr, engine(inspect=True), "开始", region=(100, 200, 64, 48), timeout=0,
        recognition_only=True, det_resize_long=960, use_textline_orientation=False))
    assert result.center_point == (105, 206)
    assert result.rect == (101, 202, 8, 8)
    assert app.captures == [(100, 200, 64, 48)]
    call = ocr.calls[0]
    assert (call["recognition_only"], call["det_resize_long"], call["use_textline_orientation"]) == (True, 960, False)
    assert result.debug_info["params"] == dict(text_to_find="开始", region=(100, 200, 64, 48),
        match_mode="contains", normalize=False, min_confidence=0., recognition_only=True,
        det_resize_long=960, use_textline_orientation=False, rotate_tall_crops=False)
    assert result.debug_info["source_image"].shape == (48, 64, 3)


def test_image_wait_keeps_roi_and_rgb_source_bgr_template_inspect_contract():
    result = asyncio.run(wait_actions.wait_for_image(
        App(), Vision([image()]), engine(inspect=True), "a.png", timeout=0, region=(100, 200, 64, 48)))
    assert result.center_point == (105, 206)
    assert result.top_left == (101, 202)
    assert result.rect == (101, 202, 8, 8)
    assert result.debug_info["source_image"][0, 0].tolist() == [1, 2, 3]
    assert result.debug_info["template_image"][0, 0].tolist() == [30, 20, 10]
    assert result.debug_info["params"]["match_method"] == cv2.TM_CCOEFF_NORMED


@pytest.mark.parametrize("actual,target,mode,normalize,expected", [
    (" START  BATTLE！ ", "start battle", "exact", True, True),
    ("执行：开始", "开始", "contains", True, True),
    ("AB-12", r"ab\d+", "regex", True, True),
    ("literal[", "[", "regex", False, True),
    ("AB", "ab", "exact", False, False),
    ("AB", "AB", "invalid", False, False),
])
def test_shared_ocr_matching_keeps_legacy_normalization_and_regex_fallback(actual, target, mode, normalize, expected):
    assert ocr_actions._matches_text(actual, target, mode, normalize=normalize) is expected


def test_shared_ocr_target_normalization_keeps_list_string_conversion_and_validation():
    assert ocr_actions._normalize_targets([1, " ", "开始"]) == ["1", "开始"]
    with pytest.raises(ValueError):
        ocr_actions._normalize_targets([" "])
    with pytest.raises(TypeError):
        ocr_actions._normalize_targets(("开始",))
