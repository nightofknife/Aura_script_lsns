from __future__ import annotations

import pytest

import plans.resonance_pc.src.actions.passenger_flow_pc_actions as flow


class _Market:
    def get_travel_fatigue(self, from_city_id: str, to_city_id: str) -> int:
        return 76 if {str(from_city_id), str(to_city_id)} == {"11", "15"} else 40


class _LanxinIsCloserMarket(_Market):
    def get_travel_fatigue(self, from_city_id: str, to_city_id: str) -> int:
        if str(from_city_id) == "1":
            return {"11": 65, "15": 25}[str(to_city_id)]
        return super().get_travel_fatigue(from_city_id, to_city_id)


def _install_happy_path(monkeypatch, start_key: str):
    destinations: list[str] = []
    monkeypatch.setattr(
        flow,
        "_read_current_city",
        lambda *_args, **_kwargs: {"city_key": start_key, "city_name": "起点"},
    )

    def recruit(*, to_city_name: str, **_kwargs):
        destinations.append(f"recruit:{to_city_name}")
        return {
            "success": True,
            "recruited_passengers": 35,
            "seat_capacity": 64,
            "flyers_used": 60,
        }

    def travel(*, to_city_name: str, **_kwargs):
        destinations.append(f"travel:{to_city_name}")
        return {"success": True, "fatigue_medicine_used": []}

    monkeypatch.setattr(flow, "resonance_pc_recruit_passengers_by_flyer", recruit)
    monkeypatch.setattr(flow, "resonance_pc_intercity_depart_and_wait", travel)
    monkeypatch.setattr(
        flow,
        "resonance_pc_enter_city_and_settle_passengers",
        lambda **_kwargs: {
            "success": True,
            "ticket_revenue": 100,
            "extra_revenue": 20,
            "total_revenue": 120,
        },
    )
    return destinations


def _run(**overrides):
    values = {
        "round_trips": 1,
        "trade_during_trip": False,
        "reposition_to_route": True,
        "preferred_start_city_id": "11",
        "use_fatigue_medicine": False,
        "allowed_fatigue_medicines": [],
        "fatigue_medicine_max_uses": 4,
        "arrival_timeout_seconds": 1800.0,
        "app": object(),
        "ocr": object(),
        "vision": object(),
        "controller": object(),
        "city_shop_data": object(),
        "market_data": _Market(),
        "trade_planner": None,
    }
    values.update(overrides)
    return flow._run_passenger_roundtrip_sync(**values)


@pytest.mark.parametrize(
    ("start_key", "expected"),
    [
        ("cape_city", ["recruit:岚心城", "travel:岚心城", "recruit:海角城", "travel:海角城"]),
        ("lanxin_city", ["recruit:海角城", "travel:海角城", "recruit:岚心城", "travel:岚心城"]),
    ],
)
def test_round_trip_starts_from_current_route_endpoint(monkeypatch, start_key, expected):
    destinations = _install_happy_path(monkeypatch, start_key)

    result = _run()

    assert result["success"] is True
    assert result["completed_round_trips"] == 1
    assert result["expected_fatigue_used"] == 152
    assert result["recruited_passengers"] == 70
    assert result["flyers_used"] == 120
    assert result["total_revenue"] == 240
    assert destinations == expected


def test_outside_route_uses_preferred_endpoint_when_fatigue_is_equal(monkeypatch):
    destinations = _install_happy_path(monkeypatch, "shoggolith_city")

    result = _run()

    assert result["success"] is True
    assert result["reposition_leg"]["to_city"] == "海角城"
    assert result["reposition_leg"]["endpoint_fatigue"] == {"海角城": 40, "岚心城": 40}
    assert result["expected_fatigue_used"] == 192
    assert destinations[0] == "travel:海角城"
    assert destinations[1:] == [
        "recruit:岚心城",
        "travel:岚心城",
        "recruit:海角城",
        "travel:海角城",
    ]


