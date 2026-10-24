"""Offline per-city threshold recovery through the real freight route orchestration."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import sys
from types import SimpleNamespace

import pytest

from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade
from plans.resonance_pc.src.services.city_shop_data_pc_service import ResonancePcCityShopDataService


_ACTIVE_HARNESS = None


def snapshot(current=1, remaining=6):
    return {"status": {"fatigue": {"current": current, "max": 1000}},
            "recovery": {"sparkling_water": {"remaining_free_uses": remaining, "daily_free_limit": 6}},
            "metadata": {"persisted": True}}


def routes():
    return [
        {"from_city_id": "15", "from_city_key": "lanxin_city", "from_city": "岚心城",
         "to_city_id": "11", "to_city_key": "cape_city", "to_city": "海角城", "buy_products": [], "raise_to_cap": False},
        {"from_city_id": "11", "from_city_key": "cape_city", "from_city": "海角城",
         "to_city_id": "15", "to_city_key": "lanxin_city", "to_city": "岚心城", "buy_products": [], "raise_to_cap": False},
    ]


class Market:
    def get_all_travel_fatigue(self):
        return {"cities": {"15": "岚心城", "11": "海角城"}, "costs": {"15": {"11": 100}, "11": {"15": 100}}}

    def get_travel_fatigue(self, from_city_id, to_city_id):
        assert (str(from_city_id), str(to_city_id)) in {("15", "11"), ("11", "15")}
        state = _ACTIVE_HARNESS
        if state is None:
            return 100
        index = state["travel_index"]
        costs = state.get("known_travel_costs", state["travel_costs"])
        cost = costs[index] if index < len(costs) else 100
        if isinstance(cost, Exception):
            raise cost
        return cost


@pytest.fixture
def harness(monkeypatch):
    operations = []
    state = {"completed": 0, "blocked": False, "current_fatigue": 250, "local_quota": 6,
             "travel_costs": [100, 100], "trade_costs": [0, 0, 0], "travel_index": 0,
             "city": {"success": True, "city_name": "岚心城", "city_key": "lanxin_city"}}

    async def init(**kwargs):
        state.update(completed=0, blocked=False, travel_index=0, route_length=len(kwargs["route"]))
        return {"run_key": "route"}

    async def update(**kwargs):
        state["blocked"] = kwargs["travel_status"] == "blocked"
        if not state["blocked"]:
            state["completed"] += 1
        return {"status": "blocked" if state["blocked"] else "running"}

    async def summary(*args, **kwargs):
        return {"status": "blocked" if state["blocked"] else "ok", "completed_leg_count": state["completed"]}

    async def noop(*args, **kwargs):
        return None

    def city_trade(**kwargs):
        index = kwargs["progress_context"]["city_index"]
        terminal = index == state.get("route_length", 2)
        operations.append("final_sale" if terminal else f"trade:{index}")
        if state.get("final_sale_failure") and terminal:
            return {"success": False, "page_state": "unknown"}
        state["current_fatigue"] += state["trade_costs"][index] if index < len(state["trade_costs"]) else 0
        observation = {"success": True, "current_fatigue": state["current_fatigue"],
                       "max_fatigue": 1000, "source": "trade_page", "reason": None}
        overrides = state.get("trade_observations", {})
        if index in overrides:
            observation = deepcopy(overrides[index])
        return {"success": True, "page_state": "city_main", "fatigue_observation": observation}

    def travel(**kwargs):
        index = state["travel_index"]
        state["travel_index"] += 1
        operations.append(f"travel:{index}")
        blocked = state.get("block_index") == index
        failed = state.get("fail_index") == index
        if not blocked and not failed:
            state["current_fatigue"] += state["travel_costs"][index] if index < len(state["travel_costs"]) else 100
        return {"status": "blocked" if blocked else "failed" if failed else "ok",
                "success": not (blocked or failed), "page_state": "city_main",
                **deepcopy(state.get("travel_results", {}).get(index, {}))}

    async def drink(**kwargs):
        count = kwargs["drink_count"]
        operations.append(f"water:{kwargs['city_name']}:{count}")
        if state.get("water_failure"):
            raise trade.CityTradeFlowError("water_failed", "water failed")
        assert 0 < count <= state["local_quota"]
        state["current_fatigue"] -= count * 50
        state["local_quota"] -= count
        return {"success": True, "page_state": "city_panel", "status": "completed",
                "requested_count": count, "completed_count": count, "recovered_fatigue": count * 50,
                "remaining_free_uses": state["local_quota"], "daily_free_limit": 6}

    def open_panel(**kwargs):
        operations.append("open_panel")
        return {"success": True, "page_state": "city_panel", "city": dict(state["city"])}

    async def recovery_action(action_name, params, **kwargs):
        if action_name == "resonance_pc.open_city_panel_from_main":
            assert params == {}
            return open_panel()
        if action_name == "resonance_pc.go_city_main_direct":
            operations.append("go_main")
            return {"success": True, "page_state": "city_main"}
        assert action_name == "resonance_pc.player_data_refresh"
        assert params == {"stages": ["profile"], "profile_sections": ["fatigue"]}
        operations.append("refresh_fatigue")
        result = snapshot(current=state["current_fatigue"], remaining=state["local_quota"])
        result["metadata"]["executed_profile_sections"] = ["fatigue"]
        return result

    async def bento(task_ref, params):
        assert task_ref == "tasks:bento_consumption_pc.yaml:bento_consumption_pc"
        operations.append("bentos")
        state.setdefault("bento_params", []).append(deepcopy(params))
        before = state["current_fatigue"]
        stop = params["bento_stop_threshold"]
        overrides = deepcopy(state.get("bento_result", {}))
        count = overrides.get("consumed_count", max(0, (before - stop + 59) // 60))
        actual_count = count if type(count) is int and count >= 0 else 1
        final = overrides.get("final_fatigue")
        state["current_fatigue"] = final if type(final) is int else max(0, before - actual_count * 60)
        result = {"success": True, "status": "completed", "reason": "stop_threshold_reached",
                  "page_state": "city_main", "initial_fatigue": before,
                  "final_fatigue": state["current_fatigue"], "consumed_count": count,
                  "completed_count": count, "work_meals_used": count, "love_bentos_used": 0,
                  "recovered_fatigue": before - state["current_fatigue"],
                  "bento_start_threshold": params["bento_start_threshold"],
                  "bento_stop_threshold": stop, "items": [{"consumed": True}] * count}
        result.update(overrides)
        return result

    state["engine"] = SimpleNamespace(bind_execution=lambda context: SimpleNamespace(run_task=bento))
    state["context"] = SimpleNamespace(data={})
    state["drink"] = drink
    state["bento"] = bento
    monkeypatch.setattr(sys.modules[__name__], "_ACTIVE_HARNESS", state)
    monkeypatch.setattr(trade, "resonance_pc_trade_route_execution_init", init)
    monkeypatch.setattr(trade, "resonance_pc_trade_route_execution_update", update)
    monkeypatch.setattr(trade, "resonance_pc_trade_route_execution_summary", summary)
    monkeypatch.setattr(trade, "resonance_pc_trade_route_execution_cleanup", noop)
    monkeypatch.setattr(trade, "_sleep_with_cancel_async", noop)
    monkeypatch.setattr(trade, "_call_recovery_action", recovery_action)
    monkeypatch.setattr(trade, "load_pc_user_info", lambda store: snapshot(remaining=state["local_quota"]))
    monkeypatch.setattr(trade, "resonance_pc_drink_sparkling_water_from_city_panel", drink)
    monkeypatch.setattr(trade, "resonance_pc_open_city_panel_from_main", open_panel)
    monkeypatch.setattr(trade, "resonance_pc_go_city_main_direct",
                        lambda **kwargs: {"success": True, "page_state": "city_main"})
    monkeypatch.setattr(trade, "resonance_pc_read_city_name_on_city_panel",
                        lambda **kw: pytest.fail("Use the city already confirmed by the opener"))
    monkeypatch.setattr(trade, "resonance_pc_market_refresh", lambda **kw: {"snapshot_id": "market"})
    monkeypatch.setattr(trade, "resonance_pc_trade_plan_optimal_route", lambda **kw: {"status": "ok", "route": routes()})
    monkeypatch.setattr(trade, "_execute_city_trade_inside_current_city", city_trade)
    monkeypatch.setattr(trade, "resonance_pc_intercity_depart_and_wait", travel)
    return operations, state


def run_full(**overrides):
    args = dict(app=object(), ocr=object(), vision=object(), state_store=object(),
                resonance_pc_city_shop_data=ResonancePcCityShopDataService(),
                resonance_pc_market_data=Market(), resonance_pc_trade_planner=object(),
                persistent_data=object(), auto_sparkling_water=True, recovery_snapshot=snapshot(),
                sparkling_water_start_threshold=300, sparkling_water_stop_threshold=200,
                bento_start_threshold=700, bento_stop_threshold=200,
                auto_rubbish_recycling=False, auto_cape_island_investment=False)
    if _ACTIVE_HARNESS is not None:
        args.update(context=_ACTIVE_HARNESS["context"], engine=_ACTIVE_HARNESS["engine"])
    args.update(overrides)
    return asyncio.run(trade.resonance_pc_auto_cycle_trade_flow(**args))


def run_harness(harness, **overrides):
    _, state = harness
    return run_full(context=state["context"], engine=state["engine"], **overrides)


def water_operations(operations):
    return [op for op in operations if op.startswith("water:")]


def test_water_runs_at_each_eligible_arrival_and_updates_observed_fatigue_and_quota(harness):
    operations, state = harness
    result = run_harness(harness, sparkling_water_start_threshold=250)
    assert result["success"] is True
    assert water_operations(operations) == ["water:海角城:3", "water:岚心城:2"]
    assert state["local_quota"] == 1 and state["current_fatigue"] == 200
    assert operations.index("travel:0") < operations.index("water:海角城:3") < operations.index("trade:1")
    assert operations.index("travel:1") < operations.index("water:岚心城:2") < operations.index("final_sale")
    assert result["sparkling_water"]["completed_count"] == 5
    assert result["sparkling_water"]["recovered_fatigue"] == 250
    assert result["sparkling_water_plan"]["mode"] == "thresholds"
    assert len(result["sparkling_water_plan"]["visits"]) == 2


def test_initial_fatigue_above_start_drinks_before_first_trade(harness):
    operations, state = harness
    state.update(current_fatigue=401, travel_costs=[0, 0])
    result = run_harness(harness)
    assert water_operations(operations) == ["water:岚心城:5"]
    assert operations.index("water:岚心城:5") < operations.index("trade:0")
    assert state["current_fatigue"] == 151 and result["final_fatigue"] == 151


def test_start_equality_does_not_drink_or_force_terminal_consumption(harness):
    operations, state = harness
    state.update(current_fatigue=300, travel_costs=[0, 0])
    result = run_harness(harness)
    assert water_operations(operations) == []
    assert result["sparkling_water"]["triggered"] is False
    assert "final_sale" in operations
    assert result["final_fatigue"] == 300


def test_trade_cost_crossing_start_is_checked_before_departure(harness):
    operations, state = harness
    state.update(current_fatigue=290, trade_costs=[40, 0, 0], travel_costs=[0, 0])
    run_harness(harness)
    assert water_operations(operations) == ["water:岚心城:3"]
    assert operations.index("trade:0") < operations.index("water:岚心城:3") < operations.index("travel:0")


def test_below_start_at_final_city_does_not_force_any_drink(harness):
    operations, state = harness
    state.update(current_fatigue=20, travel_costs=[0, 0])
    result = run_harness(harness)
    assert water_operations(operations) == []
    assert result["sparkling_water"]["status"] == "skipped"


def test_final_sale_crossing_start_runs_threshold_recovery_after_sale(harness):
    operations, state = harness
    state.update(current_fatigue=100, travel_costs=[0, 0], trade_costs=[0, 0, 250])
    result = run_harness(harness)
    assert result["success"] and result["final_fatigue"] == 200
    assert operations.index("final_sale") < operations.index("water:岚心城:3")
    assert any(row["phase"] == "final_sale" and row["initial_fatigue"] == 350
               for row in result["recovery_events"])


def test_disabled_recovery_ignores_snapshot_and_does_not_refresh(harness):
    operations, _ = harness
    result = run_harness(harness, auto_sparkling_water=False, recovery_snapshot={"invalid": True})
    assert not water_operations(operations) and "refresh_fatigue" not in operations
    assert result["sparkling_water"]["reason"] == "disabled"


@pytest.mark.parametrize("start,stop", [(None, 200), (300, None), (True, 0), (200, 200), (199, 200)])
def test_enabled_water_requires_explicit_ordered_thresholds_before_ui(harness, start, stop):
    operations, _ = harness
    with pytest.raises(ValueError, match="threshold"):
        run_harness(harness, sparkling_water_start_threshold=start, sparkling_water_stop_threshold=stop)
    assert operations == []


def test_missing_snapshot_rejected_before_any_game_input(harness):
    operations, _ = harness
    with pytest.raises(ValueError, match="recovery_snapshot"):
        run_harness(harness, recovery_snapshot=None)
    assert operations == []


def test_blocked_travel_does_not_run_arrival_recovery_or_final_sale(harness):
    operations, state = harness
    state.update(block_index=0, current_fatigue=100)
    result = run_harness(harness)
    assert result["status"] == "blocked"
    assert not water_operations(operations)
    assert "trade:1" not in operations and "final_sale" not in operations


def test_water_failure_does_not_retry_or_continue_trade(harness):
    operations, state = harness
    state.update(water_failure=True, current_fatigue=401)
    result = run_harness(harness)
    assert result["success"] is False
    assert water_operations(operations) == ["water:岚心城:5"]
    assert "trade:0" not in operations and "final_sale" not in operations


def test_actual_local_quota_overrides_stale_snapshot(harness):
    operations, state = harness
    state.update(current_fatigue=401, local_quota=1, travel_costs=[0, 0])
    result = run_harness(harness, recovery_snapshot=snapshot(current=1, remaining=6))
    assert water_operations(operations) == ["water:岚心城:1"]
    assert result["sparkling_water"]["completed_count"] == 1 and state["local_quota"] == 0


def test_zero_local_quota_keeps_trade_without_water(harness):
    operations, state = harness
    state.update(current_fatigue=401, local_quota=0)
    result = run_harness(harness, recovery_snapshot=snapshot(remaining=6))
    assert result["success"] is True and not water_operations(operations)
    assert "final_sale" in operations
