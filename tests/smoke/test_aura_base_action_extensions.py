from __future__ import annotations

import asyncio
from types import SimpleNamespace

import numpy as np
import pytest

from packages.aura_core.utils.exceptions import StopTaskException
from plans.aura_base.src.actions import interaction_actions, ocr_actions, wait_actions
from plans.aura_base.src.services.ocr_service import MultiOcrResult, OcrResult
from plans.aura_base.src.services.vision_service import MatchResult


class _Engine:
    root_context = SimpleNamespace(data={})


class _App:
    def __init__(self) -> None:
        self.moves: list[tuple[int, int, float]] = []
        self.clicks: list[tuple[int, int, str]] = []

    def capture(self, rect=None):
        width = int(rect[2]) if rect else 8
        height = int(rect[3]) if rect else 8
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

    def find_text(self, source_image, text_to_find, match_mode):
        self.find_calls += 1
        return self.results[0] if self.results else OcrResult(found=False)

    def recognize_all(self, source_image):
        self.recognize_calls += 1
        return MultiOcrResult(count=len(self.results), results=list(self.results))


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
    monkeypatch.setattr(interaction_actions, "find_image", lambda *_args, **_kwargs: next(matches))
    monkeypatch.setattr(interaction_actions, "_sleep_sync_cancellable", lambda _seconds: None)

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
    )

    assert clicked is True
    assert app.moves == [(109, 101, 0.2)]
    assert app.clicks == [(109, 101, "left")]


def test_find_image_and_click_default_is_one_probe(monkeypatch):
    calls = []

    def fake_find_image(*_args, **_kwargs):
        calls.append(True)
        return MatchResult(found=False)

    monkeypatch.setattr(interaction_actions, "find_image", fake_find_image)

    clicked = interaction_actions.find_image_and_click(
        app=_App(),
        vision=object(),
        engine=_Engine(),
        template="missing.png",
    )

    assert clicked is False
    assert len(calls) == 1


def test_find_text_and_click_required_failure_keeps_default_message(monkeypatch):
    monkeypatch.setattr(
        interaction_actions,
        "find_text",
        lambda *_args, **_kwargs: OcrResult(found=False),
    )

    with pytest.raises(StopTaskException, match="未能在指定区域找到文本 '开始'。"):
        interaction_actions.find_text_and_click(
            app=_App(),
            ocr=object(),
            engine=_Engine(),
            text_to_find="开始",
            timeout=0.0,
            required=True,
        )


def test_find_image_and_click_uses_custom_failure_message(monkeypatch):
    monkeypatch.setattr(
        interaction_actions,
        "find_image",
        lambda *_args, **_kwargs: MatchResult(found=False),
    )

    with pytest.raises(StopTaskException, match="^没有找到确认按钮$"):
        interaction_actions.find_image_and_click(
            app=_App(),
            vision=object(),
            engine=_Engine(),
            template="confirm.png",
            required=True,
            failure_message="没有找到确认按钮",
        )


def test_find_text_and_click_uses_custom_failure_message(monkeypatch):
    monkeypatch.setattr(
        interaction_actions,
        "find_text",
        lambda *_args, **_kwargs: OcrResult(found=False),
    )

    with pytest.raises(StopTaskException, match="^没有找到开始文字$"):
        interaction_actions.find_text_and_click(
            app=_App(),
            ocr=object(),
            engine=_Engine(),
            text_to_find="开始",
            required=True,
            failure_message="没有找到开始文字",
        )


def test_poll_sync_cancels_before_first_probe(monkeypatch):
    probe_calls = []
    monkeypatch.setattr(
        interaction_actions,
        "is_current_task_cancel_requested",
        lambda: True,
    )

    with pytest.raises(asyncio.CancelledError):
        interaction_actions._poll_sync(
            probe=lambda: probe_calls.append(True),
            predicate=lambda _value: False,
            timeout=1.0,
            interval=0.2,
        )

    assert probe_calls == []


def test_poll_sync_cancels_during_interval_sleep(monkeypatch):
    cancelled = False
    probe_calls = []

    def fake_sleep(_seconds):
        nonlocal cancelled
        cancelled = True

    monkeypatch.setattr(
        interaction_actions,
        "is_current_task_cancel_requested",
        lambda: cancelled,
    )
    monkeypatch.setattr(interaction_actions.time, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        interaction_actions._poll_sync(
            probe=lambda: probe_calls.append(True),
            predicate=lambda _value: False,
            timeout=1.0,
            interval=0.2,
        )

    assert len(probe_calls) == 1


def test_find_image_and_click_cancels_during_after_click_wait(monkeypatch):
    app = _App()
    cancelled = False

    def fake_sleep(_seconds):
        nonlocal cancelled
        cancelled = True

    monkeypatch.setattr(
        interaction_actions,
        "find_image",
        lambda *_args, **_kwargs: MatchResult(
            found=True,
            center_point=(10, 20),
            confidence=0.9,
        ),
    )
    monkeypatch.setattr(
        interaction_actions,
        "is_current_task_cancel_requested",
        lambda: cancelled,
    )
    monkeypatch.setattr(interaction_actions.time, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        interaction_actions.find_image_and_click(
            app=app,
            vision=object(),
            engine=_Engine(),
            template="button.png",
            after_click_sec=1.0,
        )

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

    monkeypatch.setattr(wait_actions, "find_text", fake_find_text)

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

    monkeypatch.setattr(wait_actions, "find_image", fake_find_image)

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

    monkeypatch.setattr(wait_actions, "find_templates_in_set", fake_find_templates)

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

    def fake_find_text(*args, **_kwargs):
        calls.append(args)
        return next(results)

    monkeypatch.setattr(wait_actions, "find_text", fake_find_text)

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
    assert all(call[3] == ["开始", "作战"] for call in calls)
    assert all(call[6] is True for call in calls)
    assert all(call[7] == 0.75 for call in calls)


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

    monkeypatch.setattr(wait_actions, "find_templates_in_set", fake_find_templates)

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

    monkeypatch.setattr(wait_actions, "find_templates_in_set", fake_find_templates)

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

    monkeypatch.setattr(wait_actions, "find_templates_in_set", fake_find_templates)

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
