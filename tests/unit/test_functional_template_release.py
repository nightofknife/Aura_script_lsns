"""Functional template references survive the actual release plan assembler."""
import json
from pathlib import Path

from scripts.release.assemble_release_plans import (
    collect_plan_files,
    stage_plan_tree,
    validate_selected_files,
)


ROOT = Path(__file__).resolve().parents[2]
PLAN = Path("plans/resonance_pc")
CATALOGS = (
    "data/meta/reforming_center_templates_pc.json",
    "data/meta/raw_workshop_templates_pc.json",
    "data/meta/product_workshop_staff_templates_pc.json",
    "templates/commerce/shop/black_moon/native/manifest.json",
)


def asset_references(value):
    if isinstance(value, dict):
        for key, entry in value.items():
            if key in {"image", "mask", "file", "mask_file"} and isinstance(entry, str):
                if entry.startswith("templates/"):
                    yield entry
            yield from asset_references(entry)
    elif isinstance(value, list):
        for entry in value:
            yield from asset_references(entry)


def test_release_assembly_keeps_functional_assets_and_resolves_catalogs(tmp_path):
    selected = dict((destination, source) for source, destination in collect_plan_files(ROOT / "plans"))
    references = {
        reference
        for catalog in CATALOGS
        for reference in asset_references(json.loads((ROOT / PLAN / catalog).read_text(encoding="utf-8")))
    }
    assert any(path.startswith("templates/workshop/staffing/controls/") for path in references)
    assert "templates/commerce/shop/black_moon/native/controls/local_tab.mask.png" in references
    required = {PLAN / relative for relative in (*CATALOGS, *references)}
    assert required <= selected.keys(), sorted(required - selected.keys())
    files = [(selected[destination], destination) for destination in sorted(required)]
    validate_selected_files(files)
    target = tmp_path / "release/plans"
    assert not target.exists()
    stage_plan_tree(files, target)
    for destination in required:
        staged = target / destination.relative_to("plans")
        assert staged.read_bytes() == selected[destination].read_bytes()
    for catalog in CATALOGS:
        packaged = json.loads((target / "resonance_pc" / catalog).read_text(encoding="utf-8"))
        for reference in asset_references(packaged):
            assert (target / "resonance_pc" / reference).is_file(), reference
