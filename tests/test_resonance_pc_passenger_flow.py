from __future__ import annotations

import pytest

import plans.resonance_pc.src.actions.passenger_flow_pc_actions as flow


class _Market:
    def get_travel_fatigue(self, from_city_id: str, to_city_id: str) -> int:
        return 76 if {str(from_city_id), str(to_city_id)} == {"11", "15"} else 40


def _install_happy_path(monkeypatch, start_key: str):
    destinations: list[str] = []
    monkeypatch.setattr(flow, "resonance_pc_open_passenger_management", lambda **_kwargs: {"success": True})
    monkeypatch.setattr(
        flow,
        "probe_passenger_load_from_score",
        lambda **_kwargs: {"current_passengers": 0, "seat_capacity": 64, "flyers_available": 475},
    )
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


def test_outside_route_repositions_to_cape_before_recruitment(monkeypatch):
    destinations = _install_happy_path(monkeypatch, "shoggolith_city")

    result = _run()

    assert result["success"] is True
    assert result["reposition_leg"]["to_city"] == "海角城"
    assert result["expected_fatigue_used"] == 192
    assert destinations[0] == "travel:海角城"
    assert destinations[1:] == [
        "recruit:岚心城",
        "travel:岚心城",
        "recruit:海角城",
        "travel:海角城",
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


def test_preloaded_passengers_block_before_city_or_travel(monkeypatch):
    monkeypatch.setattr(flow, "resonance_pc_open_passenger_management", lambda **_kwargs: {"success": True})
    monkeypatch.setattr(
        flow,
        "probe_passenger_load_from_score",
        lambda **_kwargs: {"current_passengers": 2, "seat_capacity": 64},
    )
    monkeypatch.setattr(flow, "_read_current_city", lambda *_args, **_kwargs: pytest.fail("must not read city"))

    result = _run()

    assert result["status"] == "blocked"
    assert result["reason"] == "preloaded_passengers_unsupported"
