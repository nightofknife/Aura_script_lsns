from __future__ import annotations

import asyncio
import copy

import plans.resonance_pc.src.actions.combined_commerce_pc_actions as combined


class _Market:
    cities = {"1": "修格里城", "11": "海角城", "15": "岚心城"}

    def get_all_travel_fatigue(self):
        return {"cities": dict(self.cities)}

    def get_travel_fatigue(self, from_city_id: str, to_city_id: str) -> int:
        pair = (str(from_city_id), str(to_city_id))
        return {
            ("11", "15"): 76,
            ("15", "11"): 76,
            ("1", "11"): 20,
            ("1", "15"): 40,
        }.get(pair, 30)


class _CityData:
    keys = {
        "修格里城": "shoggolith_city",
        "海角城": "cape_city",
        "岚心城": "lanxin_city",
    }

    def resolve_city(self, city_name: str):
        return {"city_key": self.keys[city_name], "city_name": city_name}


def _trade_result(end_city_id: str = "11", *, fatigue: int = 100):
    return {
        "success": True,
        "status": "completed",
        "reason": None,
        "route": [{"to_city_id": end_city_id, "to_city": "终点"}],
        "execution": {
            "completed_leg_count": 1,
            "cape_island_triggered_count": 0,
        },
        "final_sale": {"success": True, "page_state": "city_main"},
        "expected_fatigue_used": fatigue,
        "page_state": "city_main",
    }


def _passenger_result(end_city_id: str = "15", *, fatigue: int = 76):
    return {
        "success": True,
        "status": "completed",
        "reason": None,
        "requested_trips": 1,
        "completed_trips": 1,
        "expected_fatigue_used": fatigue,
        "end_city": {"city_id": end_city_id, "city_name": "终点"},
        "requires_manual_completion": False,
        "loaded_destination": None,
        "page_state": "city_main",
    }


def _run(**overrides):
    values = {
        "order": "trade_first",
        "total_fatigue_budget": 700,
        "trade_inputs": {"available_city_ids": ["1", "11", "15"]},
        "passenger_inputs": {
            "passenger_city_a_id": "11",
            "passenger_city_b_id": "15",
            "trip_count": 1,
            "trade_during_trip": True,
            "reposition_to_route": True,
        },
        "app": object(),
        "ocr": object(),
        "vision": object(),
        "controller": object(),
        "resonance_pc_city_shop_data": _CityData(),
        "resonance_pc_market_data": _Market(),
        "resonance_pc_trade_planner": object(),
        "state_store": object(),
        "event_bus": object(),
        "context": object(),
        "engine": object(),
    }
    values.update(overrides)
    return asyncio.run(combined.resonance_pc_auto_combined_commerce_flow(**values))


def test_trade_first_reserves_passenger_fatigue_and_does_not_mutate_inputs(monkeypatch):
    trade_calls: list[dict] = []
    passenger_calls: list[dict] = []

    async def run_trade(inputs, **_kwargs):
        trade_calls.append(dict(inputs))
        return _trade_result("11", fatigue=600)

    async def run_passenger(inputs, **_kwargs):
        passenger_calls.append(dict(inputs))
        return _passenger_result("15", fatigue=76)

    monkeypatch.setattr(combined, "_run_trade", run_trade)
    monkeypatch.setattr(combined, "_run_passenger", run_passenger)
    trade_inputs = {"available_city_ids": ["1", "11", "15"], "fatigue_budget": 999}
    passenger_inputs = {
        "passenger_city_a_id": "11",
        "passenger_city_b_id": "15",
        "trip_count": 1,
        "trade_during_trip": True,
        "reposition_to_route": True,
    }
    original_trade = copy.deepcopy(trade_inputs)
    original_passenger = copy.deepcopy(passenger_inputs)

    result = _run(trade_inputs=trade_inputs, passenger_inputs=passenger_inputs)

    assert result["success"] is True
    assert result["status"] == "completed"
    assert trade_calls[0]["fatigue_budget"] == 624
    assert trade_calls[0]["required_end_city_ids"] == ["11", "15"]
    assert passenger_calls[0]["reposition_to_route"] is False
    assert passenger_calls[0]["trade_during_trip"] is False
    assert trade_inputs == original_trade
    assert passenger_inputs == original_passenger


