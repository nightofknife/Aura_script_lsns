"""Freight waste allowance examples and public request boundaries, offline only."""
from __future__ import annotations

import inspect
import random

import pytest
from jinja2.nativetypes import NativeEnvironment

from packages.aura_core.scheduler.validation import InputValidator
from plans.resonance_pc.src.actions._freight_contract import normalize_planning_inputs
from plans.resonance_pc.src.actions import trade_planner_pc_actions as actions
from tests.contracts.test_resonance_pc_book_budget_contract import task
from tests.unit.test_resonance_pc_freight_mode_solver import assert_oracle, enumerate_oracle, product, solve, solver
from tests.unit.test_resonance_pc_sparkling_water_trade import harness, routes, run_full
from tests.unit.test_resonance_pc_freight_planner_service import service, plan


@pytest.mark.parametrize("backend", ["dense", "sparse"])
@pytest.mark.parametrize("mode", ["free", "fixed"])
@pytest.mark.parametrize("limit,books,quantity,waste", [(0, 0, 400, 0), (199, 0, 400, 0),
                                                       (200, 1, 600, 200), (999, 1, 600, 200)])
def test_600_capacity_400_stock_uses_one_book_at_inclusive_waste_boundary(
        backend, mode, limit, books, quantity, waste):
    result = solve(solver(lots={"A": {"p": 400}}), cargo_capacity=600,
                   trade_mode=mode, fixed_route_city_ids=["A", "B"],
                   book_policy="fill", book_budget=None, book_waste_limit=limit,
                   book_profit_threshold=10**12, _backend=backend)
    assert result["status"] == "ok"
    assert result["books_used"] == books
    leg = result["route"][0]
    assert leg["loaded_quantity"] == quantity
    assert leg["book_waste_quantity"] == waste
    assert leg["book_stop_reason"] == ("capacity_reached" if books else "book_waste_limit")
    assert result["book_waste_limit"] == result["request"]["book_waste_limit"] == limit


@pytest.mark.parametrize("limit,budget,books,quantity", [
    (199, None, 1, 800), (200, None, 2, 1000), (200, 1, 1, 800), (200, 0, 0, 400)])
def test_multi_book_fill_keeps_partial_load_without_exceeding_waste_or_budget(limit, budget, books, quantity):
    result = solve(solver(lots={"A": {"p": 400}}), cargo_capacity=1000,
                   book_policy="fill", book_budget=budget, book_waste_limit=limit,
                   book_profit_threshold=10**12)
    assert result["books_used"] == books
    assert result["route"][0]["loaded_quantity"] == quantity
    assert result["route"][0]["book_waste_quantity"] <= limit


def test_waste_counts_all_eligible_goods_and_ignores_unprofitable_goods():
    model = solver(products={"p": product("p", {"A": 1}, {"B": 11}),
                             "q": product("q", {"A": 1}, {"B": 2}),
                             "loss": product("loss", {"A": 10}, {"B": 1})},
                   lots={"A": {"p": 200, "q": 200, "loss": 9999}})
    result = solve(model, cargo_capacity=600, book_policy="fill", book_budget=None,
                   book_waste_limit=200, book_profit_threshold=10**12)
    assert result["books_used"] == 1
    assert result["route"][0]["book_waste_quantity"] == 200
    assert result["route"][0]["buy_product_ids"] == ["p", "q"]


@pytest.mark.parametrize("threshold", [0, 10**12, -1, True, "inactive"])
def test_fill_never_consults_saved_profit_threshold(threshold):
    result = solve(solver(lots={"A": {"p": 400}}), cargo_capacity=600,
                   book_policy="fill", book_budget=1, book_waste_limit=200,
                   book_profit_threshold=threshold)
    assert result["books_used"] == 1


def test_profit_keeps_its_threshold_and_ignores_fill_waste_limit():
    model = solver(lots={"A": {"p": 400}})
    assert solve(model, cargo_capacity=600, book_policy="profit", book_budget=1,
                 book_waste_limit=0, book_profit_threshold=0)["books_used"] == 1
    assert solve(model, cargo_capacity=600, book_policy="profit", book_budget=1,
                 book_waste_limit=200, book_profit_threshold=10**12)["books_used"] == 0


@pytest.mark.parametrize("value", [-1, True, 1.5, "200", None])
def test_active_waste_limit_must_be_nonnegative_integer(value):
    with pytest.raises(ValueError, match="book_waste_limit"):
        normalize_planning_inputs(dict(book_policy="fill", book_budget=1, book_waste_limit=value))
    with pytest.raises(ValueError, match="book_waste_limit"):
        solve(solver(), book_policy="fill", book_budget=1, book_waste_limit=value)


