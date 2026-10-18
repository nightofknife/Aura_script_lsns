"""Portable city/entry template checks using actual screenshot ROI fixtures."""
from hashlib import sha256
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
import pytest

from plans.aura_base.src.services.vision_service import VisionService
from tools.build_city_identity_templates import CITY_ROI, VISIT_ROI, build, city_names, contour_template

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
        "template": "templates/navigation/city/visit_entry.png", "method": "TM_SQDIFF_NORMED",
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


def test_builder_owns_functional_city_outputs_and_is_idempotent(tmp_path):
    """Producer probes use generated input badges, never screenshot fixtures."""
    plan, research = tmp_path / "plan", tmp_path / "reviewed-input"
    research.mkdir()
    source = plan / "src/services/city_shop_data_pc_service.py"
    source.parent.mkdir(parents=True)
    source.write_bytes((PLAN / source.relative_to(plan)).read_bytes())
    for index, key in enumerate(city_names(PLAN)):
        pixels = np.zeros((133, 133, 4), dtype=np.uint8)
        pixels[20:110, 25:100] = [30 + index * 5, 180, 220, 255]
        Image.fromarray(pixels).save(research / f"{key}.png")
    Image.new("RGB", (35, 29), (12, 80, 230)).save(research / "visit-city-icon-template.png")
    (plan / "data/meta").mkdir(parents=True)
    sibling = plan / "templates/navigation/global/owned_by_another_producer.png"
    sibling.parent.mkdir(parents=True)
    Image.new("RGB", (3, 4), (20, 30, 40)).save(sibling)
    sibling_bytes = sibling.read_bytes()
    first = build(research, plan)
    first_bytes = {path.relative_to(plan): path.read_bytes() for path in plan.rglob("*") if path.is_file()}
    assert all(path.startswith("templates/navigation/city/") for path in first["assets"])
    assert len(first["assets"]) == 43
    assert {path.relative_to(plan).as_posix() for path in (plan / "templates/navigation/city").rglob("*.png")} == set(first["assets"])
    assert build(research, plan) == first
    assert {path.relative_to(plan): path.read_bytes() for path in plan.rglob("*") if path.is_file()} == first_bytes
    assert sibling.read_bytes() == sibling_bytes
