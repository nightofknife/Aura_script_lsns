"""Only current PC workflow entry points and their assets ship in release plans."""
import importlib.util
from copy import deepcopy
import json
from pathlib import Path

from PIL import Image
import yaml

from scripts.release.assemble_release_plans import (
    collect_plan_files, stage_plan_tree, validate_selected_files,
)


ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "plans/resonance_pc"
RETIRED_ACTIONS = {
    "resonance_pc.product_workshop_quick_fill",
    "resonance_pc.purchase_black_moon_local_from_rest_menu",
    *("resonance_pc." + name for name in (
        "trade_plan_next", "trade_plan_best_cycle", "trade_plan_cycle_execution",
        "trade_plan_next_cycle_execution", "trade_simulate", "trade_loop_init",
        "trade_loop_update", "trade_loop_summary", "trade_loop_cleanup",
    )),
}
CURRENT_ACTIONS = {
    *("resonance_pc." + name for name in (
        "product_workshop_staff_assets", "product_workshop_staff_step", "product_workshop_staff_summarize",
        "begin_black_moon_local_visit", "prepare_black_moon_local_shop_step",
        "select_black_moon_local_goods", "submit_black_moon_local_visit",
        "complete_black_moon_local_visit", "finish_black_moon_local_visit",
        "trade_plan_optimal_route", "trade_route_execution_init", "trade_route_execution_update",
        "trade_route_execution_summary", "trade_route_execution_cleanup", "trade_assert_allowed_city",
        "product_collection_assets", "product_collection_scan_unlocks",
        "product_collection_collect_room", "product_collection_summarize",
        "affix_refresh_read", "affix_refresh_run",
    )),
}
RETIRED_PATHS = (
    "src/actions/product_workshop_pc_actions.py", "src/actions/_product_workshop_ui.py",
    "src/actions/black_moon_local_shop_pc_actions.py", "data/meta/product_workshop_templates_pc.json",
    "templates/workshop/production/controls/editor", "templates/commerce/shop/black_moon/cards",
    "templates/commerce/shop/black_moon/masks/item_identity_120x119.png",
)


def walk(value):
    if isinstance(value, dict):
        for key, entry in value.items():
            if key.startswith(("source", "original")) or key in {"provenance", "old"}:
                continue
            yield key, entry
            yield from walk(entry)
    elif isinstance(value, list):
        for entry in value:
            yield from walk(entry)


def test_old_public_exports_and_forwarding_modules_are_gone():
    manifest = yaml.safe_load((PLAN / "manifest.yaml").read_text(encoding="utf8"))
    names = {row["name"] for row in manifest["exports"]["actions"]}
    assert not RETIRED_ACTIONS & names
    assert CURRENT_ACTIONS <= names
    for relative in RETIRED_PATHS:
        assert not (PLAN / relative).exists(), relative
    for name in ("product_workshop_pc_actions", "_product_workshop_ui", "black_moon_local_shop_pc_actions"):
        assert importlib.util.find_spec("plans.resonance_pc.src.actions." + name) is None
    assert not (ROOT / "tools/build_product_workshop_templates.py").exists()


def test_tasks_have_no_retired_action_and_keep_current_entry_chains():
    task_actions = set()
    for path in (PLAN / "tasks").rglob("*.yaml"):
        if any(word in path.name.lower() for word in ("deep", "consciousness")):
            continue
        data = yaml.safe_load(path.read_text(encoding="utf8"))
        for key, entry in walk(data):
            if key == "action" and isinstance(entry, str):
                task_actions.add(entry.removeprefix("plans/resonance_pc/"))
    assert not RETIRED_ACTIONS & task_actions
    assert {"resonance_pc.product_workshop_staff_step", "resonance_pc.begin_black_moon_local_visit",
            "resonance_pc.finish_black_moon_local_visit"} <= task_actions


