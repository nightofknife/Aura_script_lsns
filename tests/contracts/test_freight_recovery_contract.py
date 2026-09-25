"""Public freight inputs route optional bentos to the standalone task."""
from pathlib import Path
import inspect

import pytest
import yaml
from jinja2.nativetypes import NativeEnvironment

from packages.aura_core.scheduler.validation import InputValidator
from packages.resonance_gui.logic import recovery_snapshot_task_input
from plans.resonance_pc.src.actions.city_trade_flow_pc_actions import resonance_pc_auto_cycle_trade_flow
from plans.resonance_pc.src.actions import combined_commerce_pc_actions as combined
from plans.resonance_pc.src.actions._sparkling_water_policy import validate_recovery_snapshot


ROOT = Path(__file__).resolve().parents[2]


def task(name):
    path = ROOT / "plans/resonance_pc/tasks" / f"{name}.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))[name]


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("priority", [["work_meals"], ["love_bentos"],
                                      ["work_meals", "love_bentos"], ["love_bentos", "work_meals"]])
def test_freight_inputs_validate_and_forward_without_budget_adjustment(enabled, priority):
    spec = task("auto_cycle_trade_pc")
    ok, inputs = InputValidator(None).validate_inputs_against_meta(
        spec["meta"]["inputs"],
        {"auto_bento": enabled, "bento_priority": priority, "fatigue_budget": 900},
    )
    assert ok
    env = NativeEnvironment()
    rendered = {key: env.from_string(value).render(inputs=inputs)
                for key, value in spec["steps"]["run"]["params"].items()}
    assert rendered["auto_bento"] is enabled
    assert rendered["bento_priority"] == priority
    assert rendered["fatigue_budget"] == 900
    assert rendered["base_fatigue_reserve"] == 200
    child = spec["steps"]["bento"]
    assert child["action"] == "aura.run_task"
    assert child["params"]["task_ref"] == "tasks:bento_consumption_pc.yaml:bento_consumption_pc"
    child_inputs = {key: env.from_string(value).render(inputs=inputs)
                    if isinstance(value, str) else value
                    for key, value in child["params"]["inputs"].items()}
    assert child_inputs == {
        "eat_work_meals": "work_meals" in priority,
        "eat_love_bentos": "love_bentos" in priority,
        "bento_priority": priority,
        "target_recovery_amount": 2000,
        "allow_exceed_target": False,
        "base_fatigue_reserve": 200,
    }
    assert "bento_consumption" in spec["returns"]
    assert "bento_plan" not in spec["returns"]
    assert spec["steps"]["finish"]["action"] == "resonance_pc.finish_auto_cycle_trade"


def test_defaults_and_combined_schema_only_extend_freight():
    spec = task("auto_cycle_trade_pc")
    inputs = {row["name"]: row for row in spec["meta"]["inputs"]}
    assert inputs["auto_bento"]["default"] is False
    assert inputs["bento_priority"]["default"] == ["work_meals", "love_bentos"]
    assert inspect.signature(resonance_pc_auto_cycle_trade_flow).parameters["auto_bento"].default is False
    combined_inputs = {row["name"]: row for row in task("auto_combined_commerce_pc")["meta"]["inputs"]}
    freight = combined_inputs["trade_inputs"]["properties"]
    passenger = combined_inputs["passenger_inputs"]["properties"]
    assert freight["auto_bento"]["default"] is False
    assert freight["bento_priority"]["default"] == ["work_meals", "love_bentos"]
    assert not {"auto_bento", "bento_priority"}.intersection(passenger)
    assert {"auto_bento", "bento_priority"} <= combined._TRADE_INPUT_KEYS
    assert not {"auto_bento", "bento_priority"}.intersection(combined._PREVIEW_INPUT_KEYS)
    assert not {"auto_bento", "bento_priority"}.intersection(combined._PASSENGER_INPUT_KEYS)


@pytest.mark.parametrize(("enabled", "status", "sale", "page", "expected"), [
    (True, "completed", True, "city_main", True),
    (False, "completed", True, "city_main", False),
    (True, "no_plan", True, "city_main", False),
    (True, "blocked", True, "city_main", False),
    (True, "completed", False, "city_main", False),
    (True, "completed", True, "unknown", False),
])
def test_bento_child_runs_only_after_confirmed_final_sale(enabled, status, sale, page, expected):
    expression = task("auto_cycle_trade_pc")["steps"]["bento"]["when"]
    nodes = {"run": {"output": {"success": status == "completed", "status": status,
                                "final_sale": {"success": sale}, "page_state": page}}}
    actual = NativeEnvironment().from_string(expression).render(
        inputs={"auto_bento": enabled}, nodes=nodes,
    )
    assert actual is expected


def test_preview_schema_does_not_add_recovery_inputs():
    spec = task("preview_trade_plan_pc")
    keys = {row["name"] for row in spec["meta"]["inputs"]}
    assert not {"auto_bento", "bento_priority", "recovery_snapshot"}.intersection(keys)


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
              "recovery_snapshot": snapshot}
    validator = InputValidator(None)
    ok, validated = validator.validate_inputs_against_meta(
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
            schema, {"auto_bento": True, "recovery_snapshot": snapshot},
        )
        assert ok is False