def test_outside_route_repositions_to_endpoint_with_lower_fatigue(monkeypatch):
    destinations = _install_happy_path(monkeypatch, "shoggolith_city")

    result = _run(market_data=_LanxinIsCloserMarket())

    assert result["success"] is True
    assert result["reposition_leg"]["to_city"] == "岚心城"
    assert result["reposition_leg"]["expected_fatigue"] == 25
    assert result["reposition_leg"]["endpoint_fatigue"] == {"海角城": 65, "岚心城": 25}
    assert result["expected_fatigue_used"] == 177
    assert destinations == [
        "travel:岚心城",
        "recruit:海角城",
        "travel:海角城",
        "recruit:岚心城",
        "travel:岚心城",
    ]


def test_travel_block_after_recruitment_requires_manual_completion(monkeypatch):
    _install_happy_path(monkeypatch, "cape_city")
    monkeypatch.setattr(
        flow,
        "resonance_pc_intercity_depart_and_wait",
        lambda **_kwargs: {"success": False, "status": "blocked", "reason": "fatigue_recovery_required"},
    )

    result = _run()

    assert result["status"] == "blocked"
    assert result["reason"] == "fatigue_recovery_required"
    assert result["failure_stage"] == "travel"
    assert result["requires_manual_completion"] is True
    assert result["loaded_destination"]["city_name"] == "岚心城"
    assert result["completed_legs"] == []


def test_passenger_travel_uses_shared_station_template_wait(monkeypatch):
    _install_happy_path(monkeypatch, "cape_city")
    travel_calls = []

    def travel(**kwargs):
        travel_calls.append(kwargs)
        return {"success": True, "fatigue_medicine_used": []}

    monkeypatch.setattr(flow, "resonance_pc_intercity_depart_and_wait", travel)

    result = _run()

    assert result["success"] is True
    assert len(travel_calls) == 2
    assert all(call["enter_station_timeout_seconds"] == 1800.0 for call in travel_calls)


def test_arrival_timeout_after_recruitment_is_structured_manual_block(monkeypatch):
    _install_happy_path(monkeypatch, "cape_city")

    def travel(**_kwargs):
        raise flow.IntercityDestinationError(
            code="arrival_timeout",
            message="station prompt did not appear",
            detail={"timeout_sec": 1800},
        )

    monkeypatch.setattr(flow, "resonance_pc_intercity_depart_and_wait", travel)

    result = _run()

    assert result["status"] == "blocked"
    assert result["reason"] == "arrival_timeout"
    assert result["failure_stage"] == "travel"
    assert result["requires_manual_completion"] is True
    assert result["loaded_destination"]["city_name"] == "岚心城"


def test_outside_route_stops_before_reposition_when_switch_is_off(monkeypatch):
    destinations = _install_happy_path(monkeypatch, "shoggolith_city")

    result = _run(reposition_to_route=False)

    assert result["status"] == "blocked"
    assert result["reason"] == "outside_passenger_route"
    assert destinations == []


def test_trade_runs_before_each_recruitment_and_final_arrival_is_sell_only(monkeypatch):
    events = _install_happy_path(monkeypatch, "cape_city")

    def trade_at_city(*, current_city_id, destination_city_id, final_sale, **_kwargs):
        current = flow._ROUTE_BY_ID[str(current_city_id)]["city_name"]
        destination = (
            flow._ROUTE_BY_ID[str(destination_city_id)]["city_name"]
            if destination_city_id is not None
            else None
        )
        events.append(f"trade:{current}:{destination}:{final_sale}")
        return {
            "success": True,
            "final_sale": final_sale,
            "buy_products": [] if final_sale else ["盈利商品"],
            "plan": None if final_sale else {"expected_profit": 100.0, "reason": None},
        }

    monkeypatch.setattr(flow, "_execute_passenger_trade_at_city", trade_at_city)

    result = _run(trade_during_trip=True, trade_planner=object())

    assert result["success"] is True
    assert result["trade_expected_profit"] == 200.0
    assert len(result["trade_legs"]) == 2
    assert result["trade_final_sale"]["final_sale"] is True
    assert events == [
        "trade:海角城:岚心城:False",
        "recruit:岚心城",
        "travel:岚心城",
        "trade:岚心城:海角城:False",
        "recruit:海角城",
        "travel:海角城",
        "trade:海角城:None:True",
    ]


