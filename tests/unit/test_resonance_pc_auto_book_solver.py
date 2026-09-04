"""Focused contracts for the Resonance PC Auto Book exact solver."""

from __future__ import annotations

from fractions import Fraction
import random
from typing import Any, Dict, Mapping, Sequence

import pytest

from plans.resonance_pc.src.services.resonance_pc_trade_exact_solver import (
    ResonancePcExactTradeSolver,
)


def _product(
    name: str,
    *,
    buy: Mapping[str, int],
    sell: Mapping[str, int],
) -> Dict[str, Any]:
    return {
        "name": name,
        "market": {
            "buy": {
                city_id: {"price": price}
                for city_id, price in buy.items()
            },
            "sell": {
                city_id: {"price": price}
                for city_id, price in sell.items()
            },
        },
    }


def _solver(
    *,
    cities: Sequence[str],
    costs: Mapping[str, Mapping[str, int]],
    products: Mapping[str, Mapping[str, Any]],
    buy_lot: Mapping[str, Mapping[str, int]],
) -> ResonancePcExactTradeSolver:
    return ResonancePcExactTradeSolver(
        snapshot={"snapshot_id": "auto-book-test", "products": products},
        fatigue_payload={
            "cities": {city_id: city_id for city_id in cities},
            "costs": costs,
        },
        buy_lot=buy_lot,
        trade_rules={
            "schema_version": 1,
            "model_version": "test",
            "rounding": {"mode": "javascript_math_round"},
            "prestige_levels": {
                "20": {"general_tax_bps": 0, "extra_buy_bps": 0}
            },
            "negotiation": {
                "model": "test",
                "max_adjustment_bps": 2000,
                "attempt_fatigue": 8,
                "defaults": {
                    "bargain_success_rates_bps": [10_000],
                    "bargain_step_bps": 2000,
                    "raise_success_rates_bps": [10_000],
                    "raise_step_bps": 2000,
                },
            },
        },
        allowed_city_ids=cities,
    )


def _solve(
    solver: ResonancePcExactTradeSolver,
    *,
    end_city_ids: Sequence[str] | None,
    cargo_capacity: int,
    book_budget: int,
    threshold: int,
    auto_book: bool,
    fatigue_budget: int = 1,
    backend: str | None = None,
) -> Dict[str, Any]:
    return solver.solve(
        start_city_id="A",
        required_end_city_ids=end_city_ids,
        fatigue_budget=fatigue_budget,
        cargo_capacity=cargo_capacity,
        book_budget=book_budget,
        book_profit_threshold=threshold,
        negotiation_budget=0,
        auto_book=auto_book,
        _backend=backend,
    )


def _one_edge_solver(unit_profit: int, *, lot: int = 1) -> ResonancePcExactTradeSolver:
    return _solver(
        cities=["A", "B"],
        costs={"A": {"B": 1}, "B": {}},
        products={
            "p": _product(
                "product",
                buy={"A": 1},
                sell={"B": unit_profit + 1},
            )
        },
        buy_lot={"A": {"p": lot}},
    )


def test_auto_book_uses_strict_threshold_while_manual_keeps_equality() -> None:
    solver = _one_edge_solver(500_000)

    automatic = _solve(
        solver,
        end_city_ids=["B"],
        cargo_capacity=3,
        book_budget=99,
        threshold=500_000,
        auto_book=True,
    )
    manual = _solve(
        solver,
        end_city_ids=["B"],
        cargo_capacity=3,
        book_budget=2,
        threshold=500_000,
        auto_book=False,
    )

    assert automatic["books_used"] == 0
    assert automatic["average_book_profit"] is None
    assert manual["books_used"] == 2
    assert manual["book_incremental_profit"] == 1_000_000
    assert manual["average_book_profit"] == 500_000


def test_auto_book_accepts_strictly_greater_margins_and_ignores_budget() -> None:
    solver = _one_edge_solver(500_001)

    zero_budget = _solve(
        solver,
        end_city_ids=["B"],
        cargo_capacity=3,
        book_budget=0,
        threshold=500_000,
        auto_book=True,
        backend="dense",
    )
    arbitrary_budget = _solve(
        solver,
        end_city_ids=["B"],
        cargo_capacity=3,
        book_budget=73,
        threshold=500_000,
        auto_book=True,
    )

    assert zero_budget == arbitrary_budget
    assert zero_budget["auto_book"] is True
    assert zero_budget["book_budget_ignored"] is True
    assert zero_budget["books_budget"] is None
    assert zero_budget["remaining_books"] is None
    assert zero_budget["books_used"] == 2
    assert zero_budget["book_incremental_profit"] == 1_000_002
    assert zero_budget["book_incremental_profit_exact"] == "1000002"
    assert zero_budget["average_book_profit"] == 500_001
    assert zero_budget["average_book_profit_exact"] == "500001"
    assert zero_budget["route"][0]["book_incremental_profit_exact"] == "1000002"


