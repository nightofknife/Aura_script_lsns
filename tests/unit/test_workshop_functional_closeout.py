"""Remaining workshop output ownership and complete glyph-bank rebuild checks."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

from PIL import Image
import pytest

from tools import build_product_workshop_staff_templates as staff_builder
from tools import build_raw_workshop_digit_templates as digits_builder

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "plans/resonance_pc"


def hashes(root):
    return {p.relative_to(root).as_posix(): sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def digit_inputs(tmp_path, monkeypatch):
    plan = tmp_path / "plan"
    (plan / "data/meta").mkdir(parents=True)
    monkeypatch.setattr(digits_builder, "PLAN", plan)
    counter = tmp_path / f"{digits_builder.FONT_ID}.ttf"
    footer = tmp_path / f"{digits_builder.SELECTION_FONT_ID}.ttf"
    counter.write_bytes(b"counter")
    footer.write_bytes(b"selection")
    monkeypatch.setattr(digits_builder, "render_glyph", lambda raw, char, size, scale:
                        Image.new("L", (10, 15), (sum(raw) + ord(char) + size) % 255))
    preview = tmp_path / "preview"
    digits_builder.build(tmp_path, preview, counter, footer)
    return plan, counter, footer, preview


@pytest.mark.parametrize("bad_input", ["missing", "wrong_id", "empty", "invalid_bytes", "missing_file"])
def test_rejected_selection_font_preserves_complete_bank_and_preview(tmp_path, monkeypatch, bad_input):
    plan, counter, footer, preview = digit_inputs(tmp_path, monkeypatch)
    if bad_input == "missing":
        footer = None
    elif bad_input == "wrong_id":
        footer = tmp_path / "wrong.ttf"
        footer.write_bytes(b"invalid")
    elif bad_input == "empty":
        footer.write_bytes(b"")
    elif bad_input == "missing_file":
        footer.unlink()
    else:
        monkeypatch.setattr(digits_builder, "render_glyph", lambda raw, *args:
                            (_ for _ in ()).throw(ValueError("Invalid font bytes")) if raw == b"selection"
                            else Image.new("L", (10, 15)))
    before = hashes(tmp_path)
    with pytest.raises((ValueError, FileNotFoundError)):
        digits_builder.build(tmp_path, preview, counter, footer)
    assert hashes(tmp_path) == before
    catalog = json.loads((plan / "data/meta/raw_workshop_digits_pc.json").read_text("utf-8"))
    assert len(catalog["assets"]) == 77
    assert {row["style"] for row in catalog["assets"]} == {"list", "selection"}


def test_changed_fonts_replace_owned_bank_without_obsolete_outputs(tmp_path, monkeypatch):
    plan, counter, footer, preview = digit_inputs(tmp_path, monkeypatch)
    path = plan / "data/meta/raw_workshop_digits_pc.json"
    catalog = json.loads(path.read_text("utf-8"))
    old = plan / "templates/workshop/production/digits/selection_retired_7.png"
    Image.new("L", (5, 6)).save(old)
    catalog["assets"].append({"image": old.relative_to(plan).as_posix()})
    path.write_text(json.dumps(catalog), "utf-8")
    unrelated = plan / "templates/workshop/production/controls/unrelated.png"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"not owned")
    counter.write_bytes(b"changed counter")
    digits_builder.build(tmp_path, preview, counter, footer)
    result = json.loads(path.read_text("utf-8"))
    assert len(result["assets"]) == 77
    assert result["font_sha256"] == sha256(counter.read_bytes()).hexdigest()
    assert not old.exists()
    assert unrelated.read_bytes() == b"not owned"
    bank = plan / "templates/workshop/production/digits"
    assert {p.relative_to(plan).as_posix() for p in bank.glob("*.png")} == {
        row["image"] for row in result["assets"]}
    first = hashes(plan)
    digits_builder.build(tmp_path, preview, counter, footer)
    assert hashes(plan) == first


def test_staff_refresh_retires_only_explicit_obsolete_images(tmp_path, monkeypatch):
    plan = tmp_path / "plan"
    assets = plan / "templates/workshop/staffing/editor"
    assets.mkdir(parents=True)
    monkeypatch.setattr(staff_builder, "PLAN", plan)
    monkeypatch.setattr(staff_builder, "ASSETS", assets)
    for name in staff_builder.RETIRED_FEATURES:
        (assets / (name + ".png")).write_bytes(b"retired")
    unrelated = assets / "unrelated.png"
    unrelated.write_bytes(b"not owned")
    catalog = json.loads((PLAN / "data/meta/product_workshop_staff_templates_pc.json").read_text("utf-8"))
    staff_builder.refresh_feature_blocks(catalog)
    assert len(staff_builder.RETIRED_FEATURES) == 15
    assert all(not (assets / (name + ".png")).exists() for name in staff_builder.RETIRED_FEATURES)
    assert unrelated.read_bytes() == b"not owned"
    assert not set(catalog["templates"]) & staff_builder.RETIRED_FEATURES
    for key in ("quick", "room_1_title", "room_2_title", "apply", "discard", "select_all", "cancel_all", "confirm"):
        record = catalog["templates"][key]
        assert record["threshold"] == .90
        assert record["image"].startswith("templates/workshop/staffing/editor/")
        current = json.loads((PLAN / "data/meta/product_workshop_staff_templates_pc.json").read_text("utf-8"))["templates"][key]
        for produced, previous in zip((record, *record.get("variants", [])), (current, *current.get("variants", [])), strict=True):
            with Image.open(plan / produced["image"]) as actual, Image.open(PLAN / previous["image"]) as expected:
                assert (actual.mode, actual.size, actual.tobytes()) == (expected.mode, expected.size, expected.tobytes())
            assert produced["bbox"] == previous["bbox"]
            assert produced["search_region"] == previous["search_region"]
    first = hashes(plan)
    staff_builder.refresh_feature_blocks(catalog)
    assert hashes(plan) == first


def test_workshop_functional_banks_have_no_task_directory_or_physical_orphans():
    assert not (PLAN / "templates/reforming_center").exists()
    assert not (PLAN / "templates/product_workshop_staff").exists()
    refs = set()
    def walk(value):
        if isinstance(value, str) and value.startswith("templates/") and value.endswith(".png"):
            refs.add(value)
        elif isinstance(value, dict):
            for child in value.values(): walk(child)
        elif isinstance(value, list):
            for child in value: walk(child)
    for name in ("product_workshop_staff", "raw_workshop", "reforming_center"):
        walk(json.loads((PLAN / f"data/meta/{name}_templates_pc.json").read_text("utf-8")))
    for directory in ("navigation/rooms", "workshop/staffing/editor", "workshop/production/controls"):
        files = {p.relative_to(PLAN).as_posix() for p in (PLAN / "templates" / directory).rglob("*.png")}
        assert files
        assert files <= refs
