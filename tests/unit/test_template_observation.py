"""Technical failure must not become absence or a usable click location."""
import asyncio
from types import SimpleNamespace as NS

import numpy as np
import pytest

from plans.resonance_pc.src.actions._template_observation import observe_template


REGION = (30, 40, 12, 8)


def observe(*, image=None, hit=None, require_center=True, check_cancelled=lambda: None):
    return observe_template(
        app=NS(capture=lambda **_: NS(success=True, image=(
            np.zeros((8, 12, 3), np.uint8) if image is None else image))),
        vision=NS(find_template=lambda **_: hit), template="test.png", region=REGION,
        threshold=.86, check_cancelled=check_cancelled, require_center=require_center,
    )


@pytest.mark.parametrize("hit", [None, NS(found=False),
    NS(found=False, confidence=float("nan")), NS(found=False, confidence=float("inf")),
    NS(found=False, confidence=None), NS(found="False", confidence=.9),
    NS(found=0, confidence=.9), NS(found=None, confidence=.9),
    NS(found=False, confidence=.9, debug_info={"error": "bad template"}),
    NS(found=False, confidence=.9, debug_info=["broken diagnostics"]),
])
def test_invalid_match_cannot_be_absence(hit):
    result = observe(hit=hit)
    assert result["valid"] is False and result["found"] is None
    assert "center" not in result


@pytest.mark.parametrize("image", [np.zeros((8, 12), np.uint8),
    np.zeros((8, 12, 4), np.uint8), np.zeros((7, 12, 3), np.uint8),
    np.zeros((8, 12, 3), np.float32), np.zeros((0, 12, 3), np.uint8)])
def test_invalid_capture_never_calls_matcher(image):
    def fail(**_):
        pytest.fail("Invalid capture must not reach matching")
    result = observe_template(
        app=NS(capture=lambda **_: NS(success=True, image=image)), vision=NS(find_template=fail),
        template="test.png", region=REGION, threshold=.86, check_cancelled=lambda: None,
    )
    assert result["valid"] is False and result["reason"] == "capture_failed"


@pytest.mark.parametrize("center", [None, (), (2,), (2, 3, 4), (-1, 1), (12, 1),
    (1, 8), (float("nan"), 1), (float("inf"), 1), (True, 1), ("2", "3")])
def test_positive_without_usable_center_cannot_authorize_click(center):
    result = observe(hit=NS(found=True, confidence=.99, center_point=center))
    assert result["valid"] is False and result["found"] is None
    assert result["reason"] == "invalid_match_center"


def test_valid_absence_needs_no_center_and_positive_presence_is_not_a_click():
    absent = observe(hit=NS(found=False, confidence=.25))
    presence = observe(hit=NS(found=True, confidence=.95), require_center=False)
    assert absent["valid"] and absent["found"] is False and "center" not in absent
    assert presence["valid"] and presence["found"] is True and "center" not in presence


def test_scene_parameters_and_local_to_window_coordinates_survive():
    calls = []
    result = observe_template(
        app=NS(capture=lambda **kwargs: (calls.append(kwargs), NS(
            success=True, image=np.zeros((8, 12, 3), np.uint8)))[1]),
        vision=NS(find_template=lambda **kwargs: (calls.append(kwargs), NS(
            found=np.bool_(True), confidence=.99, center_point=np.array([2, 3])))[1]),
        template="中文/template.png", region=REGION, threshold=.86,
        check_cancelled=lambda: None, require_center=True,
    )
    assert result["center"] == [32, 43]
    assert calls[0] == {"rect": REGION}
    assert calls[1]["threshold"] == .86 and calls[1]["use_grayscale"] is True
    assert calls[1]["template_image"] == "中文/template.png"


@pytest.mark.parametrize("stage", ["capture", "match"])
@pytest.mark.parametrize("throws", [False, True])
def test_cancellation_during_observation_wins_over_technical_failure(stage, throws):
    cancelled = [False]
    def check():
        if cancelled[0]:
            raise asyncio.CancelledError("scene cancelled")
    def operation():
        cancelled[0] = True
        if throws:
            raise RuntimeError("backend failure")
        return NS(success=False) if stage == "capture" else NS(found=False, confidence=.1)
    with pytest.raises(asyncio.CancelledError, match="scene cancelled"):
        observe_template(
            app=NS(capture=lambda **_: operation() if stage == "capture" else NS(
                success=True, image=np.zeros((8, 12, 3), np.uint8))),
            vision=NS(find_template=lambda **_: operation()),
            template="test.png", region=REGION, threshold=.86, check_cancelled=check,
        )


@pytest.mark.parametrize("stage", ["capture", "match"])
def test_backend_exception_is_unknown(stage):
    def fail():
        raise RuntimeError("backend failure")
    result = observe_template(
        app=NS(capture=lambda **_: fail() if stage == "capture" else NS(
            success=True, image=np.zeros((8, 12, 3), np.uint8))),
        vision=NS(find_template=lambda **_: fail()),
        template="test.png", region=REGION, threshold=.86, check_cancelled=lambda: None,
    )
    assert result["valid"] is False and result["found"] is None
    assert "backend failure" in result["error"]
