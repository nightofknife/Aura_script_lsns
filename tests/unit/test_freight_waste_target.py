"""Target routes respect fill-book policy while keeping fatigue primary."""
import pytest

from plans.resonance_pc.src.services.resonance_pc_trade_exact_solver import (
    ResonancePcExactTradeSolver,
)


def _product(buy, sell):
    return {"name": "Cargo", "market": {
        "buy": {city: {"price": price} for city, price in buy.items()},
        "sell": {city: {"price": price} for city, price in sell.items()},
    }}


def _model(*, costs=None, products=None, lots=None):
    costs = costs or {"A": {"B": 1}, "B": {}}
    return ResonancePcExactTradeSolver(
        snapshot={"snapshot_id": "target-fill", "products": products or {
            "p": _product({"A": 1}, {"B": 2})}},
        fatigue_payload={"cities": {city: city for city in costs}, "costs": costs},
        buy_lot=lots or {"A": {"p": 400}},
        trade_rules={"prestige_levels": {
            "20": {"general_tax_bps": 0, "extra_buy_bps": 0}},
            "negotiation": {"max_adjustment_bps": 2000, "attempt_fatigue": 1,
                "defaults": {"bargain_success_rates_bps": [10000], "bargain_step_bps": 2000,
                             "raise_success_rates_bps": [10000], "raise_step_bps": 2000}}},
        allowed_city_ids=tuple(costs),
    )


def _solve(model=None, **kwargs):
    request = dict(start_city_id="A", fatigue_budget=1, cargo_capacity=600,
                   book_budget=1, book_policy="fill", book_waste_limit=200,
                   book_profit_threshold=10**9, negotiation_policy="disabled",
                   target_profit=400)
    request.update(kwargs)
    return (model or _model()).solve(**request)


@pytest.mark.parametrize("budget", [1, 2, None])
def test_target_already_reachable_still_fills_with_legal_waste(budget):
    result = _solve(book_budget=budget)
    assert result["status"] == "ok"
    assert result["books_used"] == 1
    assert result["route"][0]["loaded_quantity"] == 600
    assert result["route"][0]["book_waste_quantity"] == 200


def test_target_fill_rejects_waste_above_limit():
    result = _solve(book_waste_limit=199)
    assert result["status"] == "ok"
    assert result["books_used"] == 0
    assert result["route"][0]["loaded_quantity"] == 400


def test_target_fill_stops_at_total_book_budget_before_full_load():
    result = _solve(cargo_capacity=1000, target_profit=400)
    assert result["books_used"] == 1
    assert result["route"][0]["loaded_quantity"] == 800
    assert result["remaining_books"] == 0


def test_target_fill_with_no_book_budget_keeps_baseline_load():
    result = _solve(book_budget=0)
    assert result["books_used"] == 0
    assert result["route"][0]["loaded_quantity"] == 400


def test_target_profit_policy_still_minimizes_books():
    result = _solve(book_policy="profit", book_profit_threshold=0)
    assert result["books_used"] == 0
    assert result["expected_profit"] == 400


def test_target_fill_keeps_minimum_fatigue_before_higher_profit():
    model = _model(costs={"A": {"B": 1, "C": 2}, "B": {}, "C": {}},
                   products={"p": _product({"A": 1}, {"B": 2, "C": 11})})
    result = _solve(model, fatigue_budget=2)
    assert result["city_path_ids"] == ["A", "B"]
    assert result["expected_fatigue_used"] == 1
    assert result["books_used"] == 1


def test_target_fill_keeps_books_before_navigation_to_required_endpoint():
    model = _model(costs={"A": {"B": 1}, "B": {"C": 1}, "C": {}})
    result = _solve(model, fatigue_budget=2, required_end_city_ids=["C"])
    assert result["city_path_ids"] == ["A", "B", "C"]
    assert result["books_used"] == 1
    assert result["route"][0]["loaded_quantity"] == 600


def test_target_fill_unlimited_books_does_not_expand_every_partial_load():
    model = _model(costs={"A": {"B": 1}, "B": {"C": 1}, "C": {}},
                   lots={"A": {"p": 1}})
    result = _solve(model, fatigue_budget=2, cargo_capacity=1000000,
                    target_profit=1, book_budget=None, book_waste_limit=0,
                    required_end_city_ids=["C"])
    assert result["books_used"] == 999999
    assert result["route"][0]["loaded_quantity"] == 1000000
    assert result["solver_stats"]["generated_labels"] <= 3


def test_target_fill_allocates_limited_books_to_later_better_leg():
    model = _model(costs={"A": {"B": 1}, "B": {"C": 1}, "C": {}},
                   products={"p": _product({"A": 1}, {"B": 2}),
                             "q": _product({"B": 1}, {"C": 11})},
                   lots={"A": {"p": 400}, "B": {"q": 400}})
    result = _solve(model, fatigue_budget=2, required_end_city_ids=["C"])
    assert [leg["books_used"] for leg in result["route"]] == [0, 1]
    assert result["books_used"] == 1
    assert result["expected_profit"] == 6400


def test_target_fill_preserves_higher_profit_label_after_goal_reached():
    model = _model(
        costs={"A": {"B": 1, "C": 1}, "B": {"X": 1},
               "C": {"X": 1}, "X": {"D": 1}, "D": {}},
        products={"p": _product({"B": 1}, {"X": 11}),
                  "q": _product({"C": 1}, {"X": 21})},
        lots={"B": {"p": 1}, "C": {"q": 1}},
    )
    result = _solve(model, cargo_capacity=2, book_waste_limit=0,
                    fatigue_budget=3, target_profit=10, required_end_city_ids=["D"])
    assert result["city_path_ids"] == ["A", "C", "X", "D"]
    assert result["books_used"] == 1
    assert result["expected_profit"] == 40
