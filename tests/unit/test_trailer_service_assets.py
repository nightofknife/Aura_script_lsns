"""Native towing templates against unedited user screenshot client crops."""

from hashlib import sha256
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
import pytest

from plans.aura_base.src.services.vision_service import VisionService
from plans.resonance_pc.src.actions import _trailer_service_vision as trailer


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/trailer_service"
TEMPLATES = ROOT / "plans/resonance_pc/templates/trailer_service"
NAMES = {"trailer_entry", "trailer_prompt", "gold_selected", "mileage_selected", "confirm", "cancel"}
COUNT_ROI = (790, 674, 80, 28)


def _read(path):
    with Image.open(path) as image:
        return np.asarray(image.convert("RGB")).copy()


def _manifest():
    return json.loads((TEMPLATES / "manifest.json").read_text(encoding="utf-8"))


def _entry(name):
    return next(item for item in _manifest()["templates"] if item["name"] == name)


def _asset(entry):
    path = (TEMPLATES / entry["file"]).resolve()
    assert path.is_relative_to(TEMPLATES.resolve())
    return path


class _SynchronousFrameworkVision:
    """Use the real framework SQDI matcher without starting its async worker."""

    def __init__(self):
        self.service = VisionService()

    def find_template(self, source_image, template_image, mask_image=None,
                      threshold=.8, use_grayscale=True,
                      match_method=cv2.TM_CCOEFF_NORMED, preprocess="none", **kwargs):
        assert mask_image is None
        assert threshold == .9
        assert match_method == cv2.TM_SQDIFF_NORMED
        assert use_grayscale is False
        assert preprocess == "none"
        template_image = (_read(template_image) if isinstance(template_image, (str, Path))
                          else template_image)
        return self.service._match_template_prepared(
            source_image, template_image, None, threshold,
            match_method, use_grayscale, preprocess,
        )


def _match(label, name):
    source = _read(FIXTURES / f"{label}_client.png")
    return trailer.TrailerVision(vision=_SynchronousFrameworkVision()).match(source, name)


def test_native_asset_manifest_is_complete_self_contained_and_verified():
    manifest = _manifest()
    assert {item["name"] for item in manifest["templates"]} == NAMES
    assert len(manifest["templates"]) == len(NAMES)
    assert trailer.THRESHOLD == .9
    assert Path(trailer.TEMPLATE_ROOT).resolve() == TEMPLATES.resolve()
    for entry in manifest["templates"]:
        path = _asset(entry)
        assert sha256(path.read_bytes()).hexdigest() == entry["sha256"]
        with Image.open(path) as image:
            assert image.format == "PNG"
            assert image.mode == "RGB"
            assert list(image.size) == entry["size"]
        x, y, width, height = entry["roi"]
        assert 0 <= x < x + width <= 1280
        assert 0 <= y < y + height <= 720
        assert entry["size"][0] <= width
        assert entry["size"][1] <= height
        assert not entry.get("mask")


@pytest.mark.parametrize(("label", "name"), [
    ("map", "trailer_entry"),
    ("gold", "trailer_prompt"), ("mileage", "trailer_prompt"),
    ("gold", "gold_selected"), ("mileage", "mileage_selected"),
    ("gold", "confirm"), ("mileage", "confirm"),
    ("gold", "cancel"), ("mileage", "cancel"),
])
def test_actual_screenshots_match_positive_native_templates(label, name):
    result = _match(label, name)
    assert result["found"] is True
    assert result["confidence"] >= .9
    x, y, width, height = _entry(name)["roi"]
    center_x, center_y = result["center"]
    assert x <= center_x <= x + width
    assert y <= center_y <= y + height


@pytest.mark.parametrize(("label", "name"), [
    ("gold", "mileage_selected"), ("mileage", "gold_selected"),
    ("map", "gold_selected"), ("map", "mileage_selected"),
    ("map", "trailer_prompt"), ("map", "confirm"), ("map", "cancel"),
    ("gold", "trailer_entry"), ("mileage", "trailer_entry"),
])
def test_wrong_currency_map_and_blurred_background_do_not_match(label, name):
    result = _match(label, name)
    assert result["found"] is False
    assert result["confidence"] < .9


@pytest.mark.parametrize("name", sorted(NAMES))
def test_plain_white_does_not_match_any_template(name):
    frame = np.full((720, 1280, 3), 255, dtype=np.uint8)
    result = trailer.TrailerVision(vision=_SynchronousFrameworkVision()).match(frame, name)
    assert result["found"] is False
    assert result["confidence"] < .9


@pytest.mark.parametrize(("label", "payment"), [("map", None), ("gold", "gold"),
                                                ("mileage", "mileage")])
def test_prompt_observation_requires_complete_controls_and_one_selected_currency(label, payment):
    frame = _read(FIXTURES / f"{label}_client.png")
    result = trailer.TrailerVision(vision=_SynchronousFrameworkVision()).prompt(frame)
    if payment is None:
        assert result is None
    else:
        assert result["payment"] == payment
        assert result["confirm"]["found"] is True
        assert result["cancel"]["found"] is True


def test_framework_sqdiff_confidence_is_one_minus_normalized_difference():
    entry = _entry("gold_selected")
    source = _read(FIXTURES / "gold_client.png")
    x, y, width, height = entry["roi"]
    source = source[y:y + height, x:x + width]
    template = _read(_asset(entry))
    difference = cv2.matchTemplate(source, template, cv2.TM_SQDIFF_NORMED)
    minimum, _, expected_top_left, _ = cv2.minMaxLoc(difference)
    result = VisionService()._match_template_prepared(
        source, template, None, .9, cv2.TM_SQDIFF_NORMED, False, "none",
    )
    assert result.confidence == pytest.approx(1 - minimum, abs=1e-6)
    assert result.top_left == expected_top_left


def test_count_roi_has_visible_counter_content_without_running_ocr():
    assert trailer.QUOTA_REGION == COUNT_ROI
    image = _read(FIXTURES / "map_client.png")
    x, y, width, height = COUNT_ROI
    crop = image[y:y + height, x:x + width]
    assert crop.shape == (28, 80, 3)
    assert float(crop.std()) > 20
    assert np.count_nonzero(np.all(crop > 220, axis=2)) > 50


def test_screenshot_fixtures_retain_attachment_provenance():
    sources = json.loads((FIXTURES / "sources.json").read_text(encoding="utf-8"))
    assert sources["kind"] == "actual_user_screenshots_client_crop_not_synthetic"
    assert sources["client_crop"] == [25, 56, 1305, 776]
    assert set(sources["screenshots"]) == {"map", "gold", "mileage"}
    for label, entry in sources["screenshots"].items():
        path = FIXTURES / entry["path"]
        assert path.name == f"{label}_client.png"
        assert len(entry["source_sha256"]) == 64
        assert entry["attachment"].startswith("codex-clipboard-")
        assert sha256(path.read_bytes()).hexdigest() == entry["sha256"]
        with Image.open(path) as image:
            assert image.mode == "RGB"
            assert list(image.size) == entry["size"] == [1280, 720]
