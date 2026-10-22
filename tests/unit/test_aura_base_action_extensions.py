from __future__ import annotations

import asyncio
from types import SimpleNamespace

import numpy as np
import pytest

from packages.aura_core.utils.exceptions import StopTaskException
from packages.aura_core.scheduler.operation_scope import OperationScope
from plans.aura_base.src.actions import interaction_actions, ocr_actions, wait_actions
from plans.aura_base.src.services.interaction_service import InteractionService
from plans.aura_base.src.services.ocr_service import MultiOcrResult, OcrResult
from plans.aura_base.src.services.observation_service import Observation, ObservationService
from plans.aura_base.src.services.vision_service import MatchResult


class _Engine:
    root_context = SimpleNamespace(data={})


class _App:
    def __init__(self) -> None:
        self.moves: list[tuple[int, int, float]] = []
        self.clicks: list[tuple[int, int, str]] = []

    def capture(self, rect=None):
        width = int(rect[2]) if rect else 256
        height = int(rect[3]) if rect else 256
        return SimpleNamespace(
            success=True,
            image=np.zeros((height, width, 3), dtype=np.uint8),
        )

    def move_to(self, x, y, duration=0.0):
        self.moves.append((int(x), int(y), float(duration)))

    def click(self, x=None, y=None, button="left", **_kwargs):
        self.clicks.append((int(x), int(y), str(button)))


class _Ocr:
    def __init__(self, results: list[OcrResult]) -> None:
        self.results = results
        self.find_calls = 0
        self.recognize_calls = 0

    def find_text(self, source_image, text_to_find, match_mode, *, recognition_only=False, det_resize_long=None, use_textline_orientation=True, rotate_tall_crops=False):
        self.find_calls += 1
        return self.results[0] if self.results else OcrResult(found=False)

    def recognize_all(self, source_image, *, recognition_only=False, det_resize_long=None, use_textline_orientation=True, rotate_tall_crops=False):
        self.recognize_calls += 1
        return MultiOcrResult(count=len(self.results), results=list(self.results))


def _click_service(monkeypatch, app, kind, probe):
    """Inject recognizer results while retaining real observation/input policies."""
    observation = ObservationService(app=app)
    monkeypatch.setattr(interaction_actions, "resolve_template_path", lambda _engine, _vision, path: path)

    def lookup(frame, source, **options):
        result = probe(frame, source, **options)
        key = source if kind == "image" else result.debug_info.get("matched_target", source)
        return Observation("present" if result.found else "absent", frame.id,
                           result, key, result.center_point)

    monkeypatch.setattr(observation, "match_template" if kind == "image" else "find_text", lookup)
    return InteractionService(app, observation)


def _patch_wait_lookup(monkeypatch, kind, probe):
    """Feed public observation probes; keep the real shared wait/stability loop."""
    app = SimpleNamespace(capture=lambda rect: SimpleNamespace(
        success=True, image=np.zeros((64, 64, 3), dtype=np.uint8)))
    service = ObservationService(app=app)
    monkeypatch.setattr(wait_actions, "_service", lambda *_args, **_kwargs: service)
    monkeypatch.setattr(wait_actions, "resolve_template_path", lambda _engine, _vision, path: path)
    monkeypatch.setattr(wait_actions, "expand_template_paths", lambda *_args: ["a.png", "b.png"])

    def observed(frame, result, key):
        return Observation("present" if result.found else "absent", frame.id, result,
                           key, result.center_point)

    if kind == "find_text":
        def find_text(frame, text_to_find, **options):
            result = probe(frame, text_to_find, **options)
            return observed(frame, result, result.debug_info.get("matched_target"))
        monkeypatch.setattr(service, "find_text", find_text)
    elif kind == "find_image":
        def find_image(frame, template, **options):
            return observed(frame, probe(frame, template, **options), template)
        monkeypatch.setattr(service, "match_template", find_image)
    else:
        def find_templates(frame, paths, **options):
            result = probe(frame, paths, **options)
            matches = {str(item["template"]): item["match"] for item in result["matches"]}
            if result["count"] and not matches:
                matches[str(paths[0])] = MatchResult(found=True, center_point=(5, 5))
            return tuple(observed(frame, matches.get(str(path), MatchResult()), str(path)) for path in paths)
        monkeypatch.setattr(service, "match_templates", find_templates)


