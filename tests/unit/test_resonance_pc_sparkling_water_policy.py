from __future__ import annotations

from copy import deepcopy
from fractions import Fraction

import pytest

from plans.resonance_pc.src.actions._sparkling_water_policy import (
    select_sparkling_water_stop,
    validate_recovery_snapshot,
)
from plans.resonance_pc.src.services.city_shop_data_pc_service import (
    CityShopDataError,
    ResonancePcCityShopDataService,
)
from plans.resonance_pc.src.services.resonance_pc_trade_exact_solver import (
    ResonancePcExactTradeSolver,
    TradeEdgeOption,
)


def _snapshot(current=1, remaining=6, maximum=300, limit=6):
    return {
        "status": {"fatigue": {"current": current, "max": maximum}},
        "recovery": {"sparkling_water": {"remaining_free_uses": remaining, "daily_free_limit": limit}},
        "metadata": {"persisted": True},
    }


def _leg(origin, destination, **extra):
    return {
        "from_city_id": origin, "to_city_id": destination,
        "from_city_key": f"key_{origin}", "to_city_key": f"key_{destination}",
        "from_city": f"City {origin}", "to_city": f"City {destination}",
        **extra,
    }


class _Shops:
    def __init__(self, rest=("B",)):
        self.rest = {f"City {city}" for city in rest}
        self.calls = []

    def resolve_shop_point(self, city, shop):
        self.calls.append((city, shop))
        assert shop == "rest"
        if city not in self.rest:
            raise CityShopDataError("shop_not_found_in_city", "No rest point")
        return {"x": 1, "y": 2}


def _inputs(**overrides):
    args = {
        "route": [_leg("A", "B"), _leg("B", "C")],
        "initial_city": {"city_name": "City A", "city_key": "key_A"},
        "recovery_snapshot": _snapshot(),
        "city_shop_data": _Shops(),
        "travel_fatigue": {
            "cities": {"A": "City A", "B": "City B", "C": "City C"},
            "costs": {"A": {"B": 100}, "B": {"C": 100, "A": 0}, "C": {"A": 0}},
        },
    }
    args.update(overrides)
    return args


def test_only_midpoint_rest_uses_two_cups_not_final_route_fatigue():
    result = select_sparkling_water_stop(**_inputs())
    assert result["planned"] is True
    assert result["reason"] == "selected"
    assert (result["city_index"], result["city_id"], result["city_key"], result["city_name"]) == (
        1, "B", "key_B", "City B",
    )
    assert result["drink_count"] == 2
    assert result["remaining_free_uses"] == 6
    assert result["initial_fatigue"] == 1
    assert result["cumulative_travel_fatigue"] == 100
    assert result["estimated_fatigue"] == 101
    assert result["recovery_amount"] == 100
    assert [c["estimated_fatigue"] for c in result["candidates"]] == [1, 101, 201]


def test_endpoint_wins_with_four_cups():
    result = select_sparkling_water_stop(**_inputs(city_shop_data=_Shops(("B", "C"))))
    assert (result["city_index"], result["drink_count"], result["estimated_fatigue"]) == (2, 4, 201)


@pytest.mark.parametrize("fatigue,cups", [(0, 0), (1, 0), (50, 0), (51, 1), (100, 1), (300, 5), (301, 6)])
def test_strict_threshold_and_overcap_start(fatigue, cups):
    result = select_sparkling_water_stop(**_inputs(
        route=[], recovery_snapshot=_snapshot(fatigue), city_shop_data=_Shops(("A",)),
    ))
    assert result["drink_count"] == cups
    assert result["planned"] is (cups > 0)
    assert result["city_index"] == (0 if cups else None)
    if cups:
        assert result["estimated_fatigue"] > result["recovery_amount"]
    else:
        assert result["reason"] == "insufficient_fatigue"


def test_tie_keeps_earliest_start_even_with_route():
    result = select_sparkling_water_stop(**_inputs(
        recovery_snapshot=_snapshot(301), city_shop_data=_Shops(("A", "B", "C")),
    ))
    assert result["city_index"] == 0
    assert result["drink_count"] == 6