def test_manual_mode_dense_and_sparse_results_remain_identical() -> None:
    solver = _one_edge_solver(500_000)
    arguments = {
        "end_city_ids": ["B"],
        "cargo_capacity": 3,
        "book_budget": 2,
        "threshold": 500_000,
        "auto_book": False,
    }

    assert _solve(solver, backend="dense", **arguments) == _solve(
        solver,
        backend="sparse",
        **arguments,
    )


def test_auto_book_jointly_changes_the_selected_route() -> None:
    solver = _solver(
        cities=["A", "B", "C"],
        costs={"A": {"B": 1, "C": 1}, "B": {}, "C": {}},
        products={
            "wide": _product(
                "wide",
                buy={"A": 1},
                sell={"B": 101},
            ),
            "scalable": _product(
                "scalable",
                buy={"A": 1},
                sell={"C": 251},
            ),
        },
        buy_lot={"A": {"wide": 3, "scalable": 1}},
    )

    manual = _solve(
        solver,
        end_city_ids=None,
        cargo_capacity=4,
        book_budget=0,
        threshold=200,
        auto_book=False,
    )
    automatic = _solve(
        solver,
        end_city_ids=None,
        cargo_capacity=4,
        book_budget=0,
        threshold=200,
        auto_book=True,
    )

    assert manual["selected_end_city_id"] == "B"
    assert manual["expected_profit"] == 300
    assert automatic["selected_end_city_id"] == "C"
    assert automatic["expected_profit"] == 1_000
    assert automatic["books_used"] == 3


def test_auto_book_profit_tie_prefers_the_route_using_fewer_books() -> None:
    solver = _solver(
        cities=["A", "B", "C"],
        costs={"A": {"B": 2, "C": 1}, "B": {}, "C": {"B": 1}},
        products={
            "direct": _product(
                "direct",
                buy={"A": 1},
                sell={"B": 51},
            ),
            "first_leg": _product(
                "first_leg",
                buy={"A": 1},
                sell={"C": 26},
            ),
            "second_leg": _product(
                "second_leg",
                buy={"C": 1},
                sell={"B": 26},
            ),
        },
        buy_lot={
            "A": {"direct": 1, "first_leg": 2},
            "C": {"second_leg": 3},
        },
    )

    result = _solve(
        solver,
        end_city_ids=["B"],
        cargo_capacity=3,
        book_budget=0,
        threshold=0,
        auto_book=True,
        fatigue_budget=2,
    )

    assert result["expected_profit"] == 150
    assert result["city_path_ids"] == ["A", "C", "B"]
    assert result["books_used"] == 1


def test_auto_book_sparse_search_preserves_negotiation_budget() -> None:
    result = _one_edge_solver(10).solve(
        start_city_id="A",
        required_end_city_ids=["B"],
        fatigue_budget=9,
        cargo_capacity=3,
        book_budget=0,
        book_profit_threshold=0,
        negotiation_budget=1,
        auto_book=True,
    )

    assert result["status"] == "ok"
    assert result["books_used"] == 2
    assert result["full_negotiation_used"] == 1
    assert result["full_bargain_count"] + result["full_raise_count"] == 1


def test_book_increment_uses_each_selected_edge_zero_book_baseline() -> None:
    solver = _solver(
        cities=["A", "B", "C"],
        costs={"A": {"B": 1}, "B": {"C": 1}, "C": {}},
        products={
            "ab": _product("ab", buy={"A": 1}, sell={"B": 6}),
            "bc": _product("bc", buy={"B": 1}, sell={"C": 8}),
        },
        buy_lot={"A": {"ab": 2}, "B": {"bc": 1}},
    )

    result = _solve(
        solver,
        end_city_ids=["C"],
        cargo_capacity=3,
        book_budget=0,
        threshold=0,
        auto_book=True,
        fatigue_budget=2,
    )

    assert result["city_path_ids"] == ["A", "B", "C"]
    assert [step["books_used"] for step in result["route"]] == [1, 2]
    assert [
        step["book_incremental_profit_exact"] for step in result["route"]
    ] == ["5", "14"]
    assert result["books_used"] == 3
    assert result["book_incremental_profit"] == 19
    assert result["book_incremental_profit_exact"] == "19"
    assert result["average_book_profit"] == pytest.approx(19 / 3)
    assert result["average_book_profit_exact"] == "19/3"


