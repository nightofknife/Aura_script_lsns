"""Independent whole-order price, loading, and nonconcave curve regressions."""
from fractions import Fraction
from itertools import product
import random

import pytest

from plans.resonance_pc.src.services.resonance_pc_trade_settlement import (
    TradeCandidate, game_round, optimal_load, settle_amounts,
)
from plans.resonance_pc.src.services.resonance_pc_trade_candidate_model import EdgeBookCurve, TradeEdgeOption


def candidate(pid, buy, sell, lot, buy_tax, sell_tax):
    return TradeCandidate(Fraction((sell - buy) * (10000 - sell_tax) - buy * buy_tax, 10000),
                          pid, pid, lot, buy, sell, buy_tax, sell_tax)


def exhaustive(rows, capacity, books=0):
    best = 0
    for quantities in product(*(range(min(row.lot * (books + 1), capacity) + 1) for row in rows)):
        if sum(quantities) > capacity:
            continue
        buy = sum(q * row.buy_price for q, row in zip(quantities, rows))
        sell = sum(q * row.sell_price for q, row in zip(quantities, rows))
        # Independent integer arithmetic: these exact bps values cannot fall
        # strictly inside the client's 1e-4 floating-point tolerance interval.
        buy_tax = (buy * rows[0].buy_tax_bps + 5000) // 10000
        sell_tax = (max(sell - buy, 0) * rows[0].sell_tax_bps + 5000) // 10000
        best = max(best, sell - buy - buy_tax - sell_tax)
    return best


def test_user_crystal_sample_is_whole_order_not_unit_tax():
    assert settle_amounts(688 * 2, 792 * 2, 700, 750).to_dict() == {
        "buy_goods_amount": 1376, "buy_tax": 96, "buy_total": 1472,
        "sell_goods_amount": 1584, "taxable_profit": 208, "sell_tax": 16,
        "sell_total": 1568, "net_profit": 96,
    }
    assert 2 * settle_amounts(688, 792, 700, 750).net_profit == 96
    # A second case explicitly distinguishes whole-order and per-unit rounding.
    assert settle_amounts(2, 6, 2500, 2500).net_profit == 2
    assert 2 * settle_amounts(1, 3, 2500, 2500).net_profit == 2
    assert settle_amounts(2, 4, 2500, 0).net_profit == 1
    assert 2 * settle_amounts(1, 2, 2500, 0).net_profit == 2


@pytest.mark.parametrize("value,expected", [("1.5", 2), ("-1.5", -2),
    ("1.4999", 1), ("1.49991", 2), ("-1.4999", -1), ("-1.49991", -2), ("0", 0)])
def test_game_round_strict_tolerance(value, expected):
    assert game_round(Fraction(value)) == expected


def test_losing_sale_has_no_sell_tax():
    result = settle_amounts(1000, 900, 700, 750)
    assert result.taxable_profit == result.sell_tax == 0
    assert result.net_profit == -170


def test_exact_load_matches_2000_independent_bounded_enumerations():
    rng = random.Random(61183)
    for _ in range(2000):
        tb, ts = rng.randrange(10001), rng.randrange(10001)
        rows = [candidate(str(i), rng.randrange(1, 40), rng.randrange(1, 60), rng.randrange(1, 5), tb, ts)
                for i in range(rng.randrange(1, 5))]
        rows.sort(key=lambda row: (-row.unit_profit, row.product_id))
        capacity, books = rng.randrange(1, 8), rng.randrange(3)
        buys, settlement = optimal_load(rows, capacity, books)
        assert settlement.net_profit == exhaustive(rows, capacity, books), (rows, capacity, books, buys)
        assert sum(row[2] for row in buys) <= capacity


def test_huge_identical_goods_do_not_expand_per_unit():
    rows = [candidate(str(i), 688, 792, 10**9, 700, 750) for i in range(12)]
    buys, settlement = optimal_load(rows, 10**9, 0)
    assert sum(row[2] for row in buys) <= 10**9
    assert settlement.net_profit >= settle_amounts(688 * 10**9, 792 * 10**9, 700, 750).net_profit


def test_equal_slope_billion_capacity_uses_reachable_tax_lattice_bound():
    rows = [candidate(str(i), buy, sell, 10**12, 5000, 5000)
            for i, (buy, sell) in enumerate(((1, 3), (2, 5), (3, 7)))]
    _buys, settlement = optimal_load(rows, 10**9 + 1, 0)
    assert settlement.net_profit == 500000000


