"""Public freight inputs retain the explicit-meals child boundary."""
from pathlib import Path
import inspect

import pytest
import yaml
from jinja2.nativetypes import NativeEnvironment

from packages.aura_core.scheduler.validation import InputValidator
from plans.resonance_pc.src.actions.city_trade_flow_pc_actions import resonance_pc_auto_cycle_trade_flow
from plans.resonance_pc.src.actions import combined_commerce_pc_actions as combined


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
    assert {"bento_plan", "bento_consumption"} <= spec["returns"].keys()


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


def test_preview_schema_does_not_add_recovery_inputs():
    spec = task("preview_trade_plan_pc")
    keys = {row["name"] for row in spec["meta"]["inputs"]}
    assert not {"auto_bento", "bento_priority", "recovery_snapshot"}.intersection(keys)