def test_passenger_first_preflights_then_replans_with_actual_remaining_budget(monkeypatch):
    preview_calls: list[dict] = []
    trade_calls: list[dict] = []
    passenger_calls: list[dict] = []
    monkeypatch.setattr(
        combined,
        "_read_current_city",
        lambda *_args: {"city_key": "shoggolith_city", "city_name": "修格里城"},
    )

    async def preview(**kwargs):
        preview_calls.append(dict(kwargs))
        return {"success": True, "status": "ok", "route": [{"to_city_id": "1"}]}

    async def run_passenger(inputs, **_kwargs):
        passenger_calls.append(dict(inputs))
        return _passenger_result("15", fatigue=96)

    async def run_trade(inputs, **_kwargs):
        trade_calls.append(dict(inputs))
        return _trade_result("1", fatigue=500)

    monkeypatch.setattr(combined, "_preview_trade_plan_from_start_city", preview)
    monkeypatch.setattr(combined, "_run_passenger", run_passenger)
    monkeypatch.setattr(combined, "_run_trade", run_trade)

    result = _run(order="passenger_first")

    assert result["success"] is True
    assert result["preflight"]["passenger_forecast"]["reposition_fatigue"] == 20
    assert preview_calls[0]["start_city_id"] == "15"
    assert preview_calls[0]["fatigue_budget"] == 604
    assert preview_calls[0]["reporter"] is None
    assert passenger_calls[0]["trade_during_trip"] is False
    assert trade_calls[0]["fatigue_budget"] == 604
    assert "required_end_city_ids" not in trade_calls[0]


def test_passenger_first_no_preflight_route_does_not_start_passenger(monkeypatch):
    passenger_calls: list[dict] = []
    monkeypatch.setattr(
        combined,
        "_read_current_city",
        lambda *_args: {"city_key": "cape_city", "city_name": "海角城"},
    )

    async def preview(**_kwargs):
        return {"success": True, "status": "no_plan", "reason": "no profitable route", "route": []}

    async def run_passenger(inputs, **_kwargs):
        passenger_calls.append(dict(inputs))
        return _passenger_result()

    monkeypatch.setattr(combined, "_preview_trade_plan_from_start_city", preview)
    monkeypatch.setattr(combined, "_run_passenger", run_passenger)

    result = _run(order="passenger_first")

    assert result["success"] is False
    assert result["reason"] == "trade_preflight_no_plan"
    assert result["failure_stage"] == "preflight"
    assert passenger_calls == []


def test_passenger_first_post_run_no_plan_is_not_reported_as_success(monkeypatch):
    monkeypatch.setattr(
        combined,
        "_read_current_city",
        lambda *_args: {"city_key": "cape_city", "city_name": "海角城"},
    )

    async def preview(**_kwargs):
        return {"success": True, "status": "ok", "route": [{"to_city_id": "1"}]}

    async def run_passenger(_inputs, **_kwargs):
        return _passenger_result("15", fatigue=76)

    async def run_trade(_inputs, **_kwargs):
        return {
            "success": True,
            "status": "no_plan",
            "reason": "market changed",
            "route": [],
            "expected_fatigue_used": 0,
            "page_state": "city_main",
        }

    monkeypatch.setattr(combined, "_preview_trade_plan_from_start_city", preview)
    monkeypatch.setattr(combined, "_run_passenger", run_passenger)
    monkeypatch.setattr(combined, "_run_trade", run_trade)

    result = _run(order="passenger_first")

    assert result["success"] is False
    assert result["reason"] == "post_passenger_trade_no_plan"
    assert result["passenger"]["status"] == "completed"