def test_nonpositive_linear_good_can_improve_whole_order_rounding():
    rows = (candidate("a", 71, 138, 1, 85, 9247), candidate("b", 89, 99, 1, 85, 9247))
    assert rows[1].unit_profit < 0
    buys, settlement = optimal_load(rows, 2, 0)
    assert len(buys) == 2
    assert settlement.net_profit == 5
    assert settle_amounts(71, 138, 85, 9247).net_profit == 4


def test_fill_keeps_full_load_even_when_leaving_one_slot_would_profit_one_more():
    rows = (candidate("p", 41, 94, 20, 8867, 3114),)
    buys, settlement = optimal_load(rows, 5, 0)
    assert sum(row[2] for row in buys) == 4
    assert settlement.net_profit == 1
    filled, filled_settlement = optimal_load(rows, 5, 0, fill=True)
    assert sum(row[2] for row in filled) == 5
    assert filled_settlement.net_profit == 0
    baseline = TradeEdgeOption("A", "B", 0, False, False, 1, Fraction(0), Fraction(0),
                               Fraction(settlement.net_profit), ("p",), ("p",), buys,
                               loaded_quantity=4, settlement=settlement)
    curve = EdgeBookCurve.create(baseline=baseline, candidates=rows, capacity=5,
                                 policy="fill", threshold=Fraction(999999))
    assert curve.baseline.loaded_quantity == curve.option(0, budget=0).loaded_quantity == 5
    assert curve.baseline.expected_profit == curve.profit_at(0) == 0


def test_curve_marginals_use_whole_settlement_and_first_failure():
    rows = (candidate("p", 1, 2, 1, 2500, 0),)
    buys, settlement = optimal_load(rows, 6, 0)
    baseline = TradeEdgeOption("A", "B", 0, False, False, 1, Fraction(0), Fraction(0),
                               Fraction(settlement.net_profit), ("p",), ("p",), buys,
                               loaded_quantity=1, settlement=settlement)
    curve = EdgeBookCurve.create(baseline=baseline, candidates=rows, capacity=6,
                                 policy="profit", threshold=Fraction(0))
    assert [curve.profit_at(k) for k in range(5)] == [1, 1, 2, 3, 4]
    # Marginals 0,1,1,1 are not nonincreasing. Stop at the first zero, never
    # binary-search past it on the old concavity assumption.
    assert curve.legal_limit == 0


def test_small_curves_match_every_exhaustive_load():
    rng = random.Random(7118)
    for _ in range(80):
        tb, ts = rng.randrange(1501), rng.randrange(1501)
        rows = tuple(sorted([candidate(str(i), rng.randrange(1, 10), rng.randrange(10, 25),
                                       rng.randrange(1, 3), tb, ts) for i in range(3)],
                            key=lambda row: -row.unit_profit))
        buys, settlement = optimal_load(rows, 7, 0)
        baseline = TradeEdgeOption("A", "B", 0, False, False, 1, Fraction(0), Fraction(0),
                                   Fraction(settlement.net_profit), (), (), buys,
                                   loaded_quantity=sum(row[2] for row in buys), settlement=settlement)
        curve = EdgeBookCurve.create(baseline=baseline, candidates=rows, capacity=7,
                                     policy="profit", threshold=Fraction(1))
        profits = [exhaustive(rows, 7, k) for k in range(curve.saturation_limit + 1)]
        expected = next((k - 1 for k in range(1, len(profits)) if profits[k] - profits[k - 1] < 1),
                        len(profits) - 1)
        assert curve.legal_limit == expected
        assert [curve.profit_at(k) for k in range(len(profits))] == profits


def test_billion_book_critical_threshold_uses_exact_tax_period_not_per_book_scan():
    rows = (candidate("p", 1, 3, 1, 0, 2500),)
    buys, settlement = optimal_load(rows, 10**9, 0)
    baseline = TradeEdgeOption("A", "B", 0, False, False, 1, Fraction(0), Fraction(0),
                               Fraction(settlement.net_profit), ("p",), ("p",), buys,
                               loaded_quantity=1, settlement=settlement)
    curve = EdgeBookCurve.create(baseline=baseline, candidates=rows, capacity=10**9,
                                 policy="profit", threshold=Fraction(1))
    assert curve.legal_limit == 10**9 - 1
    assert curve.profit_at(curve.legal_limit) == 1500000000
    assert len(curve._loads) < 10
    strict = EdgeBookCurve.create(baseline=baseline, candidates=rows, capacity=10**9,
                                  policy="profit", threshold=Fraction(2))
    assert strict.legal_limit == 1
