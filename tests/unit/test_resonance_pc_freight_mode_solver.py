"""Independent exhaustive oracles for all freight objectives and full ties."""
from __future__ import annotations

from copy import deepcopy
from fractions import Fraction
import random

import pytest

from plans.resonance_pc.src.services.resonance_pc_trade_exact_solver import (
    ResonancePcExactTradeSolver, expected_fatigue_to_cap,
)


def product(name, buy, sell):
    return {"name": name, "market": {
        "buy": {city: {"price": price} for city, price in buy.items()},
        "sell": {city: {"price": price} for city, price in sell.items()}}}


def solver(cities=("A", "B"), costs=None, products=None, lots=None, attempt_fatigue=1):
    return ResonancePcExactTradeSolver(
        snapshot={"snapshot_id": "frozen-oracle", "products": products or {
            "p": product("p", {"A": 1}, {"B": 11})}},
        fatigue_payload={"cities": {city: city for city in cities},
                         "costs": costs if costs is not None else {"A": {"B": 1}, "B": {}}},
        buy_lot=lots if lots is not None else {"A": {"p": 1}},
        trade_rules={"schema_version": 1, "model_version": "oracle",
                     "prestige_levels": {"20": {"general_tax_bps": 0, "extra_buy_bps": 0}},
                     "negotiation": {"max_adjustment_bps": 2000, "attempt_fatigue": attempt_fatigue,
                         "defaults": {"bargain_success_rates_bps": [10000], "bargain_step_bps": 2000,
                                      "raise_success_rates_bps": [10000], "raise_step_bps": 2000}}},
        allowed_city_ids=cities)


def solve(model, **kwargs):
    args = dict(start_city_id="A", fatigue_budget=1, cargo_capacity=3,
                book_budget=0, book_profit_threshold=0, negotiation_policy="disabled")
    args.update(kwargs)
    return model.solve(**args)


def semantic(result):
    return {key: value for key, value in result.items() if key not in {"solver_backend", "solver_stats"}}


def test_user_crystal_prices_settle_identically_in_both_modes_and_goal():
    model = solver(products={"p": product("水晶工艺品", {"A": 688}, {"B": 792})},
                   lots={"A": {"p": 2}})
    for mode, target in (("free", None), ("fixed", None), ("free", 96)):
        result = solve(model, cargo_capacity=2, trade_mode=mode, target_profit=target,
                       fixed_route_city_ids=["A", "B"], city_tax_rates_bps={"A": 700, "B": 750})
        assert result["status"] == "ok"
        assert result["expected_profit_exact"] == "96"
        assert result["route"][0]["settlement"] == {
            "buy_goods_amount": 1376, "buy_tax": 96, "buy_total": 1472,
            "sell_goods_amount": 1584, "taxable_profit": 208, "sell_tax": 16,
            "sell_total": 1568, "net_profit": 96,
        }
        assert result["assumptions"]["execution_quantities_verified"] is False
    unreachable = solve(model, cargo_capacity=2, target_profit=97,
                        city_tax_rates_bps={"A": 700, "B": 750})
    assert unreachable["status"] != "ok"


@pytest.mark.parametrize("taxes", [{"A": True}, {"A": -1}, {"A": 10001}, {"A": 0.5}, {"unknown": 700}, []])
def test_solver_rejects_invalid_actual_tax_overrides(taxes):
    with pytest.raises(ValueError, match="city_tax_rates_bps"):
        solve(solver(), city_tax_rates_bps=taxes)


def test_native_parent_prestige_and_city_specific_actual_tax_are_independent():
    model = solver(cities=("A", "B", "C"), costs={"A": {"B": 1}, "B": {}, "C": {}},
                   lots={"A": {"p": 1}})
    model.rules["prestige_levels"]["1"] = {"general_tax_bps": 1000, "extra_buy_bps": 1000}
    model.rules["tax"] = {"city_rules": {
        "A": {"prestige_city_id": "A", "delta_bps": 0, "supported_currency": True},
        "B": {"prestige_city_id": "B", "fixed_tax_bps": 1000, "supported_currency": True},
        "C": {"prestige_city_id": "A", "delta_bps": -300, "supported_currency": True},
    }}
    prestige = model._normalize_city_prestige({"default": 20, "overrides": {"A": 1}})
    assert prestige["A"] == prestige["C"] == 1
    assert model._tax_bps("C", prestige["C"]) == 700
    assert model._tax_bps("B", prestige["B"]) == 1000
    result = solve(model, city_prestige={"default": 20, "overrides": {"A": 1}},
                   city_tax_rates_bps={"C": 750})
    assert model._tax_bps("C", prestige["C"]) == 750
    assert result["request"]["city_tax_rates_bps"] == {"C": 750}


