"""Passenger hooks through the existing freight flow, without game input."""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade
from plans.resonance_pc.src.actions import _freight_passenger_session as passenger
from plans.resonance_pc.src.actions.passenger_pc_actions import PassengerPcError
from tests.unit.test_resonance_pc_sparkling_water_trade import Market, harness, routes, run_full


@pytest.fixture
def attached(harness, monkeypatch):
    operations, state = harness
    state.update(passenger_stock=450, passenger_used=70, passenger_settlement=None)

    async def main(page_state, app, vision):
        operations.append("passenger_main")
        return "city_main"

    def inventory(*args, **kwargs):
        operations.append("passenger_inventory")
        return {"success": True, "page_state": "city_main", "flyers_remaining": state["passenger_stock"]}

    def recruit(destination, **kwargs):
        assert kwargs["read_passenger_count"] is False
        operations.append("recruit:" + destination)
        kwargs["submission_observer"]()
        if state.get("recruit_failure"):
            raise PassengerPcError("flyer_balance_unreadable", "submitted but unknown")
        before = state["passenger_stock"]
        state["passenger_stock"] -= min(state["passenger_used"], before)
        return {"success": True, "page_state": "city_main", "success_marker_confirmed": True,
                "flyers_before": before, "flyers_remaining": state["passenger_stock"],
                "flyers_used": before - state["passenger_stock"]}

    def settle(**kwargs):
        operations.append("passenger_settle")
        if state.get("settlement_failure"):
            raise PassengerPcError("settlement_dismiss_failed", "arrived but settlement unknown")
        return {"success": True, "status": "completed", "ticket_revenue": 100,
                "extra_revenue": state["passenger_settlement"], "total_revenue": None}

    monkeypatch.setattr(trade, "_passenger_ensure_main", main)
    monkeypatch.setattr(passenger, "_read_flyer_inventory", inventory)
    monkeypatch.setattr(passenger, "_recruit_passengers_by_flyer", recruit)
    monkeypatch.setattr(passenger, "resonance_pc_enter_city_and_settle_passengers", settle)
    return operations, state


def test_attached_passenger_preserves_solver_request_route_and_trailer_allocation(attached, monkeypatch):
    operations, state = attached
    requests = []

    def planner(**kwargs):
        requests.append(deepcopy({k: v for k, v in kwargs.items() if k != "resonance_pc_trade_planner"}))
        return {"status": "ok", "route": routes(), "expected_profit": 12345}

    monkeypatch.setattr(trade, "resonance_pc_trade_plan_optimal_route", planner)
    common = dict(auto_sparkling_water=False, auto_cape_island_investment=False,
                  auto_rubbish_recycling=False, auto_trailer=True)
    plain = run_full(**common)
    operations.clear()
    loaded = run_full(**common, auto_passenger=True,
                      passenger_options={"seat_capacity": 64, "flyer_target": 10})
    assert requests[0] == requests[1]
    assert "auto_passenger" not in requests[1] and "passenger_options" not in requests[1]
    assert plain["route"] == loaded["route"]
    assert plain["execution"]["trailer_service"] == loaded["execution"]["trailer_service"]
    assert plain["expected_profit"] == loaded["expected_profit"] == 12345
    assert loaded["success"] is True
    assert loaded["passenger"]["flyers_used"] == 70  # whole last car may overshoot
    assert loaded["passenger"]["stop_reason"] == "target_met"
    assert loaded["passenger"]["extra_revenue"] is None
    assert operations.index("trade:0") < operations.index("recruit:海角城") < operations.index("travel:0")
    assert operations.index("travel:0") < operations.index("passenger_settle") < operations.index("trade:1")


def test_cross_stop_batch_does_not_recruit_or_settle_at_intermediate_stop(attached, monkeypatch):
    operations, _ = attached
    route = [dict(routes()[0]),
             {"from_city_id": "11", "from_city": "海角城", "to_city_id": "13", "to_city": "汇流塔",
              "buy_products": [], "raise_to_cap": False}]
    monkeypatch.setattr(trade, "resonance_pc_trade_plan_optimal_route",
                        lambda **kw: {"status": "ok", "route": deepcopy(route)})

    def plan(route, options, **kwargs):
        start = kwargs.get("start_index", 0)
        trip = {"from_index": 0, "to_index": 2, "from_city_id": "15", "to_city_id": "13",
                "from_city": "岚心城", "to_city": "汇流塔", "expected_flyers_used": 70,
                "expected_revenue": 200}
        return {"trips": [trip] if start == 0 else [], "expected_revenue": 200}

    monkeypatch.setattr(passenger, "build_passenger_plan", plan)
    result = run_full(auto_passenger=True, passenger_options={"seat_capacity": 64, "flyer_target": 10},
                      auto_sparkling_water=False, auto_cape_island_investment=False,
                      auto_rubbish_recycling=False)
    assert result["success"] is True
    assert [op for op in operations if op.startswith("recruit:")] == ["recruit:汇流塔"]
    assert operations.count("passenger_settle") == 1
    assert operations.index("trade:1") < operations.index("travel:1") < operations.index("passenger_settle")
    assert operations.index("passenger_settle") < operations.index("final_sale")


