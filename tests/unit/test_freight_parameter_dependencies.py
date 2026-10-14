"""Active-field projection is identical for GUI and offline task boundaries."""
from copy import deepcopy

import pytest

from plans.resonance_pc.src.services.resonance_pc_freight_parameters import effective_freight_inputs
from plans.resonance_pc.src.actions._freight_contract import normalize_planning_inputs


def test_projection_keeps_draft_values_while_removing_inactive_runtime_fields():
    draft = dict(trade_mode="free", book_budget=0, book_policy="fill", book_profit_threshold=123,
        target_profit_enabled=False, target_profit=9000000, fixed_route_city_ids=["1", "2"],
        reposition_to_route=True, negotiation_policy="disabled", negotiation_max_attempts=6,
        auto_sparkling_water=False, auto_bento=False, bento_priority=["love_bentos"],
        base_fatigue_reserve=456, use_fatigue_medicine=False,
        allowed_fatigue_medicines=["提神棒棒糖"], fatigue_medicine_max_uses=2)
    original = deepcopy(draft)
    result = effective_freight_inputs(draft)
    assert draft == original
    assert result == dict(trade_mode="free", book_budget=0, negotiation_policy="disabled",
                          auto_sparkling_water=False, auto_bento=False, use_fatigue_medicine=False)
    canonical = normalize_planning_inputs(draft)
    assert canonical["trade_mode"] == "free" and canonical["book_budget"] == 0
    assert "target_profit" not in canonical and "fixed_route_city_ids" not in canonical


@pytest.mark.parametrize("books", [2, None])
@pytest.mark.parametrize("mode", ["free", "fixed"])
def test_fill_policy_remains_independent_of_negotiation_and_books(mode, books):
    request = dict(trade_mode=mode, book_budget=books, book_policy="fill",
                   book_profit_threshold=500000, negotiation_policy="disabled",
                   fixed_route_city_ids=["1", "2", "1"])
    result = normalize_planning_inputs(request)
    assert result["book_budget"] == books
    assert result["book_policy"] == "fill"
    assert result["negotiation_policy"] == "disabled"
    assert result["book_profit_threshold"] == 0


@pytest.mark.parametrize("books", [1, 2, None])
def test_fill_removes_inactive_threshold_without_mutating_saved_form(books):
    draft = dict(trade_mode="free", book_budget=books, book_policy="fill",
                 book_profit_threshold=10**12)
    original = deepcopy(draft)
    assert "book_profit_threshold" not in effective_freight_inputs(draft)
    result = normalize_planning_inputs(draft)
    assert result["book_profit_threshold"] == 0
    assert result["book_budget"] == books
    assert draft == original
    draft["book_policy"] = "profit"
    assert normalize_planning_inputs(draft)["book_profit_threshold"] == 10**12


def test_target_is_optional_for_free_and_irrelevant_to_fixed():
    assert "target_profit" not in normalize_planning_inputs({"trade_mode": "free", "target_profit": None})
    assert normalize_planning_inputs({"trade_mode": "free", "target_profit": 5000000})["target_profit"] == 5000000
    fixed = normalize_planning_inputs({"trade_mode": "fixed", "fixed_route_city_ids": ["1", "2"],
                                      "target_profit": "inactive malformed draft"})
    assert "target_profit" not in fixed


@pytest.mark.parametrize("mode", ["profit", "quick", "target"])
def test_removed_route_modes_are_rejected_at_the_shared_boundary(mode):
    with pytest.raises(ValueError, match="free or fixed"):
        normalize_planning_inputs({"trade_mode": mode})


@pytest.mark.parametrize("goal", [True, False, 0, -1, 1.5])
def test_active_target_must_be_positive_integral_currency(goal):
    with pytest.raises(ValueError):
        normalize_planning_inputs({"trade_mode": "free", "target_profit": goal})