def test_native_tatu_has_no_generic_prestige_purchase_multiplier():
    model = solver(cities=("17", "B"), costs={"17": {"B": 1}, "B": {}},
                   products={"p": product("p", {"17": 100}, {"B": 200})},
                   lots={"17": {"p": 2}})
    model.rules["prestige_levels"]["20"]["extra_buy_bps"] = 20000
    model.rules["tax"] = {"city_rules": {
        "17": {"prestige_city_id": "17", "fixed_tax_bps": 1000,
               "fixed_extra_buy_bps": 0, "supported_currency": True},
        "B": {"prestige_city_id": "B", "delta_bps": 0, "supported_currency": True},
    }}
    result = solve(model, start_city_id="17", cargo_capacity=20)
    assert result["route"][0]["loaded_quantity"] == 2
    assert result["route"][0]["settlement"]["buy_goods_amount"] == 200


def _oracle_options(model, capacity, books, threshold, policy, negotiation, waste_limit=0, *, fixed=False):
    """Price and enumerate independently: no production candidate or pruning code."""
    result = {}
    flags = {"auto": [(0, 0), (0, 1), (1, 0), (1, 1)],
             "disabled": [(0, 0)], "required": [(1, 1)]}[negotiation]
    rules = model.rules["negotiation"]
    def negotiation_fatigue(side):
        defaults = rules["defaults"]
        rates = defaults[side + "_success_rates_bps"]
        successes = (rules["max_adjustment_bps"] + defaults[side + "_step_bps"] - 1) // defaults[side + "_step_bps"]
        stages = [rates[min(index, len(rates) - 1)] for index in range(successes)]
        return None if 0 in stages else sum((Fraction(10000, rate) for rate in stages), Fraction(0)) * rules["attempt_fatigue"]
    bargain_cost, raise_cost = negotiation_fatigue("bargain"), negotiation_fatigue("raise")
    for origin, row in model.fatigue_costs.items():
        for destination, travel in row.items():
            if travel <= 0 or origin == destination:
                continue
            options = [(0, travel, 0, 0, (origin, destination, 0, 0, 0, ()))]
            for bargain, raising in flags:
                if (bargain and bargain_cost is None) or (raising and raise_cost is None):
                    continue
                candidates = []
                for pid, lot in model.buy_lot.get(origin, {}).items():
                    data = model.products[pid]["market"]
                    if origin not in data["buy"] or destination not in data["sell"]:
                        continue
                    buy = data["buy"][origin]["price"]
                    sell = data["sell"][destination]["price"]
                    adjusted_buy = (2 * buy * (10000 - 2000 * bargain) + 10000) // 20000
                    adjusted_sell = (2 * sell * (10000 + 2000 * raising) + 10000) // 20000
                    unit = adjusted_sell - adjusted_buy
                    if lot > 0 and unit > 0:
                        candidates.append((unit, pid, lot))
                if not candidates:
                    continue
                candidates.sort(key=lambda item: (-item[0], item[1]))
                previous = None
                # One unit per lot gives a proven capacity-1 upper bound here.
                for k in range(capacity):
                    if books is not None and k > books:
                        break
                    free, profit, chosen = capacity, 0, []
                    for unit, pid, lot in candidates:
                        quantity = min(free, lot * (k + 1))
                        if quantity:
                            chosen.append(pid)
                            profit += quantity * unit
                            free -= quantity
                        if not free:
                            break
                    if policy == "fill" and k and sum(lot for _, _, lot in candidates) * (k + 1) - capacity > waste_limit:
                        break
                    if policy == "profit" and previous is not None and (profit - previous <= 0 or profit - previous < threshold):
                        break
                    cost = travel + (bargain_cost if bargain else 0) + (raise_cost if raising else 0)
                    options.append((profit, cost, k, bargain + raising,
                                    (origin, destination, k, bargain, raising, tuple(chosen))))
                    previous = profit
                    if policy == "fill" and not free:
                        break
            # Fixed freight can navigate only if this edge has no profitable
            # trade under the selected negotiation policy. Resource exhaustion
            # does not turn a profitable edge into an empty journey.
            result[(origin, destination)] = options[1:] if fixed and len(options) > 1 else options
    return result