def test_repeated_city_nodes_stay_distinct_and_tie_keeps_first_arrival():
    result = select_sparkling_water_stop(**_inputs(
        route=[_leg("A", "B"), _leg("B", "A"), _leg("A", "B")],
        recovery_snapshot=_snapshot(1, remaining=2),
    ))
    assert result["city_index"] == 1
    assert [(c["city_index"], c["city_id"]) for c in result["candidates"]] == [
        (0, "A"), (1, "B"), (2, "A"), (3, "B"),
    ]
    assert [c["drink_count"] for c in result["candidates"]] == [0, 2, 0, 2]


def test_later_visit_to_same_city_can_win():
    result = select_sparkling_water_stop(**_inputs(
        route=[_leg("A", "B"), _leg("B", "A"), _leg("A", "B")],
    ))
    assert (result["city_index"], result["city_id"], result["drink_count"]) == (3, "B", 4)


def test_movement_is_directed_and_zero_is_valid():
    result = select_sparkling_water_stop(**_inputs(
        route=[_leg("B", "A")], recovery_snapshot=_snapshot(51), city_shop_data=_Shops(("A",)),
    ))
    assert result["cumulative_travel_fatigue"] == 0
    assert result["drink_count"] == 1
    assert result["city_index"] == 1


@pytest.mark.parametrize("snapshot,reason", [(None, "recovery_snapshot_missing"), (_snapshot(remaining=0), "no_remaining_free_uses")])
def test_missing_snapshot_or_zero_quota_does_not_access_dependencies(snapshot, reason):
    result = select_sparkling_water_stop(**_inputs(
        recovery_snapshot=snapshot, city_shop_data=object(), travel_fatigue=None,
    ))
    assert result["planned"] is False
    assert result["reason"] == reason
    assert result["city_index"] is None
    assert result["candidates"] == []
    assert result["recovery_amount"] == 0


def test_no_rest_never_detours():
    shops = _Shops(())
    result = select_sparkling_water_stop(**_inputs(city_shop_data=shops))
    assert result["reason"] == "no_eligible_tavern_arrival"
    assert result["planned"] is False
    assert shops.calls == [("City A", "rest"), ("City B", "rest"), ("City C", "rest")]


def test_negotiation_and_leg_fatigue_are_ignored_without_mutating_inputs():
    args = _inputs(route=[
        _leg("A", "B", fatigue_cost=9999, negotiation_fatigue=9999),
        _leg("B", "C", fatigue_cost=-100, negotiation={"fatigue": 9999}),
    ])
    before = deepcopy({k: v for k, v in args.items() if k != "city_shop_data"})
    assert select_sparkling_water_stop(**args)["drink_count"] == 2
    assert {k: v for k, v in args.items() if k != "city_shop_data"} == before


@pytest.mark.parametrize("path,value", [
    (("status",), None), (("status", "fatigue"), []),
    (("status", "fatigue", "current"), -1), (("status", "fatigue", "current"), True),
    (("status", "fatigue", "current"), 1.0), (("status", "fatigue", "current"), "1"),
    (("status", "fatigue", "max"), 0), (("status", "fatigue", "max"), False),
    (("recovery",), []), (("recovery", "sparkling_water"), None),
    (("recovery", "sparkling_water", "remaining_free_uses"), -1),
    (("recovery", "sparkling_water", "remaining_free_uses"), 7),
    (("recovery", "sparkling_water", "remaining_free_uses"), True),
    (("recovery", "sparkling_water", "remaining_free_uses"), "6"),
    (("recovery", "sparkling_water", "daily_free_limit"), 5),
    (("recovery", "sparkling_water", "daily_free_limit"), 7),
    (("recovery", "sparkling_water", "daily_free_limit"), 6.0),
    (("metadata",), None), (("metadata", "persisted"), False),
    (("metadata", "persisted"), 1), (("metadata", "persisted"), "true"),
])
def test_malformed_snapshot_fails_before_dependency_access(path, value):
    snapshot = _snapshot()
    target = snapshot
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        select_sparkling_water_stop(**_inputs(recovery_snapshot=snapshot, city_shop_data=object()))


@pytest.mark.parametrize("snapshot", [None, {}, [], "snapshot"])
def test_validator_rejects_missing_or_wrong_snapshot(snapshot):
    with pytest.raises(ValueError):
        validate_recovery_snapshot(snapshot)


