"""Towing route allocation is offline; daily quota is not a planning input."""

from copy import deepcopy
import json

import pytest

from plans.resonance_pc.src.actions import _freight_trailer_policy as m


def legs():
    return [{"from_city_id": a, "to_city_id": b} for a, b in [("1", "2"), ("2", "3"),
                                                                 ("3", "2"), ("2", "1")]]


def distances():
    return {"1": {"2": 40}, "2": {"1": 40, "3": 200}, "3": {"2": 200}}


def skip(reason="daily_limit_reached", **changes):
    return {"success": True, "status": "not_used", "submitted": False, "departed": False,
            "page_state": "map", "reason": reason, **changes}


def test_longest_segments_first_and_equal_distances_keep_earlier_occurrence():
    plan = m.FreightTrailerPlan(enabled=True, daily_first_n=1, route=legs(), distances=distances())
    assert [i for i in range(4) if plan.should_use("route", i)] == [1]
    plan = m.FreightTrailerPlan(enabled=True, daily_first_n=3, route=legs(), distances=distances())
    assert [i for i in range(4) if plan.should_use("route", i)] == [0, 1, 2]
    assert [row["distance_km"] for row in plan.snapshot()["selected_legs"]] == [40, 200, 200]


def test_selection_uses_game_distances_not_fatigue_or_negotiation():
    route = legs()
    route[0].update(travel_fatigue=9999, expected_negotiation_fatigue=9999)
    plan = m.FreightTrailerPlan(enabled=True, daily_first_n=1, route=route, distances=distances())
    assert plan.should_use("route", 1) and not plan.should_use("route", 0)


def test_reposition_and_trade_share_one_allocation_in_chronological_tie_order():
    route = legs()[1:]
    reposition = legs()[1:2]
    plan = m.FreightTrailerPlan(enabled=True, daily_first_n=1, route=route, reposition=reposition,
                               distances=distances())
    assert plan.should_use("reposition", 0)
    assert not any(plan.should_use("route", i) for i in range(3))


@pytest.mark.parametrize("reason", ["daily_limit_reached", "quota_exhausted"])
def test_first_verified_quota_skip_disables_whole_run_without_reallocation(reason):
    plan = m.FreightTrailerPlan(enabled=True, daily_first_n=10, route=legs(), distances=distances())
    plan.observe("route", 0, {"trailer_service": skip(reason)})
    assert plan.snapshot()["disabled_reason"] == reason
    assert not any(plan.should_use("route", i) for i in range(4))
    assert len(plan.snapshot()["selected_legs"]) == 4
    assert not plan.should_use("reposition", 0)


@pytest.mark.parametrize("changes", [{"submitted": True}, {"departed": True}, {"success": False},
                                     {"status": "blocked"}, {"page_state": "unknown"},
                                     {"reason": "trailer_departure_unconfirmed"}])
def test_unknown_or_untrusted_results_do_not_authorize_fallback(changes):
    result = skip(**changes)
    assert not m.verified_quota_skip(result)


def test_disabled_and_empty_plans_do_not_load_distance_data(monkeypatch):
    monkeypatch.setattr(m, "_distances", lambda: pytest.fail("Disabled towing must not read any distance data"))
    plan = m.FreightTrailerPlan(route=legs())
    assert plan.snapshot()["selected_legs"] == [] and not plan.should_use("route", 0)


def test_large_x_is_capped_by_number_of_planned_legs_not_game_quota():
    plan = m.FreightTrailerPlan(enabled=True, daily_first_n=100, route=legs(), distances=distances())
    assert all(plan.should_use("route", i) for i in range(4))


def test_same_city_pair_later_occurrence_is_a_separate_candidate():
    plan = m.FreightTrailerPlan(enabled=True, daily_first_n=2, route=legs() + legs(), distances=distances())
    assert [i for i in range(8) if plan.should_use("route", i)] == [1, 2]


def test_caller_route_and_snapshot_cannot_mutate_selection():
    route = legs()
    original = deepcopy(route)
    plan = m.FreightTrailerPlan(enabled=True, route=route, distances=distances())
    plan.snapshot()["selected_legs"].clear()
    assert route == original and plan.should_use("route", 1)


@pytest.mark.parametrize("value", [True, 0, -1, "1", 1.0, None])
def test_invalid_limits_are_rejected(value):
    with pytest.raises(ValueError):
        m.FreightTrailerPlan(daily_first_n=value)


@pytest.mark.parametrize("value", [True, 0, -1, float("nan"), float("inf"), "100"])
def test_bad_distance_never_falls_back_to_fatigue(value):
    table = distances()
    table["1"]["2"] = value
    with pytest.raises(ValueError, match="Invalid towing distance"):
        m.FreightTrailerPlan(enabled=True, route=legs(), distances=table)


def test_missing_distance_is_explicit_error():
    with pytest.raises(ValueError, match="Missing towing distance"):
        m.FreightTrailerPlan(enabled=True, route=legs(), distances={})


def test_packaged_native_catalog_drives_longest_selection_without_runtime_services():
    catalog = json.loads(m.DISTANCES_FILE.read_text(encoding="utf-8"))
    route = [{"from_city_id": origin, "to_city_id": destination}
             for origin in catalog["cities"] for destination in catalog["cities"] if origin != destination]
    plan = m.FreightTrailerPlan(enabled=True, daily_first_n=3, route=route)
    expected = sorted(range(len(route)), key=lambda index:
                      (-catalog["distances_km"][route[index]["from_city_id"]][route[index]["to_city_id"]], index))[:3]
    assert [index for index in range(len(route)) if plan.should_use("route", index)] == sorted(expected)
    assert plan.snapshot()["attempts"] == []
