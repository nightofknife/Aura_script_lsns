"""Actual user overview images, plus explicitly synthetic matching negatives."""
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import _reforming_center_room_templates as vision

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/reforming_center_templates"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))


def read_image(path):
    return cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR)


@pytest.mark.parametrize("record", MANIFEST["fixtures"], ids=lambda row: row["id"])
def test_real_overview_recognizes_manual_labels_without_extra_rooms(record):
    path = FIXTURES / record["image"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == record["client_png_sha256"]
    image = read_image(path)
    assert image.shape == (720, 1280, 3)
    hits = vision.room_hits(image, vision.load_catalog())
    assert {hit["room_name"] for hit in hits} == set(record["expected_names"])
    assert all(hit["score"] >= .92 for hit in hits)


def test_all_provided_open_rooms_are_covered_and_assets_remain_plan_local():
    catalog = vision.load_catalog()
    assert len(catalog["rooms"]) == 24
    expected = {name for row in MANIFEST["fixtures"] for name in row["expected_names"]}
    assert expected == set(catalog["rooms"])
    for spec in catalog["rooms"].values():
        assets = [spec["map_title"], *spec.get("map_title_variants", [])]
        if spec["entry_type"] == "direct":
            assert len(spec["destination_markers"]) >= 2
        else:
            assets.append(spec["selected_title"])
        assets.extend(spec["destination_markers"])
        for asset in assets:
            path = (vision._PLAN / asset["image"]).resolve()
            assert path.is_relative_to(vision._PLAN.resolve())
            assert path.is_file()


def test_synthetic_integer_pan_preserves_matching_and_coordinates():
    image = read_image(FIXTURES / "overview_02.png")
    before = {row["room_name"]: row for row in vision.room_hits(image, vision.load_catalog())}
    moved = cv2.warpAffine(image, np.float32([[1, 0, 37], [0, 1, 21]]), (1280, 720),
                           flags=cv2.INTER_NEAREST)
    after = {row["room_name"]: row for row in vision.room_hits(moved, vision.load_catalog())}
    assert set(after) == set(before)
    for name in before:
        assert after[name]["center"] == [before[name]["center"][0]+37, before[name]["center"][1]+21]


@pytest.mark.parametrize("room", ["2号整顿宿舍", "2号生产车间"])
def test_synthetic_two_only_is_not_misidentified_as_one(room):
    catalog = vision.load_catalog()
    title = read_image(vision._PLAN / catalog["rooms"][room]["map_title"]["image"])
    image = np.zeros((720, 1280, 3), np.uint8)
    h, w = title.shape[:2]
    image[300:300+h, 500:500+w] = title
    assert {hit["room_name"] for hit in vision.room_hits(image, catalog)} == {room}


@pytest.mark.parametrize("color", [0, 128, 255])
def test_blank_screen_never_yields_a_room(color):
    assert vision.room_hits(np.full((720, 1280, 3), color, np.uint8), vision.load_catalog()) == []


def test_saved_live_warehouse_frame_retains_antialiased_title_edges():
    """One saved failed capture, not another live task or input sequence."""
    path = FIXTURES / "live_warehouse_20261011.png"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == "275b8523d964f59566cd3f13621c33423f55574bcefda7a1872660398d5b1f98"
    hits = {hit["room_name"]: hit for hit in vision.room_hits(read_image(path), vision.load_catalog())}
    assert set(hits) == {"原料仓库", "成品仓库"}
    assert hits["原料仓库"]["rect"][:2] == [359, 389]
    assert hits["成品仓库"]["rect"][:2] == [359, 217]
    assert all(hit["score"] >= .92 for hit in hits.values())
