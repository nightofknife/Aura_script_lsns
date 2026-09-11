"""Offline contracts for combined freight recovery snapshot handoffs."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from plans.resonance_pc.src.actions import combined_commerce_pc_actions as combined
from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade_flow


@pytest.fixture
def recovery_snapshot():
    return {
        "status": {"fatigue": {"current": 120, "max": 800}, "clarity": {"current": 80}},
        "recovery": {
            "sparkling_water": {"remaining_free_uses": 3, "daily_free_limit": 6},
            "work_meals": {"available_count": 2, "slots": [{"count": 2}]},
            "love_bentos": {"count": 0, "items": []},
        },
        "metadata": {"persisted": True, "profile_section_updated_at": {"fatigue": "now"}},
        "future_field": {"keep": [1, 2]},
    }


@pytest.fixture
def harness(monkeypatch):
    calls = {"trade": [], "passenger": [], "preview": []}
    passenger_result = {
        "success": True, "status": "completed", "requested_trips": 1,
        "completed_trips": 1, "loaded_destination": None, "page_state": "city_main",
        "requires_manual_completion": False, "end_city": {"city_id": "15"},
        "expected_fatigue_used": 27,
    }
    monkeypatch.setattr(combined, "_build_passenger_route", lambda **kwargs: {
        "city_a_id": "11", "city_b_id": "15", "trip_fatigue": 20,
    })
    monkeypatch.setattr(combined, "_read_current_city", lambda *args: {"city_id": "11"})
    monkeypatch.setattr(combined, "_passenger_forecast", lambda **kwargs: {
        "expected_fatigue": 27, "route_fatigue": 20, "reposition_fatigue": 7,
        "end_city": {"city_id": "15"},
    })

    async def preview(**kwargs):
        calls["preview"].append(kwargs)
        return {"status": "ok", "route": [{"to_city_id": "11"}]}

    async def passenger(**kwargs):
        calls["passenger"].append(kwargs)
        return deepcopy(passenger_result)

    async def trade(**kwargs):
        calls["trade"].append(kwargs)
        return {
            "success": True, "status": "completed", "route": [{"to_city_id": "11"}],
            "execution": {"completed_leg_count": 1},
            "final_sale": {"success": True, "page_state": "city_main"},
            "page_state": "city_main", "expected_fatigue_used": 40,
        }

    monkeypatch.setattr(combined, "_preview_trade_plan_from_start_city", preview)
    monkeypatch.setattr(combined, "resonance_pc_auto_passenger_trips_flow", passenger)
    monkeypatch.setattr(combined, "resonance_pc_auto_cycle_trade_flow", trade)
    service = object()
    inputs = {
        "total_fatigue_budget": 100,
        "trade_inputs": {
            "auto_sparkling_water": True, "available_city_ids": ["11", "15"],
            "auto_cape_island_investment": False, "auto_rubbish_recycling": False,
        },
        "passenger_inputs": {"trip_count": 1, "passenger_city_a_id": "11", "passenger_city_b_id": "15"},
        **{key: service for key in (
            "app", "ocr", "vision", "resonance_pc_city_shop_data", "resonance_pc_market_data",
            "resonance_pc_trade_planner", "state_store", "event_bus", "context", "engine",
        )},
    }
    return inputs, calls, passenger_result


@pytest.mark.parametrize("order", ["trade_first", "passenger_first"])
@pytest.mark.parametrize("enabled", [False, True])
def test_combined_copies_snapshot_and_adjusts_only_completed_passenger_usage(
    harness, recovery_snapshot, order, enabled,
):
    inputs, calls, _ = harness
    inputs["trade_inputs"]["auto_sparkling_water"] = enabled
    original = deepcopy(recovery_snapshot)
    persistent_data = object()
    result = asyncio.run(combined.resonance_pc_auto_combined_commerce_flow(
        **inputs, order=order, recovery_snapshot=recovery_snapshot, persistent_data=persistent_data,
    ))
    assert result["status"] == "completed"
    assert recovery_snapshot == original
    assert len(calls["trade"]) == 1
    trade = calls["trade"][0]
    expected = deepcopy(original)
    if order == "passenger_first":
        expected["status"]["fatigue"]["current"] += 27
    assert trade["recovery_snapshot"] == expected
    assert trade["recovery_snapshot"] is not recovery_snapshot
    assert trade["persistent_data"] is persistent_data
    assert trade["auto_sparkling_water"] is enabled
    assert trade["fatigue_budget"] == (80 if order == "trade_first" else 73)
    trade["recovery_snapshot"]["recovery"]["sparkling_water"]["remaining_free_uses"] = 0
    assert recovery_snapshot == original
    for child in calls["preview"] + calls["passenger"]:
        assert "recovery_snapshot" not in child
        assert "persistent_data" not in child
        assert "auto_sparkling_water" not in child


@pytest.mark.parametrize("failure", ["failed", "incomplete", "unsettled", "mismatch"])
def test_passenger_failure_never_hands_snapshot_to_trade(harness, recovery_snapshot, failure):
    inputs, calls, passenger = harness
    original = deepcopy(recovery_snapshot)
    if failure == "failed":
        passenger.update(success=False, status="failed")
    elif failure == "incomplete":
        passenger["completed_trips"] = 0
    elif failure == "unsettled":
        passenger["loaded_destination"] = "11"
    else:
        passenger["expected_fatigue_used"] = 26
    result = asyncio.run(combined.resonance_pc_auto_combined_commerce_flow(
        **inputs, order="passenger_first", recovery_snapshot=recovery_snapshot,
    ))
    assert result["status"] == "blocked"
    assert calls["trade"] == []
    assert recovery_snapshot == original


@pytest.mark.parametrize("order", ["trade_first", "passenger_first"])
def test_no_snapshot_preserves_old_trade_and_helper_call_contract(harness, monkeypatch, order):
    inputs, calls, _ = harness
    inputs["trade_inputs"].pop("auto_sparkling_water")
    real_helper = combined._run_trade

    async def old_helper(inputs, *, app, ocr, vision, city_shop_data, market_data,
                         trade_planner, state_store, event_bus, context, engine):
        return await real_helper(
            inputs, app=app, ocr=ocr, vision=vision, city_shop_data=city_shop_data,
            market_data=market_data, trade_planner=trade_planner, state_store=state_store,
            event_bus=event_bus, context=context, engine=engine,
        )

    monkeypatch.setattr(combined, "_run_trade", old_helper)
    result = asyncio.run(combined.resonance_pc_auto_combined_commerce_flow(
        **inputs, order=order, persistent_data=object(),
    ))
    assert result["status"] == "completed"
    assert len(calls["trade"]) == 1
    assert "recovery_snapshot" not in calls["trade"][0]
    assert "persistent_data" not in calls["trade"][0]
    assert "auto_sparkling_water" not in calls["trade"][0]


def test_nested_snapshot_cannot_bypass_top_level_handoff(harness, recovery_snapshot):
    inputs, calls, _ = harness
    inputs["trade_inputs"]["recovery_snapshot"] = recovery_snapshot
    inputs["passenger_inputs"]["recovery_snapshot"] = recovery_snapshot
    inputs["passenger_inputs"]["auto_sparkling_water"] = True
    result = asyncio.run(combined.resonance_pc_auto_combined_commerce_flow(
        **inputs, order="passenger_first",
    ))
    assert result["status"] == "completed"
    for child in calls["trade"] + calls["passenger"] + calls["preview"]:
        assert "recovery_snapshot" not in child


def test_combined_yaml_snapshot_is_optional_and_runtime_only():
    task = yaml.safe_load(Path("plans/resonance_pc/tasks/auto_combined_commerce_pc.yaml").read_text(
        encoding="utf-8",
    ))["auto_combined_commerce_pc"]
    snapshot = next(row for row in task["meta"]["inputs"] if row["name"] == "recovery_snapshot")
    assert snapshot["type"] == "dict"
    assert snapshot["required"] is False
    assert task["steps"]["run"]["params"]["recovery_snapshot"] == "{{ inputs.recovery_snapshot | default(none) }}"
    assert "auto_sparkling_water" in combined._TRADE_INPUT_KEYS
    assert "auto_sparkling_water" not in combined._PREVIEW_INPUT_KEYS
    assert "recovery_snapshot" not in combined._PREVIEW_INPUT_KEYS


def test_real_auto_trade_requires_snapshot_before_ui(monkeypatch):
    monkeypatch.setattr(
        trade_flow, "resonance_pc_open_city_panel_from_main",
        lambda **kwargs: pytest.fail("missing snapshot must fail before UI"),
    )
    with pytest.raises(ValueError, match="recovery_snapshot"):
        asyncio.run(trade_flow.resonance_pc_auto_cycle_trade_flow(auto_sparkling_water=True))


def test_real_auto_trade_requires_persistence_before_ui(monkeypatch, recovery_snapshot):
    monkeypatch.setattr(
        trade_flow, "resonance_pc_open_city_panel_from_main",
        lambda **kwargs: pytest.fail("missing persistence must fail before UI"),
    )
    with pytest.raises(RuntimeError, match="persistent_data"):
        asyncio.run(trade_flow.resonance_pc_auto_cycle_trade_flow(
            auto_sparkling_water=True, recovery_snapshot=recovery_snapshot,
        ))


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("order", ["trade_first", "passenger_first"])
@pytest.mark.parametrize("auto_book", [False, True])
def test_combined_handoff_enters_real_auto_trade_signature(
    harness, monkeypatch, recovery_snapshot, enabled, order, auto_book,
):
    inputs, _, _ = harness
    inputs["trade_inputs"]["auto_sparkling_water"] = enabled
    inputs["trade_inputs"]["auto_book"] = auto_book
    inputs["trade_inputs"]["base_fatigue_reserve"] = 70
    original = deepcopy(recovery_snapshot)
    captured = {}
    route = [{
        "from_city_id": "15", "to_city_id": "11",
        "from_city_key": "key15", "to_city_key": "key11",
        "from_city": "City 15", "to_city": "City 11",
    }]

    class Market:
        def get_all_travel_fatigue(self):
            return {"cities": {"11": "City 11", "15": "City 15"}, "costs": {"15": {"11": 0}}}

    class Shops:
        def resolve_shop_point(self, city, shop):
            assert shop == "rest"
            return {"x": 1, "y": 2}

    inputs["resonance_pc_market_data"] = Market()
    inputs["resonance_pc_city_shop_data"] = Shops()
    persistent_data = object()
    monkeypatch.setattr(combined, "resonance_pc_auto_cycle_trade_flow", trade_flow.resonance_pc_auto_cycle_trade_flow)
    monkeypatch.setattr(trade_flow, "resonance_pc_open_city_panel_from_main", lambda **kwargs: None)
    monkeypatch.setattr(trade_flow, "resonance_pc_read_city_name_on_city_panel", lambda **kwargs: {
        "city_id": "15", "city_key": "key15", "city_name": "City 15",
    })
    monkeypatch.setattr(trade_flow, "resonance_pc_market_refresh", lambda **kwargs: {"snapshot_id": "offline-market"})
    planning_inputs = {}

    def plan_route(**kwargs):
        planning_inputs.update(kwargs)
        return {"status": "ok", "route": deepcopy(route), "expected_fatigue_used": 40,
                "auto_book": kwargs["auto_book"]}

    monkeypatch.setattr(trade_flow, "resonance_pc_trade_plan_optimal_route", plan_route)

    async def execute_route(**kwargs):
        captured.update(kwargs)
        return {
            "status": "completed", "completed_leg_count": 1, "completed_route": deepcopy(route),
            "leg_results": [], "page_state": "city_panel",
            "sparkling_water": {
                "triggered": enabled, "status": "completed" if enabled else "not_triggered",
                "reason": "selected" if enabled else "disabled",
            },
        }

    monkeypatch.setattr(trade_flow, "_execute_route", execute_route)
    monkeypatch.setattr(trade_flow, "_execute_city_trade_inside_current_city", lambda **kwargs: {
        "success": True, "page_state": "city_main",
    })
    if not enabled:
        monkeypatch.setattr(trade_flow, "validate_recovery_snapshot", lambda snapshot: pytest.fail("disabled flag must ignore snapshot"))
        monkeypatch.setattr(trade_flow, "select_sparkling_water_stop", lambda **kwargs: pytest.fail("disabled flag must not select recovery"))
    result = asyncio.run(combined.resonance_pc_auto_combined_commerce_flow(
        **inputs, order=order, recovery_snapshot=recovery_snapshot, persistent_data=persistent_data,
    ))
    assert result["status"] == "completed", result
    assert planning_inputs["auto_book"] is auto_book
    assert result["trade"]["auto_book"] is auto_book
    assert recovery_snapshot == original
    assert captured["persistent_data"] is persistent_data
    assert result["trade"]["sparkling_water"]["triggered"] is enabled
    plan = result["trade"]["sparkling_water_plan"]
    assert captured["sparkling_water_plan"] == plan
    assert plan["base_fatigue_reserve"] == 70
    if enabled:
        assert plan["planned"] is True
        assert plan["initial_fatigue"] == (120 if order == "trade_first" else 147)
    else:
        assert plan == {"planned": False, "reason": "disabled", "drink_count": 0,
                        "base_fatigue_reserve": 70}
