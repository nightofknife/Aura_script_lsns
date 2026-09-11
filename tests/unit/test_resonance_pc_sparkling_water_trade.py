"""Trade arrival ordering and snapshot gates for on-route sparkling water."""
from __future__ import annotations

import asyncio

import pytest

from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade
from plans.resonance_pc.src.services.city_shop_data_pc_service import ResonancePcCityShopDataService


def snapshot(current=1, remaining=6):
    return {"status": {"fatigue": {"current": current, "max": 856}},
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


@pytest.fixture
def harness(monkeypatch):
    operations = []
    state = {"completed": 0, "blocked": False}

    async def init(**kwargs):
        return {"run_key": "route"}

    async def update(**kwargs):
        state["blocked"] = kwargs["travel_status"] == "blocked"
        if not state["blocked"]:
            state["completed"] += 1
        return {"status": "blocked" if state["blocked"] else "running"}

    async def summary(*args, **kwargs):
        return {"status": "blocked" if state["blocked"] else "completed", "completed_leg_count": state["completed"]}

    async def noop(*args, **kwargs):
        return None

    async def leg(**kwargs):
        index = kwargs["index"]
        operations.extend([f"trade:{index}", f"travel:{index}"])
        blocked = state.get("block_index") == index
        return {"travel": {"status": "blocked" if blocked else "ok", "success": not blocked},
                "page_state": "city_main", "city_trade": {}, "leg": kwargs["leg"]}

    async def drink(**kwargs):
        operations.append(f"water:{kwargs['city_name']}:{kwargs['drink_count']}")
        if state.get("water_failure"):
            raise RuntimeError("water failed")
        return {"success": True, "page_state": "city_panel", "status": "completed",
                "completed_count": kwargs["drink_count"]}

    def open_panel(**kwargs):
        operations.append("open_panel")
        return {"page_state": "city_panel"}

    def final_sale(**kwargs):
        operations.append("final_sale")
        return {"page_state": "city_main"}

    monkeypatch.setattr(trade, "resonance_pc_trade_route_execution_init", init)
    monkeypatch.setattr(trade, "resonance_pc_trade_route_execution_update", update)
    monkeypatch.setattr(trade, "resonance_pc_trade_route_execution_summary", summary)
    monkeypatch.setattr(trade, "resonance_pc_trade_route_execution_cleanup", noop)
    monkeypatch.setattr(trade.asyncio, "sleep", noop)
    monkeypatch.setattr(trade, "_execute_trade_leg", leg)
    monkeypatch.setattr(trade, "resonance_pc_drink_sparkling_water_from_city_panel", drink)
    monkeypatch.setattr(trade, "resonance_pc_open_city_panel_from_main", open_panel)
    monkeypatch.setattr(trade, "resonance_pc_read_city_name_on_city_panel", lambda **kw: {"city_name": "岚心城", "city_key": "lanxin_city"})
    monkeypatch.setattr(trade, "resonance_pc_market_refresh", lambda **kw: {"snapshot_id": "market"})
    monkeypatch.setattr(trade, "resonance_pc_trade_plan_optimal_route", lambda **kw: {"status": "ok", "route": routes()})
    monkeypatch.setattr(trade, "_execute_city_trade_inside_current_city", final_sale)
    return operations, state


def run_full(**overrides):
    args = dict(app=object(), ocr=object(), vision=object(), state_store=object(),
                resonance_pc_city_shop_data=ResonancePcCityShopDataService(),
                resonance_pc_market_data=Market(), resonance_pc_trade_planner=object(),
                persistent_data=object(), auto_sparkling_water=True, recovery_snapshot=snapshot(),
                base_fatigue_reserve=0,
                auto_rubbish_recycling=False, auto_cape_island_investment=False)
    args.update(overrides)
    return asyncio.run(trade.resonance_pc_auto_cycle_trade_flow(**args))


def test_configured_reserve_applies_to_full_trade_execution(harness):
    operations, _ = harness
    result = run_full(base_fatigue_reserve=200, recovery_snapshot=snapshot(current=250))
    assert result["sparkling_water_plan"]["base_fatigue_reserve"] == 200
    assert result["sparkling_water_plan"]["drink_count"] == 5
    assert result["sparkling_water_plan"]["city_index"] == 2
    assert "water:岚心城:5" in operations
    assert operations[-1] == "final_sale"


def test_negative_reserve_rejected_before_game_input(harness):
    operations, _ = harness
    with pytest.raises(ValueError, match="base_fatigue_reserve"):
        run_full(base_fatigue_reserve=-1)
    assert operations == []


def test_endpoint_drinks_four_before_final_sale(harness):
    operations, _ = harness
    result = run_full()
    assert result["sparkling_water_plan"]["city_index"] == 2
    assert result["sparkling_water_plan"]["drink_count"] == 4
    assert operations == ["open_panel", "trade:0", "travel:0", "trade:1", "travel:1", "open_panel", "water:岚心城:4", "final_sale"]
    assert result["execution"]["leg_results"][1]["sparkling_water"]["completed_count"] == 4
    assert result["sparkling_water"]["triggered"] is True


def test_start_drinks_once_before_first_trade(harness):
    operations, _ = harness
    result = run_full(recovery_snapshot=snapshot(current=301))
    assert operations[:3] == ["open_panel", "water:岚心城:6", "trade:0"]
    assert len([op for op in operations if op.startswith("water:")]) == 1
    assert result["sparkling_water_plan"]["city_index"] == 0


def test_midpoint_only_stop_uses_two_cups(harness, monkeypatch):
    operations, _ = harness
    service = ResonancePcCityShopDataService()
    original = service.resolve_shop_point

    def resolve(city_name, shop_name, *args, **kwargs):
        if city_name == "岚心城":
            from plans.resonance_pc.src.services.city_shop_data_pc_service import CityShopDataError
            raise CityShopDataError("shop_not_found_in_city", "no rest")
        return original(city_name, shop_name, *args, **kwargs)

    monkeypatch.setattr(service, "resolve_shop_point", resolve)
    result = run_full(resonance_pc_city_shop_data=service)
    assert result["sparkling_water_plan"]["city_index"] == 1
    assert "water:海角城:2" in operations
    assert operations.index("water:海角城:2") < operations.index("trade:1")


def test_unchecked_does_not_plan_or_drink_even_with_snapshot(harness):
    operations, _ = harness
    result = run_full(auto_sparkling_water=False)
    assert result["sparkling_water_plan"]["reason"] == "disabled"
    assert not any(op.startswith("water:") for op in operations)


def test_missing_snapshot_rejected_before_any_game_input(harness):
    operations, _ = harness
    with pytest.raises(ValueError, match="recovery_snapshot"):
        run_full(recovery_snapshot=None)
    assert operations == []


def test_blocked_travel_does_not_drink_or_clear_final_cargo(harness):
    operations, state = harness
    state["block_index"] = 0
    result = run_full()
    assert result["status"] == "blocked"
    assert not any(op.startswith("water:") for op in operations)
    assert "trade:1" not in operations and "final_sale" not in operations


def test_water_failure_prevents_next_trade_and_final_sale(harness):
    operations, state = harness
    state["water_failure"] = True
    with pytest.raises(RuntimeError, match="water failed"):
        run_full(recovery_snapshot=snapshot(current=301))
    assert "trade:0" not in operations and "final_sale" not in operations


def test_no_remaining_quota_keeps_existing_trade(harness):
    operations, _ = harness
    result = run_full(recovery_snapshot=snapshot(remaining=0))
    assert result["sparkling_water_plan"]["reason"] == "no_remaining_free_uses"
    assert "final_sale" in operations
    assert not any(op.startswith("water:") for op in operations)
