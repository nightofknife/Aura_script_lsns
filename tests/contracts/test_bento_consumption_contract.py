from pathlib import Path
import inspect

import pytest
import yaml
from jinja2.nativetypes import NativeEnvironment

from packages.aura_core.scheduler.validation import InputValidator
from plans.resonance_pc.src.actions.bento_auto_recovery_pc_actions import resonance_pc_consume_bentos_to_floor


ROOT = Path(__file__).resolve().parents[2]


def task():
    return yaml.safe_load((ROOT / "plans/resonance_pc/tasks/bento_consumption_pc.yaml").read_text(encoding="utf-8"))["bento_consumption_pc"]


def test_independent_task_is_also_callable_from_freight():
    spec = task()
    assert spec["meta"]["entry_point"] is True
    assert spec["meta"]["concurrency"] == "exclusive"
    assert spec["steps"]["consume"]["action"] == "resonance_pc.consume_bentos_to_floor"
    assert {"success", "status", "reason", "page_state", "consumed_count", "completed_count",
            "requires_refresh", "recovered_fatigue", "initial_fatigue", "computed_fatigue",
            "base_fatigue_reserve", "items"} <= spec["returns"].keys()
    parameters = inspect.signature(resonance_pc_consume_bentos_to_floor).parameters
    assert parameters["target_recovery_amount"].default == 2000
    assert parameters["allow_exceed_target"].default is False
    assert parameters["base_fatigue_reserve"].default == -100
    assert {row["name"] for row in spec["meta"]["inputs"]} == {
        "eat_work_meals", "eat_love_bentos", "bento_priority",
        "target_recovery_amount", "allow_exceed_target", "base_fatigue_reserve",
    }


@pytest.mark.parametrize("provided", [
    {"eat_work_meals": True, "eat_love_bentos": False, "bento_priority": ["work_meals"]},
    {"eat_work_meals": False, "eat_love_bentos": True, "bento_priority": ["love_bentos"]},
    {"eat_work_meals": True, "eat_love_bentos": True,
     "bento_priority": ["love_bentos", "work_meals"], "target_recovery_amount": 100},
])
def test_child_task_parameters_validate(provided):
    ok, validated = InputValidator(None).validate_inputs_against_meta(task()["meta"]["inputs"], provided)
    assert ok is True
    assert all(validated[key] == value for key, value in provided.items())


def test_standalone_defaults_do_not_require_meal_identities():
    ok, values = InputValidator(None).validate_inputs_against_meta(task()["meta"]["inputs"], {})
    assert ok is True
    assert values["bento_priority"] == ["work_meals", "love_bentos"]
    assert values["target_recovery_amount"] == 2000


def test_manifest_exports_shared_action_and_task():
    manifest = yaml.safe_load((ROOT / "plans/resonance_pc/manifest.yaml").read_text(encoding="utf-8"))
    assert any(row["name"] == "resonance_pc.consume_bentos_to_floor" for row in manifest["exports"]["actions"])
    assert any("bento_consumption_pc.yaml" in str(row) for row in manifest["exports"]["tasks"])


def test_items_return_is_data_not_dict_method():
    items = [{"meal": {"kind": "work_meals"}, "consumed": True}]
    result = NativeEnvironment().from_string(task()["returns"]["items"]).render(
        nodes={"consume": {"output": {"items": items}}})
    assert result == items