def test_find_text_keeps_legacy_single_target_path():
    app = _App()
    ocr = _Ocr(
        [
            OcrResult(
                found=True,
                text="开始",
                center_point=(5, 6),
                rect=(1, 2, 8, 9),
                confidence=0.8,
            )
        ]
    )

    result = ocr_actions.find_text(
        app=app,
        ocr=ocr,
        engine=_Engine(),
        text_to_find="开始",
        region=(10, 20, 30, 40),
        match_mode="contains",
    )

    assert result.found is True
    assert result.center_point == (15, 26)
    assert result.debug_info["matched_target"] == "开始"
    assert ocr.find_calls == 1
    assert ocr.recognize_calls == 0


def test_find_text_supports_normalized_multi_target_and_confidence_floor():
    app = _App()
    ocr = _Ocr(
        [
            OcrResult(
                found=True,
                text="开始",
                center_point=(4, 5),
                rect=(1, 1, 5, 5),
                confidence=0.55,
            ),
            OcrResult(
                found=True,
                text=" START  BATTLE！ ",
                center_point=(7, 8),
                rect=(2, 3, 6, 7),
                confidence=0.96,
            ),
        ]
    )

    result = ocr_actions.find_text(
        app=app,
        ocr=ocr,
        engine=_Engine(),
        text_to_find=["开始", "start battle"],
        region=(100, 200, 30, 40),
        match_mode="contains",
        normalize=True,
        min_confidence=0.7,
    )

    assert result.found is True
    assert result.text == " START  BATTLE！ "
    assert result.center_point == (107, 208)
    assert result.debug_info["matched_target"] == "start battle"
    assert ocr.find_calls == 0
    assert ocr.recognize_calls == 1


def test_find_image_and_click_waits_for_stable_center(monkeypatch):
    app = _App()
    matches = iter(
        [
            MatchResult(found=True, center_point=(100, 100), confidence=0.9),
            MatchResult(found=True, center_point=(108, 100), confidence=0.91),
            MatchResult(found=True, center_point=(109, 101), confidence=0.92),
        ]
    )
    interaction = _click_service(monkeypatch, app, "image", lambda *_args, **_kwargs: next(matches))
    monkeypatch.setattr(OperationScope, "sleep", lambda self, _seconds: self.checkpoint())

    clicked = interaction_actions.find_image_and_click(
        app=app,
        vision=object(),
        engine=_Engine(),
        template="button.png",
        timeout=1.0,
        interval=0.01,
        stable_scans=2,
        stable_center_tolerance_px=2,
        after_click_sec=0.2,
        required=True,
        interaction=interaction,
    )

    assert clicked is True
    assert app.moves == [(109, 101, 0.2)]
    assert app.clicks == [(109, 101, "left")]


def test_find_image_and_click_default_is_one_probe(monkeypatch):
    calls = []

    def fake_find_image(*_args, **_kwargs):
        calls.append(True)
        return MatchResult(found=False)

    app = _App()
    interaction = _click_service(monkeypatch, app, "image", fake_find_image)

    clicked = interaction_actions.find_image_and_click(
        app=app,
        vision=object(),
        engine=_Engine(),
        template="missing.png",
        interaction=interaction,
    )

    assert clicked is False
    assert len(calls) == 1


def test_find_text_and_click_required_failure_keeps_default_message(monkeypatch):
    app = _App()
    interaction = _click_service(monkeypatch, app, "text", lambda *_args, **_kwargs: OcrResult(found=False))

    with pytest.raises(StopTaskException, match="未能在指定区域找到文本 '开始'。"):
        interaction_actions.find_text_and_click(
            app=app,
            ocr=object(),
            engine=_Engine(),
            text_to_find="开始",
            timeout=0.0,
            required=True,
            interaction=interaction,
        )


def test_find_image_and_click_uses_custom_failure_message(monkeypatch):
    app = _App()
    interaction = _click_service(monkeypatch, app, "image", lambda *_args, **_kwargs: MatchResult(found=False))

    with pytest.raises(StopTaskException, match="^没有找到确认按钮$"):
        interaction_actions.find_image_and_click(
            app=app,
            vision=object(),
            engine=_Engine(),
            template="confirm.png",
            required=True,
            failure_message="没有找到确认按钮",
            interaction=interaction,
        )


def test_find_text_and_click_uses_custom_failure_message(monkeypatch):
    app = _App()
    interaction = _click_service(monkeypatch, app, "text", lambda *_args, **_kwargs: OcrResult(found=False))

    with pytest.raises(StopTaskException, match="^没有找到开始文字$"):
        interaction_actions.find_text_and_click(
            app=app,
            ocr=object(),
            engine=_Engine(),
            text_to_find="开始",
            required=True,
            failure_message="没有找到开始文字",
            interaction=interaction,
        )


