from __future__ import annotations

import inspect
from pathlib import Path

import pytest
import yaml

from packages.aura_core.scheduler.validation import InputValidator
from plans.resonance_pc.src.actions import city_trade_flow_pc_actions
from plans.resonance_pc.src.actions import combined_commerce_pc_actions
from plans.resonance_pc.src.actions import trade_planner_pc_actions
from plans.resonance_pc.src.services.resonance_pc_trade_planner_service import (
    DEFAULT_BOOK_PROFIT_THRESHOLD,
    ResonancePcTradePlannerService,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
TASK_ROOT = REPO_ROOT / "plans" / "resonance_pc" / "tasks"
MANIFEST_PATH = REPO_ROOT / "plans" / "resonance_pc" / "manifest.yaml"
AUTO_BOOK_RESULT_FIELDS = {
    "auto_book",
    "book_budget_ignored",
    "book_profit_threshold",
    "book_incremental_profit",
    "book_incremental_profit_exact",
    "average_book_profit",
    "average_book_profit_exact",
}


def _load_task(filename: str, task_name: str) -> dict:
    payload = yaml.safe_load((TASK_ROOT / filename).read_text(encoding="utf-8"))
    return payload[task_name]


def test_auto_book_is_exposed_by_both_trade_tasks() -> None:
    cases = (
        ("auto_cycle_trade_pc.yaml", "auto_cycle_trade_pc"),
        ("preview_trade_plan_pc.yaml", "preview_trade_plan_pc"),
    )

    for filename, task_name in cases:
        task = _load_task(filename, task_name)
        inputs = {item["name"]: item for item in task["meta"]["inputs"]}
        run_params = task["steps"]["run"]["params"]

        assert inputs["auto_book"] == {
            "name": "auto_book",
            "type": "boolean",
            "required": False,
            "default": False,
        }
        assert inputs["book_budget"]["default"] == 0
        assert inputs["book_profit_threshold"]["default"] == 500000
        assert run_params["auto_book"] == "{{ inputs.auto_book | default(false) }}"
        assert run_params["book_budget"] == "{{ inputs.book_budget | default(0) }}"
        assert run_params["book_profit_threshold"] == (
            "{{ inputs.book_profit_threshold | default(500000) }}"
        )
        assert AUTO_BOOK_RESULT_FIELDS <= set(task["returns"])


@pytest.mark.parametrize(
    ("provided", "expected_auto_book", "expected_book_budget"),
    (
        ({}, False, 0),
        ({"auto_book": True}, True, 0),
        ({"auto_book": True, "book_budget": 7}, True, 7),
    ),
)
def test_task_validation_keeps_book_budget_compatibility(
    provided: dict,
    expected_auto_book: bool,
    expected_book_budget: int,
) -> None:
    task = _load_task("preview_trade_plan_pc.yaml", "preview_trade_plan_pc")
    supplied = {"start_city_id": "1", **provided}

    ok, validated = InputValidator(None).validate_inputs_against_meta(
        task["meta"]["inputs"],
        supplied,
    )

    assert ok is True
    assert isinstance(validated, dict)
    assert validated["auto_book"] is expected_auto_book
    assert validated["book_budget"] == expected_book_budget


def test_auto_book_is_public_across_planner_and_flow_signatures() -> None:
    callables = (
        ResonancePcTradePlannerService.plan_optimal_route,
        trade_planner_pc_actions.resonance_pc_trade_plan_optimal_route,
        city_trade_flow_pc_actions._preview_trade_plan_from_start_city,
        city_trade_flow_pc_actions.resonance_pc_preview_trade_plan_flow,
        city_trade_flow_pc_actions.resonance_pc_auto_cycle_trade_flow,
    )

    assert DEFAULT_BOOK_PROFIT_THRESHOLD == 500000
    for callable_ in callables:
        parameters = inspect.signature(callable_).parameters
        assert parameters["auto_book"].default is False
        assert parameters["book_profit_threshold"].default == 500000


def test_generated_manifest_exports_auto_book_and_literal_threshold_defaults() -> None:
    manifest = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    actions = {
        action["name"]: action
        for action in manifest["exports"]["actions"]
    }
    for action_name in (
        "resonance_pc.trade_plan_optimal_route",
        "resonance_pc.preview_trade_plan_flow",
        "resonance_pc.auto_cycle_trade_flow",
    ):
        parameters = {
            parameter["name"]: parameter
            for parameter in actions[action_name]["parameters"]
        }
        assert parameters["auto_book"]["required"] is False
        assert parameters["auto_book"]["default"] is False
        assert parameters["book_profit_threshold"]["required"] is False
        assert parameters["book_profit_threshold"]["default"] == 500000


def test_combined_commerce_allows_auto_book_for_preview_and_execution() -> None:
    assert "auto_book" in combined_commerce_pc_actions._TRADE_INPUT_KEYS
    assert "auto_book" in combined_commerce_pc_actions._PREVIEW_INPUT_KEYS