def test_validator_returns_independent_copy_and_preserves_unknown_sections():
    snapshot = _snapshot(301, remaining=0, limit=6)
    snapshot["future"] = {"items": [1]}
    result = validate_recovery_snapshot(snapshot)
    assert result == snapshot
    result["future"]["items"].append(2)
    result["status"]["fatigue"]["current"] = 0
    assert snapshot["future"]["items"] == [1]
    assert snapshot["status"]["fatigue"]["current"] == 301


@pytest.mark.parametrize("remaining", [0, 6])
def test_refresh_required_snapshot_is_rejected_before_dependency_access(remaining):
    snapshot = _snapshot(remaining=remaining)
    snapshot["recovery"]["sparkling_water"]["requires_refresh"] = True
    before = deepcopy(snapshot)
    with pytest.raises(ValueError, match="requires_refresh"):
        validate_recovery_snapshot(snapshot)
    with pytest.raises(ValueError, match="requires_refresh"):
        select_sparkling_water_stop(**_inputs(
            recovery_snapshot=snapshot, city_shop_data=object(), travel_fatigue=None,
        ))
    assert snapshot == before


def test_cleared_refresh_marker_allows_policy_selection():
    snapshot = _snapshot()
    snapshot["recovery"]["sparkling_water"]["requires_refresh"] = False
    assert validate_recovery_snapshot(snapshot) == snapshot
    result = select_sparkling_water_stop(**_inputs(recovery_snapshot=snapshot))
    assert result["planned"] is True
    assert result["drink_count"] == 2


@pytest.mark.parametrize("limit", [0, -1, False])
def test_zero_remaining_still_requires_positive_daily_limit(limit):
    snapshot = _snapshot(remaining=0, limit=limit)
    with pytest.raises(ValueError, match="daily_free_limit"):
        validate_recovery_snapshot(snapshot)
    with pytest.raises(ValueError, match="daily_free_limit"):
        select_sparkling_water_stop(**_inputs(
            recovery_snapshot=snapshot, city_shop_data=object(), travel_fatigue=None,
        ))


def test_zero_remaining_with_minimum_positive_limit_is_valid():
    snapshot = _snapshot(remaining=0, limit=1)
    assert validate_recovery_snapshot(snapshot) == snapshot
    result = select_sparkling_water_stop(**_inputs(recovery_snapshot=snapshot))
    assert result["reason"] == "no_remaining_free_uses"


@pytest.mark.parametrize("cost", [-1, True, "100", 100.0, None, {}])
def test_malformed_movement_costs_fail_before_shop_lookup(cost):
    args = _inputs(city_shop_data=object())
    args["travel_fatigue"]["costs"]["B"]["C"] = cost
    with pytest.raises(ValueError, match="costs"):
        select_sparkling_water_stop(**args)


@pytest.mark.parametrize("payload", [None, {}, {"cities": {}, "costs": {}},
    {"cities": {"A": "City A"}, "costs": []},
    {"cities": {"A": "City A"}, "costs": {"A": []}},
    {"cities": {"A": "City A"}, "costs": {"X": {"A": 1}}},
    {"cities": {"A": "City A"}, "costs": {"A": {"X": 1}}},
])
def test_malformed_cost_payload(payload):
    with pytest.raises(ValueError):
        select_sparkling_water_stop(**_inputs(travel_fatigue=payload, city_shop_data=object()))


def test_missing_later_edge_fails_before_any_shop_lookup():
    args = _inputs(city_shop_data=object())
    del args["travel_fatigue"]["costs"]["B"]["C"]
    with pytest.raises(ValueError, match="Missing deterministic"):
        select_sparkling_water_stop(**args)


@pytest.mark.parametrize("route", [None, {}, [None], [_leg("A", "X")],
    [_leg("A", "B"), _leg("A", "C")],
    [_leg("A", "B"), _leg("B", "C", from_city_key="wrong")],
])
def test_malformed_or_discontinuous_route(route):
    with pytest.raises(ValueError):
        select_sparkling_water_stop(**_inputs(route=route, city_shop_data=object()))


@pytest.mark.parametrize("missing_fields", [
    ("from_city_key", "to_city_key"), ("from_city_key",), ("to_city_key",),
])
def test_optional_route_keys_allow_missing_and_mixed_key_continuity(missing_fields):
    args = _inputs()
    for leg in args["route"]:
        for field in missing_fields:
            del leg[field]
    result = select_sparkling_water_stop(**args)
    assert (result["city_index"], result["drink_count"]) == (1, 2)
    assert result["city_key"] == ("B" if "to_city_key" in missing_fields else "key_B")


