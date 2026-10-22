"""Compatibility at the room-reader/framework boundary, without game input."""
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from plans.aura_base.src.services.vision_service import VisionService
from plans.resonance_pc.src.actions import _framework_room_templates as framework
from plans.resonance_pc.src.actions import _reforming_center_room_templates as old


def outcome(call):
    try:
        return ("value", call())
    except Exception as exc:
        return (type(exc), str(exc))


@pytest.mark.parametrize("kind", ["missing", "directory", "corrupt", "empty", "escape"])
def test_template_load_failure_preserves_legacy_type_and_message(monkeypatch, tmp_path, kind):
    monkeypatch.setattr(old, "_PLAN", tmp_path)
    monkeypatch.setattr(framework, "_PLAN", tmp_path)
    old._asset.cache_clear()
    if kind == "directory":
        (tmp_path / "asset.png").mkdir()
    elif kind == "corrupt":
        (tmp_path / "asset.png").write_bytes(b"not an image")
    elif kind == "empty":
        (tmp_path / "asset.png").write_bytes(b"")
    name = "../outside.png" if kind == "escape" else "asset.png"
    matcher = framework.RoomTemplateMatcher(VisionService())
    assert outcome(lambda: matcher._asset(name)) == outcome(lambda: old._asset(name))


def test_successful_templates_use_injected_loader_and_operation_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(framework, "_PLAN", tmp_path)
    pixels = np.full((14, 20, 3), (12, 80, 230), np.uint8)
    calls = []

    def load(path, *, flags):
        calls.append((path, flags))
        return pixels

    matcher = framework.RoomTemplateMatcher(SimpleNamespace(load_image_file=load))
    assert matcher._asset("native.png") is pixels
    assert matcher._asset("native.png") is pixels
    assert calls == [(tmp_path / "native.png", cv2.IMREAD_COLOR)]


def test_missing_service_does_not_fall_back_to_private_matching():
    with pytest.raises(RuntimeError, match="injected vision service"):
        framework.RoomTemplateMatcher(None)


@pytest.mark.parametrize("region", [
    (420, 170, 200, 100), (0, 0, 5, 5), (1280, 0, 10, 10),
    (0, 0, -1, 20), (0, 0, 0, 20), (0, 0, 10), (0, 0, "bad", 20),
])
def test_roi_results_and_failures_preserve_legacy_contract(monkeypatch, region):
    pixels = np.random.default_rng(471).integers(0, 256, (14, 20, 3), dtype=np.uint8)
    image = np.zeros((720, 1280, 3), np.uint8)
    image[210:224, 460:480] = pixels
    matcher = framework.RoomTemplateMatcher(VisionService())
    monkeypatch.setattr(old, "_asset", lambda name: pixels)
    monkeypatch.setattr(matcher, "_asset", lambda name: pixels)
    spec = {"image": "synthetic", "threshold": .92}
    assert outcome(lambda: matcher.match_asset(image, spec, region)) == outcome(lambda: old.match_asset(image, spec, region))


def test_service_receives_gray_pixels_and_returns_client_coordinates(monkeypatch):
    pixels = np.random.default_rng(471).integers(0, 256, (14, 20, 3), dtype=np.uint8)
    image = np.zeros((720, 1280, 3), np.uint8)
    image[210:224, 460:480] = pixels
    before = image.copy()
    calls = []
    vision = VisionService()

    def find(**kwargs):
        calls.append(kwargs)
        return vision.find_template(**kwargs)

    matcher = framework.RoomTemplateMatcher(SimpleNamespace(find_template=find))
    monkeypatch.setattr(matcher, "_asset", lambda name: pixels)
    hit = matcher.match_asset(image, {"image": "synthetic", "threshold": .99}, (420, 170, 200, 100))
    assert hit["rect"] == [460, 210, 20, 14] and hit["center"] == [470, 217]
    assert len(calls) == 1
    assert calls[0]["source_image"].ndim == calls[0]["template_image"].ndim == 2
    assert calls[0]["use_grayscale"] is False and calls[0]["preprocess"] == "none"
    np.testing.assert_array_equal(image, before)


def test_framework_error_does_not_become_valid_absence(monkeypatch):
    pixels = np.random.default_rng(471).integers(0, 256, (14, 20, 3), dtype=np.uint8)
    error = SimpleNamespace(debug_info={"error": "recognition unavailable"}, confidence=0.0, top_left=None)
    matcher = framework.RoomTemplateMatcher(SimpleNamespace(find_template=lambda **kwargs: error))
    monkeypatch.setattr(matcher, "_asset", lambda name: pixels)
    with pytest.raises(ValueError, match="recognition unavailable"):
        matcher.match_asset(np.zeros((720, 1280, 3), np.uint8), {"image": "synthetic"})


@pytest.mark.parametrize("scores,legacy", [
    ([[np.nan, np.inf]], None),
    ([[.99, np.inf]], {"score": float(np.float32(.99)), "rect": [0, 0, 20, 14], "center": [10, 7]}),
    ([[-np.inf, -np.inf]], None),
])
def test_injected_nonfinite_maps_have_explicit_fail_closed_contract(monkeypatch, scores, legacy):
    pixels = np.random.default_rng(471).integers(0, 256, (14, 20, 3), dtype=np.uint8)
    image = np.zeros((720, 1280, 3), np.uint8)
    matcher = framework.RoomTemplateMatcher(VisionService())
    monkeypatch.setattr(old, "_asset", lambda name: pixels)
    monkeypatch.setattr(matcher, "_asset", lambda name: pixels)
    monkeypatch.setattr(cv2, "matchTemplate", lambda *args, **kwargs: np.array(scores, np.float32))
    assert old.match_asset(image, {"image": "synthetic", "threshold": .92}) == legacy
    with pytest.raises(ValueError, match="^Room template observation failed: invalid match result$"):
        matcher.match_asset(image, {"image": "synthetic", "threshold": .92})