def test_click_cancels_before_first_probe(monkeypatch):
    app = _App()
    cancelled = False
    probe_calls = []

    def probe(*_args, **_kwargs):
        probe_calls.append(True)
        return MatchResult(found=False)

    interaction = _click_service(monkeypatch, app, "image", probe)
    with OperationScope(cancel_check=lambda: cancelled).activate():
        cancelled = True
        with pytest.raises(asyncio.CancelledError):
            interaction_actions.find_image_and_click(
                app=app, vision=object(), engine=_Engine(), template="button.png",
                timeout=1., interval=.2, interaction=interaction)

    assert probe_calls == []


def test_click_cancels_during_interval_sleep(monkeypatch):
    app = _App()
    cancelled = False
    probe_calls = []

    def fake_sleep(scope, _seconds):
        nonlocal cancelled
        cancelled = True
        scope.checkpoint()

    def probe(*_args, **_kwargs):
        probe_calls.append(True)
        return MatchResult(found=False)

    interaction = _click_service(monkeypatch, app, "image", probe)
    monkeypatch.setattr(OperationScope, "sleep", fake_sleep)
    with OperationScope(cancel_check=lambda: cancelled).activate():
        with pytest.raises(asyncio.CancelledError):
            interaction_actions.find_image_and_click(
                app=app, vision=object(), engine=_Engine(), template="button.png",
                timeout=1., interval=.2, interaction=interaction)

    assert len(probe_calls) == 1


def test_find_image_and_click_cancels_during_after_click_wait(monkeypatch):
    app = _App()
    cancelled = False

    def fake_sleep(scope, _seconds):
        nonlocal cancelled
        cancelled = True
        scope.checkpoint()

    interaction = _click_service(monkeypatch, app, "image", lambda *_args, **_kwargs: MatchResult(
        found=True, center_point=(10, 20), confidence=.9))
    monkeypatch.setattr(OperationScope, "sleep", fake_sleep)
    with OperationScope(cancel_check=lambda: cancelled).activate():
        with pytest.raises(asyncio.CancelledError):
            interaction_actions.find_image_and_click(
                app=app, vision=object(), engine=_Engine(), template="button.png",
                after_click_sec=1., interaction=interaction)

    assert app.clicks == [(10, 20, "left")]


def test_wait_for_text_requires_same_target_to_be_stable(monkeypatch):
    results = iter(
        [
            OcrResult(found=True, text="开始", debug_info={"matched_target": "开始"}),
            OcrResult(found=True, text="作战", debug_info={"matched_target": "作战"}),
            OcrResult(found=True, text="作战", debug_info={"matched_target": "作战"}),
        ]
    )
    calls = []

    def fake_find_text(*_args, **_kwargs):
        calls.append(True)
        return next(results)

    _patch_wait_lookup(monkeypatch, "find_text", fake_find_text)

    result = asyncio.run(
        wait_actions.wait_for_text(
            app=object(),
            ocr=object(),
            engine=_Engine(),
            text_to_find=["开始", "作战"],
            timeout=1.0,
            interval=0.0,
            stable_scans=2,
        )
    )

    assert result.text == "作战"
    assert len(calls) == 3


def test_wait_for_image_resets_stability_after_center_jump(monkeypatch):
    results = iter(
        [
            MatchResult(found=True, center_point=(10, 10), confidence=0.8),
            MatchResult(found=True, center_point=(20, 10), confidence=0.9),
            MatchResult(found=True, center_point=(21, 11), confidence=0.95),
        ]
    )
    calls = []

    def fake_find_image(*_args, **_kwargs):
        calls.append(True)
        return next(results)

    _patch_wait_lookup(monkeypatch, "find_image", fake_find_image)

    result = asyncio.run(
        wait_actions.wait_for_image(
            app=object(),
            vision=object(),
            engine=_Engine(),
            template="button.png",
            timeout=1.0,
            interval=0.0,
            stable_scans=2,
            stable_center_tolerance_px=2,
        )
    )

    assert result.center_point == (21, 11)
    assert len(calls) == 3


def test_wait_for_template_set_disappearance_is_stable(monkeypatch):
    counts = iter([0, 1, 0, 0])
    calls = []

    def fake_find_templates(*_args, **_kwargs):
        calls.append(True)
        return {"count": next(counts), "matches": []}

    _patch_wait_lookup(monkeypatch, "find_templates_in_set", fake_find_templates)

    disappeared = asyncio.run(
        wait_actions.wait_for_templates_in_set_to_disappear(
            app=object(),
            vision=object(),
            engine=_Engine(),
            templates_ref="button.png",
            timeout=1.0,
            interval=0.0,
            stable_scans=2,
        )
    )

    assert disappeared is True
    assert len(calls) == 4