def test_actual_exact_serializer_route_without_keys_is_accepted():
    args = _inputs()
    solver = ResonancePcExactTradeSolver(
        snapshot={}, fatigue_payload=args["travel_fatigue"], buy_lot={},
        trade_rules={}, allowed_city_ids=["A", "B", "C"],
    )
    args["route"] = [
        solver._serialize_option(TradeEdgeOption(
            from_city_id=origin, to_city_id=destination, books_used=0,
            bargain_to_cap=True, raise_to_cap=True, travel_fatigue=100,
            expected_bargain_fatigue=Fraction(999), expected_raise_fatigue=Fraction(999),
            expected_profit=Fraction(1000), buy_product_ids=(), buy_product_names=(), buys=(),
        ))
        for origin, destination in (("A", "B"), ("B", "C"))
    ]
    assert all("from_city_key" not in leg and "to_city_key" not in leg for leg in args["route"])
    result = select_sparkling_water_stop(**args)
    assert (result["city_index"], result["city_key"], result["drink_count"]) == (1, "B", 2)
    assert result["estimated_fatigue"] == 101


@pytest.mark.parametrize("error", [CityShopDataError("location_json_invalid", "bad file"),
    CityShopDataError("shop_point_invalid", "bad coordinates"),
    CityShopDataError("city_not_resolved", "unknown city"), RuntimeError("unexpected"),
])
def test_only_missing_shop_error_is_suppressed(error):
    class BrokenShops:
        def resolve_shop_point(self, *_args):
            raise error

    with pytest.raises(type(error)) as raised:
        select_sparkling_water_stop(**_inputs(city_shop_data=BrokenShops()))
    assert raised.value is error


def test_route_start_is_authoritative_over_initial_city():
    result = select_sparkling_water_stop(**_inputs(
        initial_city={"city_id": "X"}, recovery_snapshot=_snapshot(301), city_shop_data=_Shops(("A",)),
    ))
    assert result["city_index"] == 0
    assert result["city_id"] == "A"


@pytest.mark.parametrize("initial", [
    {"city_name": "City A", "city_key": "key_A"},
    {"city_name": "City A", "city_key": "key_A", "city_id": "A"},
    {"city_key": "A"},
])
def test_empty_route_resolves_start(initial):
    result = select_sparkling_water_stop(**_inputs(
        route=[], initial_city=initial, recovery_snapshot=_snapshot(51), city_shop_data=_Shops(("A",)),
    ))
    assert (result["city_id"], result["city_index"], result["drink_count"]) == ("A", 0, 1)


def test_empty_route_uses_configured_alias_resolution():
    class AliasShops(_Shops):
        def resolve_city(self, name):
            return {"city_key": "key_A" if name == "alias" else f"key_{name[-1]}"}

    result = select_sparkling_water_stop(**_inputs(
        route=[], initial_city={"city_key": "key_A"}, recovery_snapshot=_snapshot(51),
        city_shop_data=AliasShops(("A",)),
    ))
    assert result["city_id"] == "A"


@pytest.mark.parametrize("initial", [
    {"city_key": "freeport"},
    {"city_name": "7\u53f7\u81ea\u7531\u6e2f", "city_key": "freeport"},
])
def test_empty_route_resolves_real_configured_freeport_alias(initial):
    result = select_sparkling_water_stop(**_inputs(
        route=[], initial_city=initial, recovery_snapshot=_snapshot(301, remaining=3),
        city_shop_data=ResonancePcCityShopDataService(),
        travel_fatigue={
            "cities": {"3": "\u4e03\u53f7\u81ea\u7531\u6e2f"},
            "costs": {"3": {"3": 0}},
        },
    ))
    assert (result["city_id"], result["city_key"], result["drink_count"]) == ("3", "freeport", 3)


@pytest.mark.parametrize("ambiguous", [False, True])
def test_empty_route_requires_unambiguous_city(ambiguous):
    args = _inputs(route=[], initial_city={"city_name": "Unknown"})
    if ambiguous:
        args["initial_city"] = {"city_name": "City A"}
        args["travel_fatigue"]["cities"]["B"] = "City A"
    with pytest.raises(ValueError, match="exactly one"):
        select_sparkling_water_stop(**args)
