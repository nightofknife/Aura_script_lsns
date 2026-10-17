"""Single-batch trade settlement and exact bounded cargo selection.

Prices are already negotiated integer unit prices. Buy tax is charged on the
whole purchase; sell tax is charged only on the positive whole-batch spread.
The purchase tax is NOT part of the inventory cost used for the sell tax.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from fractions import Fraction
from math import gcd
from typing import NamedTuple, Sequence

from .resonance_pc_trade_solver_common import check_solver_cancelled

PRICING_MODEL = "single_batch_profit_tax_v1"


def game_round(value: Fraction) -> int:
    """Client NumRound/NumFloorPrecision, including its strict 1e-4 tolerance."""
    value = Fraction(value)
    sign = -1 if value < 0 else 1
    shifted = abs(value) + Fraction(1, 2)
    integer = shifted.numerator // shifted.denominator
    if shifted - integer + Fraction(1, 10000) > 1:
        integer += 1
    return sign * integer


@dataclass(frozen=True)
class Settlement:
    buy_goods_amount: int = 0
    buy_tax: int = 0
    buy_total: int = 0
    sell_goods_amount: int = 0
    taxable_profit: int = 0
    sell_tax: int = 0
    sell_total: int = 0
    net_profit: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


def settle_amounts(buy: int, sell: int, buy_tax_bps: int, sell_tax_bps: int) -> Settlement:
    buy_tax = game_round(Fraction(buy * buy_tax_bps, 10000))
    taxable = max(sell - buy, 0)
    sell_tax = game_round(Fraction(taxable * sell_tax_bps, 10000))
    return Settlement(buy, buy_tax, buy + buy_tax, sell, taxable, sell_tax,
                      sell - sell_tax, sell - sell_tax - buy - buy_tax)


class TradeCandidate(NamedTuple):
    # The first four fields deliberately retain the existing curve layout.
    unit_profit: Fraction  # unrounded linear estimate, NOT additive settled profit
    product_id: str
    product_name: str
    lot: int
    buy_price: int
    sell_price: int
    buy_tax_bps: int
    sell_tax_bps: int


def linear_profit(candidates: Sequence[TradeCandidate], capacity: int, books: int) -> Fraction:
    result = Fraction(0)
    for row in candidates:
        if row.unit_profit <= 0:
            break
        quantity = min(capacity, row.lot * (books + 1))
        result += row.unit_profit * quantity
        capacity -= quantity
        if not capacity:
            break
    return result


def optimal_load(candidates: Sequence[TradeCandidate], capacity: int, books: int, *, fill: bool = False):
    """Return an exact maximum-profit load, using an admissible linear bound.

    Ignoring whole-order rounding gives a fractional linear knapsack, solved
    greedily because cargo sizes are all one. Each of the two rounded taxes
    can improve profit by strictly less than 0.5, hence integer settled profit
    is at most ceil(linear optimum). This bound also holds for a negative gross
    spread: the linear relaxation overcredits its sell-tax rebate.

    The greedy load is only an incumbent, never the optimality argument.
    Branches whose exact integer upper bound cannot improve it are discarded.
    Identical prices are grouped. Equal linear slopes use the 10000-unit tax
    period: a lexicographically maximal optimum cannot transfer 10000 units
    from a later group to an earlier one, since this preserves both taxes and
    total cargo. This bounds equal-slope tails independently of huge capacity.
    """
    check_solver_cancelled()
    if fill:
        candidates = tuple(row for row in candidates if row.unit_profit > 0)
    if not candidates:
        return (), Settlement()
    buy_rate, sell_rate = candidates[0].buy_tax_bps, candidates[0].sell_tax_bps
    # Merge identical economic goods, then split their quantity in stable order.
    groups = []
    lookup = {}
    for row in candidates:
        key = row.buy_price, row.sell_price
        if key not in lookup:
            lookup[key] = len(groups)
            groups.append([row, 0, []])
        group = groups[lookup[key]]
        group[1] += row.lot * (books + 1)
        group[2].append(row)
    groups.sort(key=lambda group: -group[0].unit_profit)
    n = len(groups)
    weights = [int(group[0].unit_profit * 10000) for group in groups]
    limits = [min(group[1], capacity) for group in groups]
    if fill:
        capacity = min(capacity, sum(limits))
    buy_grid = sell_grid = 10000
    for row, _limit, _members in groups:
        buy_grid = gcd(buy_grid, row.buy_price * buy_rate)
        sell_grid = gcd(sell_grid, (row.sell_price - row.buy_price) * sell_rate)
    # Fractional taxes lie on these exact integer lattices. E.g. 50% taxes
    # only have residues 0/.5, neither of which ever rounds DOWN. The generic
    # <1 bound is then unnecessarily loose and disastrous for equal slopes.
    rounding_gain = (4999 // buy_grid) * buy_grid + (4999 // sell_grid) * sell_grid

    def upper(index, free, score):
        for j in range(index, n):
            if weights[j] <= 0:
                break
            q = min(free, limits[j])
            score += q * weights[j]
            free -= q
            if not free:
                break
        # For bps-tax multiples of 1e-4 the largest downward error of each tax
        # is .4999. This is a tighter version of ceil(linear optimum).
        return (score + rounding_gain) // 10000

    chosen = [0] * n
    free = capacity
    for index in range(n):
        if weights[index] > 0:
            chosen[index] = min(free, limits[index])
            free -= chosen[index]
    def settlement(quantities):
        return settle_amounts(sum(q * groups[i][0].buy_price for i, q in enumerate(quantities)),
                              sum(q * groups[i][0].sell_price for i, q in enumerate(quantities)),
                              buy_rate, sell_rate)
    best_q = chosen[:]
    best = settlement(best_q)
    if best.net_profit < 0:
        best_q, best = [0] * n, Settlement()
    root_bound = upper(0, capacity, 0)

    def visit(index, free, score, buy, sell):
        nonlocal best, best_q
        check_solver_cancelled()
        if upper(index, free, score) <= best.net_profit:
            return
        if index == n or free == 0:
            if fill and free:
                return
            value = settle_amounts(buy, sell, buy_rate, sell_rate)
            if value.net_profit > best.net_profit:
                best, best_q = value, chosen[:index] + [0] * (n - index)
            return
        row = groups[index][0]
        maximum = min(free, limits[index])
        minimum = max(0, free - sum(limits[index + 1:])) if fill else 0
        # Equal positive slopes: a canonical optimum fills the available slots
        # (dropping cargo may improve rounding; keep the final 10000-unit tail).
        # Transfer-period restriction is safe regardless of whether it is full.
        if weights[index] > 0 and all(w == weights[index] for w in weights[index:]):
            minimum = max(minimum, maximum - (n - index) * 10000)
        # Positive slopes are best-first descending; nonpositive are ascending.
        quantities = range(maximum, minimum - 1, -1) if weights[index] > 0 else range(minimum, maximum + 1)
        for q in quantities:
            if q % 256 == 0:
                check_solver_cancelled()
            next_score = score + q * weights[index]
            bound = upper(index + 1, free - q, next_score)
            if bound <= best.net_profit:
                # Ordered slopes make the relaxed bound monotone in q.
                break
            chosen[index] = q
            visit(index + 1, free - q, next_score,
                  buy + q * row.buy_price, sell + q * row.sell_price)
            if best.net_profit >= root_bound:
                return
        chosen[index] = 0

    if root_bound > best.net_profit:
        visit(0, capacity, 0, 0, 0)
    allocations = {}
    for quantity, (_row, _limit, members) in zip(best_q, groups):
        for row in members:
            take = min(quantity, row.lot * (books + 1))
            if take:
                allocations[row.product_id] = take
            quantity -= take
    buys = tuple((row.product_id, row.product_name, allocations[row.product_id], row.unit_profit)
                 for row in candidates if row.product_id in allocations)
    return buys, best
