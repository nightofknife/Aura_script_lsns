"""Offline freight orchestration contracts; no game, GUI or backend execution."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import threading
from types import MethodType, SimpleNamespace

import pytest
import numpy as np

from packages.aura_core.api import ACTION_REGISTRY, ActionDefinition
from packages.aura_core.context.execution import ExecutionContext
from packages.aura_core.engine import ExecutionEngine
from packages.aura_core.scheduler import operation_scope
from packages.aura_core.scheduler.cancellation import (
    clear_task_cancel, has_pending_sync_actions,
)
from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade
from plans.resonance_pc.src.actions import combined_commerce_pc_actions as combined
from plans.resonance_pc.src.actions import _city_panel_vision as city_vision
from packages.resonance_gui.logic import TradeProgressState, reduce_trade_progress
from tests.unit.test_resonance_pc_sparkling_water_handoff import harness as combined_harness
from tests.unit.test_resonance_pc_sparkling_water_trade import (
    harness as trade_harness, routes, run_full, snapshot,
)


CALL_RECOVERY_ACTION = trade._call_recovery_action


class MemoryStore:
    def __init__(self):
        self.reads = []
        self.document = snapshot(remaining=2)

    def read(self, *, file):
        assert file == "user-info.json"
        self.reads.append(file)
        return deepcopy(self.document)


@pytest.fixture
def recovery_runtime(monkeypatch):
    calls = []
    state = {"fatigue": 300}
    context = SimpleNamespace(data={})
    store = MemoryStore()
    package = SimpleNamespace(package=SimpleNamespace(canonical_id="@test/recovery"))
    engine = SimpleNamespace(state_store=object(), services={}, orchestrator=SimpleNamespace(
        loaded_package=package, resolve_service=lambda key: store,
    ))

    engine.bind_execution = MethodType(ExecutionEngine.bind_execution, engine)

    def register(name, function, dependencies=None):
        definition = ActionDefinition(
            func=function, name=name, read_only=False, public=True,
            service_deps=dependencies or {}, plugin=package,
        )
        monkeypatch.setitem(ACTION_REGISTRY._actions_by_fqid, definition.fqid, definition)

    def refresh(stages, profile_sections, context, engine):
        assert context is runtime.context and engine is runtime.engine
        calls.append(("refresh", {"stages": stages, "profile_sections": profile_sections}))
        assert stages == ["profile"] and profile_sections == ["fatigue"]
        return deepcopy(state.get("refresh_result", {
            "status": {"fatigue": {"current": state.get("fatigue_source", {}).get("current_fatigue", state["fatigue"]), "max": 1000}},
            "metadata": {"persisted": True, "executed_profile_sections": ["fatigue"]},
        }))

    def go_main():
        calls.append(("go_main", {}))
        return state.get("main_result", {"success": True, "page_state": "city_main"})

    def open_panel(**params):
        assert params == {}
        calls.append(("open_panel", params))
        return deepcopy(state.get("panel_result", {"success": True, "page_state": "city_panel"}))

    register("resonance_pc.player_data_refresh", refresh)
    register("resonance_pc.go_city_main_direct", go_main)
    register("resonance_pc.open_city_panel_from_main", open_panel)
    runtime = SimpleNamespace(calls=calls, state=state, context=context, engine=engine,
                              store=store, register=register)
    return runtime


@pytest.fixture
def freight(trade_harness, recovery_runtime, monkeypatch):
    operations, state = trade_harness
    runtime = recovery_runtime
    runtime.state["fatigue_source"] = state
    state["local_quota"] = 2
    monkeypatch.setattr(trade, "_call_recovery_action", CALL_RECOVERY_ACTION)
    from plans.resonance_pc.src.actions._player_data_persistence import load_pc_user_info
    from packages.aura_core.engine.execution_facade import ExecutionFacade
    monkeypatch.setattr(trade, "load_pc_user_info", load_pc_user_info)
    planned = {"status": "ok", "route": routes(), "expected_profit": 123456,
               "expected_fatigue_used": 333, "remaining_expected_fatigue": 567}
    planner_calls = []

    def planner(**kwargs):
        planner_calls.append(kwargs)
        return deepcopy(planned)

    async def drink(**kwargs):
        result = await state["drink"](**kwargs)
        runtime.store.document["recovery"]["sparkling_water"]["remaining_free_uses"] = state["local_quota"]
        return result

    async def child_task(facade, task_ref, params, **kwargs):
        assert facade._engine is runtime.engine
        runtime.calls.append(("consume", {"task_ref": task_ref, **deepcopy(params)}))
        return await state["bento"](task_ref, params)

    monkeypatch.setattr(ExecutionFacade, "run_task", child_task)
    monkeypatch.setattr(trade, "resonance_pc_drink_sparkling_water_from_city_panel", drink)
    monkeypatch.setattr(trade, "resonance_pc_trade_plan_optimal_route", planner)

    def run(**kwargs):
        args = dict(auto_sparkling_water=False, auto_bento=True,
                    bento_priority=["work_meals"],
                    recovery_snapshot=None, context=runtime.context, engine=runtime.engine,
                    persistent_data=runtime.store, fatigue_budget=900)
        args.update(kwargs)
        return run_full(**args)

    return SimpleNamespace(run=run, operations=operations, state=state,
                           runtime=runtime, planned=planned, planner_calls=planner_calls)


@pytest.mark.parametrize("page", ["city_main", "city_panel"])
def test_fatigue_only_registered_action_from_confirmed_pages(recovery_runtime, page):
    runtime = recovery_runtime
    result = asyncio.run(trade._refresh_recovery_fatigue(
        page_state=page, context=runtime.context, engine=runtime.engine,
    ))
    assert result == 300
    expected = [("refresh", {"stages": ["profile"], "profile_sections": ["fatigue"]})]
    assert runtime.calls == ([("go_main", {})] if page == "city_panel" else []) + expected
    assert runtime.store.reads == []


@pytest.mark.parametrize("page", ["unknown", "exchange", ""])
def test_unknown_page_rejected_before_any_recovery_action(recovery_runtime, page):
    runtime = recovery_runtime
    with pytest.raises(Exception) as error:
        asyncio.run(trade._refresh_recovery_fatigue(
            page_state=page, context=runtime.context, engine=runtime.engine,
        ))
    assert error.value.code == "recovery_invalid_start_page"
    assert runtime.calls == []


def test_failed_main_restore_stops_before_refresh(recovery_runtime):
    runtime = recovery_runtime
    runtime.state["main_result"] = {"success": False, "page_state": "unknown"}
    with pytest.raises(Exception) as error:
        asyncio.run(trade._refresh_recovery_fatigue(
            page_state="city_panel", context=runtime.context, engine=runtime.engine,
        ))
    assert error.value.code == "recovery_main_not_restored"
    assert runtime.calls == [("go_main", {})]


@pytest.mark.parametrize("change", [
    {"metadata": {"persisted": False, "executed_profile_sections": ["fatigue"]}},
    {"metadata": {"persisted": True, "executed_profile_sections": ["fatigue", "work_meals"]}},
    {"status": {"fatigue": {"current": True, "max": 856}}},
])
def test_invalid_water_refresh_never_reads_inventory(recovery_runtime, change):
    runtime = recovery_runtime
    runtime.state["refresh_result"] = {
        "status": {"fatigue": {"current": 300, "max": 856}},
        "metadata": {"persisted": True, "executed_profile_sections": ["fatigue"]},
        **change,
    }
    with pytest.raises(Exception) as error:
        asyncio.run(trade._refresh_recovery_fatigue(
            page_state="city_main", context=runtime.context, engine=runtime.engine,
        ))
    assert error.value.code == "recovery_fatigue_invalid"
    assert [name for name, _ in runtime.calls] == ["refresh"]
    assert runtime.store.reads == []


def finish(result):
    return asyncio.run(trade.resonance_pc_finish_auto_cycle_trade(trade_result=result))


def test_bento_is_run_as_a_bound_task_before_trade_not_at_terminal_handoff(freight):
    freight.state.update(current_fatigue=701, travel_costs=[0, 0])
    result = freight.run()
    assert result["success"] is True and result["page_state"] == "city_main"
    assert freight.operations.index("bentos") < freight.operations.index("trade:0")
    assert freight.operations.count("bentos") == 1 and freight.operations.count("final_sale") == 1
    assert "bento_pending" not in result and "bento_progress_checkpoint" not in result
    child = next(values for name, values in freight.runtime.calls if name == "consume")
    assert child == {"task_ref": "tasks:bento_consumption_pc.yaml:bento_consumption_pc",
                     "eat_work_meals": True, "eat_love_bentos": False,
                     "bento_priority": ["work_meals"], "bento_start_threshold": 700,
                     "bento_stop_threshold": 200}
    assert result["bento_consumption"]["final_fatigue"] == 161
    assert result["bento_consumption"]["consumed_count"] == 9


def test_water_precedes_bento_and_bento_uses_fresh_post_water_fatigue(freight):
    freight.state.update(current_fatigue=801, travel_costs=[0, 0])
    result = freight.run(auto_sparkling_water=True, recovery_snapshot=snapshot(),
                         sparkling_water_start_threshold=700, sparkling_water_stop_threshold=500)
    assert result["success"]
    assert freight.operations.index("water:岚心城:2") < freight.operations.index("bentos")
    rows = result["recovery_events"]
    first = next(row for row in rows if row["bento_consumption"]["triggered"])
    assert first["initial_fatigue"] == 801
    assert first["sparkling_water"]["final_fatigue"] == 701
    assert first["bento_consumption"]["initial_fatigue"] == 701


def test_water_that_reaches_below_bento_start_skips_bentos(freight):
    freight.state.update(current_fatigue=701, travel_costs=[0, 0])
    result = freight.run(auto_sparkling_water=True, recovery_snapshot=snapshot(),
                         sparkling_water_start_threshold=500, sparkling_water_stop_threshold=450)
    assert result["success"] and "bentos" not in freight.operations
    assert result["sparkling_water"]["completed_count"] == 2
    assert result["final_fatigue"] == 601


def test_only_bento_threshold_met_at_arrival_does_not_wait_for_water(freight):
    freight.state.update(current_fatigue=550, travel_costs=[300, 0])
    result = freight.run(auto_sparkling_water=True, recovery_snapshot=snapshot(),
                         sparkling_water_start_threshold=900, sparkling_water_stop_threshold=100)
    assert result["success"]
    assert not any(op.startswith("water:") for op in freight.operations)
    assert freight.operations.index("travel:0") < freight.operations.index("bentos") < freight.operations.index("trade:1")
    arrival = next(row for row in result["recovery_events"] if row["phase"] == "arrival")
    assert arrival["initial_fatigue"] == 850
    assert arrival["initial_fatigue_source"] == "route_derived"
    assert arrival["bento_consumption"]["initial_fatigue"] == 850
    assert arrival["bento_consumption"]["final_fatigue"] == 190
    assert arrival["sparkling_water"]["triggered"] is False
    assert arrival["sparkling_water"]["current_fatigue"] == 850
    assert arrival["checked_order"] == ["sparkling_water", "bento"]
    assert arrival["fatigue_source"] == "bento"
    assert result["final_fatigue"] == 190
    assert freight.state["local_quota"] == 2


def test_distinct_thresholds_trigger_bento_then_water_in_a_later_city(freight):
    freight.state.update(current_fatigue=850, travel_costs=[800, 0])
    result = freight.run(auto_sparkling_water=True, recovery_snapshot=snapshot(),
                         sparkling_water_start_threshold=900, sparkling_water_stop_threshold=100)
    assert result["success"]
    operations = freight.operations
    assert operations.index("bentos") < operations.index("trade:0") < operations.index("travel:0") < operations.index("water:海角城:2")
    assert operations.count("bentos") == 2
    start = next(row for row in result["recovery_events"] if row["phase"] == "start")
    assert start["sparkling_water"]["triggered"] is False
    assert start["bento_consumption"]["final_fatigue"] == 190
    arrival = next(row for row in result["recovery_events"] if row["phase"] == "arrival")
    assert arrival["initial_fatigue"] == 990
    assert arrival["sparkling_water"]["final_fatigue"] == 890
    assert arrival["bento_consumption"]["initial_fatigue"] == 890
    assert arrival["bento_consumption"]["final_fatigue"] == 170
    assert arrival["checked_order"] == ["sparkling_water", "bento"]
    assert result["bento_consumption"]["consumed_count"] == 23
    assert result["sparkling_water"]["completed_count"] == 2
    assert result["final_fatigue"] == 170
    assert profile_refresh_count(freight) == 2


def test_water_failure_blocks_bento_and_following_trades(freight):
    freight.state.update(current_fatigue=850, water_failure=True)
    result = freight.run(auto_sparkling_water=True, recovery_snapshot=snapshot())
    assert result["success"] is False
    assert "bentos" not in freight.operations
    assert freight.operations.count("water:岚心城:2") == 1
    assert "trade:0" not in freight.operations and "travel:0" not in freight.operations
    assert result["bento_consumption"]["consumed_count"] == 0
    assert result["sparkling_water"]["completed_count"] == 0
    assert result["final_fatigue"] is None


@pytest.mark.parametrize("unavailable", ["no_rest", "zero_quota"])
def test_unavailable_water_falls_through_to_bentos_at_current_city(freight, monkeypatch, unavailable):
    freight.state.update(current_fatigue=701, travel_costs=[0, 0])
    if unavailable == "no_rest":
        monkeypatch.setattr(trade, "_has_recovery_rest_point", lambda *args: False)
    else:
        freight.state["local_quota"] = 0
        freight.runtime.store.document["recovery"]["sparkling_water"]["remaining_free_uses"] = 0
    result = freight.run(auto_sparkling_water=True, recovery_snapshot=snapshot())
    assert result["success"] and result["final_fatigue"] == 161
    assert not any(op.startswith("water:") for op in freight.operations)
    assert freight.operations.index("bentos") < freight.operations.index("trade:0")


def test_multiple_city_results_aggregate_confirmed_portions_and_measurements(freight):
    freight.state.update(current_fatigue=450, travel_costs=[200, 200])
    result = freight.run(bento_start_threshold=400, bento_stop_threshold=300)
    assert result["success"] and freight.operations.count("bentos") == 3
    assert result["bento_consumption"]["consumed_count"] == 10
    assert result["bento_consumption"]["recovered_fatigue"] == 600
    assert result["final_fatigue"] == 250
    assert len([v for v in result["bento_consumption"]["visits"] if v["triggered"]]) == 3


def profile_refresh_count(freight):
    return sum(name == "refresh" for name, _params in freight.runtime.calls)


def test_trade_observations_and_route_costs_avoid_reopening_profile_at_each_checkpoint(freight):
    freight.state.update(current_fatigue=100, travel_costs=[100, 100])
    result = freight.run()
    assert result["success"] and result["final_fatigue"] == 300
    assert profile_refresh_count(freight) == 1
    arrivals = [row for row in result["recovery_events"] if row["phase"] == "arrival"]
    assert [row["initial_fatigue"] for row in arrivals] == [200, 300]
    assert freight.operations.count("final_sale") == 1


@pytest.mark.parametrize("index", [0, 1, 2], ids=["first-trade", "middle-trade", "final-sale"])
def test_failed_trade_page_reading_falls_back_before_recovery_decision(freight, index):
    freight.state.update(current_fatigue=100, travel_costs=[100, 100],
                         trade_observations={index: {
        "success": False, "current_fatigue": None, "max_fatigue": None,
        "source": "trade_page", "reason": "fatigue_unrecognized"}})
    result = freight.run()
    assert result["success"] and result["final_fatigue"] == 300
    assert profile_refresh_count(freight) == 2
    assert freight.operations.count("final_sale") == 1


@pytest.mark.parametrize("cost", [None, True, -1, RuntimeError("missing route cost")])
def test_missing_or_invalid_route_cost_forces_arrival_profile_calibration(freight, cost):
    freight.state.update(current_fatigue=100, travel_costs=[150, 0], known_travel_costs=[cost, 0])
    result = freight.run()
    assert result["success"] and result["final_fatigue"] == 250
    assert profile_refresh_count(freight) == 2
    arrival = next(row for row in result["recovery_events"] if row["phase"] == "arrival")
    assert arrival["initial_fatigue"] == 250


@pytest.mark.parametrize("special", [
    {"fatigue_medicine_use_count": 1}, {"travel_method": "trailer"}, {"encounter_actions": 1},
])
def test_special_travel_forces_actual_calibration_at_confirmed_arrival(freight, special):
    freight.state.update(current_fatigue=100, travel_costs=[100, 100], travel_results={0: special})
    result = freight.run()
    assert result["success"] and result["final_fatigue"] == 300
    assert profile_refresh_count(freight) == 2


def test_trade_reading_includes_negotiation_without_adding_it_twice(freight):
    freight.state.update(current_fatigue=100, trade_costs=[24, 16, 8], travel_costs=[50, 70])
    result = freight.run()
    assert result["success"] and result["final_fatigue"] == 268
    assert profile_refresh_count(freight) == 1
    checkpoints = [(row["phase"], row["initial_fatigue"]) for row in result["recovery_events"]]
    assert checkpoints == [("start", 100), ("pre_departure", 124), ("arrival", 174),
                           ("pre_departure", 190), ("arrival", 260), ("final_sale", 268)]


def test_next_trade_reading_replaces_route_estimate_before_next_leg(freight):
    # A stale road dataset overstates the first leg; a trade observation repairs it.
    freight.state.update(current_fatigue=100, travel_costs=[100, 50], known_travel_costs=[120, 50])
    result = freight.run()
    assert result["success"] and result["final_fatigue"] == 250
    checkpoints = [(row["phase"], row["initial_fatigue"]) for row in result["recovery_events"]]
    assert ("arrival", 220) in checkpoints and ("pre_departure", 200) in checkpoints
    assert checkpoints[-2:] == [("arrival", 250), ("final_sale", 250)]
    assert profile_refresh_count(freight) == 1


def test_post_water_actual_reading_becomes_new_route_baseline(freight):
    freight.state.update(current_fatigue=701, travel_costs=[49, 50])
    result = freight.run(auto_sparkling_water=True, recovery_snapshot=snapshot(),
                         sparkling_water_start_threshold=700, sparkling_water_stop_threshold=450)
    assert result["success"] and result["final_fatigue"] == 700
    # Two free cups leave 601; the second arrival at 700 only meets the bento start.
    assert result["sparkling_water"]["completed_count"] == 2
    assert "bentos" not in freight.operations
    assert profile_refresh_count(freight) == 2


def test_post_bento_actual_reading_becomes_new_route_baseline(freight):
    freight.state.update(current_fatigue=701, travel_costs=[50, 50])
    result = freight.run()
    assert result["success"] and result["final_fatigue"] == 261
    assert freight.operations.count("bentos") == 1
    assert profile_refresh_count(freight) == 1


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_partial_child_failure_is_retained_without_retry_or_next_trade(freight, status):
    freight.state.update(current_fatigue=701, bento_result={
        "success": False, "status": status, "reason": "bento_cancelled" if status == "cancelled" else "rating_failed",
        "page_state": "unknown", "consumed_count": 1, "completed_count": 0,
        "work_meals_used": 1, "final_fatigue": None, "recovered_fatigue": None,
        "items": [{"consumed": True, "completed": False, "phase": "rating_pending"}],
    })
    result = freight.run()
    assert result["success"] is False
    assert result["bento_consumption"]["consumed_count"] == 1
    assert result["bento_consumption"]["completed_count"] == 0
    assert result["final_fatigue"] is None
    assert freight.operations.count("bentos") == 1
    assert "trade:0" not in freight.operations and "final_sale" not in freight.operations


@pytest.mark.parametrize("change", [
    {"page_state": "city_panel"},
    {"final_fatigue": None},
    {"consumed_count": True},
    {"bento_start_threshold": 999},
])
def test_unconfirmed_or_invalid_bento_result_stops_route(freight, change):
    freight.state.update(current_fatigue=701, bento_result=change)
    result = freight.run()
    assert result["success"] is False
    assert freight.operations.count("bentos") == 1 and "trade:0" not in freight.operations


def test_finish_only_passes_through_already_aggregated_business_results(freight):
    freight.state.update(current_fatigue=701, travel_costs=[0, 0])
    core = freight.run()
    result = finish(core)
    assert result == core
    assert freight.operations.count("bentos") == 1


def test_no_available_bentos_above_stop_is_a_resource_skip_not_a_false_recovery(freight):
    freight.state.update(current_fatigue=701, travel_costs=[0, 0], bento_result={
        "status": "skipped", "reason": "no_available_meals", "consumed_count": 0,
        "completed_count": 0, "work_meals_used": 0, "recovered_fatigue": 0,
        "final_fatigue": 701, "items": [],
    })
    result = freight.run()
    assert result["success"] and result["status"] == "completed"
    assert result["bento_consumption"]["consumed_count"] == 0
    assert result["final_fatigue"] == 701
    assert "final_sale" in freight.operations
    visits = [row for row in result["bento_consumption"]["visits"] if row["triggered"]]
    assert visits and all(row["reason"] == "no_available_meals" for row in visits)


def test_fresh_bento_entry_can_skip_after_observing_fatigue_below_start(freight):
    freight.state.update(current_fatigue=701, travel_costs=[0, 0], bento_result={
        "status": "skipped", "reason": "start_threshold_not_exceeded", "consumed_count": 0,
        "completed_count": 0, "work_meals_used": 0, "initial_fatigue": 699,
        "final_fatigue": 699, "recovered_fatigue": 0, "items": [],
    })
    result = freight.run()
    assert result["success"] and result["final_fatigue"] == 699
    assert freight.operations.count("bentos") == 1
    assert result["bento_consumption"]["consumed_count"] == 0


def test_final_sale_failure_does_not_run_terminal_recovery(freight):
    freight.state.update(current_fatigue=100, final_sale_failure=True)
    result = freight.run()
    assert result["success"] is False
    assert "bento_pending" not in result and "bentos" not in freight.operations
    assert not any(row["phase"] == "final_sale" for row in result["recovery_events"])


def test_post_sale_bento_failure_keeps_confirmed_sale_and_unknown_final_fatigue(freight):
    freight.state.update(current_fatigue=100, travel_costs=[0, 0], trade_costs=[0, 0, 650],
                         bento_result={
        "success": False, "status": "failed", "reason": "rating_failed", "page_state": "unknown",
        "consumed_count": 1, "completed_count": 0, "work_meals_used": 1,
        "final_fatigue": None, "recovered_fatigue": None,
        "items": [{"consumed": True, "completed": False, "phase": "rating_pending"}],
    })
    result = freight.run()
    assert result["success"] is False and result["status"] == "failed"
    assert result["reason"] == "rating_failed"
    assert result["final_sale"]["success"] is True
    assert result["final_sale"]["page_state"] == "city_main"
    assert result["bento_consumption"]["consumed_count"] == 1
    assert result["bento_consumption"]["completed_count"] == 0
    assert result["bento_consumption"]["recovered_fatigue"] is None
    assert result["final_fatigue"] is None and result["resources"]["actual_fatigue"] is None
    assert freight.operations.count("final_sale") == freight.operations.count("bentos") == 1
    assert freight.operations.index("final_sale") < freight.operations.index("bentos")
    event = result["recovery_events"][-1]
    assert event["phase"] == "final_sale" and event["initial_fatigue"] == 750
    assert event["bento_consumption"]["items"][0]["phase"] == "rating_pending"


def test_arrival_recovery_failure_keeps_confirmed_leg_without_next_trade(freight):
    freight.state.update(current_fatigue=100, travel_costs=[650, 0], bento_result={
        "success": False, "status": "failed", "reason": "bento_fatigue_unrecognized",
        "page_state": "bento_cabinet", "consumed_count": 1, "completed_count": 1,
        "work_meals_used": 1, "final_fatigue": None, "recovered_fatigue": None,
        "items": [{"consumed": True, "completed": True, "phase": "completed"}],
    })
    result = freight.run()
    assert result["success"] is False and result["reason"] == "bento_fatigue_unrecognized"
    execution = result["execution"]
    assert execution["completed_leg_count"] == 1 and freight.state["completed"] == 1
    assert len(execution["leg_results"]) == 1
    leg = execution["leg_results"][0]
    assert leg["travel"]["success"] is True and leg["travel"]["status"] == "ok"
    assert leg["city_trade"]["success"] is True
    assert result["bento_consumption"]["consumed_count"] == 1
    assert result["final_fatigue"] is None
    assert freight.operations.count("bentos") == 1
    assert "trade:1" not in freight.operations and "travel:1" not in freight.operations
    assert "final_sale" not in freight.operations


def test_trade_progress_finishes_once_with_monotonic_sequence(freight):
    class Events:
        def __init__(self):
            self.rows = []

        async def publish(self, event):
            self.rows.append(event.to_dict())

    bus = Events()
    context = ExecutionContext(cid="freight-bento-progress")
    freight.runtime.context = context
    freight.state.update(current_fatigue=701, travel_costs=[0, 0])
    core = freight.run(event_bus=bus)
    assert core["success"]
    payloads = [row["payload"] for row in bus.rows]
    assert any(row["stage"] == "bento" and row["state"] == "started" for row in payloads)
    assert [(row["stage"], row["state"]) for row in payloads].count(("task", "completed")) == 1
    assert payloads[-1]["stage"] == "task" and payloads[-1]["state"] == "completed"
    sequences = [row["sequence"] for row in payloads]
    assert sequences == list(range(1, len(sequences) + 1))
    state = TradeProgressState(cid="freight-bento-progress")
    for row in bus.rows:
        state = reduce_trade_progress(state, row, expected_cid=context.data["cid"])
    assert state.state == "completed"


@pytest.mark.parametrize("water,bento", [(False, False), (False, True), (True, False), (True, True)])
def test_recovery_does_not_refill_planning_budget_or_change_profit(freight, water, bento):
    freight.state["current_fatigue"] = 701
    original = deepcopy(freight.planned)
    result = freight.run(auto_sparkling_water=water, auto_bento=bento,
                         recovery_snapshot=snapshot() if water else None)
    assert result["status"] == "completed"
    assert len(freight.planner_calls) == 1 and freight.planner_calls[0]["fatigue_budget"] == 900
    for key in ("route", "expected_profit", "expected_fatigue_used", "remaining_expected_fatigue"):
        assert result[key] == original[key]
    assert freight.planned == original
    if not water and not bento:
        assert freight.runtime.calls == [] and freight.runtime.store.reads == []


def test_no_route_never_refreshes_or_consumes(freight):
    freight.planned.update(status="no_plan", route=[])
    result = freight.run(auto_sparkling_water=True, recovery_snapshot=snapshot())
    assert result["success"] is False
    assert freight.runtime.calls == [] and freight.runtime.store.reads == []
    assert "final_sale" not in freight.operations and "bentos" not in freight.operations


def test_blocked_arrival_does_not_start_recovery_at_failed_destination(freight):
    freight.state.update(current_fatigue=100, block_index=0)
    result = freight.run(auto_sparkling_water=True, recovery_snapshot=snapshot())
    assert result["status"] == "blocked"
    assert not any(row["city_index"] == 1 for row in result["recovery_events"])
    assert "final_sale" not in freight.operations and "bentos" not in freight.operations


def test_failed_arrival_blocks_terminal_work(freight):
    freight.state.update(current_fatigue=100, fail_index=0)
    with pytest.raises(Exception) as error:
        freight.run(auto_sparkling_water=True, recovery_snapshot=snapshot())
    assert error.value.code == "trade_arrival_not_confirmed"
    assert "final_sale" not in freight.operations and "bentos" not in freight.operations


@pytest.mark.parametrize("order", ["trade_first", "passenger_first"])
def test_combined_auto_bento_is_rejected_before_any_game_action(
    freight, combined_harness, monkeypatch, order,
):
    inputs, calls, _ = combined_harness
    inputs.update(context=freight.runtime.context, engine=freight.runtime.engine,
                  total_fatigue_budget=900)
    inputs["trade_inputs"].update(auto_sparkling_water=True, auto_bento=True,
                                  bento_priority=["work_meals"], bento_start_threshold=700, bento_stop_threshold=200)
    from tests.unit.test_resonance_pc_sparkling_water_trade import Market
    from plans.resonance_pc.src.services.city_shop_data_pc_service import ResonancePcCityShopDataService
    inputs["resonance_pc_market_data"] = Market()
    inputs["resonance_pc_city_shop_data"] = ResonancePcCityShopDataService()
    monkeypatch.setattr(combined, "resonance_pc_auto_cycle_trade_flow",
                        trade.resonance_pc_auto_cycle_trade_flow)
    result = asyncio.run(combined.resonance_pc_auto_combined_commerce_flow(
        **inputs, order=order, persistent_data=freight.runtime.store,
        recovery_snapshot=snapshot(),
    ))
    assert result["status"] == "blocked" and result["reason"] == "combined_auto_bento_unsupported"
    assert result["failure_stage"] == "preflight"
    assert freight.operations == [] and calls == {"trade": [], "passenger": [], "preview": []}


@pytest.mark.parametrize("opened", [
    {"success": False, "page_state": "city_panel"},
    {"success": True, "page_state": "city_main"},
    {"page_state": "city_panel"},
])
def test_unconfirmed_registered_opener_stops_water_and_terminal_work(freight, opened):
    freight.runtime.state["panel_result"] = opened
    freight.state["current_fatigue"] = 701
    result = freight.run(auto_sparkling_water=True, recovery_snapshot=snapshot())
    assert result["success"] is False
    assert not any(op.startswith("water:") for op in freight.operations)
    assert "final_sale" not in freight.operations and "bentos" not in freight.operations


@pytest.mark.parametrize("action_name,params,page", [
    ("resonance_pc.open_city_panel_from_main", {}, "city_panel"),
])
def test_registered_recovery_adapter_tracks_cancelled_sync_worker(
    recovery_runtime, monkeypatch, action_name, params, page,
):
    runtime = recovery_runtime
    cid = f"freight-integration-cancel-{action_name}"
    started, release, exited = threading.Event(), threading.Event(), threading.Event()
    runtime.context.data["cid"] = cid
    monkeypatch.setattr(operation_scope, "get_config_value", lambda *args: 0)

    def worker(**kwargs):
        assert kwargs == params
        started.set()
        try:
            assert release.wait(5)
            return {"success": True, "page_state": page}
        finally:
            exited.set()

    runtime.register(action_name, worker)

    async def exercise():
        task = asyncio.create_task(trade._call_recovery_action(
            action_name, params,
            context=runtime.context, engine=runtime.engine,
        ))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            assert has_pending_sync_actions(cid)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert has_pending_sync_actions(cid)
        finally:
            release.set()
            assert await asyncio.to_thread(exited.wait, 2)
            if not task.done():
                await task

    try:
        asyncio.run(exercise())
        assert not has_pending_sync_actions(cid)
    finally:
        release.set()
        clear_task_cancel(cid)


@pytest.mark.parametrize("already_cancelled", [False, True],
                         ids=["cancelled-during-template-match", "cancelled-before-template-match"])
def test_real_city_panel_opener_never_clicks_after_cancellation(monkeypatch, already_cancelled):
    cancelled = {"value": already_cancelled}
    operations = []
    frame = np.zeros((170, 175, 3), np.uint8)
    monkeypatch.setattr(trade, "is_current_task_cancel_requested", lambda: cancelled["value"])

    def capture(*, rect):
        operations.append("capture")
        assert rect == city_vision.CITY_REGION
        return SimpleNamespace(success=True, image=frame)

    def find_templates_batch(**options):
        operations.append("match")
        assert options["source_image"] is frame
        # Cancellation while vision was busy must prevent accepting its result or clicking.
        cancelled["value"] = True
        return [SimpleNamespace(found=True, confidence=.99, debug_info={}) for _ in range(21)]

    def click(**kwargs):
        pytest.fail("A completed template result must not permit a click after cancellation")

    app = SimpleNamespace(capture=capture, click=click)
    vision = SimpleNamespace(find_templates_batch=find_templates_batch)
    with pytest.raises(trade.CityTradeFlowError) as error:
        trade.resonance_pc_open_city_panel_from_main(
            app=app, vision=vision, timeout_sec=0,
        )
    assert error.value.code == "trade_cancelled"
    assert operations == ([] if already_cancelled else ["capture", "match"])


def test_required_navigation_does_not_click_when_cancelled_during_template_wait(monkeypatch):
    cancelled = {"value": False}
    operations = []
    monkeypatch.setattr(trade, "is_current_task_cancel_requested", lambda: cancelled["value"])
    monkeypatch.setattr(trade, "probe_depart_button", lambda **kwargs: {"found": False})

    def wait_template(*args, **kwargs):
        operations.append("template_wait")
        cancelled["value"] = True
        return {"found": True, "center": [198, 37]}

    monkeypatch.setattr(trade, "_wait_template", wait_template)
    app = SimpleNamespace(click=lambda **kwargs: pytest.fail("Cancelled navigation must not click"))
    with pytest.raises(trade.CityTradeFlowError) as error:
        trade.resonance_pc_go_city_main_direct(app=app, vision=object(), timeout_sec=0)
    assert error.value.code == "trade_cancelled"
    assert operations == ["template_wait"]