def enumerate_oracle(model, *, mode, capacity, books, threshold, negotiation,
                     budget, target=1, endpoints=None, fixed=None, negotiation_budget=None, waste_limit=0, policy=None):
    policy = policy or ("fill" if mode == "quick" else "profit")
    negotiation = "required" if mode == "quick" else negotiation
    choices = _oracle_options(model, capacity, books, threshold, policy, negotiation, waste_limit,
                              fixed=mode == "fixed")
    records = []
    closed = fixed is not None and fixed[0] == fixed[-1]

    def visit(path, signatures, profit, fatigue, used, neg):
        depth = len(signatures)
        if depth and (mode != "fixed" or signatures[-1][-1]) and (
                mode == "fixed" or endpoints is None or path[-1] in endpoints):
            records.append((profit, fatigue, used, neg, depth, path, signatures))
        if mode == "fixed":
            if not closed and depth == len(fixed) - 1:
                return
            destination = fixed[(depth % (len(fixed) - 1)) + 1]
            destinations = [destination]
        else:
            destinations = model.allowed_city_ids
        for destination in destinations:
            for p, cost, k, n, signature in choices.get((path[-1], destination), []):
                if fatigue + cost > budget or (books is not None and used + k > books):
                    continue
                if negotiation_budget is not None and neg + n > negotiation_budget:
                    continue
                visit(path + (destination,), signatures + (signature,), profit + p,
                      fatigue + cost, used + k, neg + n)

    visit(("A",), (), 0, 0, 0, 0)
    if mode == "target":
        reachable = [row for row in records if row[0] >= target]
        best = min(reachable, key=lambda row: ((row[1], -row[0], *row[2:])
                   if policy == "fill" else row[1:])) if reachable else None
    elif mode == "fixed":
        tradable = [row for row in records if row[0] > 0]
        best = min(tradable, key=lambda row: (-row[1], -row[0], *row[2:])) if tradable else None
    else:
        positive = [row for row in records if row[0] > 0]
        best = min(positive, key=lambda row: (-row[0], *row[1:])) if positive else None
    return best, max((row[0] for row in records), default=0)


def assert_oracle(actual, expected):
    if expected is None:
        assert actual["status"] != "ok"
        assert actual["route"] == []
        return
    profit, fatigue, books, negotiations, depth, path, signatures = expected
    assert actual["status"] == "ok"
    assert Fraction(actual["expected_profit_exact"]) == profit
    assert Fraction(actual["expected_fatigue_used_exact"]) == fatigue
    assert actual["books_used"] == books
    assert actual["full_negotiation_used"] == negotiations
    assert len(actual["route"]) == depth
    assert tuple(actual["city_path_ids"]) == path
    assert tuple((leg["from_city_id"], leg["to_city_id"], leg["books_used"],
                  int(leg["bargain_to_cap"]), int(leg["raise_to_cap"]), tuple(leg["buy_product_ids"]))
                 for leg in actual["route"]) == signatures


@pytest.mark.parametrize("mode,goal", [("free", None), ("free", 25), ("fixed", None)])
@pytest.mark.parametrize("budget", [0, 1, None])
def test_all_modes_honor_unique_finite_or_unlimited_book_budget(mode, goal, budget):
    model = solver()
    result = solve(model, trade_mode=mode, book_budget=budget, fatigue_budget=3,
                   fixed_route_city_ids=["A", "B"], target_profit=goal)
    assert result["book_budget"] is budget
    assert "auto_book" not in result
    if result["status"] == "ok" and budget is not None:
        assert result["books_used"] <= budget
    if goal is not None:
        assert result["status"] == ("ok" if budget is None else "target_unreachable")


@pytest.mark.parametrize("budget", [2, None])
def test_unified_threshold_equality_and_positive_increment(budget):
    model = solver(products={"p": product("p", {"A": 1}, {"B": 500001})})
    result = solve(model, book_budget=budget, book_profit_threshold=500000)
    assert result["books_used"] == 2  # old Auto Book strict > boundary is intentionally removed
    assert result["average_book_profit_exact"] == "500000"
    assert solve(model, book_budget=budget, book_profit_threshold=500001)["books_used"] == 0
    zero = solve(model, book_budget=None, cargo_capacity=1)
    assert zero["books_used"] == 0
    assert zero["route"][0]["next_book_marginal_profit_exact"] == "0"


def test_book_prefix_rejects_last_margin_despite_good_average():
    result = solve(solver(), book_budget=None, cargo_capacity=4, book_profit_threshold=15,
                   # each book gives 20 until the final partial load gives only 10
                   )
    assert result["books_used"] == 0
    model = solver(lots={"A": {"p": 2}})
    result = solve(model, book_budget=None, cargo_capacity=5, book_profit_threshold=15)
    assert result["books_used"] == 1
    assert result["route"][0]["book_stop_reason"] == "profit_threshold"
    assert result["route"][0]["next_book_marginal_profit_exact"] == "10"