def test_auto_book_zero_threshold_stops_at_large_capacity_saturation() -> None:
    result = _solve(
        _one_edge_solver(1),
        end_city_ids=["B"],
        cargo_capacity=1_000_000,
        book_budget=0,
        threshold=0,
        auto_book=True,
    )

    assert result["books_used"] == 999_999
    assert result["expected_profit"] == 1_000_000


def test_binary_auto_edge_choice_matches_small_bruteforce_family() -> None:
    solver = _solver(
        cities=["A", "B"],
        costs={"A": {"B": 1}, "B": {}},
        products={
            "top": _product("top", buy={"A": 1}, sell={"B": 11}),
            "second": _product(
                "second",
                buy={"A": 1},
                sell={"B": 7},
            ),
        },
        buy_lot={"A": {"top": 1, "second": 2}},
    )
    common = {
        "from_city": "A",
        "to_city": "B",
        "bargain_to_cap": False,
        "raise_to_cap": False,
        "travel_fatigue": 1,
        "expected_bargain_fatigue": Fraction(0, 1),
        "expected_raise_fatigue": Fraction(0, 1),
        "cargo_capacity": 7,
        "prestige_by_city": {"A": 20, "B": 20},
        "unlocked_products": None,
    }
    family = solver._build_edge_option_family(book_budget=6, **common)
    expected = family[0]
    for previous, candidate in zip(family, family[1:]):
        if candidate.expected_profit - previous.expected_profit <= 5:
            break
        expected = candidate

    automatic = solver._build_auto_edge_option(
        book_profit_threshold=Fraction(5, 1),
        **common,
    )

    assert automatic.books_used == expected.books_used == 2
    assert automatic.expected_profit == expected.expected_profit
    assert automatic.buys == expected.buys
    assert automatic.book_incremental_profit == (
        expected.expected_profit - family[0].expected_profit
    )


def test_binary_auto_edge_choice_matches_randomized_bruteforce_families() -> None:
    rng = random.Random(20260905)
    for case_index in range(100):
        product_count = rng.randint(1, 5)
        capacity = rng.randint(1, 30)
        threshold = rng.randint(0, 80)
        products: Dict[str, Mapping[str, Any]] = {}
        lots: Dict[str, int] = {}
        for product_index in range(product_count):
            product_id = f"p{product_index}"
            unit_profit = rng.randint(1, 40)
            products[product_id] = _product(
                product_id,
                buy={"A": 1},
                sell={"B": unit_profit + 1},
            )
            lots[product_id] = rng.randint(1, 6)

        solver = _solver(
            cities=["A", "B"],
            costs={"A": {"B": 1}, "B": {}},
            products=products,
            buy_lot={"A": lots},
        )
        common = {
            "from_city": "A",
            "to_city": "B",
            "bargain_to_cap": False,
            "raise_to_cap": False,
            "travel_fatigue": 1,
            "expected_bargain_fatigue": Fraction(0, 1),
            "expected_raise_fatigue": Fraction(0, 1),
            "cargo_capacity": capacity,
            "prestige_by_city": {"A": 20, "B": 20},
            "unlocked_products": None,
        }
        family = solver._build_edge_option_family(
            book_budget=capacity,
            **common,
        )
        expected = family[0]
        for previous, candidate in zip(family, family[1:]):
            if candidate.expected_profit - previous.expected_profit <= threshold:
                break
            expected = candidate

        automatic = solver._build_auto_edge_option(
            book_profit_threshold=Fraction(threshold, 1),
            **common,
        )

        assert automatic.books_used == expected.books_used, case_index
        assert automatic.expected_profit == expected.expected_profit, case_index
        assert automatic.buys == expected.buys, case_index
        assert automatic.book_incremental_profit == (
            expected.expected_profit - family[0].expected_profit
        ), case_index


def test_no_plan_result_keeps_auto_book_contract() -> None:
    result = _solve(
        _one_edge_solver(-1),
        end_city_ids=["B"],
        cargo_capacity=3,
        book_budget=12,
        threshold=500_000,
        auto_book=True,
    )

    assert result["status"] == "no_plan"
    assert result["auto_book"] is True
    assert result["book_budget_ignored"] is True
    assert result["books_budget"] is None
    assert result["remaining_books"] is None
    assert result["book_profit_threshold"] == 500_000
    assert result["book_incremental_profit"] == 0
    assert result["book_incremental_profit_exact"] == "0"
    assert result["average_book_profit"] is None
    assert result["average_book_profit_exact"] is None
