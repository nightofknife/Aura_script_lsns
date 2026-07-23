from __future__ import annotations

import json
import random
from fractions import Fraction
from pathlib import Path

import pytest

from plans.resonance.src.actions.trade_planner_actions import (
    resonance_trade_plan_optimal_route,
)
from plans.resonance.src.services.resonance_trade_exact_solver import (
    ResonanceExactTradeSolver,
    expected_fatigue_to_cap,
    js_round,
)
from plans.resonance.src.services.resonance_trade_planner_service import (
    ResonanceTradePlannerService,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
TRADE_RULES = json.loads(
    (REPO_ROOT / "plans" / "resonance" / "data" / "meta" / "trade_rules.json").read_text(
        encoding="utf-8"
    )
)


def _snapshot(prices: dict[str, dict[str, dict[str, int]]]) -> dict:
    products = {}
    for product_id, sides in prices.items():
        products[product_id] = {
            "name": f"product-{product_id}",
            "market": {
                side: {
                    city_id: {"price": price}
                    for city_id, price in city_prices.items()
                }
                for side, city_prices in sides.items()
            },
        }
    return {"snapshot_id": "frozen-test", "products": products}


def _solver(
    *,
    cities: list[str],
    costs: dict[str, dict[str, int]],
    buy_lot: dict[str, dict[str, int]],
    prices: dict[str, dict[str, dict[str, int]]],
    trade_rules: dict | None = None,
) -> ResonanceExactTradeSolver:
    return ResonanceExactTradeSolver(
        snapshot=_snapshot(prices),
        fatigue_payload={
            "schema_version": "1.0.0",
            "cities": {city_id: f"city-{city_id}" for city_id in cities},
            "costs": costs,
        },
        buy_lot=buy_lot,
        trade_rules=trade_rules or TRADE_RULES,
        allowed_city_ids=cities,
    )


def _solve(solver: ResonanceExactTradeSolver, **overrides):
    inputs = {
        "start_city_id": "1",
        "fatigue_budget": 3,
        "cargo_capacity": 1,
        "book_budget": 0,
        "book_profit_threshold": 0,
        "negotiation_budget": 0,
        "all_plan": 0,
        "bargain_success_rates_bps": [5000],
        "bargain_step_bps": 1000,
        "raise_success_rates_bps": [5000],
        "raise_step_bps": 1000,
        "trade_level": 20,
        "city_prestige": {"default": 20, "overrides": {}},
        "product_unlocks": {"mode": "all", "product_ids": []},
        "active_events": [],
    }
    inputs.update(overrides)
    return solver.solve(**inputs)


def test_game_rounding_is_exact_and_is_not_bankers_rounding():
    assert js_round(Fraction(1, 2)) == 1
    assert js_round(Fraction(5, 2)) == 3
    assert js_round(Fraction(-1, 2)) == 0
    assert js_round(Fraction(-3, 2)) == -1


def test_expected_fatigue_formula_defaults_sequences_and_cap_rounding():
    assert expected_fatigue_to_cap(success_rates_bps=[5000], step_bps=1000) == 32
    assert expected_fatigue_to_cap(
        success_rates_bps=[6300, 5300], step_bps=1170
    ) == 8 * (Fraction(100, 63) + Fraction(100, 53))
    assert expected_fatigue_to_cap(success_rates_bps=[5000], step_bps=700) == 48
    assert expected_fatigue_to_cap(success_rates_bps=[10000], step_bps=1170) == 16
    assert expected_fatigue_to_cap(success_rates_bps=[5000, 0], step_bps=1000) is None


def test_versioned_trade_rules_only_keep_binary_to_cap_negotiation_metadata():
    assert TRADE_RULES["schema_version"] == "2.0.0"
    negotiation = TRADE_RULES["negotiation"]
    assert negotiation["model"] == "binary_to_cap_expected_fatigue"
    assert negotiation["max_adjustment_bps"] == 2000
    assert negotiation["attempt_fatigue"] == 8
    assert negotiation["defaults"] == {
        "bargain_success_rates_bps": [5000],
        "bargain_step_bps": 1000,
        "raise_success_rates_bps": [5000],
        "raise_step_bps": 1000,
    }
    assert {
        "base_attempts",
        "base_success_bps",
        "success_decay_bps",
        "trade_level_rate_bps_per_level",
    }.isdisjoint(negotiation)


@pytest.mark.parametrize(
    ("rates", "step", "message"),
    [
        ([], 1000, "non-empty"),
        ([10001], 1000, "<= 10000"),
        ([-1], 1000, ">= 0"),
        ([5000.5], 1000, "integer"),
        ([5000], 0, ">= 1"),
        ([5000], 2001, "<= 2000"),
    ],
)
def test_expected_fatigue_formula_rejects_invalid_profile(rates, step, message):
    with pytest.raises(ValueError, match=message):
        expected_fatigue_to_cap(success_rates_bps=rates, step_bps=step)


def test_tax_purchase_quantity_and_product_unlock_formula():
    solver = _solver(
        cities=["1", "2"],
        costs={"1": {"2": 1}, "2": {"1": 1}},
        buy_lot={"1": {"p1": 2, "p2": 1}, "2": {}},
        prices={
            "p1": {"buy": {"1": 100}, "sell": {"2": 200}},
            "p2": {"buy": {"1": 100}, "sell": {"2": 300}},
        },
    )

    result = _solve(solver, fatigue_budget=1, cargo_capacity=10)
    leg = result["route"][0]
    assert leg["buys"][0]["product_id"] == "p2"
    assert leg["buys"][1]["product_id"] == "p1"
    assert leg["buys"][1]["quantity"] == 6
    assert leg["buys"][1]["expected_unit_profit_exact"] == "85"

    only_p1 = _solve(
        solver,
        fatigue_budget=1,
        cargo_capacity=10,
        product_unlocks={"mode": "only", "product_ids": ["p1"]},
    )
    assert only_p1["route"][0]["buy_product_ids"] == ["p1"]
    locked_all = _solve(
        solver,
        fatigue_budget=1,
        product_unlocks={"mode": "only", "product_ids": []},
    )
    assert locked_all["status"] == "no_plan"


def test_all_plan_zero_counts_full_operations_and_pays_expected_fatigue():
    solver = _solver(
        cities=["1", "2"],
        costs={"1": {"2": 9}, "2": {"1": 100}},
        buy_lot={"1": {"p": 1}, "2": {}},
        prices={"p": {"buy": {"1": 100}, "sell": {"2": 200}}},
    )

    one = _solve(solver, fatigue_budget=41, negotiation_budget=1)
    assert one["status"] == "ok"
    assert one["all_plan"] == 0
    assert one["full_negotiation_used"] == 1
    assert one["full_bargain_count"] == 0
    assert one["full_raise_count"] == 1
    assert one["expected_fatigue_used_exact"] == "41"
    assert one["remaining_negotiation"] == 0
    leg = one["route"][0]
    assert leg["bargain_to_cap"] is False
    assert leg["raise_to_cap"] is True
    assert leg["expected_raise_fatigue_exact"] == "32"
    assert leg["expected_fatigue_cost_exact"] == "41"

    both = _solve(solver, fatigue_budget=73, negotiation_budget=2)
    assert both["full_negotiation_used"] == 2
    assert both["route"][0]["bargain_to_cap"] is True
    assert both["route"][0]["raise_to_cap"] is True
    assert both["route"][0]["expected_negotiation_fatigue_exact"] == "64"


def test_all_plan_one_ignores_count_budget_but_respects_fatigue():
    solver = _solver(
        cities=["1", "2"],
        costs={"1": {"2": 9}, "2": {"1": 100}},
        buy_lot={"1": {"p": 1}, "2": {}},
        prices={"p": {"buy": {"1": 100}, "sell": {"2": 200}}},
    )

    one = _solve(solver, all_plan=1, fatigue_budget=41, negotiation_budget=0)
    assert one["negotiation_budget_ignored"] is True
    assert one["remaining_negotiation"] is None
    assert one["full_negotiation_used"] == 1
    assert one["route"][0]["raise_to_cap"] is True

    both_zero_budget = _solve(solver, all_plan=1, fatigue_budget=73, negotiation_budget=0)
    both_large_budget = _solve(solver, all_plan=1, fatigue_budget=73, negotiation_budget=99)
    assert both_zero_budget["full_negotiation_used"] == 2
    assert both_zero_budget["city_path_ids"] == both_large_budget["city_path_ids"]
    assert both_zero_budget["expected_profit_exact"] == both_large_budget["expected_profit_exact"]
    assert both_zero_budget["expected_fatigue_used_exact"] == both_large_budget[
        "expected_fatigue_used_exact"
    ]


def test_edge_enumeration_keeps_all_four_binary_choices_with_asymmetric_costs():
    solver = _solver(
        cities=["1", "2"],
        costs={"1": {"2": 9}, "2": {"1": 100}},
        buy_lot={"1": {"p": 1}, "2": {}},
        prices={"p": {"buy": {"1": 100}, "sell": {"2": 200}}},
    )
    prestige = solver._normalize_city_prestige({"default": 20, "overrides": {}})
    bargain = solver._normalize_negotiation_profile(
        side="bargain", success_rates_bps=[10000], step_bps=2000
    )
    raise_profile = solver._normalize_negotiation_profile(
        side="raise", success_rates_bps=[5000], step_bps=1000
    )
    options = solver._build_edge_options(
        city_ids=["1", "2"],
        cargo_capacity=1,
        book_budget=0,
        book_profit_threshold=Fraction(0, 1),
        negotiation_budget=0,
        all_plan=1,
        bargain_profile=bargain,
        raise_profile=raise_profile,
        prestige_by_city=prestige,
        unlocked_products=None,
    )[("1", "2")]

    costs = {
        (option.bargain_to_cap, option.raise_to_cap): option.expected_fatigue_cost
        for option in options
    }
    assert costs == {
        (False, False): Fraction(9, 1),
        (True, False): Fraction(17, 1),
        (False, True): Fraction(41, 1),
        (True, True): Fraction(49, 1),
    }


def test_fractional_expected_fatigue_is_a_hard_exact_constraint():
    solver = _solver(
        cities=["1", "2"],
        costs={"1": {"2": 1}, "2": {"1": 1}},
        buy_lot={"1": {"p": 1}, "2": {}},
        prices={"p": {"buy": {"1": 100}, "sell": {"2": 110}}},
    )
    common = {
        "all_plan": 1,
        "bargain_success_rates_bps": [6000],
        "bargain_step_bps": 2000,
        "raise_success_rates_bps": [0],
        "raise_step_bps": 2000,
    }

    too_small = _solve(solver, fatigue_budget=14, **common)
    enough = _solve(solver, fatigue_budget=15, **common)

    assert too_small["status"] == "no_plan"
    assert enough["status"] == "ok"
    assert enough["expected_fatigue_used_exact"] == "43/3"
    assert enough["route"][0]["expected_bargain_fatigue_exact"] == "40/3"


def test_zero_success_stage_disables_only_that_full_option_and_warns():
    solver = _solver(
        cities=["1", "2"],
        costs={"1": {"2": 1}, "2": {"1": 1}},
        buy_lot={"1": {"p": 1}, "2": {}},
        prices={"p": {"buy": {"1": 100}, "sell": {"2": 200}}},
    )
    result = _solve(
        solver,
        all_plan=1,
        fatigue_budget=40,
        bargain_success_rates_bps=[0],
        raise_success_rates_bps=[0],
    )

    assert result["status"] == "ok"
    assert result["full_negotiation_used"] == 0
    assert result["assumptions"]["bargain_profile"]["expected_fatigue"] is None
    assert result["assumptions"]["raise_profile"]["expected_fatigue"] is None
    assert any("bargain_to_cap is unavailable" in warning for warning in result["warnings"])
    assert any("raise_to_cap is unavailable" in warning for warning in result["warnings"])


def test_trade_level_no_longer_changes_negotiation_model():
    solver = _solver(
        cities=["1", "2"],
        costs={"1": {"2": 9}, "2": {"1": 9}},
        buy_lot={"1": {"p": 1}, "2": {}},
        prices={"p": {"buy": {"1": 100}, "sell": {"2": 200}}},
    )
    level_one = _solve(solver, all_plan=1, fatigue_budget=41, trade_level=1)
    level_twenty = _solve(solver, all_plan=1, fatigue_budget=41, trade_level=20)

    assert level_one["expected_profit_exact"] == level_twenty["expected_profit_exact"]
    assert level_one["expected_fatigue_used_exact"] == level_twenty[
        "expected_fatigue_used_exact"
    ]
    assert level_one["route"] == level_twenty["route"]
    assert level_one["assumptions"]["trade_level_affects_negotiation"] is False


def test_repeated_city_open_endpoint_and_empty_migration_are_supported():
    solver = _solver(
        cities=["1", "2"],
        costs={"1": {"2": 1}, "2": {"1": 1}},
        buy_lot={"1": {"p": 1}, "2": {}},
        prices={"p": {"buy": {"1": 100}, "sell": {"2": 200}}},
    )

    result = _solve(solver, fatigue_budget=3)

    assert result["status"] == "ok"
    assert result["city_path_ids"] == ["1", "2", "1", "2"]
    assert result["route"][1]["buy_products"] == []
    assert result["route"][1]["expected_profit_exact"] == "0"
    assert result["expected_profit_exact"] == "170"
    assert result["remaining_expected_fatigue_exact"] == "0"


def test_book_threshold_uses_exact_same_edge_and_negotiation_marginal_profit():
    solver = _solver(
        cities=["1", "2"],
        costs={"1": {"2": 1}, "2": {"1": 1}},
        buy_lot={"1": {"p": 1}, "2": {}},
        prices={"p": {"buy": {"1": 100}, "sell": {"2": 300}}},
    )
    prestige = {"default": 1, "overrides": {}}

    included = _solve(
        solver,
        fatigue_budget=1,
        cargo_capacity=2,
        book_budget=1,
        book_profit_threshold=160,
        city_prestige=prestige,
    )
    excluded = _solve(
        solver,
        fatigue_budget=1,
        cargo_capacity=2,
        book_budget=1,
        book_profit_threshold=161,
        city_prestige=prestige,
    )

    assert included["books_used"] == 1
    assert included["expected_profit_exact"] == "320"
    assert excluded["books_used"] == 0
    assert excluded["expected_profit_exact"] == "160"


def test_new_contract_removes_attempt_and_legacy_resource_fields():
    solver = _solver(
        cities=["1", "2"],
        costs={"1": {"2": 9}, "2": {"1": 9}},
        buy_lot={"1": {"p": 1}, "2": {}},
        prices={"p": {"buy": {"1": 100}, "sell": {"2": 200}}},
    )
    result = _solve(solver, all_plan=1, fatigue_budget=41)
    leg = result["route"][0]

    assert {"fatigue_used", "remaining_fatigue", "negotiation_used"}.isdisjoint(result)
    assert {
        "bargain_attempts",
        "raise_attempts",
        "negotiation_used",
        "negotiation_fatigue",
        "fatigue_cost",
    }.isdisjoint(leg)


def test_public_read_only_action_integrates_profile_rules_and_city_resolution():
    buy_lot_payload = json.loads(
        (REPO_ROOT / "plans" / "resonance" / "data" / "meta" / "buy_lot.json").read_text(
            encoding="utf-8"
        )
    )
    product_id = next(iter(buy_lot_payload["city_product_buy_lot"]["3"]))
    snapshot = _snapshot({product_id: {"buy": {"3": 100}, "sell": {"8": 200}}})
    fatigue = {
        "schema_version": "1.0.0",
        "cities": {"3": "city-3", "8": "city-8"},
        "costs": {"3": {"8": 9}, "8": {"3": 100}},
    }

    class FakeMarketData:
        def get_latest(self):
            return snapshot

        def get_snapshot(self, snapshot_id: str):
            assert snapshot_id == "frozen-test"
            return snapshot

        def get_all_travel_fatigue(self):
            return fatigue

    service = ResonanceTradePlannerService(
        FakeMarketData(),
        plan_root=REPO_ROOT / "plans" / "resonance",
    )

    result = resonance_trade_plan_optimal_route(
        current_city_key="freeport",
        snapshot_id="frozen-test",
        fatigue_budget=41,
        cargo_capacity=1,
        negotiation_budget=0,
        all_plan=1,
        available_city_ids=["3", "8"],
        resonance_trade_planner=service,
    )

    assert result["status"] == "ok"
    assert result["snapshot_id"] == "frozen-test"
    assert result["city_path_ids"] == ["3", "8"]
    assert result["full_negotiation_used"] == 1
    assert result["assumptions"]["rule_schema_version"] == "2.0.0"
    assert result["assumptions"]["rule_model_version"] == (
        "resonance_trade_binary_to_cap_2026_07_19"
    )


def _raw_edge_options(
    solver: ResonanceExactTradeSolver,
    *,
    cargo_capacity: int,
    book_budget: int,
    negotiation_budget: int,
    all_plan: int,
):
    prestige = solver._normalize_city_prestige({"default": 20, "overrides": {}})
    bargain = solver._normalize_negotiation_profile(
        side="bargain", success_rates_bps=[10000], step_bps=2000
    )
    raise_profile = solver._normalize_negotiation_profile(
        side="raise", success_rates_bps=[10000], step_bps=2000
    )
    table = {}
    for from_city in solver.allowed_city_ids:
        for to_city in solver.allowed_city_ids:
            if from_city == to_city:
                continue
            travel = int((solver.fatigue_costs.get(from_city) or {}).get(to_city, 0))
            if travel <= 0:
                continue
            options = []
            for bargain_to_cap in (False, True):
                for raise_to_cap in (False, True):
                    full_used = int(bargain_to_cap) + int(raise_to_cap)
                    if all_plan == 0 and full_used > negotiation_budget:
                        continue
                    for books_used in range(book_budget + 1):
                        options.append(
                            solver._build_edge_option(
                                from_city=from_city,
                                to_city=to_city,
                                books_used=books_used,
                                bargain_to_cap=bargain_to_cap,
                                raise_to_cap=raise_to_cap,
                                travel_fatigue=travel,
                                expected_bargain_fatigue=(
                                    bargain.expected_fatigue
                                    if bargain_to_cap
                                    else Fraction(0, 1)
                                ),
                                expected_raise_fatigue=(
                                    raise_profile.expected_fatigue
                                    if raise_to_cap
                                    else Fraction(0, 1)
                                ),
                                cargo_capacity=cargo_capacity,
                                prestige_by_city=prestige,
                                unlocked_products=None,
                            )
                        )
            table[(from_city, to_city)] = options
    return table


def _brute_force_best(
    solver: ResonanceExactTradeSolver,
    *,
    start: str,
    fatigue_budget: int,
    cargo_capacity: int,
    book_budget: int,
    negotiation_budget: int,
    all_plan: int,
):
    options = _raw_edge_options(
        solver,
        cargo_capacity=cargo_capacity,
        book_budget=book_budget,
        negotiation_budget=negotiation_budget,
        all_plan=all_plan,
    )
    best = None

    def better(candidate, existing):
        if existing is None or candidate[0] != existing[0]:
            return existing is None or candidate[0] > existing[0]
        candidate_route_signature = tuple(option.stable_signature for option in candidate[5])
        existing_route_signature = tuple(option.stable_signature for option in existing[5])
        return (
            candidate[1],
            candidate[2],
            candidate[3],
            len(candidate[5]),
            candidate[4],
            candidate_route_signature,
        ) < (
            existing[1],
            existing[2],
            existing[3],
            len(existing[5]),
            existing[4],
            existing_route_signature,
        )

    def visit(city, fatigue, books, full_negotiation, profit, path, route):
        nonlocal best
        if route and profit > 0:
            candidate = (profit, fatigue, books, full_negotiation, path, route)
            if better(candidate, best):
                best = candidate
        for to_city in solver.allowed_city_ids:
            if to_city == city:
                continue
            for option in options.get((city, to_city), ()):
                next_fatigue = fatigue + option.expected_fatigue_cost
                next_books = books + option.books_used
                next_negotiation = full_negotiation + option.full_negotiation_used
                if next_fatigue > fatigue_budget or next_books > book_budget:
                    continue
                if all_plan == 0 and next_negotiation > negotiation_budget:
                    continue
                visit(
                    to_city,
                    next_fatigue,
                    next_books,
                    next_negotiation,
                    profit + option.expected_profit,
                    path + (to_city,),
                    route + (option,),
                )

    visit(start, Fraction(0, 1), 0, 0, Fraction(0, 1), (start,), ())
    return best


@pytest.mark.parametrize("all_plan", [0, 1])
@pytest.mark.parametrize("seed", range(6))
def test_exact_label_solver_matches_complete_brute_force(all_plan: int, seed: int):
    random_source = random.Random(seed)
    city_count = random_source.randint(2, 3)
    cities = [str(index + 1) for index in range(city_count)]
    costs = {
        from_city: {
            to_city: random_source.randint(2, 3)
            for to_city in cities
            if to_city != from_city
        }
        for from_city in cities
    }
    buy_lot = {city: {f"p{city}": 1} for city in cities}
    prices = {}
    for city in cities:
        product_id = f"p{city}"
        prices[product_id] = {
            "buy": {city: random_source.randint(80, 140)},
            "sell": {
                destination: random_source.randint(70, 240)
                for destination in cities
                if destination != city
            },
        }

    solver = _solver(cities=cities, costs=costs, buy_lot=buy_lot, prices=prices)
    fatigue_budget = 10
    cargo_capacity = 3
    book_budget = 1
    negotiation_budget = 2
    result = _solve(
        solver,
        fatigue_budget=fatigue_budget,
        cargo_capacity=cargo_capacity,
        book_budget=book_budget,
        negotiation_budget=negotiation_budget,
        all_plan=all_plan,
        bargain_success_rates_bps=[10000],
        bargain_step_bps=2000,
        raise_success_rates_bps=[10000],
        raise_step_bps=2000,
    )
    brute = _brute_force_best(
        solver,
        start="1",
        fatigue_budget=fatigue_budget,
        cargo_capacity=cargo_capacity,
        book_budget=book_budget,
        negotiation_budget=negotiation_budget,
        all_plan=all_plan,
    )

    if brute is None:
        assert result["status"] == "no_plan"
    else:
        assert result["status"] == "ok"
        assert Fraction(result["expected_profit_exact"]) == brute[0]
        assert Fraction(result["expected_fatigue_used_exact"]) == brute[1]
        assert result["books_used"] == brute[2]
        assert result["full_negotiation_used"] == brute[3]
        assert result["city_path_ids"] == list(brute[4])
        assert [
            (
                leg["from_city_id"],
                leg["to_city_id"],
                leg["books_used"],
                leg["bargain_to_cap"],
                leg["raise_to_cap"],
                tuple(leg["buy_product_ids"]),
            )
            for leg in result["route"]
        ] == [option.stable_signature for option in brute[5]]


def test_active_events_are_explicitly_ignored_with_warning():
    solver = _solver(
        cities=["1", "2"],
        costs={"1": {"2": 1}, "2": {"1": 1}},
        buy_lot={"1": {"p": 1}, "2": {}},
        prices={"p": {"buy": {"1": 100}, "sell": {"2": 200}}},
    )

    result = _solve(solver, fatigue_budget=1, active_events=["placeholder-event"])

    assert result["assumptions"]["active_events_included"] is False
    assert result["warnings"] == [
        "trade rule metadata is versioned but still requires validation against game samples",
        "active_events is accepted but ignored by this planner version",
    ]


def test_invalid_all_plan_is_rejected_without_integer_coercion():
    solver = _solver(
        cities=["1", "2"],
        costs={"1": {"2": 1}, "2": {"1": 1}},
        buy_lot={"1": {}, "2": {}},
        prices={},
    )

    with pytest.raises(ValueError, match="all_plan must be an integer"):
        _solve(solver, all_plan=0.5)
    with pytest.raises(ValueError, match="all_plan must be <= 1"):
        _solve(solver, all_plan=2)
