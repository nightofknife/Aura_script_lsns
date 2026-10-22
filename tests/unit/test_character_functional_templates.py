"""Functional art sharing preserves scene profiles and business identities."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image
import pytest

from tools.build_eternal_scuffle_assets import FunctionalAssetWriter, add_occupied_templates, source_ordered_entries


PLAN = Path(__file__).resolve().parents[2] / "plans/resonance_pc"


def test_sorted_physical_emission_restores_logical_source_order(tmp_path):
    source = [{"id": 9}, {"id": 2}, {"id": 6}]
    assets = {}
    writer = FunctionalAssetWriter(tmp_path, assets)
    emitted = []
    for row in sorted(source, key=lambda row: row["id"]):
        relative = writer.write(Image.new("RGB", (2, 2), (1, 2, 3)),
                                f"templates/inventory/equipment/art/{row['id']}/icon_88.png",
                                "icon/image", {"source_sha256": str(row["id"])})
        emitted.append({**row, "template": relative})
    logical = source_ordered_entries(emitted, source)
    assert [row["id"] for row in logical] == [9, 2, 6]
    assert all(row["template"].endswith("/2/icon_88.png") for row in logical)


def test_scuffle_functional_art_has_complete_logical_coverage_and_source_records():
    catalog = json.loads((PLAN / "data/meta/eternal_scuffle.json").read_text(encoding="utf-8"))
    development = json.loads((PLAN / "development/template_sources/characters/stand/scuffle/manifest.json").read_text(encoding="utf-8"))
    sources = {row["id"]: row for row in development["characters"]}
    assert len(catalog["characters"]) == 99
    assert len(catalog["equipment"]) == 176
    by_id = {row["id"]: row for row in catalog["equipment"]}
    assert list(sources) == [row["id"] for row in catalog["characters"]]
    for row in catalog["characters"]:
        source = sources[row["id"]]
        assert not {"stand_templates", "stand_masks", "stand_sources"} & row.keys()
        assert len(source["stand_templates"]) == len(source["stand_masks"]) == len(source["stand_sources"]) == 8
        assert source["source"] == row["source"]
        assert row["name_template"].startswith("templates/characters/nameplates/scuffle/")
        assert all(ref.startswith("development/template_sources/characters/stand/scuffle/") for ref in source["stand_templates"])
    for left, right in ((11800009, 11800010), (11800053, 11800278), (11800057, 11800101)):
        assert by_id[left]["template"] == by_id[right]["template"]
        assert by_id[left]["name_template"] != by_id[right]["name_template"]
        assert by_id[left]["id"] != by_id[right]["id"]
    assert not set(catalog["assets"]) & set(development["assets"])
    assert all(relative.startswith("templates/") for relative in catalog["assets"])
    assert all(relative.startswith("development/template_sources/characters/stand/scuffle/") for relative in development["assets"])
    for relative, metadata in {**catalog["assets"], **development["assets"]}.items():
        path = PLAN / relative
        assert hashlib.sha256(path.read_bytes()).hexdigest() == metadata["sha256"]
        if relative.startswith(("templates/characters/", "templates/inventory/equipment/", "development/template_sources/characters/")):
            for source in metadata.get("source_variants", [metadata]):
                assert source.get("source_sha256") or source.get("source_font_sha256")
    assert not (PLAN / "templates/eternal_scuffle/characters").exists()
    assert not (PLAN / "templates/eternal_scuffle/equipment").exists()
    assert not (PLAN / "templates/character_stars").exists()


def test_asset_writer_shares_only_equal_pixels_modes_and_views_and_is_repeatable(tmp_path):
    def generate():
        assets = {}
        writer = FunctionalAssetWriter(tmp_path, assets)
        image = Image.new("RGBA", (12, 12), (30, 40, 50, 255))
        first = writer.write(image, "templates/inventory/equipment/art/1/icon_88.png", "icon/image", {"source_sha256": "first"})
        alias = writer.write(image.copy(), "templates/inventory/equipment/art/2/icon_88.png", "icon/image", {"source_sha256": "second"})
        other_view = writer.write(image, "templates/inventory/equipment/art/1/tips_88.png", "tips/image", {"source_sha256": "first"})
        other_mode = writer.write(image.convert("RGB"), "templates/inventory/equipment/art/1/icon_rgb.png", "icon/image", {"source_sha256": "first"})
        assert first == alias
        assert other_view != first != other_mode
        assert [row["source_sha256"] for row in assets[first]["source_variants"]] == ["first", "second"]
        assert not (tmp_path / "templates/inventory/equipment/art/2/icon_88.png").exists()
        writer.finalize({"assets": assets}, tmp_path / "data/meta/test_assets.json")
        return assets
    first = generate()
    files = {path.relative_to(tmp_path).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in tmp_path.rglob("*.png")}
    assert generate() == first
    assert {path.relative_to(tmp_path).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in tmp_path.rglob("*.png")} == files
    assert set(first) == set(files)
    writer = FunctionalAssetWriter(tmp_path, {})
    with pytest.raises(ValueError, match="escapes"):
        writer.write(Image.new("RGB", (1, 1)), "templates/../../escape.png", "icon/image", {})


def test_card_frame_generator_writes_only_function_owned_paths_and_keeps_profiles(tmp_path):
    source = tmp_path / "source"
    (source / "ui").mkdir(parents=True)
    for quality in ("UR", "SSR", "SR", "R"):
        Image.new("RGBA", (90, 90), (17, 38, 61, 255)).save(source / f"ui/RandomBattle_Equipment_{quality}.png")
    plan = tmp_path / "plan"
    catalog = {"assets": {}, "controls": {}}
    add_occupied_templates(catalog, source, plan)
    for key, control in catalog["controls"].items():
        assert key.startswith("occupied_")
        assert control["threshold"] == .90
        assert control["match_method"] == "TM_SQDIFF_NORMED"
        assert control["template"].startswith("templates/inventory/equipment/frames/scuffle/")
    assert len({row["mask"] for row in catalog["controls"].values()}) == 1
    assert next(iter(catalog["controls"].values()))["mask"].endswith("/occupied_mask.png")
    assert {path.relative_to(plan).as_posix() for path in plan.rglob("*.png")} == set(catalog["assets"])
    assert not (plan / "templates/eternal_scuffle").exists()
