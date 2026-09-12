"""Offline boundaries and exhaustive small-domain checks for freight recovery."""

from __future__ import annotations

import ast
from copy import deepcopy
from fractions import Fraction
from itertools import permutations, product
import json
from pathlib import Path

import pytest

from plans.resonance_pc.src.actions import _freight_recovery_policy as policy
from plans.resonance_pc.src.actions._bento_consumption_policy import (
    meal_identity, resolve_meal, validate_inventory, validate_meals,
)
from plans.resonance_pc.src.actions._freight_recovery_policy import (
    estimate_remaining_consumption, plan_bento_meals, plan_water_use,
    select_last_water_arrival, validate_bento_priority,
)


ROOT = Path(__file__).resolve().parents[2]
PRIORITY = ["work_meals", "love_bentos"]
SLOTS = ("05:00", "12:00", "18:00")


def leg(origin="A", destination="B", **extra):
    return {
        "from_city_id": origin, "to_city_id": destination,
        "from_city_key": f"key_{origin}", "to_city_key": f"key_{destination}",
        "from_city": f"City {origin}", "to_city": f"City {destination}", **extra,
    }


@pytest.fixture
def catalog():
    return json.loads((ROOT / "plans/resonance_pc/data/meta/love_bento.json").read_text(encoding="utf-8"))


def love(catalog, food_id=83300002, role_id=1, days=1):
    food = next(item for item in catalog["items"] if item["id"] == food_id)
    return {"role_id": role_id, "role_name": f"Role {role_id}", "food_id": food_id,
            "food_name": food["name"], "remaining_days": days}


def inventory(*rows, slots=()):
    return {
        "work_meals": {"available_count": len(slots), "slots": [
            {"issue_time": slot, "available": slot in slots} for slot in SLOTS
        ], "updated_at": "2026-09-12T01:00:00Z"},
        "love_bentos": {"count": len(rows), "items": list(rows),
                        "updated_at": "2026-09-12T01:01:00Z"},
    }


@pytest.mark.parametrize("rest,expected", [
    (set(), None), ({"City A"}, None), ({"City B"}, 1),
    ({"City C"}, 2), ({"City A", "City B", "City C"}, 2),
])
def test_last_arrival_includes_endpoint_but_not_start(rest, expected):
    calls = []

    def lookup(name):
        calls.append(name)
        return name in rest

    result = select_last_water_arrival([leg(), leg("B", "C")], lookup)
    assert result["city_index"] == expected
    assert result["planned"] is (expected is not None)
    assert "City A" not in calls
    if expected:
        city = "B" if expected == 1 else "C"
        assert result == {"planned": True, "reason": "selected", "city_index": expected,
                          "city_id": city, "city_key": f"key_{city}", "city_name": f"City {city}"}
    else:
        assert result["reason"] == "no_eligible_rest_arrival"
        assert all(result[key] is None for key in ("city_id", "city_key", "city_name"))


def test_empty_route_needs_no_lookup_and_never_uses_start():
    assert select_last_water_arrival([], None) == {
        "planned": False, "reason": "no_route", "city_index": None,
        "city_id": None, "city_key": None, "city_name": None,
    }


def test_repeated_city_uses_last_arrival_even_if_earlier_would_have_more_cups():
    route = [leg(), leg("B", "A"), leg(), leg("B", "C")]
    result = select_last_water_arrival(route, lambda city: city == "City B")
    assert (result["city_index"], result["city_id"]) == (3, "B")
    water = plan_water_use(50, 300, 200, 6)
    assert not water["planned"]
    assert result["city_index"] == 3


@pytest.mark.parametrize("answer", [None, 0, 1, [], {}, {"x": 1, "y": 2}, "true"])
def test_rest_lookup_requires_boolean(answer):
    with pytest.raises(ValueError, match="boolean"):
        select_last_water_arrival([leg()], lambda _: answer)


def test_lookup_errors_propagate_without_fallback():
    error = RuntimeError("broken configured lookup")

    def broken(_):
        raise error

    with pytest.raises(RuntimeError) as raised:
        select_last_water_arrival([leg(), leg("B", "C")], broken)
    assert raised.value is error
    with pytest.raises(ValueError, match="callable"):
        select_last_water_arrival([leg()], {})


