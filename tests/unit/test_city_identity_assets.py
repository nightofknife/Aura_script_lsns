"""Portable city/entry template checks using actual screenshot ROI fixtures."""
from hashlib import sha256
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
import pytest

from plans.aura_base.src.services.vision_service import VisionService
from tools.build_city_identity_templates import CITY_ROI, VISIT_ROI, city_names, contour_template

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "plans/resonance_pc"
FIXTURES = ROOT / "tests/fixtures/city_identity"
CATALOG = json.loads((PLAN / "data/meta/city_identity_templates.json").read_text(encoding="utf-8"))


def _read(path):
    with Image.open(path) as image:
        return np.asarray(image).copy()


def _city_results(source):
    service = VisionService()
    return service._find_templates_batch_sync(
        source, [_read(PLAN / entry["template"]) for entry in CATALOG["cities"]],
        [_read(PLAN / entry["mask"]) for entry in CATALOG["cities"]],
        .55, True, cv2.TM_CCORR_NORMED, "edge",
    )


def test_catalog_covers_canonical_city_names_and_fixed_matching_contract():
    assert CATALOG["schema_version"] == 1
    assert {row["city_key"]: row["city_name"] for row in CATALOG["cities"]} == city_names(PLAN)
    assert len(CATALOG["cities"]) == 21
    assert CATALOG["city_match"] == {
        "method": "TM_CCORR_NORMED", "threshold": .55, "roi": CITY_ROI,
        "use_grayscale": True, "preprocess": "edge",
    }
    assert CATALOG["visit_entry"] == {
        "template": "templates/visit_city_entry.png", "method": "TM_SQDIFF_NORMED",
        "threshold": .9, "roi": VISIT_ROI, "use_grayscale": False, "preprocess": "none",
    }


def test_all_assets_are_self_contained_verified_and_city_masks_use_contour_neighborhoods():
    references = {CATALOG["visit_entry"]["template"]}
    for city in CATALOG["cities"]:
        references.update((city["template"], city["mask"]))
        template, mask = _read(PLAN / city["template"]), _read(PLAN / city["mask"])
        assert template.shape == (133, 133, 3)
        assert mask.shape == (133, 133)
        expected = cv2.dilate(cv2.Canny(cv2.cvtColor(template, cv2.COLOR_RGB2GRAY), 50, 150),
                              np.ones((5, 5), np.uint8))
        np.testing.assert_array_equal(mask, expected)
        assert set(np.unique(mask)) == {0, 255}
    assert references == set(CATALOG["assets"])
    for relative, metadata in CATALOG["assets"].items():
        path = PLAN / relative
        assert path.resolve().is_relative_to((PLAN / "templates").resolve())
        assert sha256(path.read_bytes()).hexdigest() == metadata["sha256"]
        with Image.open(path) as image:
            assert image.format == "PNG"
            assert list(image.size) == metadata["size"]
            assert image.mode == metadata["mode"]
    assert _read(PLAN / CATALOG["visit_entry"]["template"]).shape == (29, 35, 3)


@pytest.mark.parametrize(("label", "expected"), [("main", None), ("city", "gronru_city"),
                                                  ("bridge", "farstar_bridge")])
def test_actual_screenshot_city_rois_choose_correct_identity_or_no_identity(label, expected):
    results = _city_results(_read(FIXTURES / f"{label}_city_roi.png"))
    hits = [(city["city_key"], result) for city, result in zip(CATALOG["cities"], results)
            if result.found and np.isfinite(result.confidence)]
    if expected is None:
        assert hits == []
    else:
        assert hits
        assert max(hits, key=lambda item: item[1].confidence)[0] == expected
        assert len(hits) == 1


@pytest.mark.parametrize(("label", "expected"), [("main", True), ("city", False), ("bridge", False)])
def test_actual_screenshot_visit_rois_detect_only_main_entry(label, expected):
    hit = VisionService()._match_template_prepared(
        _read(FIXTURES / f"{label}_visit_roi.png"),
        _read(PLAN / CATALOG["visit_entry"]["template"]), None,
        .9, cv2.TM_SQDIFF_NORMED, False, "none",
    )
    assert hit.found is expected
    if expected:
        assert hit.top_left == (93, 39)


def test_screenshot_fixture_provenance_and_roi_content_hashes():
    sources = json.loads((FIXTURES / "sources.json").read_text(encoding="utf-8"))
    assert sources["kind"] == "actual_user_screenshot_rois_not_synthetic"
    assert set(sources["screenshots"]) == {"main", "city", "bridge"}
    for label, metadata in sources["screenshots"].items():
        assert len(metadata["source_sha256"]) == 64
        assert metadata["attachment"].startswith("codex-clipboard-")
        assert metadata["client_crop"] == [25, 56, 1305, 776]
        for kind, region in metadata["regions"].items():
            path = FIXTURES / region["path"]
            assert path.name == f"{label}_{kind}_roi.png"
            assert sha256(path.read_bytes()).hexdigest() == region["sha256"]
            with Image.open(path) as image:
                assert list(image.size) == region["roi"][2:]
                assert image.mode == "RGB"


def test_builder_rejects_wrong_size_or_empty_city_badge():
    with pytest.raises(ValueError, match="133x133 RGBA"):
        contour_template(Image.new("RGB", (133, 133)))
    with pytest.raises(ValueError, match="no usable contour"):
        contour_template(Image.new("RGBA", (133, 133)))
