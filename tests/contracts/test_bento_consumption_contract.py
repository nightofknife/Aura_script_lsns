from pathlib import Path
import inspect

import pytest
import yaml
from jinja2.nativetypes import NativeEnvironment

from packages.aura_core.scheduler.validation import InputValidator
from plans.resonance_pc.src.actions.bento_consumption_pc_actions import resonance_pc_consume_bentos


ROOT = Path(__file__).resolve().parents[2]


def task():
    return yaml.safe_load((ROOT / "plans/resonance_pc/tasks/bento_consumption_pc.yaml").read_text(encoding="utf-8"))["bento_consumption_pc"]


def test_child_task_is_reusable_without_gui_entry():
    spec = task()
    assert spec["meta"]["entry_point"] is False
    assert spec["meta"]["concurrency"] == "exclusive"
    assert spec["steps"]["consume"]["action"] == "resonance_pc.consume_bentos"
    assert {"success", "status", "reason", "page_state", "consumed_count", "completed_count",
            "requires_refresh", "requested_count", "items"} <= spec["returns"].keys()
    parameters = inspect.signature(resonance_pc_consume_bentos).parameters
    assert parameters["meals"].default is inspect.Parameter.empty
    assert set(parameters) == {"meals", "app", "ocr", "vision", "persistent_data"}
    assert {row["name"] for row in spec["meta"]["inputs"]} == {"meals"}
    assert not {"base_fatigue_reserve", "initial_fatigue", "final_fatigue"}.intersection(spec["returns"])


@pytest.mark.parametrize("provided", [{"meals": []}, {"meals": [{"kind": "work_meals", "issue_time": "12:00"}]},
                                     {"meals": [{"kind": "love_bentos", "food_id": 83300019, "role_id": 7, "remaining_days": 1}]}])
def test_child_task_parameters_validate(provided):
    ok, validated = InputValidator(None).validate_inputs_against_meta(task()["meta"]["inputs"], provided)
    assert ok is True
    assert validated["meals"] == provided["meals"]


def test_meals_are_required():
    ok, _ = InputValidator(None).validate_inputs_against_meta(task()["meta"]["inputs"], {})
    assert ok is False


def test_manifest_exports_shared_action_and_task():
    manifest = yaml.safe_load((ROOT / "plans/resonance_pc/manifest.yaml").read_text(encoding="utf-8"))
    assert any(row["name"] == "resonance_pc.consume_bentos" for row in manifest["exports"]["actions"])
    assert any("bento_consumption_pc.yaml" in str(row) for row in manifest["exports"]["tasks"])


def test_items_return_is_data_not_dict_method():
    items = [{"meal": {"kind": "work_meals"}, "consumed": True}]
    result = NativeEnvironment().from_string(task()["returns"]["items"]).render(
        nodes={"consume": {"output": {"items": items}}})
    assert result == items