def test_whole_route_book_budget_is_shared_between_cities():
    model = solver(cities=("A", "B", "C"), costs={"A": {"B": 1}, "B": {"C": 1}, "C": {}},
                   products={"p": product("p", {"A": 1}, {"B": 11}),
                             "q": product("q", {"B": 1}, {"C": 101})},
                   lots={"A": {"p": 400}, "B": {"q": 400}})
    result = solve(model, cargo_capacity=600, fatigue_budget=2, required_end_city_ids=["C"],
                   book_policy="fill", book_budget=1, book_waste_limit=200,
                   book_profit_threshold=10**12)
    assert result["books_used"] == 1
    assert [leg["books_used"] for leg in result["route"]] == [0, 1]
    assert [leg["loaded_quantity"] for leg in result["route"]] == [400, 600]


@pytest.mark.parametrize("name", ["auto_cycle_trade_pc", "preview_trade_plan_pc"])
def test_task_schema_forwards_waste_limit_to_action(name):
    from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade
    spec = task(name)
    supplied = dict(book_policy="fill", book_budget=1, book_waste_limit=200)
    if name == "preview_trade_plan_pc":
        supplied["start_city_id"] = "1"
    ok, inputs = InputValidator(None).validate_inputs_against_meta(spec["meta"]["inputs"], supplied)
    assert ok, inputs
    rendered = {key: NativeEnvironment().from_string(value).render(inputs=inputs)
                for key, value in spec["steps"]["run"]["params"].items()}
    action = trade.resonance_pc_auto_cycle_trade_flow if name == "auto_cycle_trade_pc" else trade.resonance_pc_preview_trade_plan_flow
    inspect.signature(action).bind(**rendered)
    assert rendered["book_waste_limit"] == 200


def test_planner_action_passes_active_waste_limit():
    calls = []
    class Planner:
        def plan_optimal_route(self, **kwargs):
            calls.append(kwargs)
            return {"status": "ok"}
    actions.resonance_pc_trade_plan_optimal_route(book_policy="fill", book_budget=1,
        book_waste_limit=200, resonance_pc_trade_planner=Planner())
    assert calls[0]["book_waste_limit"] == 200


def test_waste_allowance_participates_in_cache_but_fill_profit_threshold_does_not(service):
    api, calls = service
    first = plan(api, book_budget=1, book_policy="fill", book_waste_limit=0, book_profit_threshold=1)
    second = plan(api, book_budget=1, book_policy="fill", book_waste_limit=1, book_profit_threshold=1)
    assert len(calls) == 2
    cached = plan(api, book_budget=1, book_policy="fill", book_waste_limit=1, book_profit_threshold=10**12)
    assert len(calls) == 2
    assert first["book_waste_limit"] == 0 and second == cached


def test_execution_request_preserves_fill_waste_limit(harness, monkeypatch):
    from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade
    calls = []
    def planner(**kwargs):
        calls.append(kwargs)
        return {"status": "ok", "route": routes(), "book_budget": 1, "books_used": 1}
    monkeypatch.setattr(trade, "resonance_pc_trade_plan_optimal_route", planner)
    result = run_full(book_budget=1, book_policy="fill", book_waste_limit=200,
                      auto_sparkling_water=False)
    assert result["success"] is True
    assert calls[0]["book_waste_limit"] == 200
    assert result["book_waste_limit"] == result["input_snapshot"]["book_waste_limit"] == 200


def test_random_fill_target_matches_independent_exhaustive_routes():
    rng = random.Random(20261015)
    for case in range(100):
        cities = ("A", "B", "C")
        costs = {city: {dest: rng.randint(1, 3) for dest in cities if dest != city} for city in cities}
        products, lots = {}, {city: {} for city in cities}
        for index in range(2):
            pid = f"p{index}"
            products[pid] = product(pid, {city: rng.randint(1, 5) for city in cities},
                                   {city: rng.randint(2, 10) for city in cities})
            for city in cities:
                lots[city][pid] = rng.randint(1, 3)
        model = solver(cities=cities, costs=costs, products=products, lots=lots)
        capacity, budget = rng.randint(2, 7), rng.randint(1, 4)
        books, waste, target = rng.choice([0, 1, 2, None]), rng.randint(0, capacity), rng.randint(1, 50)
        ends = rng.choice([None, ["B"], ["B", "C"]])
        expected, maximum = enumerate_oracle(model, mode="target", policy="profit" if books == 0 else "fill", capacity=capacity,
            books=books, threshold=10**12, negotiation="disabled", budget=budget, target=target,
            endpoints=ends, waste_limit=waste)
        actual = solve(model, book_policy="fill", cargo_capacity=capacity, fatigue_budget=budget,
            book_budget=books, book_waste_limit=waste, book_profit_threshold=10**12,
            target_profit=target, required_end_city_ids=ends)
        try:
            assert_oracle(actual, expected)
            assert actual["maximum_reachable_profit"] == maximum
        except AssertionError as exc:
            raise AssertionError(f"case={case}, expected={expected}, actual={actual}") from exc