@pytest.mark.parametrize("route", [None, {}, (), [None], [{}],
    [leg(from_city_id=1)], [leg(to_city_id=" ")], [leg(to_city=None)],
    [leg(), leg("A", "C")], [leg(), leg("B", "C", from_city_key="wrong")],
])
def test_bad_routes_are_not_silent_noops(route):
    with pytest.raises(ValueError):
        select_last_water_arrival(route, lambda _: True)
    with pytest.raises(ValueError):
        estimate_remaining_consumption(route, 1, {})


@pytest.mark.parametrize("missing", [("from_city_key",), ("to_city_key",),
    ("from_city_key", "to_city_key"), ("from_city", "to_city", "from_city_key", "to_city_key")])
def test_optional_route_names_and_keys(missing):
    route = [leg(), leg("B", "C")]
    for row in route:
        for field in missing:
            del row[field]
    result = select_last_water_arrival(route, lambda _: True)
    assert result["city_id"] == "C"
    assert result["city_key"] == ("C" if "to_city_key" in missing else "key_C")
    assert estimate_remaining_consumption(route, 1, {"B": {"C": 24}})["remaining_consumption"] == 24


@pytest.mark.parametrize("rounds,expected", [(0, 0), (1, 24), (2, 40), (3, 64), (4, 80)])
def test_negotiation_formula_rounds_are_aggregated(rounds, expected):
    route = [leg(str(i), str(i + 1), raise_to_cap=(i < rounds)) for i in range(4)]
    costs = {str(i): {str(i + 1): 0} for i in range(1, 4)}
    result = estimate_remaining_consumption(route, 1, costs)
    assert result["negotiation_rounds"] == rounds
    assert result["estimated_negotiation_cost"] == expected
    assert result["remaining_consumption"] == expected


def test_current_arrival_sale_future_buys_and_endpoint_sale_are_counted_once():
    route = [leg(str(i), str(i + 1), bargain_to_cap=True, raise_to_cap=True) for i in range(4)]
    result = estimate_remaining_consumption(route, 2, {"2": {"3": 17}, "3": {"4": 23}})
    assert result == {
        "arrival_index": 2, "remaining_travel_cost": 40, "bargain_rounds": 2,
        "raise_rounds": 3, "negotiation_rounds": 5, "negotiation_steps": 10,
        "estimated_failures": 3, "estimated_negotiation_cost": 104,
        "remaining_consumption": 144,
        "travel_legs": [
            {"leg_index": 2, "from_city_id": "2", "to_city_id": "3", "cost": 17},
            {"leg_index": 3, "from_city_id": "3", "to_city_id": "4", "cost": 23},
        ], "bargain_leg_indices": [2, 3], "raise_leg_indices": [1, 2, 3],
    }


@pytest.mark.parametrize("raise_flag,cost", [(True, 24), (False, 0)])
def test_endpoint_retains_only_final_sale(raise_flag, cost):
    route = [leg(bargain_to_cap=True, raise_to_cap=raise_flag)]
    result = estimate_remaining_consumption(route, 1, {})
    assert result["remaining_travel_cost"] == 0
    assert result["travel_legs"] == result["bargain_leg_indices"] == []
    assert result["remaining_consumption"] == cost


