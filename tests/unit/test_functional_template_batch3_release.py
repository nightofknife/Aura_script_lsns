"""Third-batch resources retain business identity after real release staging."""
import json
from pathlib import Path
from types import SimpleNamespace

from plans.aura_base.src.services.vision_service import VisionService
from plans.resonance_pc.src.actions import character_pc_actions as characters
from plans.resonance_pc.src.actions import inventory_pc_actions as inventory
from scripts.release.assemble_release_plans import (
    collect_plan_files,
    stage_plan_tree,
    validate_selected_files,
)


ROOT = Path(__file__).resolve().parents[2]
PLAN = Path("plans/resonance_pc")
CATALOGS = (
    "player_characters.json", "eternal_scuffle.json", "inventory_equipment.json",
    "inventory_digits.json", "inventory_expiry_digits.json", "raw_workshop_digits_pc.json",
)


def references(value):
    if isinstance(value, str):
        if value.startswith("templates/"):
            yield value
    elif isinstance(value, list):
        for entry in value:
            yield from references(entry)
    elif isinstance(value, dict):
        for key, entry in value.items():
            # Original source paths remain historical evidence, never load paths.
            if key.startswith(("source", "original")) or key in {"provenance", "old"}:
                continue
            if key.startswith("templates/"):
                yield key
            yield from references(entry)


def test_staged_character_equipment_and_digit_banks_preserve_business_records(tmp_path):
    selected = {destination: source for source, destination in collect_plan_files(ROOT / "plans")}
    assert not any(destination.is_relative_to(PLAN / "development") for destination in selected)
    assert not any(destination.is_relative_to(PLAN / "templates/characters/stand") for destination in selected)
    required = {PLAN / "data/meta" / name for name in CATALOGS}
    for name in CATALOGS:
        payload = json.loads((ROOT / PLAN / "data/meta" / name).read_text(encoding="utf-8"))
        for reference in references(payload):
            relative = PLAN / reference
            if (ROOT / relative).is_dir():
                required.update(path for path in selected if path.is_relative_to(relative))
            else:
                required.add(relative)
    assert required <= selected.keys(), sorted(required - selected.keys())
    files = [(selected[destination], destination) for destination in sorted(required)]
    validate_selected_files(files)
    target = tmp_path / "release/plans"
    assert not target.exists()
    stage_plan_tree(files, target)
    package = target / "resonance_pc"
    for destination in required:
        assert (target / destination.relative_to("plans")).read_bytes() == selected[destination].read_bytes()

    character_spec = json.loads((package / "data/meta/player_characters.json").read_text(encoding="utf-8"))
    loaded = characters.load_character_catalog(package / "data/meta/player_characters.json", plan_root=package)
    assert [(row["character_id"], row["name"]) for row in loaded["characters"]] == [
        (row["character_id"], row["name"]) for row in character_spec["characters"]]
    by_id = {row["character_id"]: row for row in loaded["characters"]}
    assert by_id["弗兰"]["template_refs"][0] == by_id["空"]["template_refs"][0]
    assert by_id["弗兰"]["character_id"] != by_id["空"]["character_id"]

    equipment_spec = json.loads((package / "data/meta/inventory_equipment.json").read_text(encoding="utf-8"))
    equipment = inventory.load_inventory_catalog(package / "data/meta/inventory_equipment.json", plan_root=package)
    assert [row["item_id"] for row in equipment["items"]] == [row["equipment_id"] for row in equipment_spec["equipment"]]
    for reader, name in ((inventory.load_inventory_digit_catalog, "inventory_digits.json"),
                         (inventory.load_inventory_expiry_digit_catalog, "inventory_expiry_digits.json")):
        assert reader(package / "data/meta" / name, plan_root=package,
                      vision=SimpleNamespace(load_image_file=VisionService.load_image_file))
