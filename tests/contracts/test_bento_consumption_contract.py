from pathlib import Path
import inspect

import pytest
import yaml
from jinja2.nativetypes import NativeEnvironment

from packages.aura_core.scheduler.validation import InputValidator
from plans.resonance_pc.src.actions.bento_auto_recovery_pc_actions import resonance_pc_consume_bentos_to_floor


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_THRESHOLDS = {"bento_start_threshold": 600, "bento_stop_threshold": 100}
THRESHOLDS = set(DEFAULT_THRESHOLDS)
LEGACY_CONTROLS = {"target_recovery_amount", "allow_exceed_target", "base_fatigue_reserve"}


def task():
    return yaml.safe_load((ROOT / "plans/resonance_pc/tasks/bento_consumption_pc.yaml").read_text(encoding="utf-8"))["bento_consumption_pc"]


def test_independent_task_is_also_callable_from_freight():
    spec = task()
    assert spec["meta"]["role"] == "internal" and spec["meta"]["entry_point"] is False
    assert spec["meta"]["concurrency"] == "exclusive"
    assert spec["steps"]["consume"]["action"] == "resonance_pc.consume_bentos_to_floor"
    assert {"success", "status", "reason", "page_state", "consumed_count", "completed_count",
            "requires_refresh", "recovered_fatigue", "initial_fatigue", "final_fatigue",
            "items", *THRESHOLDS} <= spec["returns"].keys()
    assert "computed_fatigue" not in spec["returns"]
    assert not LEGACY_CONTROLS.intersection(spec["returns"])
    parameters = inspect.signature(resonance_pc_consume_bentos_to_floor).parameters
    assert all(parameters[key].default == DEFAULT_THRESHOLDS[key] for key in THRESHOLDS)
    assert not LEGACY_CONTROLS.intersection(parameters)
    assert {row["name"] for row in spec["meta"]["inputs"]} == {
        "eat_work_meals", "eat_love_bentos", "bento_priority", *THRESHOLDS,
    }
    assert set(spec["steps"]["consume"]["params"]) == {
        "eat_work_meals", "eat_love_bentos", "bento_priority", *THRESHOLDS,
    }


@pytest.mark.parametrize("provided", [
    {"eat_work_meals": True, "eat_love_bentos": False, "bento_priority": ["work_meals"]},
    {"eat_work_meals": False, "eat_love_bentos": True, "bento_priority": ["love_bentos"]},
    {"eat_work_meals": True, "eat_love_bentos": True,
     "bento_priority": ["love_bentos", "work_meals"]},
])
def test_child_task_parameters_validate_and_forward(provided):
    provided = {**provided, "bento_start_threshold": 500, "bento_stop_threshold": 100}
    spec = task()
    ok, validated = InputValidator(None).validate_inputs_against_meta(spec["meta"]["inputs"], provided)
    assert ok is True
    rendered = {key: NativeEnvironment().from_string(value).render(inputs=validated)
                for key, value in spec["steps"]["consume"]["params"].items()}
    assert rendered == provided


def test_standalone_defaults_match_freight_thresholds():
    spec = task()
    ok, values = InputValidator(None).validate_inputs_against_meta(spec["meta"]["inputs"], {})
    assert ok is True
    assert values["bento_priority"] == ["work_meals", "love_bentos"]
    for key in THRESHOLDS:
        assert values[key] == DEFAULT_THRESHOLDS[key]
        schema = next(row for row in spec["meta"]["inputs"] if row["name"] == key)
        assert schema["default"] == DEFAULT_THRESHOLDS[key] and schema["nullable"] is True
        assert schema["strict_integer"] is True and schema["min"] == 0


@pytest.mark.parametrize("key", sorted(THRESHOLDS))
@pytest.mark.parametrize("value", [-1, True, 3.5, "300"])
def test_child_schema_rejects_non_integer_or_negative_thresholds(key, value):
    ok, _ = InputValidator(None).validate_inputs_against_meta(task()["meta"]["inputs"], {key: value})
    assert ok is False


def test_manifest_exports_shared_action_with_only_current_controls_and_task():
    manifest = yaml.safe_load((ROOT / "plans/resonance_pc/manifest.yaml").read_text(encoding="utf-8"))
    action = next(row for row in manifest["exports"]["actions"]
                  if row["name"] == "resonance_pc.consume_bentos_to_floor")
    params = {row["name"]: row for row in action["parameters"]}
    assert set(params) == {"eat_work_meals", "eat_love_bentos", "bento_priority", *THRESHOLDS}
    assert all(params[key]["default"] == DEFAULT_THRESHOLDS[key] for key in THRESHOLDS)
    assert any("bento_consumption_pc.yaml" in str(row) for row in manifest["exports"]["tasks"])


def test_items_return_is_data_not_dict_method():
    items = [{"meal": {"kind": "work_meals"}, "consumed": True}]
    result = NativeEnvironment().from_string(task()["returns"]["items"]).render(
        nodes={"consume": {"output": {"items": items}}})
    assert result == items


@pytest.mark.parametrize("fatigue", [215, 0, None])
def test_final_fatigue_return_preserves_live_value_or_unknown(fatigue):
    result = NativeEnvironment().from_string(task()["returns"]["final_fatigue"]).render(
        nodes={"consume": {"output": {"final_fatigue": fatigue}}})
    assert result is None if fatigue is None else result == fatigue