def test_endpoint_and_zero_trade_budget_fail_before_child_tasks(monkeypatch):
    calls: list[str] = []

    async def run_trade(_inputs, **_kwargs):
        calls.append("trade")
        return _trade_result()

    async def run_passenger(_inputs, **_kwargs):
        calls.append("passenger")
        return _passenger_result()

    monkeypatch.setattr(combined, "_run_trade", run_trade)
    monkeypatch.setattr(combined, "_run_passenger", run_passenger)

    unavailable = _run(trade_inputs={"available_city_ids": ["1", "11"]})
    insufficient = _run(total_fatigue_budget=76)

    assert unavailable["reason"] == "passenger_endpoint_unavailable"
    assert insufficient["reason"] == "insufficient_trade_fatigue"
    assert calls == []


def test_trade_handoff_failure_stops_passenger_and_preserves_consumed_fatigue(monkeypatch):
    passenger_calls: list[dict] = []

    async def run_trade(_inputs, **_kwargs):
        return _trade_result("1", fatigue=120)

    async def run_passenger(inputs, **_kwargs):
        passenger_calls.append(dict(inputs))
        return _passenger_result()

    monkeypatch.setattr(combined, "_run_trade", run_trade)
    monkeypatch.setattr(combined, "_run_passenger", run_passenger)

    result = _run()

    assert result["reason"] == "trade_handoff_invalid"
    assert result["failure_stage"] == "trade_handoff"
    assert result["expected_fatigue_used"] == 120
    assert result["remaining_fatigue"] == 580
    assert passenger_calls == []


def test_passenger_forecast_mismatch_stops_trade(monkeypatch):
    trade_calls: list[dict] = []
    monkeypatch.setattr(
        combined,
        "_read_current_city",
        lambda *_args: {"city_key": "cape_city", "city_name": "海角城"},
    )

    async def preview(**_kwargs):
        return {"success": True, "status": "ok", "route": [{"to_city_id": "1"}]}

    async def run_passenger(_inputs, **_kwargs):
        return _passenger_result("15", fatigue=75)

    async def run_trade(inputs, **_kwargs):
        trade_calls.append(dict(inputs))
        return _trade_result()

    monkeypatch.setattr(combined, "_preview_trade_plan_from_start_city", preview)
    monkeypatch.setattr(combined, "_run_passenger", run_passenger)
    monkeypatch.setattr(combined, "_run_trade", run_trade)

    result = _run(order="passenger_first")

    assert result["reason"] == "passenger_forecast_mismatch"
    assert result["failure_stage"] == "passenger_handoff"
    assert result["expected_fatigue_used"] == 75
    assert trade_calls == []


def test_passenger_forecast_accounts_for_reposition_and_even_trip_parity():
    forecast = combined._passenger_forecast(
        passenger_inputs={
            "passenger_city_a_id": "11",
            "passenger_city_b_id": "15",
            "trip_count": 2,
            "reposition_to_route": True,
        },
        current={"city_key": "shoggolith_city", "city_name": "修格里城"},
        city_shop_data=_CityData(),
        market_data=_Market(),
    )

    assert forecast["start_city"]["city_id"] == "11"
    assert forecast["end_city"]["city_id"] == "11"
    assert forecast["reposition_fatigue"] == 20
    assert forecast["route_fatigue"] == 152
    assert forecast["expected_fatigue"] == 172


def test_same_passenger_city_is_rejected_before_child_tasks(monkeypatch):
    calls: list[str] = []

    async def run_trade(_inputs, **_kwargs):
        calls.append("trade")
        return _trade_result()

    monkeypatch.setattr(combined, "_run_trade", run_trade)

    result = _run(
        passenger_inputs={
            "passenger_city_a_id": "11",
            "passenger_city_b_id": "11",
            "trip_count": 1,
        }
    )

    assert result["reason"] == "passenger_route_invalid"
    assert result["failure_stage"] == "preflight"
    assert calls == []
