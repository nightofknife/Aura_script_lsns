"""Freight tasks pass explicit per-resource thresholds to the shared flow."""
from pathlib import Path
import inspect

import pytest
import yaml
from jinja2.nativetypes import NativeEnvironment

from packages.aura_core.scheduler.validation import InputValidator
from packages.resonance_gui.logic import recovery_snapshot_task_input
from plans.resonance_pc.src.actions.city_trade_flow_pc_actions import (
    resonance_pc_auto_cycle_trade_flow,
    resonance_pc_finish_auto_cycle_trade,
)
from plans.resonance_pc.src.actions import combined_commerce_pc_actions as combined
from plans.resonance_pc.src.actions._sparkling_water_policy import validate_recovery_snapshot


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_THRESHOLDS = {"sparkling_water_start_threshold": 600, "sparkling_water_stop_threshold": 100,
                      "bento_start_threshold": 600, "bento_stop_threshold": 100}
THRESHOLDS = {
    "sparkling_water_start_threshold": 600,
    "sparkling_water_stop_threshold": 200,
    "bento_start_threshold": 500,
    "bento_stop_threshold": 100,
}


def task(name):
    path = ROOT / "plans/resonance_pc/tasks" / f"{name}.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))[name]


def render_params(spec, inputs):
    env = NativeEnvironment()
    return {key: env.from_string(value).render(inputs=inputs)
            for key, value in spec["steps"]["run"]["params"].items()}


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("priority", [["work_meals"], ["love_bentos"],
                                      ["work_meals", "love_bentos"], ["love_bentos", "work_meals"]])
def test_freight_inputs_forward_thresholds_without_budget_adjustment(enabled, priority):
    spec = task("auto_cycle_trade_pc")
    ok, inputs = InputValidator(None).validate_inputs_against_meta(
        spec["meta"]["inputs"],
        {"auto_sparkling_water": enabled, "auto_bento": enabled,
         "bento_priority": priority, "fatigue_budget": 900, **THRESHOLDS},
    )
    assert ok
    rendered = render_params(spec, inputs)
    assert rendered["auto_sparkling_water"] is enabled
    assert rendered["auto_bento"] is enabled
    assert rendered["bento_priority"] == priority
    assert "recovery_priority" not in rendered
    assert rendered["fatigue_budget"] == 900
    assert {key: rendered[key] for key in THRESHOLDS} == THRESHOLDS
    assert "base_fatigue_reserve" not in rendered
    assert "recovery_trigger_fatigue" not in rendered
    assert "bento" not in spec["steps"]
    assert {"bento_consumption", "recovery_events", "final_fatigue",
            "estimated_fatigue", "fatigue_tracking"} <= spec["returns"].keys()
    assert "bento_plan" not in spec["returns"]


def test_defaults_and_combined_schema_only_extend_freight():
    spec = task("auto_cycle_trade_pc")
    inputs = {row["name"]: row for row in spec["meta"]["inputs"]}
    assert inputs["auto_bento"]["default"] is False
    assert inputs["bento_priority"]["default"] == ["work_meals", "love_bentos"]
    flow_parameters = inspect.signature(resonance_pc_auto_cycle_trade_flow).parameters
    assert flow_parameters["auto_bento"].default is False
    combined_inputs = {row["name"]: row for row in task("auto_combined_commerce_pc")["meta"]["inputs"]}
    freight = combined_inputs["trade_inputs"]["properties"]
    passenger = combined_inputs["passenger_inputs"]["properties"]
    assert freight["auto_bento"]["default"] is False
    assert freight["bento_priority"]["default"] == ["work_meals", "love_bentos"]
    for key in THRESHOLDS:
        assert flow_parameters[key].default == DEFAULT_THRESHOLDS[key]
        for schema in (inputs[key], freight[key]):
            assert schema["default"] == DEFAULT_THRESHOLDS[key]
            assert schema["nullable"] is True
            assert schema["strict_integer"] is True
            assert schema["min"] == 0
    recovery_keys = {"auto_bento", "bento_priority", *THRESHOLDS}
    assert recovery_keys <= combined._TRADE_INPUT_KEYS
    assert not recovery_keys.intersection(passenger)
    assert not recovery_keys.intersection(combined._PREVIEW_INPUT_KEYS)
    assert not recovery_keys.intersection(combined._PASSENGER_INPUT_KEYS)
    assert "base_fatigue_reserve" not in inputs
    assert "base_fatigue_reserve" not in freight
    ok, values = InputValidator(None).validate_inputs_against_meta(spec["meta"]["inputs"], {})
    assert ok is True
    assert {key: render_params(spec, values)[key] for key in THRESHOLDS} == DEFAULT_THRESHOLDS
    assert "recovery_priority" not in inputs
    assert "recovery_priority" not in freight
    assert "recovery_priority" not in flow_parameters
    assert "recovery_priority" not in combined._TRADE_INPUT_KEYS


@pytest.mark.parametrize("key", THRESHOLDS)
@pytest.mark.parametrize("value", [-1, True, 3.5, "300"])
def test_both_freight_schemas_reject_non_integer_or_negative_thresholds(key, value):
    validator = InputValidator(None)
    for name, supplied in (
        ("auto_cycle_trade_pc", {key: value}),
        ("auto_combined_commerce_pc", {"trade_inputs": {key: value}}),
    ):
        ok, _ = validator.validate_inputs_against_meta(task(name)["meta"]["inputs"], supplied)
        assert ok is False