def test_fill_stops_at_first_full_load_not_product_replacement():
    model = solver(products={"p": product("p", {"A": 1}, {"B": 11}),
                             "q": product("q", {"A": 1}, {"B": 3})},
                   lots={"A": {"p": 1, "q": 3}})
    profit = solve(model, book_budget=None, cargo_capacity=4)
    quick = solve(model, trade_mode="free", book_policy="fill", negotiation_policy="required", book_budget=None, cargo_capacity=4, fatigue_budget=3)
    assert profit["books_used"] == 3
    assert quick["books_used"] == 0
    assert quick["route"][0]["book_stop_reason"] == "already_full_without_books"
    assert quick["route"][0]["bargain_to_cap"] and quick["route"][0]["raise_to_cap"]


@pytest.mark.parametrize("mode", ["free", "fixed"])
@pytest.mark.parametrize("book_budget", [2, None])
def test_fill_ignores_high_threshold_including_last_partial_book(mode, book_budget):
    model = solver(lots={"A": {"p": 2}})
    args = dict(trade_mode=mode, fixed_route_city_ids=["A", "B"],
                book_policy="fill", book_budget=book_budget, cargo_capacity=5, book_waste_limit=1)
    baseline = solve(model, book_profit_threshold=0, **args)
    high_threshold = solve(model, book_profit_threshold=200000, **args)
    assert semantic(high_threshold) == semantic(baseline)
    assert high_threshold["status"] == "ok"
    assert high_threshold["books_used"] == 2
    assert high_threshold["route"][0]["loaded_quantity"] == 5
    assert high_threshold["route"][0]["book_stop_reason"] == "capacity_reached"
    assert high_threshold["assumptions"]["book_threshold_applies"] is False


def test_fill_curve_ignores_threshold_without_relying_on_solver_normalization():
    from plans.resonance_pc.src.services.resonance_pc_trade_candidate_model import (
        EdgeBookCurve, TradeEdgeOption,
    )
    from plans.resonance_pc.src.services.resonance_pc_trade_settlement import TradeCandidate
    baseline = TradeEdgeOption(
        "A", "B", 0, False, False, 1, Fraction(0), Fraction(0), Fraction(20),
        ("p",), ("p",), (("p", "p", 2, Fraction(10)),),
        cargo_capacity=5, loaded_quantity=2,
    )
    fill = EdgeBookCurve.create(baseline=baseline,
                               candidates=(TradeCandidate(Fraction(10), "p", "p", 2, 1, 11, 0, 0),),
                               capacity=5, policy="fill", threshold=Fraction(200000), waste_limit=1)
    profit = EdgeBookCurve.create(baseline=baseline, candidates=fill.candidates,
                                 capacity=5, policy="profit", threshold=Fraction(200000))
    assert fill.legal_limit == 2
    assert fill.option(2, budget=None).loaded_quantity == 5
    assert fill.profit_at(2) - fill.profit_at(1) == 10
    assert profit.legal_limit == 0
    assert profit.limit_reason == "profit_threshold"


@pytest.mark.parametrize("book_budget", [0, 1, 3, 7, None])
def test_fixed_fill_respects_one_shared_book_budget_across_repeated_profitable_legs(book_budget):
    model = solver(costs={"A": {"B": 1}, "B": {"A": 1}},
                   products={"p": product("p", {"A": 1}, {"B": 11}),
                             "q": product("q", {"B": 1}, {"A": 101})},
                   lots={"A": {"p": 1}, "B": {"q": 1}})
    result = solve(model, trade_mode="fixed", fixed_route_city_ids=["A", "B", "A"],
                   book_policy="fill", book_budget=book_budget, book_profit_threshold=200000,
                   cargo_capacity=3, fatigue_budget=5)
    assert result["status"] == "ok"
    assert len(result["route"]) == 5
    assert all(leg["buys"] and leg["leg_type"] == "trade" for leg in result["route"])
    total = sum(leg["books_used"] for leg in result["route"])
    assert result["books_used"] == total
    assert total == (10 if book_budget is None else book_budget)
    remaining = book_budget
    for leg in result["route"]:
        assert leg["remaining_book_budget_before"] == remaining
        if remaining is not None:
            remaining -= leg["books_used"]
            assert remaining >= 0
        assert leg["remaining_book_budget_after"] == remaining
    assert result["remaining_books"] == remaining
    if book_budget == 0:
        assert all(leg["loaded_quantity"] == 1 for leg in result["route"])
    if book_budget == 1:
        assert sum(leg["books_used"] for leg in result["route"] if leg["from_city_id"] == "B") == 1
        assert all(leg["books_used"] == 0 for leg in result["route"] if leg["from_city_id"] == "A")


