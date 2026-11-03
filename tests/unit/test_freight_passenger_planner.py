"""Passenger allocation over fixed freight visits, with an exhaustive oracle."""

from copy import deepcopy
from datetime import datetime, timezone
import itertools

import pytest

from packages.resonance_shared.passenger_parameters import (
    DEFAULT_PASSENGER_OPTIONS, normalize_passenger_options, resolve_flyer_target,
)
from plans.resonance_pc.src.actions import _freight_passenger_planner as planner


def route(*cities):
    return [{"from_city_id": a, "to_city_id": b, "from_city": a, "to_city": b,
             "buy_products": ["preserve"], "books_used": 1} for a, b in zip(cities, cities[1:])]


@pytest.fixture
def simple_model(monkeypatch):
    ids = ("A", "B", "C", "D")
    model = {
        "model_version": "test", "distance_ratio": .02, "flyer_batch": 10,
        "ticket_distance": 5000, "seat_prices": [100, 150, 200, 250, 300],
        "seat_ids": [1, 2, 3, 4, 5], "clean_block_bps": 3000, "clean_good_bps": 8000,
        "demand_basics": .3, "demand_out": 1.5, "grades": [{"upwin": 0}],
        "distance_buckets": [{"distance": 5000, "adPassageMin": 16, "adPassageMax": 16, "earning": 1}],
        "cities": {i: {"name": i, "leaflet_enabled": True, "leaflet_unlock": 0,
                        "spots": [{"id": 1, "order": 1, "unlock_prestige": 0,
                                                "pool": [[1, 1]], "tags": []}]} for i in ids},
        "passengers": {"1": {"seat_required": False, "seat_demand": [], "pay_num": 64,
                                "tags": [], "demand": {}}}, "tags": {},
        "raw_distances": {a: {b: abs(ids.index(a) - ids.index(b)) * 5000 for b in ids} for a in ids},
    }
    monkeypatch.setattr(planner, "_load_model", lambda: model)
    return model


def test_options_are_strict_and_do_not_mutate_shared_defaults():
    original = deepcopy(DEFAULT_PASSENGER_OPTIONS)
    value = normalize_passenger_options({"seat_capacity": 64, "scores": {"comfort": 12}})
    assert value["scores"]["comfort"] == 12
    assert value["scores"]["foodScores"] == 7000
    value["scores"]["foodScores"] = 1
    assert DEFAULT_PASSENGER_OPTIONS == original
    assert normalize_passenger_options({}, enabled=False)["seat_capacity"] is None
    with pytest.raises(ValueError, match="seat_capacity"):
        normalize_passenger_options({})


@pytest.mark.parametrize("changes", [
    {"seat_capacity": True}, {"seat_capacity": 0}, {"flyer_target": "10"},
    {"flyer_target": 1.0}, {"transport_level": 101}, {"seat_level": 0},
    {"scores": {"comfort": False}}, {"scores": {"invented": 1}},
    {"furniture_bonus_bps": -1}, {"cleanliness_bps": 10001},
    {"news": "random"}, {"invented": 3},
])
def test_invalid_parameters_are_rejected(changes):
    with pytest.raises(ValueError):
        normalize_passenger_options({"seat_capacity": 64, **changes})


def test_server_day_boundary_is_utc_plus_eight_at_five():
    options = {"transport_level": 30}
    # Saturday 04:59 server time is still the Friday allowance.
    assert resolve_flyer_target(options, datetime(2026, 11, 6, 20, 59, tzinfo=timezone.utc)) == 80
    assert resolve_flyer_target(options, datetime(2026, 11, 6, 21, 0, tzinfo=timezone.utc)) == 130
    # Monday before five remains Sunday's allowance; naive means server time.
    assert resolve_flyer_target(options, datetime(2026, 11, 9, 4, 59)) == 130
    assert resolve_flyer_target(options, datetime(2026, 11, 9, 5, 0)) == 80
    assert resolve_flyer_target({"flyer_target": 7}, datetime(2026, 11, 9)) == 7


def test_whole_maximum_fill_can_overshoot_target_by_an_entire_car(simple_model):
    plan = planner.build_passenger_plan(route("A", "B"), {"seat_capacity": 512, "flyer_target": 1}, inventory=450)
    assert plan["expected_flyers_used"] == 320
    assert plan["trips"][0]["expected_passengers"] == 512
    assert plan["stop_reason"] == "target_completed"
    assert len(plan["trips"]) == 1