def test_settlement_precedes_arrival_investment(attached, monkeypatch):
    operations, _ = attached

    async def investment(**kwargs):
        operations.append("investment")
        return {"success": True, "triggered": True, "status": "invested", "page_state": "city_main"}

    monkeypatch.setattr(trade, "_execute_cape_island_investment_after_arrival", investment)
    result = run_full(auto_passenger=True, passenger_options={"seat_capacity": 64, "flyer_target": 10},
                      auto_sparkling_water=False, auto_cape_island_investment=True,
                      auto_rubbish_recycling=False)
    assert result["success"] is True
    assert operations.index("passenger_settle") < operations.index("investment")


@pytest.mark.parametrize("failure", ["recruit_failure", "settlement_failure"])
def test_submitted_or_settlement_failure_keeps_destination_and_stops_freight(attached, failure):
    operations, state = attached
    state[failure] = True
    result = run_full(auto_passenger=True, passenger_options={"seat_capacity": 64, "flyer_target": 10},
                      auto_sparkling_water=False, auto_cape_island_investment=False,
                      auto_rubbish_recycling=False)
    assert result["success"] is False
    assert result["requires_manual_completion"] is True
    assert result["loaded_destination"]["city_name"] == "海角城"
    assert "trade:1" not in operations and "final_sale" not in operations
    if failure == "recruit_failure":
        assert "travel:0" not in operations
    else:
        assert result["execution"]["leg_results"][0]["travel"]["success"] is True


def test_insufficient_route_is_normal_partial_completion(attached):
    _, state = attached
    state["passenger_used"] = 10
    result = run_full(auto_passenger=True, passenger_options={"seat_capacity": 64, "flyer_target": 1000},
                      auto_sparkling_water=False, auto_cape_island_investment=False,
                      auto_rubbish_recycling=False)
    assert result["success"] is True
    assert result["passenger"]["stop_reason"] == "route_exhausted"
    assert result["passenger"]["remaining_target"] > 0
    assert result["passenger"]["loaded_destination"] is None


@pytest.mark.parametrize("options", [{}, {"seat_capacity": True}, {"seat_capacity": 0}])
def test_invalid_options_rejected_before_game(harness, options):
    operations, _ = harness
    with pytest.raises(ValueError):
        run_full(auto_passenger=True, passenger_options=options)
    assert operations == []


def test_fixed_attachment_rejected_before_game(harness):
    operations, _ = harness
    with pytest.raises(ValueError, match="free"):
        run_full(trade_mode="fixed", fixed_route_city_ids=["15", "11"],
                 auto_passenger=True, passenger_options={"seat_capacity": 64})
    assert operations == []


def test_submitted_cancellation_reaches_framework_cleanup_evidence(attached, monkeypatch):
    operations, state = attached

    def cancel(destination, **kwargs):
        kwargs["submission_observer"]()
        raise asyncio.CancelledError()

    monkeypatch.setattr(passenger, "_recruit_passengers_by_flyer", cancel)
    with pytest.raises(asyncio.CancelledError):
        run_full(auto_passenger=True, passenger_options={"seat_capacity": 64, "flyer_target": 10},
                 auto_sparkling_water=False, auto_cape_island_investment=False,
                 auto_rubbish_recycling=False)
    evidence = state["context"].data["freight_passenger"]
    assert evidence["requires_manual_completion"] is True
    assert evidence["loaded_destination"]["state"] == "submitted_unknown"
    assert "travel:0" not in operations


def test_terminal_guard_never_sells_with_unsettled_passengers(attached, monkeypatch):
    operations, _ = attached

    async def no_settlement(self, index, **kwargs):
        return {"page_state": kwargs["page_state"]}

    monkeypatch.setattr(passenger.FreightPassengerSession, "after_arrival", no_settlement)
    result = run_full(auto_passenger=True, passenger_options={"seat_capacity": 64, "flyer_target": 10},
                      auto_sparkling_water=False, auto_cape_island_investment=False,
                      auto_rubbish_recycling=False)
    assert result["success"] is False and result["reason"] == "passenger_batch_unsettled"
    assert result["requires_manual_completion"] is True
    assert "final_sale" not in operations


def test_passenger_replanning_events_never_advance_freight_revision(attached):
    _, state = attached

    class Bus:
        def __init__(self):
            self.events = []

        async def publish(self, event):
            self.events.append(event.payload)

    async def run():
        bus = Bus()
        reporter = trade._TradeProgressReporter(bus, "attached", asyncio.get_running_loop())
        token = trade._ACTIVE_PROGRESS_REPORTER.set(reporter)
        try:
            result = await trade.resonance_pc_auto_cycle_trade_flow.__wrapped__(
                auto_passenger=True, passenger_options={"seat_capacity": 64, "flyer_target": 10},
                app=object(), ocr=object(), vision=object(), state_store=object(),
                resonance_pc_city_shop_data=object(), resonance_pc_market_data=Market(),
                resonance_pc_trade_planner=object(), context=state["context"], engine=state["engine"],
                auto_cape_island_investment=False, auto_rubbish_recycling=False)
        finally:
            trade._ACTIVE_PROGRESS_REPORTER.reset(token)
        return result, bus.events

    result, events = asyncio.run(run())
    assert result["success"] is True
    revisions = [e["data"]["route_revision"] for e in events if e["stage"] == "passenger_planning"]
    assert len(revisions) >= 2 and set(revisions) == {1}
    assert [e for e in events if e["stage"] == "passenger_planning"][-1]["data"]["passenger"]["loaded_destination"] is None
