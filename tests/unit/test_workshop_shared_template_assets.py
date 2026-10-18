"""Shared ownership and producer checks; generated inputs are not live evidence."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageFont
import pytest

from plans.resonance_pc.src.actions import _product_workshop_staff_ui as staff
from plans.resonance_pc.src.actions import _raw_workshop_ui as raw
from plans.resonance_pc.src.actions import _reforming_center_room_templates as rooms
from tools import build_product_workshop_staff_templates as staff_builder
from tools import build_raw_workshop_templates as raw_builder
from tools import build_reforming_center_templates as room_builder

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "plans/resonance_pc"
CONTROLS = "templates/workshop/staffing/controls/"


def hashes(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def test_shared_edit_keeps_independent_observation_profiles_and_source_identity():
    catalog = rooms.load_catalog()
    markers = [catalog["rooms"][name]["destination_markers"][-1]
               for name in ("1号生产车间", "2号生产车间")]
    saved = staff.catalog()["templates"]["saved_marker"]
    assert {marker["image"] for marker in markers} == {saved["image"]} == {CONTROLS+"edit.png"}
    assert {marker["mask"] for marker in markers} == {CONTROLS+"edit.mask.png"}
    assert saved == staff_builder.saved_marker_profile(markers[0])
    assert saved["threshold"] == .90
    assert all(marker.get("threshold", .92) == .92 for marker in markers)
    assert saved["source_id"] == "249b7a0e-253f-4690-ae3e-4f1d574cd508"
    assert saved["search_region"] == [1081, 662, 111, 47]
    # A descriptive mask in the room catalog remains unused by its grayscale
    # matcher; sharing must not silently switch matching algorithms.
    image = cv2.imread(str(ROOT/"tests/fixtures/product_workshop_staff/live_room1_entry_20261017.png"))
    before = rooms.match_asset(image, markers[0])
    without_mask = deepcopy(markers[0])
    without_mask.pop("mask")
    assert before == rooms.match_asset(image, without_mask)
    assert before["center"] == [1136, 685]
    assert staff.control(image, "saved_marker")["score"] == before["score"]


def test_four_room_records_share_pixels_without_losing_room_provenance():
    names = ("植物纤维原料车间", "动物纤维原料车间", "缝纫工具原料车间", "人造纤维原料车间")
    records = [rooms.load_catalog()["rooms"][name]["destination_markers"][-1] for name in names]
    assert {record["image"] for record in records} == {CONTROLS+"select_prisoner.png"}
    assert {record["mask"] for record in records} == {CONTROLS+"select_prisoner.mask.png"}
    assert len({record["source_id"] for record in records}) == 4
    assert all(record["source_bbox_xywh"] == [877, 656, 77, 25] for record in records)
    assert not list((PLAN/"templates/reforming_center").glob("destination_*_select_prisoner*.png"))
    assert not list((PLAN/"templates/reforming_center").glob("destination_*_workshop_edit*.png"))
    assert not (PLAN/"templates/product_workshop_staff"/"saved_marker.png").exists()


def test_raw_shared_selection_keeps_original_matching_and_current_staffing_variants():
    source = ROOT/"tests/fixtures/raw_workshop_ui"
    image = cv2.imread(str(source/"selection_empty.png"))
    expected = {"select_all": ("select_all.png", [935, 670]),
                "staff_confirm": ("confirm.png", [1160, 670]),
                "selection_back": ("back.png", [95, 40])}
    for name, (filename, point) in expected.items():
        assert raw._specs()[name]["image"] == CONTROLS+"selection/"+filename
        hit = raw._control(image, name)
        assert hit["score"] > .99 and hit["center"] == point
    assert raw._specs()["cancel_all"]["image"] == CONTROLS+"selection/cancel_all.png"
    checked = cv2.imread(str(source/"selection_checked.png"))
    assert raw._control(checked, "cancel_all")["center"] == [935, 670]
    assert raw._control(checked, "cancel_all")["score"] > .99
    # Full native/user reference blocks are different visual variants.
    assert staff.catalog()["templates"]["select_all"]["image"] != raw._specs()["select_all"]["image"]


def test_raw_builder_reproduces_shared_pixels_and_is_idempotent(tmp_path, monkeypatch):
    """Wrap saved original clients in artificial window padding for builder I/O."""
    plan = tmp_path/"plan"
    (plan/"data/meta").mkdir(parents=True)
    monkeypatch.setattr(raw_builder, "PLAN", plan)
    monkeypatch.setattr(raw_builder, "review_sheet", lambda *args: None)
    monkeypatch.setattr(raw_builder, "font", lambda _: ImageFont.load_default())
    source = tmp_path/"source"
    source.mkdir()
    for name, identifier in raw_builder.SOURCES.items():
        image = Image.new("RGB", (1332, 802))
        with Image.open(ROOT/"tests/fixtures/raw_workshop_ui"/(name+".png")) as client:
            image.paste(client, (25, 56))
        image.save(source/("codex-clipboard-"+identifier+".png"))
    preview = tmp_path/"preview"
    raw_builder.build(source, preview)
    first = hashes(plan)
    data = json.loads((plan/"data/meta/raw_workshop_templates_pc.json").read_text("utf-8"))
    records = {entry["name"]: entry for entry in data["assets"]}
    for name in raw_builder.SHARED_SELECTION:
        current = raw._specs()[name]
        generated = records[name]
        assert generated["image"] == current["image"]
        assert generated["sha256"] == current["sha256"]
        assert generated["client_bbox_xywh"] == current["client_bbox_xywh"]
    raw_builder.build(source, preview)
    assert hashes(plan) == first
    assert len(data["assets"]) == len(raw_builder.ANNOTATIONS)
    assert len(list((plan/"templates").rglob("*.png"))) == len(data["assets"])


def room_builder_inputs(tmp_path, monkeypatch):
    """Synthetic white/gray texture tests deduplication, not room recognition."""
    plan = tmp_path/"plan"
    (plan/"data/meta").mkdir(parents=True)
    (plan/"data/meta/reforming_center_rooms_pc.json").write_bytes(
        (PLAN/"data/meta/reforming_center_rooms_pc.json").read_bytes())
    monkeypatch.setattr(room_builder, "PLAN", plan)
    identifiers = set(room_builder.OVERVIEW.values())
    identifiers.update(source for source, _ in room_builder.SELECTED_TITLES.values())
    identifiers.update(source for source, _ in room_builder.DESTINATIONS.values())
    identifiers.update(source for variants in room_builder.MAP_TITLE_VARIANTS.values() for source, _ in variants)
    source = tmp_path/"source"
    source.mkdir()
    yy, xx = np.indices((802, 1332))
    gray = np.where((xx+yy)%3, 240, 40).astype(np.uint8)
    for index, identifier in enumerate(sorted(identifiers)):
        image = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        image[0, 0] = index
        cv2.imwrite(str(source/("codex-clipboard-"+identifier+".png")), image)
    return source, plan


def test_room_builder_shared_bank_is_idempotent_and_retains_four_sources(tmp_path, monkeypatch):
    source, plan = room_builder_inputs(tmp_path, monkeypatch)
    room_builder.build(source)
    first = hashes(plan)
    data = json.loads((plan/"data/meta/reforming_center_templates_pc.json").read_text("utf-8"))
    markers = [room["destination_markers"][-1] for room in data["rooms"].values()
               if room["room_key"] in list(room_builder.DESTINATIONS)[:4]]
    assert len(markers) == len({marker["source_id"] for marker in markers}) == 4
    assert {marker["image"] for marker in markers} == {CONTROLS+"select_prisoner.png"}
    shared = plan/"templates/workshop/staffing/controls"
    assert {path.name for path in shared.iterdir()} == {"edit.png", "edit.mask.png", "select_prisoner.png", "select_prisoner.mask.png"}
    room_builder.build(source)
    assert hashes(plan) == first


def test_room_builder_rejects_different_pixels_in_a_shared_control(tmp_path, monkeypatch):
    source, _ = room_builder_inputs(tmp_path, monkeypatch)
    identifier = room_builder.DESTINATIONS["动物纤维原料车间"][0]
    path = source/("codex-clipboard-"+identifier+".png")
    image = cv2.imread(str(path))
    image[712, 902] = 0
    cv2.imwrite(str(path), image)
    with pytest.raises(ValueError, match="Conflicting shared control pixels"):
        room_builder.build(source)