def test_no_more_recruitment_after_target_even_when_later_trips_pay_more(simple_model):
    plan = planner.build_passenger_plan(route("A", "B", "C", "D"), {"seat_capacity": 16, "flyer_target": 1}, inventory=450)
    assert len(plan["trips"]) == 1
    assert (plan["trips"][0]["from_index"], plan["trips"][0]["to_index"]) == (0, 3)
    assert plan["expected_flyers_used"] == 10


def test_exhausting_target_has_priority_over_one_high_revenue_interval(simple_model):
    plan = planner.build_passenger_plan(route("A", "B", "C", "D"), {"seat_capacity": 16, "flyer_target": 30}, inventory=100)
    assert [(t["from_index"], t["to_index"]) for t in plan["trips"]] == [(0, 1), (1, 2), (2, 3)]
    assert plan["expected_flyers_used"] == 30


def test_short_route_reports_remaining_target_without_adding_travel(simple_model):
    source = route("A", "B")
    original = deepcopy(source)
    plan = planner.build_passenger_plan(source, {"seat_capacity": 16, "flyer_target": 100}, inventory=450)
    assert plan["expected_flyers_used"] == 10
    assert plan["stop_reason"] == "route_exhausted"
    assert source == original


def test_repeated_destination_only_has_its_first_future_arrival(simple_model):
    source = route("A", "B", "C", "B", "D")
    opts = normalize_passenger_options({"seat_capacity": 16})
    visits = planner._visits(source, simple_model)
    candidates = planner._trip_candidates(visits, opts, None, 0, simple_model)
    assert [(t["to_city_id"], t["to_index"]) for t in candidates[0]] == [("B", 1), ("C", 2), ("D", 4)]
    assert [(t["to_city_id"], t["to_index"]) for t in candidates[1]] == [("C", 2), ("D", 4)]


def test_runtime_replan_uses_only_future_visits_and_actual_remaining_inventory(simple_model):
    plan = planner.build_passenger_plan(route("A", "B", "C", "D"), {"seat_capacity": 64},
                                        inventory=29, target_remaining=100, start_index=1)
    assert plan["target"] == 100
    assert plan["expected_flyers_used"] == 20
    assert plan["stop_reason"] == "inventory_exhausted"
    assert all(t["from_index"] >= 1 for t in plan["trips"])


def test_preview_inventory_is_explicit_target_assumption(simple_model):
    plan = planner.build_passenger_plan(route("A", "B"), {"seat_capacity": 512, "flyer_target": 1})
    assert plan["inventory"] == 1
    assert plan["trips"] == []
    assert plan["stop_reason"] == "inventory_below_batch"
    assert any("Preview inventory" in text for text in plan["assumptions"])


def test_cleanliness_and_unlock_restrictions_prevent_planning(simple_model):
    source = route("A", "B")
    assert planner.build_passenger_plan(source, {"seat_capacity": 64, "cleanliness_bps": 2999})["stop_reason"] == "cleanliness_too_low"
    simple_model["cities"]["A"]["spots"][0]["unlock_prestige"] = 10
    assert planner.build_passenger_plan(source, {"seat_capacity": 64}, city_prestige={"default": 9})["stop_reason"] == "no_eligible_trips"
    assert planner.build_passenger_plan(source, {"seat_capacity": 64}, city_prestige={"default": 9, "overrides": {"A": 10}})["trips"]


def test_revenue_uses_weighted_passenger_mix_and_separate_ticket_extra(simple_model):
    first = simple_model["passengers"]["1"]
    first["demand"] = {"comfort": [{"common": 0, "most": 100, "out": -1, "pay": 100}]}
    second = deepcopy(first)
    second["demand"]["comfort"][0]["pay"] = 300
    simple_model["passengers"]["2"] = second
    simple_model["cities"]["A"]["spots"][0]["pool"] = [[1, 1], [2, 3]]
    plan = planner.build_passenger_plan(route("A", "B"),
                                        {"seat_capacity": 16, "flyer_target": 10, "furniture_bonus_bps": 1000})
    trip = plan["trips"][0]
    assert trip["expected_ticket_revenue"] == 16 * 300
    assert trip["expected_extra_revenue"] == 16 * 250 * 1.1
    assert trip["expected_revenue"] == 9200


def test_crowding_and_ideal_headcount_do_not_change_first_version_revenue(simple_model):
    simple_model["passengers"]["1"]["demand"] = {
        "comfort": [{"common": 0, "most": 100, "out": -1, "pay": 100}]}
    options = {"seat_capacity": 64, "flyer_target": 10}
    before = planner.build_passenger_plan(route("A", "B"), options, inventory=100)
    simple_model["passengers"]["1"]["pay_num"] = 1
    after = planner.build_passenger_plan(route("A", "B"), options, inventory=100)
    assert before["expected_revenue"] == after["expected_revenue"]
    assert after["trips"][0]["expected_extra_revenue"] == 6400