@pytest.mark.parametrize("mode", ["free", "fixed"])
def test_profit_policy_still_enforces_threshold_without_suppressing_base_purchase(mode):
    model = solver(lots={"A": {"p": 2}})
    args = dict(trade_mode=mode, fixed_route_city_ids=["A", "B"],
                book_policy="profit", book_budget=None, cargo_capacity=5)
    unfiltered = solve(model, book_profit_threshold=0, **args)
    thresholded = solve(model, book_profit_threshold=200000, **args)
    assert unfiltered["books_used"] == 2
    assert thresholded["status"] == "ok"
    assert thresholded["books_used"] == 0
    assert thresholded["route"][0]["loaded_quantity"] == 2
    assert thresholded["route"][0]["buys"]
    assert thresholded["route"][0]["book_stop_reason"] == "profit_threshold"
    assert thresholded["assumptions"]["book_threshold_applies"] is True


def test_fixed_budget_700_buys_each_profitable_leg_and_stops_instead_of_empty_travel():
    model = solver(costs={"A": {"B": 70}, "B": {"A": 70}}, attempt_fatigue=8,
                   products={"p": product("p", {"A": 10}, {"B": 100}),
                             "q": product("q", {"B": 10}, {"A": 100})},
                   lots={"A": {"p": 100}, "B": {"q": 100}})
    result = solve(model, trade_mode="fixed", fixed_route_city_ids=["A", "B", "A"],
                   fatigue_budget=700, cargo_capacity=630, book_budget=None,
                   book_policy="fill", book_profit_threshold=200000, book_waste_limit=70,
                   negotiation_policy="required", bargain_success_rates_bps=[5000],
                   raise_success_rates_bps=[5000], bargain_step_bps=1000, raise_step_bps=1000)
    assert result["status"] == "ok"
    assert len(result["route"]) == 5
    assert result["expected_fatigue_used_exact"] == "670"
    assert result["remaining_expected_fatigue_exact"] == "30"
    assert result["books_used"] == 30
    assert all(leg["buys"] and leg["leg_type"] == "trade"
               and leg["loaded_quantity"] == 630
               and leg["bargain_to_cap"] and leg["raise_to_cap"]
               for leg in result["route"])
    for budget in (133, 134, 204):
        bounded = solve(model, trade_mode="fixed", fixed_route_city_ids=["A", "B", "A"],
                        fatigue_budget=budget, negotiation_policy="required",
                        bargain_success_rates_bps=[5000], raise_success_rates_bps=[5000],
                        bargain_step_bps=1000, raise_step_bps=1000)
        assert len(bounded["route"]) == (0 if budget == 133 else 1)
        assert all(leg["buys"] for leg in bounded["route"])


def test_fixed_can_reposition_through_unprofitable_edge_but_drops_navigation_tail():
    model = solver(cities=("A", "B", "C", "D"),
                   costs={"A": {"B": 1}, "B": {"C": 1}, "C": {"D": 1}, "D": {}},
                   products={"p": product("p", {"B": 1}, {"C": 11})},
                   lots={"B": {"p": 1}})
    result = solve(model, trade_mode="fixed", fixed_route_city_ids=["A", "B", "C", "D"],
                   fatigue_budget=3)
    assert result["status"] == "ok"
    assert result["city_path_ids"] == ["A", "B", "C"]
    assert [leg["leg_type"] for leg in result["route"]] == ["navigation", "trade"]
    assert result["route"][0]["buys"] == []
    assert result["route"][-1]["buys"]
    assert result["expected_fatigue_used_exact"] == "2"
    assert result["stop_reason"] == "no_further_trade_within_budget"


def test_quick_globally_allocates_books_to_later_higher_profit_leg():
    model = solver(cities=("A", "B", "C"), costs={"A": {"B": 1}, "B": {"C": 1}, "C": {}},
                   products={"p": product("p", {"A": 1}, {"B": 11}),
                             "q": product("q", {"B": 1}, {"C": 101})},
                   lots={"A": {"p": 1}, "B": {"q": 1}})
    result = solve(model, trade_mode="free", book_policy="fill", negotiation_policy="required", fatigue_budget=6, book_budget=1,
                   cargo_capacity=2, required_end_city_ids=["C"])
    assert [leg["books_used"] for leg in result["route"]] == [0, 1]
    assert result["route"][0]["loaded_quantity"] == 1
    assert result["route"][0]["book_stop_reason"] == "global_book_allocation"


def test_required_negotiation_never_falls_back_to_unnegotiated_trade():
    model = solver()
    assert solve(model, trade_mode="free", book_policy="fill", negotiation_policy="required", fatigue_budget=2)["status"] == "no_plan"
    assert solve(model, trade_mode="free", book_policy="fill", negotiation_policy="required", fatigue_budget=9,
                 bargain_success_rates_bps=[0])["status"] == "no_plan"