def test_short_route_negotiation_flags_exhaustively_match_operation_timeline():
    for length in range(1, 5):
        for flags in product((False, True), repeat=2 * length):
            route = [leg(str(i), str(i + 1), bargain_to_cap=flags[2*i],
                         raise_to_cap=flags[2*i+1]) for i in range(length)]
            costs = {str(i): {str(i+1): i + 1} for i in range(length)}
            for arrival in range(1, length + 1):
                pending = [(i, kind) for i in range(length) for kind, location in
                           (("buy", i), ("sell", i+1)) if location >= arrival and
                           flags[2*i + (kind == "sell")]]
                result = estimate_remaining_consumption(route, arrival, costs)
                assert result["negotiation_rounds"] == len(pending)
                assert result["bargain_leg_indices"] == [i for i, kind in pending if kind == "buy"]
                assert result["raise_leg_indices"] == [i for i, kind in pending if kind == "sell"]
                steps = 2 * len(pending)
                assert result["remaining_consumption"] == sum(range(arrival + 1, length + 1)) + 8 * (
                    steps + (steps + 3) // 4)


@pytest.mark.parametrize("index", [None, True, False, 0, -1, 1.0, "1", 3])
def test_invalid_arrival_indices(index):
    with pytest.raises(ValueError, match="arrival_index"):
        estimate_remaining_consumption([leg(), leg("B", "C")], index, {})


def test_no_arrival_exists_on_empty_route():
    with pytest.raises(ValueError, match="arrival_index"):
        estimate_remaining_consumption([], 1, {})


@pytest.mark.parametrize("costs", [None, [], {}, {"B": None}, {"C": {"B": 50}},
    {"B": {"C": -1}}, {"B": {"C": True}}, {"B": {"C": 1.0}}, {"B": {"C": "24"}}])
def test_missing_or_invalid_remaining_directed_cost_is_an_error(costs):
    with pytest.raises(ValueError):
        estimate_remaining_consumption([leg(), leg("B", "C")], 1, costs)


@pytest.mark.parametrize("flag", [None, 0, 1, "false", [], {}])
@pytest.mark.parametrize("field,index", [("raise_to_cap", 0), ("bargain_to_cap", 1)])
def test_pending_negotiation_flags_must_be_booleans(field, index, flag):
    route = [leg(), leg("B", "C")]
    route[index][field] = flag
    with pytest.raises(ValueError, match=field):
        estimate_remaining_consumption(route, 1, {"B": {"C": 0}})


@pytest.mark.parametrize("fatigue,remaining,reserve,uses,count,immediate,endpoint", [
    (401, 100, 200, 6, 6, 101, 201), (250, 0, 200, 6, 1, 200, 200),
    (50, 300, 200, 6, 0, 50, 350), (51, 199, 200, 6, 1, 1, 200),
    (51, 198, 200, 6, 0, 51, 249), (0, 500, 200, 6, 0, 0, 500),
    (500, 0, 200, 0, 0, 500, 500), (100, 0, 200, 6, 0, 100, 100),
    (50, 0, 0, 6, 0, 50, 50), (51, 0, 0, 6, 1, 1, 1),
])
def test_water_exact_boundaries(fatigue, remaining, reserve, uses, count, immediate, endpoint):
    result = plan_water_use(fatigue, remaining, reserve, uses)
    assert result["drink_count"] == count
    assert result["immediate_fatigue"] == immediate
    assert result["estimated_endpoint_fatigue"] == endpoint
    assert result["recovery_amount"] == 50 * count
    assert result["planned"] is (count > 0)
    assert result["current_fatigue"] == fatigue
    assert result["remaining_consumption"] == remaining
    assert result["reserve"] == reserve
    assert result["free_uses"] == uses


def test_water_is_maximal_against_bruteforce_including_infeasible_zero_noops():
    for fatigue, remaining, reserve, uses in product(
        range(0, 405, 5), (0, 1, 24, 49, 50, 99, 100, 200, 301), (0, 1, 200, 400), range(7),
    ):
        feasible = [k for k in range(1, uses + 1)
                    if fatigue - 50*k > 0 and fatigue - 50*k + remaining >= reserve]
        result = plan_water_use(fatigue, remaining, reserve, uses)
        assert result["drink_count"] == max(feasible, default=0)


def test_water_large_integer_math_has_no_float_rounding():
    fatigue = 10**100 + 51
    result = plan_water_use(fatigue, 10**100, 10**100 + 1, 6)
    assert result["drink_count"] == 6
    assert result["estimated_endpoint_fatigue"] == 2 * 10**100 - 249


@pytest.mark.parametrize("bad", [None, -1, True, False, 1.0, "1", {}, []])
@pytest.mark.parametrize("field", ["current_fatigue", "remaining_consumption", "reserve", "free_uses"])
def test_water_rejects_invalid_numeric_values(field, bad):
    args = dict(current_fatigue=401, remaining_consumption=100, reserve=200, free_uses=6)
    args[field] = bad
    with pytest.raises(ValueError, match=field):
        plan_water_use(**args)


@pytest.mark.parametrize("bad", [None, [], (), {}, "work_meals", True, [None], [1], [[]],
    ["unknown"], ["work_meals", "work_meals"], ["love_bentos", "love_bentos"]])
def test_priority_validation_is_strict_even_on_low_fatigue(bad):
    with pytest.raises(ValueError, match="priority"):
        validate_bento_priority(bad)
    with pytest.raises(ValueError, match="priority"):
        plan_bento_meals(0, 200, bad, None, None)


@pytest.mark.parametrize("priority", [["work_meals"], ["love_bentos"], PRIORITY, PRIORITY[::-1]])
def test_priority_is_a_copied_ordered_nonempty_subset(priority):
    result = validate_bento_priority(priority)
    assert result == priority
    assert result is not priority


@pytest.mark.parametrize("fatigue", [0, 199, 200])
def test_low_fatigue_does_not_inspect_cache_or_catalog(fatigue):
    result = plan_bento_meals(fatigue, 200, PRIORITY, None, None)
    assert result["reason"] == "fatigue_at_or_below_reserve"
    assert result["meals"] == result["skipped"] == []
    assert result["inventory_updated_at"] == {}
    assert result["planned_remaining_fatigue"] == fatigue


def test_documented_meals_example_and_identity_only_requests(catalog):
    data = inventory(love(catalog), slots=SLOTS[:2])
    result = plan_bento_meals(300, 200, PRIORITY, data, catalog)
    assert result["meals"] == [
        {"kind": "work_meals", "issue_time": "05:00"},
        {"kind": "work_meals", "issue_time": "12:00"},
        {"kind": "love_bentos", "role_id": 1, "food_id": 83300002, "remaining_days": 1},
    ]
    assert result["recovery_amount"] == 92
    assert result["planned_remaining_fatigue"] == 208
    assert result["remaining_allowance"] == 8
    assert result["planned"] is True
    assert validate_meals(result["meals"]) == result["meals"]
    assert result["inventory_updated_at"] == {kind: data[kind]["updated_at"] for kind in PRIORITY}


def test_priority_changes_selection_without_knapsack_optimization(catalog):
    data = inventory(love(catalog), slots=SLOTS[:1])
    work_first = plan_bento_meals(240, 200, PRIORITY, data, catalog)
    love_first = plan_bento_meals(240, 200, PRIORITY[::-1], data, catalog)
    assert [meal["kind"] for meal in work_first["meals"]] == ["work_meals"]
    assert [meal["kind"] for meal in love_first["meals"]] == ["love_bentos"]
    assert (work_first["recovery_amount"], love_first["recovery_amount"]) == (36, 20)


def test_expiring_nonfit_skips_to_later_fitting_without_reordering_types(catalog):
    data = inventory(love(catalog, 83300019, days=0), love(catalog, days=2), slots=SLOTS[:1])
    result = plan_bento_meals(240, 200, PRIORITY[::-1], data, catalog)
    assert [meal_identity(meal) for meal in result["meals"]] == [("love_bentos", 1, 83300002, 2)]
    assert [entry["reason"] for entry in result["skipped"]] == ["does_not_fit", "does_not_fit"]
    assert [entry["recovery_amount"] for entry in result["skipped"]] == [50, 36]
    assert [entry["remaining_allowance"] for entry in result["skipped"]] == [40, 20]


def test_stable_slot_expiry_role_food_sort_independent_of_cache_order(catalog):
    rows = [love(catalog, 83300011, 2, 1), love(catalog, 83300002, 2, 1),
            love(catalog, 83300019, 1, 1), love(catalog, 83300014, 9, 0)]
    expected = [("work_meals", slot) for slot in SLOTS] + [
        ("love_bentos", 9, 83300014, 0), ("love_bentos", 1, 83300019, 1),
        ("love_bentos", 2, 83300002, 1), ("love_bentos", 2, 83300011, 1),
    ]
    for permutation in permutations(rows):
        data = inventory(*permutation, slots=SLOTS)
        data["work_meals"]["slots"].reverse()
        result = plan_bento_meals(1000, 200, PRIORITY, data, catalog)
        assert [meal_identity(meal) for meal in result["meals"]] == expected


@pytest.mark.parametrize("allowance,count", [(0, 0), (35, 0), (36, 1), (71, 1), (72, 2), (108, 3)])
def test_workmeal_exact_base_boundaries(allowance, count):
    result = plan_bento_meals(200 + allowance, 200, ["work_meals"], inventory(slots=SLOTS), None)
    assert len(result["meals"]) == count
    assert result["recovery_amount"] == 36 * count


def test_every_love_food_uses_catalog_base_not_cached_effect_or_account_bonus(catalog):
    for food in catalog["items"]:
        row = love(catalog, food["id"])
        row.update(fatigue_recovery=999, rating_stars=999, x=111, y=222)
        data = inventory(row)
        base = food["fatigue_recovery"]
        result = plan_bento_meals(200 + base, 200, ["love_bentos"], data, catalog)
        assert result["recovery_amount"] == base
        assert result["planned_remaining_fatigue"] == 200
        assert len(result["meals"]) == 1
        assert not plan_bento_meals(199 + base, 200, ["love_bentos"], data, catalog)["meals"]


def test_selected_kind_only_and_raw_catalog_requires_no_images(catalog):
    data = inventory(love(catalog), slots=SLOTS[:1])
    assert plan_bento_meals(300, 200, ["work_meals"], {"work_meals": data["work_meals"]}, None)["planned"]
    assert plan_bento_meals(300, 200, ["love_bentos"], {"love_bentos": data["love_bentos"]}, catalog)["planned"]
    data["love_bentos"] = {"degraded": True, "requires_refresh": True}
    assert plan_bento_meals(300, 200, ["work_meals"], data, None)["planned"]


@pytest.mark.parametrize("kind", PRIORITY)
@pytest.mark.parametrize("field,value", [("degraded", True), ("degraded", "false"),
    ("requires_refresh", True), ("requires_refresh", 1), ("requires_refresh", None),
    ("requires_refresh", "false"), ("updated_at", 123)])
def test_bad_selected_cache_is_error_not_a_nonfit(kind, field, value, catalog):
    data = inventory(love(catalog), slots=SLOTS[:1])
    data[kind][field] = value
    with pytest.raises(ValueError):
        plan_bento_meals(201, 200, PRIORITY, data, catalog)


@pytest.mark.parametrize("bad", [None, {}, [], {"recovery": {}}])
def test_missing_selected_inventory_is_error(bad, catalog):
    with pytest.raises(ValueError):
        plan_bento_meals(201, 200, PRIORITY, bad, catalog)


def test_all_enabled_types_validated_even_if_first_type_would_exhaust_allowance(catalog):
    data = inventory(love(catalog), slots=SLOTS[:1])
    data["love_bentos"]["count"] = 2
    with pytest.raises(ValueError, match="count"):
        plan_bento_meals(236, 200, PRIORITY, data, catalog)


def test_duplicate_and_unknown_identities_are_not_silently_dropped(catalog):
    row = love(catalog)
    with pytest.raises(ValueError, match="duplicate"):
        plan_bento_meals(201, 200, ["love_bentos"], inventory(row, deepcopy(row)), catalog)
    row["food_id"] = 99999999
    with pytest.raises(ValueError, match="unknown food_id"):
        plan_bento_meals(201, 200, ["love_bentos"], inventory(row), catalog)


def test_empty_stock_and_nonfitting_stock_have_distinct_normal_reasons(catalog):
    empty = plan_bento_meals(300, 200, PRIORITY, inventory(), catalog)
    assert empty["reason"] == "no_available_meals"
    assert not empty["planned"] and not empty["skipped"]
    nonfit = plan_bento_meals(201, 200, PRIORITY, inventory(slots=SLOTS[:1]), catalog)
    assert nonfit["reason"] == "no_fitting_meals"
    assert not nonfit["planned"] and len(nonfit["skipped"]) == 1


@pytest.mark.parametrize("bad", [None, -1, True, 1.0, "200"])
@pytest.mark.parametrize("field", ["current_fatigue", "reserve"])
def test_bento_fatigue_inputs_are_nonnegative_integers(field, bad):
    args = dict(current_fatigue=300, reserve=200, priority=PRIORITY,
                cached_inventory=None, food_catalog=None)
    args[field] = bad
    with pytest.raises(ValueError, match=field):
        plan_bento_meals(**args)


def test_bento_greedy_policy_matches_reference_across_allowances(catalog):
    data = inventory(love(catalog, 83300019, days=0), love(catalog, days=1),
                     love(catalog, 83300011, days=2), slots=SLOTS)
    for priority in (PRIORITY, PRIORITY[::-1], PRIORITY[:1], PRIORITY[1:]):
        candidates = validate_inventory(priority, data, catalog)
        candidates.sort(key=lambda row: (priority.index(row["kind"]),
            (row["issue_time"],) if row["kind"] == "work_meals" else
            (row["remaining_days"], row["role_id"], row["food_id"])))
        for allowance in range(251):
            expected = []
            left = allowance
            for candidate in candidates:
                if candidate["fatigue_recovery"] <= left:
                    expected.append(meal_identity(candidate))
                    left -= candidate["fatigue_recovery"]
            result = plan_bento_meals(200 + allowance, 200, priority, data, catalog)
            assert [meal_identity(meal) for meal in result["meals"]] == expected
            assert result["remaining_allowance"] == left
            assert result["planned_remaining_fatigue"] == 200 + left
            assert sum(resolve_meal(meal, data, catalog)["fatigue_recovery"]
                       for meal in result["meals"]) == result["recovery_amount"]


def test_input_purity_and_outputs_do_not_alias_input_records(catalog):
    route = [leg(fatigue_cost=9999, remaining_expected_fatigue=9999), leg("B", "C")]
    costs = {"B": {"C": 0}}
    data = inventory(love(catalog), slots=SLOTS)
    priority = list(PRIORITY)
    before = deepcopy((route, costs, data, catalog, priority))
    stop = select_last_water_arrival(route, lambda _: True)
    estimate = estimate_remaining_consumption(route, 1, costs)
    result = plan_bento_meals(300, 200, priority, data, catalog)
    assert estimate["remaining_consumption"] == 0
    assert (route, costs, data, catalog, priority) == before
    result["meals"][0]["issue_time"] = "changed"
    result["priority"].reverse()
    result["inventory_updated_at"]["work_meals"] = "changed"
    estimate["travel_legs"][0]["cost"] = 999
    stop["city_name"] = "changed"
    assert (route, costs, data, catalog, priority) == before


def test_actual_exact_solver_serialized_route_is_accepted():
    from plans.resonance_pc.src.services.resonance_pc_trade_exact_solver import (
        ResonancePcExactTradeSolver, TradeEdgeOption,
    )

    travel = json.loads((ROOT / "plans/resonance_pc/data/meta/city_travel_fatigue.json").read_text(encoding="utf-8"))
    solver = ResonancePcExactTradeSolver(snapshot={}, fatigue_payload=travel,
        buy_lot={}, trade_rules={}, allowed_city_ids=["1", "2", "3"])
    route = [solver._serialize_option(TradeEdgeOption(
        from_city_id=origin, to_city_id=destination, books_used=0,
        bargain_to_cap=True, raise_to_cap=True, travel_fatigue=24,
        expected_bargain_fatigue=Fraction(999), expected_raise_fatigue=Fraction(999),
        expected_profit=Fraction(1000), buy_product_ids=(), buy_product_names=(), buys=(),
    )) for origin, destination in (("1", "2"), ("2", "3"))]
    before = deepcopy(route)
    assert all("to_city_key" not in row for row in route)
    stop = select_last_water_arrival(route, lambda _: True)
    assert (stop["city_index"], stop["city_key"]) == (2, "3")
    estimate = estimate_remaining_consumption(route, 1, travel["costs"])
    assert estimate["remaining_consumption"] == travel["costs"]["2"]["3"] + 64
    assert route == before


def test_policy_imports_only_standard_library_and_existing_pure_bento_contract():
    source = Path(policy.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert modules == {"__future__", "collections.abc", "typing", "_bento_consumption_policy"}
    assert not any(isinstance(node, ast.Import) for node in ast.walk(tree))
