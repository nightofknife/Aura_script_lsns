"""Canonical freight book budgets and generated registration; no GUI or live input."""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest
import yaml

from packages.aura_core.scheduler.validation import InputValidator
from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade
from plans.resonance_pc.src.actions import combined_commerce_pc_actions as combined
from plans.resonance_pc.src.actions import trade_planner_pc_actions as planner
from plans.resonance_pc.src.services.resonance_pc_trade_planner_service import ResonancePcTradePlannerService

ROOT = Path(__file__).resolve().parents[2]
TASK_ROOT = ROOT / "plans/resonance_pc/tasks"


def task(name):
    return yaml.safe_load((TASK_ROOT / f"{name}.yaml").read_text("utf-8"))[name]


@pytest.mark.parametrize("name", ["auto_cycle_trade_pc", "preview_trade_plan_pc"])
def test_canonical_budget_contract_is_exposed_by_both_tasks(name):
    spec = task(name)
    inputs = {row["name"]: row for row in spec["meta"]["inputs"]}
    assert inputs["book_budget"]["nullable"] is True
    assert inputs["book_budget"]["default"] == 0
    assert inputs["book_profit_threshold"]["default"] == 500000
    assert inputs["trade_mode"]["enum"] == ["profit", "quick", "fixed", "target"]
    assert "auto_book" not in inputs and "auto_book" not in spec["returns"]
    assert {"book_budget", "book_budget_unlimited", "book_profit_threshold",
            "book_incremental_profit", "average_book_profit", "planning_status",
            "city_visits", "reposition"} <= spec["returns"].keys()


@pytest.mark.parametrize("budget", [0, 7, None])
def test_schema_keeps_explicit_budget(budget):
    ok, inputs = InputValidator(None).validate_inputs_against_meta(
        task("preview_trade_plan_pc")["meta"]["inputs"], {"start_city_id": "1", "book_budget": budget})
    assert ok and inputs["book_budget"] == budget


def test_old_auto_book_is_not_an_accepted_task_field():
    ok, _ = InputValidator(None).validate_inputs_against_meta(
        task("preview_trade_plan_pc")["meta"]["inputs"], {"start_city_id": "1", "auto_book": True})
    assert not ok


def test_public_signatures_match_modes_and_default_baseline():
    for callable_ in (ResonancePcTradePlannerService.plan_optimal_route,
                      planner.resonance_pc_trade_plan_optimal_route,
                      trade._preview_trade_plan_from_start_city,
                      trade.resonance_pc_preview_trade_plan_flow,
                      trade.resonance_pc_auto_cycle_trade_flow):
        params = inspect.signature(callable_).parameters
        assert params["trade_mode"].default == "profit"
        assert params["book_budget"].default == 0
        assert params["book_profit_threshold"].default == 500000
        assert params["fatigue_budget"].default == 700 and params["cargo_capacity"].default == 750
        assert "auto_book" not in params and "fixed_route_repeat_count" not in params


def test_generated_registration_has_new_inputs_not_old_aliases():
    manifest = yaml.safe_load((ROOT / "plans/resonance_pc/manifest.yaml").read_text("utf-8"))
    actions = {row["name"]: row for row in manifest["exports"]["actions"]}
    for name in ("resonance_pc.trade_plan_optimal_route", "resonance_pc.preview_trade_plan_flow",
                 "resonance_pc.auto_cycle_trade_flow"):
        params = {row["name"]: row for row in actions[name]["parameters"]}
        assert "auto_book" not in params
        assert params["trade_mode"]["default"] == "profit"
        assert params["book_profit_threshold"]["default"] == 500000
        assert params["fatigue_budget"]["default"] == 700 and params["cargo_capacity"]["default"] == 750


@pytest.mark.parametrize("order", ["trade_first", "passenger_first"])
@pytest.mark.parametrize("budget", [0, 6, None])
def test_retained_combined_task_forwards_canonical_budget_and_recovery(order, budget):
    assert "auto_book" not in combined._TRADE_INPUT_KEYS
    assert {"trade_mode", "book_budget"} <= combined._PREVIEW_INPUT_KEYS
    snapshot = {"status": {"fatigue": {"current": 101, "max": 856}},
                "recovery": {"sparkling_water": {"remaining_free_uses": 6, "daily_free_limit": 6}},
                "metadata": {"persisted": True}}
    ok, inputs = InputValidator(None).validate_inputs_against_meta(
        task("auto_combined_commerce_pc")["meta"]["inputs"],
        {"order": order, "total_fatigue_budget": 700,
         "trade_inputs": {"book_budget": budget, "trade_mode": "profit",
                          "auto_sparkling_water": True, "required_end_city_ids": ["15"]},
         "passenger_inputs": {"passenger_city_a_id": "11", "passenger_city_b_id": "15", "trip_count": 1},
         "recovery_snapshot": snapshot})
    assert ok, inputs
    assert inputs["trade_inputs"]["book_budget"] == budget
    assert inputs["trade_inputs"]["auto_sparkling_water"] is True
    assert inputs["recovery_snapshot"] == snapshot