def test_large_unlimited_curves_are_exact_and_target_keeps_low_book_candidate():
    model = solver()
    profit = solve(model, book_budget=None, cargo_capacity=1000000)
    target = solve(model, trade_mode="free", book_budget=None, cargo_capacity=1000000, target_profit=25)
    assert profit["books_used"] == 999999
    assert profit["expected_profit_exact"] == "10000000"
    assert target["books_used"] == 2
    assert target["expected_profit_exact"] == "30"
    assert target["solver_stats"]["generated_labels"] == 1


@pytest.mark.parametrize("books", [999998, 1000000])
def test_large_finite_single_leg_budget_uses_exact_curve_without_materializing_all_counts(books):
    result = solve(solver(), book_budget=books, cargo_capacity=1000000)
    assert result["books_used"] == min(books, 999999)
    assert result["expected_profit"] == 10 * min(books + 1, 1000000)
    assert result["remaining_books"] == books - result["books_used"]


def test_binding_book_budget_exercises_both_exact_backends():
    model = solver()
    args = dict(cargo_capacity=10, book_budget=3, fatigue_budget=2)
    dense = solve(model, _backend="dense", **args)
    sparse = solve(model, _backend="sparse", **args)
    assert dense["solver_backend"] == "dense"
    assert sparse["solver_backend"] == "sparse"
    assert semantic(dense) == semantic(sparse)


def test_target_preserves_lower_profit_shorter_label_at_the_same_intermediate_state():
    model = solver(cities=("A", "B", "C", "D"),
        costs={"A": {"B": 2, "C": 1}, "C": {"B": 1}, "B": {"D": 1}, "D": {}},
        products={"ab": product("ab", {"A": 1}, {"B": 6}),
                  "ac": product("ac", {"A": 1}, {"C": 5}),
                  "cb": product("cb", {"C": 1}, {"B": 7}),
                  "bd": product("bd", {"B": 1}, {"D": 6})},
        lots={"A": {"ab": 1, "ac": 1}, "C": {"cb": 1}, "B": {"bd": 1}})
    result = solve(model, trade_mode="free", cargo_capacity=1, fatigue_budget=3,
                   target_profit=10, required_end_city_ids=["D"])
    assert result["city_path_ids"] == ["A", "B", "D"]
    assert result["expected_profit"] == 10
    assert result["expected_fatigue_used_exact"] == "3"


def test_target_preserves_lower_profit_stable_path_when_all_resources_tie():
    model = solver(cities=("A", "B", "C", "D", "E"),
        costs={"A": {"B": 1, "C": 1}, "B": {"D": 1}, "C": {"D": 1}, "D": {"E": 1}, "E": {}},
        products={"a": product("a", {"A": 1}, {"B": 5, "C": 10}),
                  "b": product("b", {"B": 1, "C": 1}, {"D": 5}),
                  "d": product("d", {"D": 1}, {"E": 5})},
        lots={"A": {"a": 1}, "B": {"b": 1}, "C": {"b": 1}, "D": {"d": 1}})
    result = solve(model, trade_mode="free", cargo_capacity=1, fatigue_budget=3,
                   target_profit=12, required_end_city_ids=["E"])
    assert result["city_path_ids"] == ["A", "B", "D", "E"]
    assert result["expected_profit"] == 12


def test_navigation_after_reaching_target_does_not_fabricate_trades_or_sell_flags():
    model = solver(cities=("A", "B", "C"), costs={"A": {"B": 1}, "B": {"C": 1}, "C": {}})
    result = solve(model, trade_mode="free", cargo_capacity=1, fatigue_budget=2,
                   target_profit=10, required_end_city_ids=["C"])
    assert result["city_path_ids"] == ["A", "B", "C"]
    assert result["route"][-1]["leg_type"] == "navigation"
    assert result["route"][-1]["buys"] == []
    assert result["city_visits"][1]["sell_intent"]["source_leg_index"] == 0
    assert result["city_visits"][2]["sell_intent"] is None


def test_cooperative_cancellation_stops_candidate_construction(monkeypatch):
    import asyncio
    from plans.resonance_pc.src.services import resonance_pc_trade_solver_common as common
    monkeypatch.setattr(common, "is_current_task_cancel_requested", lambda: True)
    with pytest.raises(asyncio.CancelledError):
        solve(solver(), book_budget=None)


def test_target_fatigue_then_books_not_excess_profit_and_endpoint_is_required():
    model = solver(cities=("A", "B", "C"), costs={"A": {"B": 2, "C": 1}, "B": {}, "C": {"B": 1}},
                   products={"p": product("p", {"A": 1}, {"B": 101, "C": 51})},
                   lots={"A": {"p": 2}})
    target = solve(model, trade_mode="free", fatigue_budget=2, target_profit=100)
    assert target["city_path_ids"] == ["A", "C"]
    endpoint = solve(model, trade_mode="free", fatigue_budget=2, target_profit=100,
                     required_end_city_ids=["B"])
    assert endpoint["city_path_ids"] == ["A", "B"]
    assert endpoint["city_visits"][-1]["buy_intent"] is None
    assert endpoint["city_visits"][0]["sell_intent"] is None


