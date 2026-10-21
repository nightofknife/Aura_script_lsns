"""The approved 64-source inventory has one explicit, executable placement."""
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from packages.aura_core.packaging.core.package_manager import PackageManager
from packages.aura_core.packaging.core.task_catalog import PUBLIC_TASK_ROLES, is_user_entry, select_tasks, task_role
from packages.aura_core.packaging.core.task_loader import TaskLoader
from packages.aura_core.packaging.core.task_validator import TaskDefinitionValidator, TaskValidationError
from packages.aura_core.packaging.manifest.generator import ManifestGenerator
from packages.aura_core.types import TaskRefResolver

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "plans/resonance_pc"
REMOVED = {
    "scuffle_checked_round_pc": "scuffle_round_pc",
    "scuffle_checked_pair_pc": "scuffle_draft_pair_pc",
    "scuffle_checked_advance_pc": "scuffle_advance_pc",
    "auto_battle_input_preview_pc": "auto_battle_dispatch_pc",
    "navigate_reforming_center_room_pc": "open_reforming_center_room_pc",
}
RENAMED = {"consciousness_deep_dive_single_run_test_pc": "consciousness_deep_dive_random_run_pc"}
ADDED = {"affix_refresh_pc", "affix_refresh_read_pc"}


def definitions():
    return {key: spec for path in (PLAN / "tasks").glob("*.yaml")
            for key, spec in yaml.safe_load(path.read_text(encoding="utf-8")).items()}


def references(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "task_ref":
                yield item
            yield from references(item)
    elif isinstance(value, list):
        for item in value:
            yield from references(item)


def test_all_64_source_definitions_accounted_without_old_aliases():
    inventory = json.loads((ROOT / "tests/fixtures/task_hierarchy_source_inventory.json").read_text(encoding="utf-8"))
    original = {row["key"] for row in inventory}
    current = definitions()
    assert len(inventory) == len(original) == 64
    assert set(current) == (original - REMOVED.keys() - RENAMED.keys()) | set(RENAMED.values()) | ADDED
    assert len(current) == 61
    assert all(replacement in current for replacement in (*REMOVED.values(), *RENAMED.values()))
    assert all(spec["meta"]["domain"] for spec in current.values())
    assert Counter(task_role(spec["meta"]) for spec in current.values()) == {
        "lifecycle": 2, "orchestration": 1, "user_task": 11, "user_tool": 3,
        "developer": 9, "internal": 35,
    }


def test_runtime_and_manifest_publish_one_matching_canonical_entry_per_definition():
    loader = TaskLoader("resonance_pc", PLAN)
    runtime = loader.get_all_task_definitions()
    assert not loader.get_task_load_errors()
    assert len(runtime) == 61
    generated = ManifestGenerator(PLAN).generate()["exports"]["tasks"]
    assert {row["id"] for row in generated} == set(runtime)
    assert len(generated) == len(runtime)
    for key, spec in runtime.items():
        resolved = TaskRefResolver.resolve(spec["__task_ref__"], default_package="resonance_pc")
        assert resolved.loader_path == key
        assert loader.get_task_data(resolved.loader_path) is spec
        assert loader.get_task_data(key.split("/")[-1]) is None


def test_all_nested_task_references_resolve_and_runtime_never_depends_on_developer_wrappers():
    loader = TaskLoader("resonance_pc", PLAN)
    for key, spec in definitions().items():
        for ref in references(spec):
            resolved = TaskRefResolver.resolve(ref, default_package="resonance_pc")
            assert resolved.task_key is not None, (key, ref)
            child = loader.get_task_data(resolved.loader_path)
            assert child is not None, (key, ref)
            if task_role(spec["meta"]) != "developer":
                assert task_role(child["meta"]) != "developer", (key, ref)


def test_user_discovery_uses_roles_and_keeps_internal_tasks_resolvable():
    rows = [{"plan_name": "resonance_pc", "full_task_id": key, "task_ref": key, "meta": spec["meta"]}
            for key, spec in definitions().items()]
    selected = select_tasks(rows)
    assert len(selected) == 17
    assert all(task_role(row["meta"]) in PUBLIC_TASK_ROLES for row in selected)
    assert {row["task_ref"] for row in select_tasks(rows, roles=("developer",))} == {
        key for key, spec in definitions().items() if spec["meta"]["role"] == "developer"}
    assert not is_user_entry(definitions()["player_data_get_latest"]["meta"])


@pytest.mark.parametrize("meta", [{}, {"entry_point": True}, {"role": "obsolete"},
                                  {"role": "internal", "entry_point": True}])
def test_missing_unknown_or_conflicting_role_is_rejected(meta):
    validator = TaskDefinitionValidator(plan_name="example", enable_schema_validation=True, strict_validation=True)
    with pytest.raises(TaskValidationError, match="role"):
        validator.validate_file({"example": {"meta": meta, "steps": {"work": {"action": "work"}}}}, Path("example.yaml"))


def test_scuffle_merged_checks_keep_pair_captain_advance_and_exit_boundaries():
    tasks = definitions()
    for key in ("scuffle_round_pc", "scuffle_draft_pair_pc", "scuffle_advance_pc"):
        assert tasks[key]["steps"]["finish"]["action"] == "resonance_pc.eternal_scuffle_checkpoint"
        assert tasks[key]["steps"]["finish"]["params"]["require_child"] is True
    round_steps = tasks["scuffle_round_pc"]["steps"]
    assert round_steps["captain"]["depends_on"] == "check_pairs"
    assert round_steps["captain"]["action"] == "resonance_pc.eternal_scuffle_captain"
    assert round_steps["check_settle"]["params"]["phases"] == ["home"]
    assert tasks["scuffle_draft_pair_pc"]["steps"]["equipment"]["depends_on"] == "role"


def test_specialized_battle_batches_and_action_summary_group_are_retained():
    tasks = definitions()
    for key in ("auto_battle_ct_tie_an_batch_pc", "auto_battle_ct_regional_ops_batch_pc",
                "auto_battle_gp_action_summary_batch_pc", "auto_battle_gp_structural_batch_pc"):
        assert tasks[key]["meta"]["role"] == "internal"
    batch_refs = list(references(tasks["auto_battle_gp_action_summary_batch_pc"]))
    group_refs = list(references(tasks["auto_battle_gp_action_summary_run_group_pc"]))
    assert any(ref.endswith(":auto_battle_gp_action_summary_run_group_pc") for ref in batch_refs)
    assert any(ref.endswith(":auto_battle_gp_action_summary_run_difficulty_pc") for ref in group_refs)


def test_cache_only_plan_is_not_discovered_or_given_a_generated_manifest(tmp_path):
    ghost = tmp_path / "plans/abandoned"
    (ghost / "src/__pycache__").mkdir(parents=True)
    (ghost / "src/__pycache__/stale.pyc").write_bytes(b"cache")
    (ghost / "screenshots").mkdir()
    (ghost / "screenshots/view.png").write_bytes(b"image")
    (ghost / "tasks").mkdir()
    manager = PackageManager(tmp_path / "packages", tmp_path / "plans")
    manager.auto_sync_manifest = True
    manager._auto_sync_manifests()
    assert list(manager._iter_package_dirs()) == []
    assert not (ghost / "manifest.yaml").exists()