def test_release_stage_contains_current_inputs_without_retired_assets(tmp_path):
    selected = {destination: source for source, destination in collect_plan_files(ROOT / "plans")}
    relative_plan = Path("plans/resonance_pc")
    for retired in RETIRED_PATHS:
        assert not any(path == relative_plan / retired or path.is_relative_to(relative_plan / retired)
                       for path in selected)
    catalogs = ("data/meta/product_workshop_staff_templates_pc.json",
                "templates/commerce/shop/black_moon/native/manifest.json",
                "templates/commerce/shop/black_moon/calibrated/manifest.json")
    required = {relative_plan / "manifest.yaml"}
    task_sources = {path: source for path, source in selected.items()
                    if path.is_relative_to(relative_plan / "tasks") and path.suffix in {".yaml", ".yml"}}
    required.update(task_sources)
    for relative in catalogs:
        path = PLAN / relative
        required.add(relative_plan / relative)
        payload = json.loads(path.read_text(encoding="utf8"))
        for key, entry in walk(payload):
            if key not in {"image", "mask", "file", "mask_file"} or not isinstance(entry, str):
                continue
            if not entry.endswith(".png"):
                continue
            if relative.startswith("data/meta/") and not entry.startswith("templates/"):
                # Scenario screenshots are development evidence, not runtime assets.
                continue
            asset = PLAN / entry if entry.startswith("templates/") else path.parent / entry
            required.add(asset.relative_to(ROOT))
    assert required <= selected.keys(), sorted(required - selected.keys())
    files = [(selected[path], path) for path in sorted(required)]
    validate_selected_files(files)
    target = tmp_path / "release/plans"
    stage_plan_tree(files, target)
    for source, relative in files:
        staged = target / relative.relative_to("plans")
        if relative == relative_plan / "manifest.yaml":
            original = yaml.safe_load(source.read_text(encoding="utf8"))
            expected = deepcopy(original)
            runtime_exports = []
            for task in original["exports"]["tasks"]:
                task_source = relative_plan / task["source"]
                if task_source not in task_sources or task.get("role") == "developer":
                    continue
                definitions = yaml.safe_load(task_sources[task_source].read_text(encoding="utf8"))
                task_id = str(task.get("id", ""))
                if task_id in definitions or task_id.rsplit("/", 1)[-1] in definitions:
                    runtime_exports.append(task)
            expected["exports"]["tasks"] = runtime_exports
            assert yaml.safe_load(staged.read_text(encoding="utf8")) == expected
            assert CURRENT_ACTIONS <= {row["name"] for row in expected["exports"]["actions"]}
            assert runtime_exports and all(row.get("role") != "developer" for row in runtime_exports)
            continue
        if relative in task_sources:
            definitions = yaml.safe_load(source.read_text(encoding="utf8"))
            expected = {key: value for key, value in definitions.items()
                        if not (isinstance(value, dict) and value.get("meta", {}).get("role") == "developer")}
            assert yaml.safe_load(staged.read_text(encoding="utf8")) == expected
            continue
        assert staged.read_bytes() == source.read_bytes()
        if staged.suffix == ".png":
            with Image.open(staged) as image:
                image.load()
    native = json.loads((target / "resonance_pc" / catalogs[1]).read_text(encoding="utf8"))
    catalog = json.loads((PLAN / "data/meta/black_moon_local_shop_catalog.json").read_text(encoding="utf8"))
    expected_ids = {row["item_id"] for row in catalog["products"]}
    assert {row["item_id"] for row in native["cards"]["items"]} == expected_ids
    assert len(native["cards"]["items"]) == len(expected_ids)
    assert {row["state"] for row in native["cards"]["items"]} == {"normal"}
    expected_files = {entry for key, entry in walk({"templates": native["templates"], "items": native["cards"]["items"]})
                      if key in {"file", "mask_file"} and isinstance(entry, str) and entry.endswith(".png")}
    assert set(native["files"]) == expected_files
    assert not list((target / "resonance_pc/templates/commerce/shop/black_moon/native/items").glob("*_selected.png"))