def test_unreachable_returns_actual_maximum_but_no_executable_route():
    result = solve(solver(), trade_mode="free", target_profit=100,
                   book_budget=1, required_end_city_ids=["B"])
    assert result["status"] == "target_unreachable"
    assert result["maximum_reachable_profit_exact"] == "20"
    assert result["target_gap"] == 80
    assert result["route"] == []
    assert result["diagnostics"]["execution_allowed"] is False


def test_fixed_fatigue_is_primary_and_partial_final_circuit_is_allowed():
    model = solver(costs={"A": {"B": 2}, "B": {"A": 2}})
    result = solve(model, trade_mode="fixed", fixed_route_city_ids=["A", "B", "A"],
                   fatigue_budget=7, negotiation_policy="auto")
    expected, _ = enumerate_oracle(model, mode="fixed", capacity=3, books=0, threshold=0,
                                  negotiation="auto", budget=7, fixed=["A", "B", "A"])
    assert_oracle(result, expected)
    assert result["expected_fatigue_used_exact"] == "7"
    assert "fixed_route_repeat_count" not in result["request"]
    assert result["city_visits"][-1]["buy_intent"] is None


def test_fixed_partial_circle_and_exact_shared_resource_boundaries():
    model = solver(costs={"A": {"B": 2}, "B": {"A": 2}})
    result = solve(model, trade_mode="fixed", fixed_route_city_ids=["A", "B", "A"],
                   fatigue_budget=6, book_budget=2)
    assert result["city_path_ids"] == ["A", "B", "A", "B"]
    assert result["completed_circuits"] == 1 and result["partial_circuit_legs"] == 1
    assert result["books_used"] == 2
    assert result["remaining_expected_fatigue_exact"] == "0"
    assert [leg["round_index"] for leg in result["route"]] == [0, 0, 1]
    assert solve(model, trade_mode="fixed", fixed_route_city_ids=["A", "B", "A"],
                 fatigue_budget=1)["status"] == "fixed_route_infeasible"


def test_fixed_open_route_does_not_append_return_and_zero_edges_are_rejected():
    model = solver(costs={"A": {"B": 2}, "B": {"A": 2}})
    result = solve(model, trade_mode="fixed", fixed_route_city_ids=["A", "B"], fatigue_budget=100)
    assert result["city_path_ids"] == ["A", "B"]
    assert result["stop_reason"] == "open_route_completed"
    model.fatigue_costs["A"]["B"] = 0
    with pytest.raises(ValueError, match="strictly positive"):
        solve(model, trade_mode="fixed", fixed_route_city_ids=["A", "B", "A"])


def test_fixed_requires_at_least_one_effective_trade_not_only_budget_consuming_navigation():
    model = solver()
    result = solve(model, trade_mode="fixed", fixed_route_city_ids=["A", "B"],
                   negotiation_policy="required", fatigue_budget=2)
    assert result["status"] == "fixed_route_infeasible"
    assert result["route"] == []


def test_reposition_is_separate_pure_navigation_and_charged_to_total_budget():
    model = solver(cities=("A", "B", "C"), costs={"A": {"B": 3}, "B": {"C": 2}, "C": {"B": 2}},
                   products={"p": product("p", {"B": 1}, {"C": 11})}, lots={"B": {"p": 1}})
    args = dict(trade_mode="fixed", fixed_route_city_ids=["B", "C", "B"], fatigue_budget=9)
    with pytest.raises(ValueError, match="actual start"):
        solve(model, **args)
    result = solve(model, reposition_to_route=True, **args)
    assert result["reposition_expected_fatigue_exact"] == "3"
    assert result["route_expected_fatigue_exact"] == "6"
    assert result["total_expected_fatigue_exact"] == "9"
    assert result["city_path_ids"] == ["B", "C", "B", "C"]
    assert result["start_city_id"] == "A" and result["route_start_city_id"] == "B"
    assert result["reposition_route"][0]["leg_type"] == "reposition"
    assert result["reposition_route"][0]["books_used"] == 0
    assert result["reposition_route"][0]["buys"] == []
    assert result["city_visits"][0]["sell_intent"] is None
    blocked = solve(model, reposition_to_route=True, **dict(args, fatigue_budget=4))
    assert blocked["status"] == "fixed_route_infeasible"
    assert blocked["reposition_route"] == []
    at_start = solve(model, start_city_id="B", reposition_to_route=True, **args)
    assert at_start["reposition_route"] == []


