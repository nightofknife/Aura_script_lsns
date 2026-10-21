"""Retired planner API boundaries and the surviving route execution contract."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from plans.resonance_pc.src.actions import trade_planner_pc_actions as actions
from plans.resonance_pc.src.services import resonance_pc_trade_planner_service as planners


@pytest.mark.parametrize("name", [
    "resonance_pc_trade_plan_next",
    "resonance_pc_trade_plan_best_cycle",
    "resonance_pc_trade_plan_cycle_execution",
    "resonance_pc_trade_plan_next_cycle_execution",
    "resonance_pc_trade_simulate",
    "resonance_pc_trade_loop_init",
    "resonance_pc_trade_loop_update",
    "resonance_pc_trade_loop_summary",
    "resonance_pc_trade_loop_cleanup",
])
def test_retired_action_names_have_no_callable_or_forwarding_alias(name):
    assert not hasattr(actions, name)


@pytest.mark.parametrize("name", [
    "plan_next_step", "plan_best_cycle", "plan_cycle_execution",
    "plan_next_cycle_execution", "simulate_until_stop",
])
def test_retired_service_entry_points_are_unavailable(name):
    planner = planners.ResonancePcTradePlannerService(SimpleNamespace())
    assert not hasattr(planner, name)


def test_constructor_no_longer_accepts_legacy_beam_configuration():
    with pytest.raises(TypeError, match="beam_width"):
        planners.ResonancePcTradePlannerService(SimpleNamespace(), beam_width=8)
    planner = planners.ResonancePcTradePlannerService(SimpleNamespace())
    assert not hasattr(planner, "beam_width")
    assert not hasattr(planner, "_max_sell_price_cache")


def test_current_city_constraints_still_validate_identity_and_return_independent_snapshots(tmp_path):
    meta = tmp_path / "data/meta"
    meta.mkdir(parents=True)
    (meta / "trade_constraints.json").write_text(json.dumps({
        "schema_version": "1.0.0",
        "allowed_city_ids": ["1", "2"],
        "default_available_city_ids": ["1", "2"],
        "city_id_to_key": {"1": "shoggolith_city", "2": "brcl_outpost"},
    }), encoding="utf-8")
    planner = planners.ResonancePcTradePlannerService(SimpleNamespace(), plan_root=tmp_path)
    constraints = planner.get_trade_constraints()
    constraints["allowed_city_ids"].clear()
    constraints["city_id_to_key"]["1"] = "changed"
    assert planner.assert_allowed_city(city_id="1")["city_key"] == "shoggolith_city"
    assert planner.assert_allowed_city(city_key="brcl_outpost")["city_id"] == "2"
    assert planner.get_trade_constraints()["allowed_city_ids"] == ["1", "2"]
    with pytest.raises(planners.ResonancePcTradePlannerError) as caught:
        planner.assert_allowed_city(city_key="brcl_outpost", city_id="1")
    assert caught.value.code == "city_mapping_conflict"


class Store:
    def __init__(self):
        self.values = {"another_flow": {"preserved": True}}

    async def get(self, key, default=None):
        return self.values.get(key, default)

    async def set(self, key, value):
        self.values[key] = value

    async def delete(self, key):
        del self.values[key]


def test_current_route_keeps_partial_receipts_confirmed_usage_and_cleanup(monkeypatch):
    monkeypatch.setattr(actions, "current_cid", lambda: "retirement")
    store = Store()
    route = [{"from_city": "A", "to_city": "B"}, {"from_city": "B", "to_city": "C"}]

    async def execute():
        initial = await actions.resonance_pc_trade_route_execution_init(route=route, state_store=store)
        key = initial["run_key"]
        assert key == "resonance_pc.auto_cycle_trade.retirement.route"
        route[0]["from_city"] = "changed input"
        first = await actions.resonance_pc_trade_route_execution_update(
            key, fatigue_medicine_used=[{"name": "tea", "count": 2}],
            fatigue_medicine_use_count=1, state_store=store)
        assert first["status"] == "running" and first["completed_leg_count"] == 1
        assert first["completed_route"] == [{"from_city": "A", "to_city": "B"}]
        assert first["current_leg"] == {"from_city": "B", "to_city": "C"}
        blocked = await actions.resonance_pc_trade_route_execution_update(
            key, travel_status="blocked", reason="fatigue_shortfall", blocked_at="departure",
            fatigue_medicine_used=[{"name": "tea", "count": 1}, {"name": "coffee", "count": 1}],
            fatigue_medicine_use_count=2, state_store=store)
        expected = deepcopy(blocked)
        assert blocked["status"] == "blocked" and blocked["should_continue"] is False
        assert blocked["reason"] == "fatigue_shortfall" and blocked["blocked_at"] == "departure"
        assert blocked["completed_leg_count"] == 1 and blocked["index"] == 1
        assert blocked["blocked_leg"] == {"from_city": "B", "to_city": "C"}
        assert blocked["current_leg"] is None
        assert blocked["fatigue_medicine_used"] == [{"name": "tea", "count": 3}, {"name": "coffee", "count": 1}]
        assert blocked["fatigue_medicine_use_count"] == 3
        blocked["completed_route"][0]["to_city"] = "changed receipt"
        blocked["fatigue_medicine_used"][0]["count"] = 99
        assert await actions.resonance_pc_trade_route_execution_summary(key, state_store=store) == expected
        assert await actions.resonance_pc_trade_route_execution_cleanup(key, state_store=store) == {
            "success": True, "run_key": key}
        assert store.values == {"another_flow": {"preserved": True}}

    asyncio.run(execute())


def test_current_route_can_finish_all_legs_without_legacy_loop_state():
    store = Store()

    async def execute():
        initial = await actions.resonance_pc_trade_route_execution_init(
            route=[{"from_city": "A", "to_city": "B"}], state_store=store)
        result = await actions.resonance_pc_trade_route_execution_update(initial["run_key"], state_store=store)
        assert result["status"] == "ok" and result["should_continue"] is False
        assert result["completed_route"] == [{"from_city": "A", "to_city": "B"}]
        assert result["current_leg"] is None and result["completed_leg_count"] == 1
        await actions.resonance_pc_trade_route_execution_cleanup(initial["run_key"], state_store=store)

    asyncio.run(execute())