def test_finish_only_normalizes_the_shared_flow_result():
    finish = task("auto_cycle_trade_pc")["steps"]["finish"]
    assert finish["action"] == "resonance_pc.finish_auto_cycle_trade"
    assert finish["depends_on"] == "run"
    assert set(finish["params"]) == {"trade_result"}
    result = {"success": True, "status": "completed", "recovery_events": [{"city_index": 0}]}
    rendered = NativeEnvironment().from_string(finish["params"]["trade_result"]).render(
        nodes={"run": {"output": result}},
    )
    assert rendered == result
    parameters = inspect.signature(resonance_pc_finish_auto_cycle_trade).parameters
    assert not {"bento_framework", "auto_bento", "base_fatigue_reserve"}.intersection(parameters)


def test_preview_schema_does_not_add_recovery_inputs():
    keys = {row["name"] for row in task("preview_trade_plan_pc")["meta"]["inputs"]}
    assert not {"auto_bento", "bento_priority", "recovery_priority", "recovery_snapshot", *THRESHOLDS}.intersection(keys)


@pytest.mark.parametrize(("water", "bento"), [(True, True), (True, False), (False, True)])
def test_gui_recovery_snapshot_passes_freight_input_validation(water, bento):
    recovery = {}
    if water:
        recovery["sparkling_water"] = {"remaining_free_uses": 4, "daily_free_limit": 6}
    if bento:
        recovery["bento_count"] = {"count": 9}
    player = {"status": {"fatigue": {"current": 420, "max": 856}},
              "recovery": recovery, "metadata": {"persisted": True}}
    snapshot = recovery_snapshot_task_input(player)
    inputs = {"auto_sparkling_water": water, "auto_bento": bento,
              "recovery_snapshot": snapshot, **THRESHOLDS}
    ok, validated = InputValidator(None).validate_inputs_against_meta(
        task("auto_cycle_trade_pc")["meta"]["inputs"], inputs,
    )
    assert ok is True
    assert validated["recovery_snapshot"] == snapshot
    assert ("bento_count" in validated["recovery_snapshot"]["recovery"]) is bento
    if water:
        assert validate_recovery_snapshot(snapshot)["recovery"]["sparkling_water"]["remaining_free_uses"] == 4
    else:
        with pytest.raises(ValueError, match="recovery.sparkling_water"):
            validate_recovery_snapshot(snapshot)


def test_bento_only_snapshot_is_valid_at_both_combined_input_boundaries():
    snapshot = recovery_snapshot_task_input({
        "status": {"fatigue": {"current": 420, "max": 856}},
        "recovery": {"bento_count": {"count": 9}},
        "metadata": {"persisted": True},
    })
    schema = task("auto_combined_commerce_pc")["meta"]["inputs"]
    for supplied in ({"recovery_snapshot": snapshot},
                     {"trade_inputs": {"recovery_snapshot": snapshot}}):
        ok, _ = InputValidator(None).validate_inputs_against_meta(schema, supplied)
        assert ok is True


def test_recovery_snapshot_stays_optional_and_bad_bento_counts_are_rejected():
    schema = task("auto_cycle_trade_pc")["meta"]["inputs"]
    ok, _ = InputValidator(None).validate_inputs_against_meta(
        schema, {"auto_sparkling_water": False, "auto_bento": False},
    )
    assert ok is True
    for count in (-1, 13):
        snapshot = {"status": {"fatigue": {"current": 420, "max": 856}},
                    "recovery": {"bento_count": {"count": count}},
                    "metadata": {"persisted": True}}
        ok, _ = InputValidator(None).validate_inputs_against_meta(
            schema, {"auto_bento": True, "recovery_snapshot": snapshot, **THRESHOLDS},
        )
        assert ok is False


def test_manifest_exports_thresholds_without_legacy_recovery_controls():
    manifest = yaml.safe_load((ROOT / "plans/resonance_pc/manifest.yaml").read_text(encoding="utf-8"))
    actions = {row["name"]: row for row in manifest["exports"]["actions"]}
    params = {row["name"]: row for row in actions["resonance_pc.auto_cycle_trade_flow"]["parameters"]}
    assert all(params[key]["default"] == DEFAULT_THRESHOLDS[key] for key in THRESHOLDS)
    assert "recovery_priority" not in params
    assert not {"base_fatigue_reserve", "recovery_trigger_fatigue"}.intersection(params)
    assert {row["name"] for row in actions["resonance_pc.finish_auto_cycle_trade"]["parameters"]} == {"trade_result"}


def test_recovery_order_is_not_a_task_input_or_runtime_parameter():
    freight = {row["name"] for row in task("auto_cycle_trade_pc")["meta"]["inputs"]}
    combined_inputs = {row["name"]: row for row in task("auto_combined_commerce_pc")["meta"]["inputs"]}
    assert "recovery_priority" not in freight
    assert "recovery_priority" not in combined_inputs["trade_inputs"]["properties"]
    assert "recovery_priority" not in inspect.signature(resonance_pc_auto_cycle_trade_flow).parameters
    assert "bento_priority" in freight