def test_destination_must_support_leaflets_and_meet_its_own_prestige_gate(simple_model):
    source = route("A", "B")
    options = {"seat_capacity": 64}
    simple_model["cities"]["B"]["leaflet_enabled"] = False
    assert planner.build_passenger_plan(source, options)["stop_reason"] == "no_eligible_trips"
    simple_model["cities"]["B"]["leaflet_enabled"] = True
    simple_model["cities"]["B"]["leaflet_unlock"] = 10
    assert not planner.build_passenger_plan(source, options, city_prestige={"default": 9})["trips"]
    assert planner.build_passenger_plan(source, options,
        city_prestige={"default": 9, "overrides": {"B": 10}})["trips"]
    simple_model["cities"]["A"]["leaflet_enabled"] = False
    assert not planner.build_passenger_plan(source, options)["trips"]


def test_grade_uses_highest_qualified_tier_once_and_news_cannot_silently_apply(simple_model):
    simple_model["grades"] = [{"comfort": 0, "upwin": 0},
                               {"comfort": 100, "upwin": .1}, {"comfort": 200, "upwin": .2}]
    simple_model["passengers"]["1"]["demand"] = {
        "comfort": [{"common": 0, "most": 100, "out": -1, "pay": 100}]}
    trip = planner.build_passenger_plan(route("A", "B"),
                                        {"seat_capacity": 16, "flyer_target": 10})["trips"][0]
    assert trip["expected_extra_revenue"] == 1920


def test_zero_remaining_target_and_empty_route_are_valid(simple_model):
    assert planner.build_passenger_plan([], {"seat_capacity": 64}, target_remaining=0)["stop_reason"] == "target_completed"
    assert planner.build_passenger_plan([], {"seat_capacity": 64})["trips"] == []


def test_ties_choose_earlier_indices_deterministically(simple_model):
    for row in simple_model["raw_distances"].values():
        for key in row:
            row[key] = 5000
    plan = planner.build_passenger_plan(route("A", "B", "C", "D"), {"seat_capacity": 16, "flyer_target": 1})
    # Preview stock of one cannot recruit; use actual balance for the tie case.
    assert not plan["trips"]
    plan = planner.build_passenger_plan(route("A", "B", "C", "D"), {"seat_capacity": 16, "flyer_target": 1}, inventory=100)
    assert [(t["from_index"], t["to_index"]) for t in plan["trips"]] == [(0, 1)]


def test_dynamic_program_matches_exhaustive_interval_oracle(simple_model):
    source = route("A", "B", "C", "B", "D")
    for capacity, target, inventory in itertools.product((16, 24, 40), (1, 20, 35, 100), (20, 50, 100)):
        opts = normalize_passenger_options({"seat_capacity": capacity, "flyer_target": target})
        candidates = planner._trip_candidates(planner._visits(source, simple_model), opts, None, 0, simple_model)
        outcomes = []

        def enumerate_plans(index, spent, trips, revenue):
            outcomes.append((max(0, target - spent), -round(revenue, 6), len(trips),
                             tuple((t["from_index"], t["to_index"]) for t in trips), spent))
            if spent >= target or inventory - spent < 10:
                return
            for i in range(index, len(source)):
                for candidate in candidates.get(i, []):
                    trip = planner._full_trip(candidate, inventory - spent, opts, simple_model)
                    enumerate_plans(trip["to_index"], spent + trip["expected_flyers_used"],
                                    trips + [trip], round(revenue + trip["expected_revenue"], 6))

        enumerate_plans(0, 0, [], 0.0)
        optimum = min(outcomes)
        actual = planner.build_passenger_plan(source, opts, inventory=inventory)
        assert (max(0, target - actual["expected_flyers_used"]), -actual["expected_revenue"],
                len(actual["trips"]), tuple((t["from_index"], t["to_index"]) for t in actual["trips"]),
                actual["expected_flyers_used"]) == optimum


def test_native_catalog_is_self_contained_and_produces_a_bounded_plan():
    planner._load_model.cache_clear()
    source = route("11", "15", "13", "11")
    opts = {"seat_capacity": 64, "flyer_target": 150}
    before = deepcopy(source)
    result = planner.build_passenger_plan(source, opts, inventory=200)
    assert result["model_version"] == "cn-passenger-approx-v1"
    assert result["trips"]
    assert 0 < result["expected_flyers_used"] <= 200
    assert result["expected_revenue"] > 0
    assert all(t["expected_passengers"] <= 64 for t in result["trips"])
    assert source == before
