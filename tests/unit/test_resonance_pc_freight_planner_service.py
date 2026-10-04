"""Four-mode public service and cache isolation contracts, without UI calls."""
from __future__ import annotations

from copy import deepcopy
import inspect

import pytest

from plans.resonance_pc.src.services import resonance_pc_trade_planner_service as module
from tests.unit.test_resonance_pc_freight_mode_solver import solver, product


@pytest.fixture
def service(monkeypatch):
    model = solver(cities=("A", "B", "C"), costs={"A": {"B": 1, "C": 1},
                   "B": {"A": 1, "C": 1}, "C": {"A": 1, "B": 1}},
                   products={"p": product("p", {"A": 1, "B": 1}, {"B": 11, "C": 11})},
                   lots={"A": {"p": 1}, "B": {"p": 1}})
    class Market:
        def get_latest(self):
            return deepcopy(model.snapshot)
        def get_all_travel_fatigue(self):
            return {"cities": model.city_names, "costs": model.fatigue_costs}
    result = module.ResonancePcTradePlannerService(Market())
    monkeypatch.setattr(result, "_load_trade_constraints_payload", lambda: {
        "default_available_city_ids": ["A", "B", "C"], "key_to_city_id": {}})
    monkeypatch.setattr(result, "_load_buy_lot_payload", lambda: {"city_product_buy_lot": model.buy_lot})
    monkeypatch.setattr(result, "_load_product_unlocks_payload", lambda: {"product_ids": []})
    monkeypatch.setattr(result, "_load_trade_rules_payload", lambda: model.rules)
    calls = []
    original = module.ResonancePcExactTradeSolver.solve
    def record(self, **kwargs):
        calls.append(deepcopy(kwargs))
        return original(self, **kwargs)
    monkeypatch.setattr(module.ResonancePcExactTradeSolver, "solve", record)
    return result, calls


def plan(service, **kwargs):
    args = dict(current_city_id="A", fatigue_budget=5, cargo_capacity=3,
                negotiation_policy="disabled", book_profit_threshold=0)
    args.update(kwargs)
    return service.plan_optimal_route(**args)


def test_public_contract_removes_auto_book_and_repeat_count():
    signature = inspect.signature(module.ResonancePcTradePlannerService.plan_optimal_route).parameters
    assert signature["trade_mode"].default == "profit"
    assert signature["fatigue_budget"].default == 700
    assert signature["cargo_capacity"].default == 750
    assert signature["book_budget"].default == 0
    assert signature["reposition_to_route"].default is False
    assert signature["book_profit_threshold"].default == 500000
    assert not {"auto_book", "all_plan", "fixed_route_repeat_count"} & signature.keys()


def test_finite_and_infinite_cache_keys_are_distinct_and_results_are_deep_copied(service):
    api, calls = service
    original = plan(api, book_budget=None)
    original["route"][0]["buys"].clear()
    original["request"]["city_prestige"].clear()
    cached = plan(api, book_budget=None)
    assert len(calls) == 1
    assert cached["route"][0]["buys"]
    assert cached["request"]["city_prestige"]
    for budget in (0, 1, 2):
        result = plan(api, book_budget=budget)
        assert result["book_budget"] == budget
    assert len(calls) == 4


def test_modes_targets_full_fixed_order_and_reposition_participate_in_cache(service):
    api, calls = service
    requests = [dict(trade_mode="profit"), dict(trade_mode="quick"),
                dict(trade_mode="target", target_profit=10), dict(trade_mode="target", target_profit=20),
                dict(trade_mode="fixed", fixed_route_city_ids=["A", "B", "A"]),
                dict(trade_mode="fixed", fixed_route_city_ids=["A", "B", "A", "B", "A"]),
                dict(trade_mode="fixed", fixed_route_city_ids=["A", "B", "A"], reposition_to_route=True)]
    for request in requests:
        plan(api, **request)
    assert len(calls) == len(requests)
    for request in requests:
        plan(api, **request)
    assert len(calls) == len(requests)
    assert calls[1]["book_policy"] == "fill" and calls[1]["negotiation_policy"] == "required"


def test_irrelevant_mode_fields_are_excluded_from_cache(service):
    api, calls = service
    first = plan(api, target_profit=999, fixed_route_city_ids=["A", "B"], reposition_to_route=True)
    second = plan(api, target_profit=123, fixed_route_city_ids=["B", "A"])
    assert len(calls) == 1
    assert first == second


def test_fixed_ignores_free_route_city_scope_but_keeps_current_city_for_reposition(service):
    api, _ = service
    result = plan(api, trade_mode="fixed", fixed_route_city_ids=["B", "C", "B"],
                  reposition_to_route=True, available_city_ids=["A", "B"], required_end_city_ids=["A"])
    assert result["status"] == "ok"
    assert result["required_end_city_ids"] == []
    assert result["reposition_city_path_ids"] == ["A", "B"]
    assert set(result["city_path_ids"]) == {"B", "C"}


@pytest.mark.parametrize("inputs", [{"book_budget": -1}, {"book_budget": True},
    {"trade_mode": "unknown"}, {"trade_mode": "target", "target_profit": 0},
    {"trade_mode": "fixed", "fixed_route_city_ids": "AB"},
    {"trade_mode": "fixed", "fixed_route_city_ids": ["B", "C"]}])
def test_invalid_inputs_raise_structured_error(service, inputs):
    api, _ = service
    with pytest.raises(module.ResonancePcTradePlannerError) as error:
        plan(api, **inputs)
    assert error.value.code == "invalid_optimal_route_input"
