"""Black Moon purchase maps reach freight execution, never route preview."""

from pathlib import Path

import pytest
import yaml
from jinja2.nativetypes import NativeEnvironment

from packages.aura_core.scheduler.validation import InputValidator
from plans.resonance_pc.src.actions import combined_commerce_pc_actions as combined
from plans.resonance_pc.src.actions.black_moon_local_select_pc_actions import normalize_purchase_table
from tools.black_moon_purchase_schema import purchase_table_schema, render_purchase_table_input


ROOT = Path(__file__).resolve().parents[2]
FIELDS = {
    "auto_black_moon_local_purchase",
    "black_moon_purchase_table",
    "black_moon_purchase_record_scope",
}


def task(name):
    return yaml.safe_load(
        (ROOT / "plans/resonance_pc/tasks" / f"{name}.yaml").read_text(encoding="utf-8")
    )[name]


def freight_schema(combined_task=False):
    if combined_task:
        fields = task("auto_combined_commerce_pc")["meta"]["inputs"]
        trade = next(row for row in fields if row["name"] == "trade_inputs")
        return trade["properties"]["black_moon_purchase_table"]
    return next(
        row for row in task("auto_cycle_trade_pc")["meta"]["inputs"]
        if row["name"] == "black_moon_purchase_table"
    )


@pytest.mark.parametrize("combined_task", [False, True])
def test_catalog_generated_complete_false_schema(combined_task):
    schema = freight_schema(combined_task)
    assert schema == purchase_table_schema("black_moon_purchase_table")
    assert len(schema["properties"]) == 140
    assert len(schema["default"]) == 140
    assert set(schema["default"].values()) == {False}


@pytest.mark.parametrize("combined_task", [False, True])
def test_known_partial_table_expands_but_unknown_id_rejected(combined_task):
    schema = freight_schema(combined_task)
    validator = InputValidator(None)
    ok, values, error = validator.validate_input_value(
        schema, {"purchase_order_book": True}, "black_moon_purchase_table"
    )
    assert ok, error
    assert len(values) == 140
    assert values["purchase_order_book"] is True
    assert sum(values.values()) == 1
    assert not validator.validate_input_value(
        schema, {"not_in_catalog": True}, "black_moon_purchase_table"
    )[0]


@pytest.mark.parametrize("mode", ["profit", "quick", "fixed", "target"])
def test_freight_map_forwarding_all_modes(mode):
    spec = task("auto_cycle_trade_pc")
    ok, inputs = InputValidator(None).validate_inputs_against_meta(
        spec["meta"]["inputs"],
        {
            "trade_mode": mode, "auto_black_moon_local_purchase": True,
            "black_moon_purchase_table": {"purchase_order_book": True},
            "black_moon_purchase_record_scope": "account_test",
        },
    )
    assert ok, inputs
    env = NativeEnvironment()
    params = spec["steps"]["run"]["params"]
    rendered = {key: env.from_string(params[key]).render(inputs=inputs) for key in FIELDS}
    assert rendered["auto_black_moon_local_purchase"] is True
    assert rendered["black_moon_purchase_table"] == inputs["black_moon_purchase_table"]
    assert rendered["black_moon_purchase_record_scope"] == "account_test"
    assert "black_moon_local_purchase_items" not in params


def test_combined_execution_whitelist_copies_map_and_excludes_preview():
    spec = task("auto_combined_commerce_pc")
    ok, values = InputValidator(None).validate_inputs_against_meta(
        spec["meta"]["inputs"],
        {"trade_inputs": {
            "auto_black_moon_local_purchase": True,
            "black_moon_purchase_table": {"purchase_order_book": True},
            "black_moon_purchase_record_scope": "account_test",
        }},
    )
    assert ok, values
    source = values["trade_inputs"]
    execution = combined._filtered(source, combined._TRADE_INPUT_KEYS)
    assert FIELDS <= execution.keys()
    assert execution["black_moon_purchase_table"]["purchase_order_book"] is True
    assert execution["black_moon_purchase_table"] is not source["black_moon_purchase_table"]
    assert not FIELDS.intersection(combined._filtered(execution, combined._PREVIEW_INPUT_KEYS))
    assert not FIELDS.intersection(combined._PASSENGER_INPUT_KEYS)
    assert "black_moon_local_purchase_items" not in combined._TRADE_INPUT_KEYS


def test_legacy_list_is_rejected_in_both_public_schemas():
    validator = InputValidator(None)
    assert not validator.validate_inputs_against_meta(
        task("auto_cycle_trade_pc")["meta"]["inputs"],
        {"black_moon_local_purchase_items": ["purchase_order_book"]},
    )[0]
    assert not validator.validate_inputs_against_meta(
        task("auto_combined_commerce_pc")["meta"]["inputs"],
        {"trade_inputs": {"black_moon_local_purchase_items": ["purchase_order_book"]}},
    )[0]


@pytest.mark.parametrize("value", [1, 0, "true", "false", [], None])
def test_raw_action_map_requires_genuine_boolean(value):
    # The scheduler uses standard boolean coercion; raw action calls remain strict.
    with pytest.raises(ValueError):
        normalize_purchase_table({"purchase_order_book": value})


@pytest.mark.parametrize("list_item", [False, True])
def test_generator_renders_reusable_input_snippet(list_item):
    rendered = render_purchase_table_input(indent=0, list_item=list_item)
    parsed = yaml.safe_load(rendered)
    schema = parsed[0] if list_item else parsed["purchase_table"]
    assert schema == purchase_table_schema()


def test_full_purchase_child_shares_catalog_schema_and_expands_false_defaults():
    spec = task("black_moon_local_purchase_pc")
    schema = next(row for row in spec["meta"]["inputs"] if row["name"] == "purchase_table")
    assert schema == purchase_table_schema(required=True)
    ok, values = InputValidator(None).validate_inputs_against_meta(
        spec["meta"]["inputs"],
        {"city_name": "gronru_city", "purchase_table": {}},
    )
    assert ok, values
    assert len(values["purchase_table"]) == 140
    assert set(values["purchase_table"].values()) == {False}
    assert not InputValidator(None).validate_inputs_against_meta(
        spec["meta"]["inputs"],
        {"city_name": "gronru_city", "purchase_table": {"unknown": True}},
    )[0]
