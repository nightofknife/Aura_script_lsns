"""Exact whole-settlement book curves (rounded marginals need not be concave)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from fractions import Fraction
from math import gcd, lcm
from typing import Any, Optional, Tuple

from .resonance_pc_trade_solver_common import check_solver_cancelled
from .resonance_pc_trade_settlement import Settlement, TradeCandidate, linear_profit, optimal_load


@dataclass(frozen=True)
class TradeEdgeOption:
    from_city_id: str
    to_city_id: str
    books_used: int
    bargain_to_cap: bool
    raise_to_cap: bool
    travel_fatigue: int
    expected_bargain_fatigue: Fraction
    expected_raise_fatigue: Fraction
    expected_profit: Fraction
    buy_product_ids: Tuple[str, ...]
    buy_product_names: Tuple[str, ...]
    buys: Tuple[Tuple[str, str, int, Fraction], ...]
    book_incremental_profit: Fraction = Fraction(0)
    cargo_capacity: int = 0
    loaded_quantity: int = 0
    book_stop_reason: str = "no_suitable_products"
    legal_book_limit: int = 0
    book_waste_quantity: int = 0
    next_book_marginal_profit: Fraction = Fraction(0)
    settlement: Settlement = Settlement()

    @property
    def full_negotiation_used(self) -> int:
        return int(self.bargain_to_cap) + int(self.raise_to_cap)

    @property
    def expected_negotiation_fatigue(self) -> Fraction:
        return self.expected_bargain_fatigue + self.expected_raise_fatigue

    @property
    def expected_fatigue_cost(self) -> Fraction:
        return Fraction(self.travel_fatigue) + self.expected_negotiation_fatigue

    @property
    def stable_signature(self) -> Tuple[Any, ...]:
        return (self.from_city_id, self.to_city_id, self.books_used,
                int(self.bargain_to_cap), int(self.raise_to_cap), self.buy_product_ids)


@dataclass(frozen=True)
class EdgeBookCurve:
    baseline: TradeEdgeOption
    candidates: Tuple[TradeCandidate, ...]
    capacity: int
    policy: str
    threshold: Fraction
    legal_limit: int
    saturation_limit: int
    limit_reason: str
    _loads: dict = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def create(cls, *, baseline: TradeEdgeOption, candidates: tuple,
               capacity: int, policy: str, threshold: Fraction, waste_limit: int = 0) -> "EdgeBookCurve":
        positive = tuple(row for row in candidates if row.unit_profit > 0)
        if not positive:
            return cls(baseline, (), capacity, policy, threshold, 0, 0,
                       "no_suitable_products")
        if policy == "fill":
            candidates = positive
            buys, settlement = optimal_load(candidates, capacity, 0, fill=True)
            baseline = replace(baseline, buys=buys, settlement=settlement,
                               expected_profit=Fraction(settlement.net_profit),
                               loaded_quantity=sum(row[2] for row in buys),
                               buy_product_ids=tuple(row[0] for row in buys),
                               buy_product_names=tuple(row[1] for row in buys))
            limiting_lot = sum(row.lot for row in positive)
        else:
            # A lower-slope item can win a rounding tie even after the top item
            # fills cargo. Saturate when EVERY economically distinct supply is
            # unconstrained, not merely the highest linear-profit group.
            limiting_lot = min(row.lot for row in candidates)
        saturation = max((capacity + limiting_lot - 1) // limiting_lot - 1, 0)
        curve = cls(baseline, candidates, capacity, policy, threshold, saturation,
                    saturation, "capacity_reached" if policy == "fill" else "profit_saturated")
        if policy == "fill":
            # Fill only up to the first full load, independent of book profit.
            # Earlier partial loads remain available for the route-wide budget.
            if saturation == 0:
                return replace(curve, limit_reason="already_full_without_books")
            waste = limiting_lot * (saturation + 1) - capacity
            if waste > waste_limit:
                return replace(curve, legal_limit=saturation - 1, limit_reason="book_waste_limit")
            return curve
        last = curve._threshold_prefix(saturation)
        reason = curve.limit_reason if last == saturation else "profit_threshold"
        if saturation == 0 and baseline.loaded_quantity == capacity:
            reason = "already_full_without_books"
        return replace(curve, legal_limit=last, limit_reason=reason)

    def profit_at(self, books: int) -> int:
        return self._load(books)[1].net_profit

    def _load(self, books):
        if books not in self._loads:
            self._loads[books] = optimal_load(self.candidates, self.capacity, books, fill=self.policy == "fill")
        return self._loads[books]

    def _threshold_prefix(self, saturation):
        """Find the FIRST illegal marginal, without assuming rounded concavity.

        The linear relaxation U(k) is concave and actual P(k) is within one
        unit below/above U(k). Therefore U(k)-U(k-1)-2 is a safe lower bound
        for a marginal. Binary search may skip this provably safe prefix only;
        the potentially ambiguous suffix is checked in chronological order.
        Tax-free curves are exactly linear-relaxation curves, permitting the
        original logarithmic search without a rounding assumption.
        """
        if not saturation:
            return 0
        tax_free = all(not row.buy_tax_bps and not row.sell_tax_bps for row in self.candidates)
        low, high, safe = 1, saturation, 0
        while low <= high:
            check_solver_cancelled()
            middle = (low + high) // 2
            delta = linear_profit(self.candidates, self.capacity, middle) - linear_profit(
                self.candidates, self.capacity, middle - 1)
            lower = delta if tax_free else delta - 2
            if lower > 0 and lower >= self.threshold:
                safe, low = middle, middle + 1
            else:
                high = middle - 1
        if tax_free:
            return safe
        previous = self.profit_at(safe)
        books = safe + 1
        homogeneous = len({(row.buy_price, row.sell_price, row.buy_tax_bps, row.sell_tax_bps)
                           for row in self.candidates}) == 1
        period = full = None
        if homogeneous:
            row = self.candidates[0]
            lot = sum(item.lot for item in self.candidates)
            quantity_period = lcm(10000 // gcd(row.buy_price * row.buy_tax_bps, 10000),
                                  10000 // gcd((row.sell_price - row.buy_price) * row.sell_tax_bps, 10000))
            period = quantity_period // gcd(quantity_period, lot)
            full = max((self.capacity + lot - 1) // lot - 1, 0)
        checked = 0
        while books <= saturation:
            check_solver_cancelled()
            profit = self.profit_at(books)
            marginal = profit - previous
            if marginal <= 0 or marginal < self.threshold:
                return books - 1
            previous = profit
            books += 1
            checked += 1
            if homogeneous and checked == period and books < full:
                # Identical economic goods: adding quantity_period items adds
                # integer taxes and a positive integer net amount. Optimal
                # quantity choices repeat by this period, including the option
                # to leave slots empty. Before saturation the book marginals
                # therefore repeat exactly; inspect one cycle, then its final
                # partial-capacity boundary. No concavity assumption is used.
                books = full
                previous = self.profit_at(full - 1)
        return saturation

    def minimum_books_for_profit(self, profit: int, limit: int) -> Optional[int]:
        if self.profit_at(limit) < profit:
            return None
        low, high = 0, limit
        while low < high:
            middle = (low + high) // 2
            if self.profit_at(middle) >= profit:
                high = middle
            else:
                low = middle + 1
        return low

    def option(self, books: int, *, budget: Optional[int], target: bool = False) -> TradeEdgeOption:
        if not 0 <= books <= self.legal_limit:
            raise ValueError("book count is outside the legal curve prefix")
        buys, settlement = self._load(books)
        free = self.capacity - sum(row[2] for row in buys)
        reason = self.limit_reason
        if books < self.legal_limit:
            reason = "target_objective" if target else "global_book_allocation"
            if budget is not None and books == budget:
                reason = "total_book_budget"
        profit = Fraction(self.profit_at(books))
        return replace(self.baseline, books_used=books, expected_profit=profit,
                       buy_product_ids=tuple(row[0] for row in buys),
                       buy_product_names=tuple(row[1] for row in buys), buys=tuple(buys),
                       book_incremental_profit=profit - self.baseline.expected_profit,
                       cargo_capacity=self.capacity, loaded_quantity=self.capacity - free,
                       book_waste_quantity=(max(sum(row.lot for row in self.candidates if row.unit_profit > 0) * (books + 1)
                                                - self.capacity, 0) if books else 0),
                       book_stop_reason=reason, legal_book_limit=self.legal_limit,
                       next_book_marginal_profit=Fraction(self.profit_at(books + 1) - profit),
                       settlement=settlement)


__all__ = ["TradeEdgeOption", "EdgeBookCurve"]
