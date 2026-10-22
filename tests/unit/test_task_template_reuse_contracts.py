"""Task-local template reuse without development snapshots or time benchmarks."""
from __future__ import annotations

from collections import Counter
from types import SimpleNamespace as NS

import cv2
import numpy as np
import pytest

from plans.aura_base.src.services.vision_service import VisionService
from plans.resonance_pc.src.actions import character_pc_actions as characters
from plans.resonance_pc.src.actions import player_data_pc_actions as warehouse
from plans.resonance_pc.src.actions import _template_observation as observation


def test_character_catalog_decodes_each_declared_asset_once_and_retains_entry(monkeypatch):
    reader = characters._read_image_file
    calls = Counter()
    def read(path, flags=cv2.IMREAD_UNCHANGED):
        calls[str(path)] += 1
        return reader(path, flags)
    monkeypatch.setattr(characters, "_read_image_file", read)
    catalog = characters.load_character_catalog()
    declared = Counter(row["template_path"] for row in catalog["templates"])
    declared.update(catalog[key] for key in ("entry_template_path", "lit_star_template_path", "lit_star_mask_path"))
    assert calls == declared
    service = VisionService()
    # The entry is exclusively a grayscale match; its catalog snapshot may
    # already be prepared. RGB conversion is independently covered below.
    expected = service._prepare_image(catalog["entry_template_path"], use_grayscale=True)
    actual = service._prepare_image(catalog["entry_template_image"], use_grayscale=True)
    assert np.array_equal(actual, expected)


@pytest.mark.parametrize("channels", [1, 3, 4])
def test_character_loaded_pixels_retain_rgb_contract_without_second_decode(monkeypatch, channels):
    gray = np.array([[0, 64], [128, 255]], np.uint8)
    raw = gray if channels == 1 else np.stack((gray, 255 - gray, gray // 2), axis=2)
    if channels == 4:
        raw = np.dstack((raw, gray))
    def fail(*_):
        pytest.fail("decoded pixels must not read another file")
    monkeypatch.setattr(characters, "_read_image_file", fail)
    actual = characters._identity_template_rgb("unused.png", raw)
    conversion = {1: cv2.COLOR_GRAY2RGB, 3: cv2.COLOR_BGR2RGB, 4: cv2.COLOR_BGRA2RGB}[channels]
    assert np.array_equal(actual, cv2.cvtColor(raw, conversion))


def test_warehouse_cache_only_loads_after_valid_capture_and_retries_failed_load():
    events = []
    width, height = warehouse._WAREHOUSE_ENTRY_REGION[2:]
    pixels = (np.arange(width * height * 3).reshape(height, width, 3) % 256).astype(np.uint8)
    captures = iter([NS(success=False), NS(success=True, image=pixels), NS(success=True, image=pixels),
                     NS(success=True, image=pixels)])
    def capture(**_):
        events.append("capture")
        return next(captures)
    loads = [0]
    def load(*_):
        events.append("load")
        loads[0] += 1
        if loads[0] == 1:
            raise OSError("unavailable")
        return cv2.cvtColor(pixels[2:9, 3:14], cv2.COLOR_BGR2GRAY)
    app, vision, cache = NS(capture=capture), NS(load_image_file=load), {}
    assert warehouse._match_warehouse_entry(app, vision, template_cache=cache)["reason"] == "capture_failed"
    assert not cache
    with pytest.raises(warehouse.StopTaskException, match="warehouse entry template is unavailable"):
        warehouse._match_warehouse_entry(app, vision, template_cache=cache)
    assert not cache
    first = warehouse._match_warehouse_entry(app, vision, template_cache=cache)
    second = warehouse._match_warehouse_entry(app, vision, template_cache=cache)
    assert first == second and first["found"]
    assert events == ["capture", "capture", "load", "capture", "load", "capture"]


def test_default_observation_keeps_original_rgb_array_identity(monkeypatch):
    region = (10, 20, 12, 8)
    image = (np.arange(8 * 12 * 3).reshape(8, 12, 3) % 256).astype(np.uint8)
    calls = []
    def fail(*_):
        pytest.fail("legacy observation must delegate grayscale preparation")
    monkeypatch.setattr(observation.cv2, "cvtColor", fail)
    def match(**options):
        calls.append(options)
        return NS(found=False, confidence=.1)
    result = observation.observe_template(app=NS(capture=lambda **_: NS(success=True, image=image)),
        vision=NS(find_template=match), template="button.png", region=region, threshold=.86,
        check_cancelled=lambda: None)
    assert result["valid"] and result["found"] is False
    assert calls[0]["source_image"] is image
    assert calls[0]["use_grayscale"] is True


def test_shared_frame_keeps_per_template_thresholds_and_single_capture(monkeypatch):
    region = (10, 20, 12, 8)
    captures, calls = [], []
    image = (np.arange(8 * 12 * 3).reshape(8, 12, 3) % 256).astype(np.uint8)
    expected_gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    convert = observation.cv2.cvtColor
    conversions = []
    def convert_once(pixels, code):
        conversions.append((pixels, code))
        return convert(pixels, code)
    monkeypatch.setattr(observation.cv2, "cvtColor", convert_once)
    def capture(**options):
        captures.append(options)
        return NS(success=True, image=image)
    def match(**options):
        calls.append(options)
        return NS(found=True, confidence=.99, center_point=(3, 4))
    app, vision = NS(capture=capture), NS(find_template=match)
    frame = observation.capture_template_frame(app=app, region=region, check_cancelled=lambda: None)
    for threshold in (.86, .90):
        result = observation.observe_template(app=app, vision=vision, template="button.png", region=region,
            threshold=threshold, check_cancelled=lambda: None, require_center=True, frame=frame)
        assert result["valid"] and result["center"] == [13, 24]
    assert len(captures) == 1
    assert [call["threshold"] for call in calls] == [.86, .90]
    assert calls[0]["source_image"] is calls[1]["source_image"]
    assert len(conversions) == 1 and conversions[0][0] is image
    assert np.array_equal(calls[0]["source_image"], expected_gray)
    assert frame["image"] is image


def test_shared_frame_region_mismatch_stays_unknown_without_capture_or_match():
    region = (10, 20, 12, 8)
    frame = {"valid": True, "region": (11, 20, 12, 8), "image": np.zeros((8, 12, 3), np.uint8)}
    def fail(**_):
        pytest.fail("mismatched evidence must not be recaptured or matched")
    result = observation.observe_template(app=NS(capture=fail), vision=NS(find_template=fail),
        template="button.png", region=region, threshold=.86, check_cancelled=lambda: None, frame=frame)
    assert result["reason"] == "capture_failed" and result["found"] is None
