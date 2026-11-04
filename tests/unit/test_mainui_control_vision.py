"""Shared decisions against independent observed pixels and failure boundaries."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
from PIL import Image
import pytest

from plans.resonance_pc.src.actions import _mainui_control_vision as controls
from plans.resonance_pc.src.actions import _black_moon_local_exit_vision as black_moon


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def observed_main():
    frame = np.zeros((720, 1280, 3), np.uint8)
    frame[600:720, 900:1280] = np.asarray(Image.open(
        FIXTURES / "mainui_controls/actual-bottom-right-20261104.png").convert("RGB"))
    return frame


@pytest.fixture(autouse=True)
def clear_caches():
    controls.load_control_profile.cache_clear()
    controls._load_pixels.cache_clear()
    yield
    controls.load_control_profile.cache_clear()
    controls._load_pixels.cache_clear()


class App:
    def __init__(self, frame):
        self.frame, self.captures = frame, []

    def capture(self, *, rect):
        self.captures.append(rect)
        x, y, w, h = rect
        return SimpleNamespace(success=True, image=self.frame[y:y+h, x:x+w])


@pytest.mark.parametrize("control", ["depart", "train_maintenance", "passenger_management"])
def test_observed_user_pixels_match_both_shared_entry_points(control):
    frame = observed_main()
    app = App(frame)
    profile = controls.load_control_profile(control)
    captured = controls.probe_mainui_control(app=app, control=control, check_cancelled=lambda: None)
    stored = controls.match_mainui_control_frame(frame, control)
    assert captured["valid"] is True and captured["found"] is True
    assert captured["confidence"] >= profile.threshold
    assert stored["found"] is True
    assert stored["confidence"] == pytest.approx(captured["confidence"], abs=1e-6)
    assert stored["center"] == captured["center"]
    x, y, w, h = profile.region
    assert x <= stored["center"][0] < x+w and y <= stored["center"][1] < y+h
    assert app.captures == [profile.region]


def test_black_moon_uses_the_same_actual_departure_decision():
    frame = observed_main()
    assert controls.match_mainui_control_frame(frame, "depart")["found"] is True
    assert black_moon.BlackMoonLocalExitVision().observe(frame)["page_state"] == "main"


@pytest.mark.parametrize("control", ["depart", "train_maintenance", "passenger_management", "horn"])
def test_center_is_client_absolute_and_matches_capture_and_frame_paths(control):
    profile = controls.load_control_profile(control)
    pixels = np.asarray(Image.open(controls.PLAN_ROOT / profile.template).convert("RGB"))
    frame = np.full((720, 1280, 3), 17, np.uint8)
    x, y = profile.region[0]+7, profile.region[1]+5
    w, h = profile.size
    frame[y:y+h, x:x+w] = pixels
    stored = controls.match_mainui_control_frame(frame, control)
    captured = controls.probe_mainui_control(app=App(frame), control=control, check_cancelled=lambda: None)
    assert stored["found"] is True and captured["found"] is True
    assert stored["center"] == captured["center"] == [x+w//2, y+h//2]


def test_old_small_departure_artwork_is_not_a_compatibility_variant():
    frame = np.zeros((720, 1280, 3), np.uint8)
    old = np.asarray(Image.open(FIXTURES / "depart_button/actual-matched-crop.png").convert("RGB"))
    frame[641:683, 1150:1256] = old
    result = controls.match_mainui_control_frame(frame, "depart")
    assert result["valid"] is True and result["found"] is False
    assert result["center"] is None


@pytest.mark.parametrize("control", ["depart", "train_maintenance", "passenger_management", "horn"])
@pytest.mark.parametrize("value", [0, 255])
def test_blank_pixels_are_valid_negative_evidence(control, value):
    result = controls.match_mainui_control_frame(np.full((720, 1280, 3), value, np.uint8), control)
    assert result["valid"] is True and result["found"] is False
    assert result["center"] is None


@pytest.mark.parametrize("delta,expected", [(-.0001, False), (0, True), (.0001, True)])
@pytest.mark.parametrize("control", ["depart", "train_maintenance", "passenger_management", "horn"])
def test_threshold_boundary_has_one_decision_for_each_method(monkeypatch, delta, expected, control):
    profile = controls.load_control_profile(control)
    score = profile.threshold + delta
    raw = 1-score if profile.match_method == cv2.TM_SQDIFF_NORMED else score
    monkeypatch.setattr(controls.cv2, "matchTemplate", lambda *args: np.array([[raw]], np.float64))
    result = controls.match_mainui_control_frame(observed_main(), control)
    assert result["valid"] is True and result["found"] is expected


@pytest.mark.parametrize("frame", [None, np.zeros((100, 160, 3), np.uint8),
                                  np.zeros((720, 1280, 4), np.uint8),
                                  np.zeros((720, 1280, 3), np.float32)])
def test_invalid_frame_is_unknown_not_absence(frame):
    result = controls.match_mainui_control_frame(frame, "depart")
    assert result["valid"] is False and result["found"] is None
    assert result["reason"] == "invalid_frame" and result["center"] is None


@pytest.mark.parametrize("failure", ["exception", "unsuccessful", "bad_pixels"])
def test_capture_errors_never_become_negative_matches(monkeypatch, failure):
    def capture(**kwargs):
        if failure == "exception":
            raise RuntimeError("capture unavailable")
        return SimpleNamespace(success=failure != "unsuccessful", image=None)
    monkeypatch.setattr(controls.cv2, "matchTemplate", lambda *args: pytest.fail("invalid capture was matched"))
    result = controls.probe_mainui_control(app=SimpleNamespace(capture=capture), control="depart", check_cancelled=lambda: None)
    assert result["valid"] is False and result["found"] is None
    assert result["reason"] == "capture_failed"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_backend_evidence_is_unknown(monkeypatch, value):
    monkeypatch.setattr(controls.cv2, "matchTemplate", lambda *args: np.array([[value]], np.float32))
    result = controls.match_mainui_control_frame(observed_main(), "depart")
    assert result["valid"] is False and result["found"] is None
    assert result["reason"] == "template_match_failed"


def test_unknown_main_evidence_does_not_authorize_black_moon_exit(monkeypatch):
    monkeypatch.setattr(black_moon, "match_mainui_control_frame", lambda *args: {"valid": False, "found": None})
    assert black_moon.BlackMoonLocalExitVision().observe(observed_main())["page_state"] == "unknown"


def test_cancellation_before_capture_is_propagated():
    app = App(observed_main())
    def cancelled():
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        controls.probe_mainui_control(app=app, control="depart", check_cancelled=cancelled)
    assert app.captures == []
