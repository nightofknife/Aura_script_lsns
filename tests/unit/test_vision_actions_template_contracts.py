"""Contracts for the template actions changed by the capture timing refactor."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from packages.aura_core.utils.exceptions import StopTaskException
from plans.aura_base.src.actions import vision_actions as actions
from plans.aura_base.src.services import _vision_timing as timing_module
from plans.aura_base.src.services.vision_service import MatchResult, MultiMatchResult


CAPTURE_ACTIONS = (
    "find_image", "find_all_images", "find_best_image",
    "find_templates_in_set", "find_all_templates_in_set", "assert_all_templates_in_set",
)
REGION = (20, 30, 12, 9)


@pytest.fixture(autouse=True, params=[0, 1], ids=["timing_off", "timing_sink_broken"])
def isolated_timing_and_assets(monkeypatch, request):
    monkeypatch.setattr(timing_module, "_SAMPLE_EVERY", request.param)
    monkeypatch.setattr(timing_module, "_sample_counts", defaultdict(int))

    def broken_sink(*args, **kwargs):
        raise RuntimeError("timing sink unavailable")

    monkeypatch.setattr(timing_module, "log_event", broken_sink)
    monkeypatch.setattr(actions, "resolve_template_path", lambda engine, vision, ref: ref)
    # assert_all resolves its set before capture; keep that unrelated branch valid.
    monkeypatch.setattr(actions, "expand_template_paths", lambda *args: [Path("asset.png")])


def _invoke(action, capture, **extra):
    arguments = {
        "app": SimpleNamespace(capture=capture),
        "vision": object(),
        "engine": SimpleNamespace(root_context=SimpleNamespace(data={})),
        "region": REGION,
    }
    arguments["templates_ref" if action.endswith("_in_set") else "template"] = "asset.png"
    arguments.update(extra)
    return getattr(actions, action)(**arguments)


@pytest.mark.parametrize("inspect_mode", [False, True])
@pytest.mark.parametrize("region", [None, REGION])
def test_find_image_ownership_debug_and_region_offset(monkeypatch, inspect_mode, region):
    frame = np.arange(9 * 12 * 3, dtype=np.uint8).reshape(9, 12, 3)
    original_pixels = frame.copy()
    debug_template = np.ones((2, 4, 3), dtype=np.uint8)
    capture_calls, match_calls, imread_calls = [], [], []
    local_match = MatchResult(
        found=True, top_left=(2, 3), center_point=(4, 4), rect=(2, 3, 4, 2),
        confidence=0.97, debug_info={"existing": "preserved"},
    )

    def capture(*, rect):
        capture_calls.append(rect)
        return SimpleNamespace(success=True, image=frame)

    def find_template(**kwargs):
        match_calls.append(kwargs)
        return local_match

    def imread(path):
        imread_calls.append(path)
        return debug_template

    monkeypatch.setattr(actions.cv2, "imread", imread)
    engine = SimpleNamespace(root_context=SimpleNamespace(
        data={"initial": {"__is_inspect_mode__": inspect_mode}},
    ))
    result = _invoke(
        "find_image", capture, vision=SimpleNamespace(find_template=find_template),
        engine=engine, region=region, mask="mask.png",
    )
    assert capture_calls == [region]
    assert len(match_calls) == 1
    received = match_calls[0]
    assert received["template_image"] == "asset.png"
    assert received["mask_image"] == "mask.png"
    np.testing.assert_array_equal(received["source_image"], original_pixels)
    assert result is local_match
    offset_x, offset_y = region[:2] if region else (0, 0)
    assert result.top_left == (2 + offset_x, 3 + offset_y)
    assert result.center_point == (4 + offset_x, 4 + offset_y)
    assert result.rect == (2 + offset_x, 3 + offset_y, 4, 2)
    assert result.confidence == 0.97
    assert result.debug_info["existing"] == "preserved"
    if inspect_mode:
        assert received["source_image"] is not frame
        assert not np.shares_memory(received["source_image"], frame)
        assert result.debug_info["source_image"] is received["source_image"]
        assert result.debug_info["template_image"] is debug_template
        assert result.debug_info["params"]["region"] == region
        assert result.debug_info["params"]["template"] == "asset.png"
        assert imread_calls == ["asset.png"]
        frame[:] = 0
        np.testing.assert_array_equal(result.debug_info["source_image"], original_pixels)
    else:
        assert received["source_image"] is frame
        assert result.debug_info == {"existing": "preserved"}
        assert imread_calls == []


@pytest.mark.parametrize("action", CAPTURE_ACTIONS)
@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
def test_capture_exception_propagates_original_object(action, error_type):
    error = error_type("capture interrupted")
    calls = []

    def capture(*, rect):
        calls.append(rect)
        raise error

    with pytest.raises(error_type) as raised:
        _invoke(action, capture)
    assert raised.value is error
    assert calls == [REGION]


@pytest.mark.parametrize("action", CAPTURE_ACTIONS)
def test_unsuccessful_capture_keeps_action_failure_contract(action):
    calls = []

    def capture(*, rect):
        calls.append(rect)
        # No image member: failure must return before any access or matching.
        return SimpleNamespace(success=False)

    if action == "assert_all_templates_in_set":
        with pytest.raises(StopTaskException, match="capture failed while checking template set") as raised:
            _invoke(action, capture)
        assert raised.value.success is False
    else:
        result = _invoke(action, capture)
        if action == "find_image":
            assert result == MatchResult(debug_info={"error": "capture_failed"})
        elif action == "find_best_image":
            assert result == MatchResult()
        elif action == "find_all_images":
            assert result == MultiMatchResult()
        else:
            assert result == {"count": 0, "matches": []}
    assert calls == [REGION]


def test_assert_all_capture_failure_retains_custom_stop_message():
    with pytest.raises(StopTaskException, match="requested failure message") as raised:
        _invoke(
            "assert_all_templates_in_set", lambda **kwargs: SimpleNamespace(success=False),
            message="requested failure message",
        )
    assert raised.value.success is False