def test_fractional_expected_fatigue_retains_exact_budget_boundary():
    model = solver()
    result = solve(model, trade_mode="free", book_policy="fill", negotiation_policy="required", fatigue_budget=4,
                   bargain_success_rates_bps=[6000], raise_success_rates_bps=[7500])
    assert result["expected_fatigue_used_exact"] == "4"
    assert expected_fatigue_to_cap(success_rates_bps=[6000], step_bps=2000, attempt_fatigue=1) == Fraction(5, 3)
    assert solve(model, trade_mode="free", book_policy="fill", negotiation_policy="required", fatigue_budget=3,
                 bargain_success_rates_bps=[6000], raise_success_rates_bps=[7500])["status"] == "no_plan"


def test_inputs_are_not_mutated_and_invalid_mode_inputs_do_not_silently_coerce():
    model = solver()
    original = deepcopy((model.snapshot, model.fatigue_costs, model.buy_lot))
    for kwargs in ({"book_budget": True}, {"cargo_capacity": 0}, {"target_profit": 0, "trade_mode": "free"},
                   {"book_policy": "bogus", "book_budget": 1}, {"negotiation_policy": "bogus"}):
        with pytest.raises((TypeError, ValueError)):
            solve(model, **kwargs)
    solve(model, book_budget=None)
    assert (model.snapshot, model.fatigue_costs, model.buy_lot) == original


def test_randomized_all_objectives_and_complete_tie_order_against_exhaustive_oracle():
    rng = random.Random(20261004)
    for case in range(250):
        cities = ("A", "B", "C")
        costs = {city: {dest: rng.randint(1, 3) for dest in cities if dest != city} for city in cities}
        products, lots = {}, {city: {} for city in cities}
        for index in range(3):
            pid = f"p{index}"
            buys = {city: rng.randint(1, 8) for city in cities}
            sells = {city: rng.randint(3, 20) for city in cities}
            products[pid] = product(pid, buys, sells)
            for city in cities:
                lots[city][pid] = rng.randint(1, 3)
        model = solver(cities=cities, costs=costs, products=products, lots=lots)
        for side in ("bargain", "raise"):
            model.rules["negotiation"]["defaults"][side + "_success_rates_bps"] = rng.choice(
                [[10000], [6000], [5000, 7500], [0]])
            model.rules["negotiation"]["defaults"][side + "_step_bps"] = rng.choice([1000, 2000])
        capacity, budget = rng.randint(2, 5), rng.randint(2, 5)
        books, threshold = rng.choice([0, 1, 2, None]), rng.randint(0, 20)
        negotiation = rng.choice(["disabled", "auto", "required"])
        neg_budget = rng.choice([None, 1, 3])
        endpoints = rng.choice([None, ["B"], ["B", "C"]])
        target = rng.randint(10, 100)
        waste_limit = rng.randint(0, capacity)
        for mode in ("profit", "quick", "target", "fixed"):
            fixed = ["A", "B", "C", "A"] if mode == "fixed" else None
            expected, maximum = enumerate_oracle(model, mode=mode, capacity=capacity, books=books,
                         threshold=threshold, negotiation=negotiation, budget=budget, target=target,
                         endpoints=endpoints, fixed=fixed, negotiation_budget=neg_budget, waste_limit=waste_limit)
            args = dict(trade_mode="fixed" if mode == "fixed" else "free", fatigue_budget=budget, cargo_capacity=capacity, book_budget=books,
                        book_profit_threshold=threshold, negotiation_policy="required" if mode == "quick" else negotiation,
                        book_policy="fill" if mode == "quick" else "profit", book_waste_limit=waste_limit,
                        negotiation_budget=neg_budget, target_profit=target if mode == "target" else None,
                        required_end_city_ids=endpoints, fixed_route_city_ids=fixed)
            actual = solve(model, **args)
            try:
                assert_oracle(actual, expected)
                if mode == "target":
                    assert actual["maximum_reachable_profit"] == maximum
                if mode in {"profit", "quick"} and books is not None:
                    backend = actual.get("solver_backend")
                    if actual["status"] == "ok" and backend in {"dense", "sparse"}:
                        other_backend = "sparse" if backend == "dense" else "dense"
                        assert semantic(actual) == semantic(solve(model, _backend=other_backend, **args))
                    else:
                        assert semantic(solve(model, _backend="dense", **args)) == semantic(
                            solve(model, _backend="sparse", **args))
            except AssertionError as exc:
                raise AssertionError(f"case={case}, args={args}, expected={expected}, actual={actual}") from exc