def test_wait_for_text_set_disappearance_resets_when_any_target_reappears(monkeypatch):
    results = iter(
        [
            OcrResult(found=False),
            OcrResult(found=True, text="作战", debug_info={"matched_target": "作战"}),
            OcrResult(found=False),
            OcrResult(found=False),
        ]
    )
    calls = []

    def fake_find_text(*args, **kwargs):
        calls.append((args, kwargs))
        return next(results)

    _patch_wait_lookup(monkeypatch, "find_text", fake_find_text)

    disappeared = asyncio.run(
        wait_actions.wait_for_text_to_disappear(
            app=object(),
            ocr=object(),
            engine=_Engine(),
            text_to_monitor=["开始", "作战"],
            timeout=1.0,
            interval=0.0,
            stable_scans=2,
            normalize=True,
            min_confidence=0.75,
        )
    )

    assert disappeared is True
    assert len(calls) == 4
    assert all(args[1] == ["开始", "作战"] for args, _kwargs in calls)
    assert all(kwargs["normalize"] is True for _args, kwargs in calls)
    assert all(kwargs["min_confidence"] == 0.75 for _args, kwargs in calls)


def test_wait_for_any_template_in_set_requires_same_template(monkeypatch):
    results = iter(
        [
            {
                "count": 1,
                "matches": [
                    {
                        "template": "a.png",
                        "match": MatchResult(found=True, center_point=(10, 10), confidence=0.9),
                    }
                ],
            },
            {
                "count": 1,
                "matches": [
                    {
                        "template": "b.png",
                        "match": MatchResult(found=True, center_point=(10, 10), confidence=0.95),
                    }
                ],
            },
            {
                "count": 1,
                "matches": [
                    {
                        "template": "b.png",
                        "match": MatchResult(found=True, center_point=(11, 11), confidence=0.96),
                    }
                ],
            },
        ]
    )
    calls = []

    def fake_find_templates(*_args, **_kwargs):
        calls.append(True)
        return next(results)

    _patch_wait_lookup(monkeypatch, "find_templates_in_set", fake_find_templates)

    result = asyncio.run(
        wait_actions.wait_for_any_template_in_set(
            app=object(),
            vision=object(),
            engine=_Engine(),
            templates_ref="buttons/*.png",
            timeout=1.0,
            interval=0.0,
            stable_scans=2,
            stable_center_tolerance_px=2,
        )
    )

    assert result["template"] == "b.png"
    assert result["match"].center_point == (11, 11)
    assert len(calls) == 3


def test_wait_for_any_template_in_set_resets_after_center_jump(monkeypatch):
    centers = iter([(10, 10), (20, 10), (21, 11)])
    calls = []

    def fake_find_templates(*_args, **_kwargs):
        center = next(centers)
        calls.append(center)
        return {
            "count": 1,
            "matches": [
                {
                    "template": "a.png",
                    "match": MatchResult(found=True, center_point=center, confidence=0.9),
                }
            ],
        }

    _patch_wait_lookup(monkeypatch, "find_templates_in_set", fake_find_templates)

    result = asyncio.run(
        wait_actions.wait_for_any_template_in_set(
            app=object(),
            vision=object(),
            engine=_Engine(),
            templates_ref="buttons/*.png",
            timeout=1.0,
            interval=0.0,
            stable_scans=2,
            stable_center_tolerance_px=2,
        )
    )

    assert result["template"] == "a.png"
    assert result["match"].center_point == (21, 11)
    assert len(calls) == 3


def test_wait_for_any_template_in_set_default_returns_first_best_match(monkeypatch):
    calls = []
    expected = MatchResult(found=True, center_point=(5, 6), confidence=0.91)

    def fake_find_templates(*_args, **_kwargs):
        calls.append(True)
        return {
            "count": 1,
            "matches": [{"template": "a.png", "match": expected}],
        }

    _patch_wait_lookup(monkeypatch, "find_templates_in_set", fake_find_templates)

    result = asyncio.run(
        wait_actions.wait_for_any_template_in_set(
            app=object(),
            vision=object(),
            engine=_Engine(),
            templates_ref="buttons/*.png",
            timeout=1.0,
            interval=0.0,
        )
    )

    assert result == {"template": "a.png", "match": expected}
    assert len(calls) == 1