def test_trade_planner_rejects_stale_refresh_without_using_planner(monkeypatch):
    monkeypatch.setattr(
        flow,
        "resonance_pc_market_refresh",
        lambda **_kwargs: {"snapshot_id": "old", "stale": True, "stale_reason": "offline"},
    )
    monkeypatch.setattr(
        flow,
        "resonance_pc_trade_plan_optimal_route",
        lambda **_kwargs: pytest.fail("stale market must not be planned"),
    )

    plan = flow._prepare_passenger_trade_plan(
        source_city_id="11",
        destination_city_id="15",
        market_data=_Market(),
        trade_planner=object(),
    )

    assert plan["status"] == "skip_buy"
    assert plan["reason"] == "stale_market_rejected"
    assert plan["buy_products"] == []


def test_trade_planner_accepts_only_positive_fixed_direction(monkeypatch):
    refresh_calls = []
    planner_calls = []

    def refresh(**kwargs):
        refresh_calls.append(kwargs)
        return {"snapshot_id": "fresh", "stale": False}

    def plan(**kwargs):
        planner_calls.append(kwargs)
        return {
            "status": "ok",
            "expected_profit": 1234.0,
            "route": [
                {
                    "from_city_id": "11",
                    "to_city_id": "15",
                    "buy_products": ["商品甲", "商品乙"],
                    "expected_profit": 1234.0,
                }
            ],
        }

    monkeypatch.setattr(flow, "resonance_pc_market_refresh", refresh)
    monkeypatch.setattr(flow, "resonance_pc_trade_plan_optimal_route", plan)

    result = flow._prepare_passenger_trade_plan(
        source_city_id="11",
        destination_city_id="15",
        market_data=_Market(),
        trade_planner=object(),
    )

    assert result["status"] == "planned"
    assert result["buy_products"] == ["商品甲", "商品乙"]
    assert result["expected_profit"] == 1234.0
    assert refresh_calls[0]["force"] is True
    assert planner_calls[0]["available_city_ids"] == ["11", "15"]
    assert planner_calls[0]["book_budget"] == 0
    assert planner_calls[0]["negotiation_budget"] == 0


def test_trade_planner_route_mismatch_becomes_sell_only(monkeypatch):
    monkeypatch.setattr(
        flow,
        "resonance_pc_market_refresh",
        lambda **_kwargs: {"snapshot_id": "fresh", "stale": False},
    )
    monkeypatch.setattr(
        flow,
        "resonance_pc_trade_plan_optimal_route",
        lambda **_kwargs: {
            "status": "ok",
            "expected_profit": 999,
            "route": [
                {
                    "from_city_id": "11",
                    "to_city_id": "6",
                    "buy_products": ["错误商品"],
                    "expected_profit": 999,
                }
            ],
        },
    )

    result = flow._prepare_passenger_trade_plan(
        source_city_id="11",
        destination_city_id="15",
        market_data=_Market(),
        trade_planner=object(),
    )

    assert result["reason"] == "fixed_route_plan_mismatch"
    assert result["buy_products"] == []


def test_refresh_failure_sells_existing_cargo_but_never_buys(monkeypatch):
    calls = []
    monkeypatch.setattr(
        flow,
        "resonance_pc_market_refresh",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("network down")),
    )
    monkeypatch.setattr(flow, "resonance_pc_open_city_panel_from_main", lambda **_kwargs: None)

    def execute_trade(**kwargs):
        calls.append(kwargs)
        return {"success": True, "page_state": "city_main"}

    monkeypatch.setattr(flow, "_execute_city_trade_inside_current_city", execute_trade)

    result = flow._execute_passenger_trade_at_city(
        current_city_id="11",
        destination_city_id="15",
        final_sale=False,
        app=object(),
        ocr=object(),
        vision=object(),
        controller=object(),
        city_shop_data=object(),
        market_data=_Market(),
        trade_planner=object(),
    )

    assert result["plan"]["reason"] == "market_refresh_failed"
    assert result["buy_products"] == []
    assert calls[0]["buy_products"] == []
    assert calls[0]["books_used"] == 0
    assert calls[0]["sell_raise_to_cap"] is False
    assert calls[0]["buy_bargain_to_cap"] is False
