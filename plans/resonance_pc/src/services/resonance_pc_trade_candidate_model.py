"""Pure, exact book curves shared by the four freight objectives.

For sorted integer unit profits p[i] and cumulative lots L[i],
P(k) = sum((p[i] - p[i+1]) * min(capacity, L[i] * (k+1))).
This is concave, so discrete marginal profits are nonincreasing. Both the
saturation bound and a threshold-prefix binary search are therefore exact.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction
from typing import Any, Optional, Tuple

from .resonance_pc_trade_solver_common import check_solver_cancelled


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
    next_book_marginal_profit: Fraction = Fraction(0)

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
    candidates: Tuple[Tuple[Fraction, str, str, int], ...]
    capacity: int
    policy: str
    threshold: Fraction
    legal_limit: int
    saturation_limit: int
    limit_reason: str

    @classmethod
    def create(cls, *, baseline: TradeEdgeOption, candidates: tuple,
               capacity: int, policy: str, threshold: Fraction) -> "EdgeBookCurve":
        if not candidates:
            return cls(baseline, (), capacity, policy, threshold, 0, 0,
                       "no_suitable_products")
        if policy == "fill":
            limiting_lot = sum(row[3] for row in candidates)
        else:
            limiting_lot = sum(row[3] for row in candidates if row[0] == candidates[0][0])
        saturation = max((capacity + limiting_lot - 1) // limiting_lot - 1, 0)
        curve = cls(baseline, candidates, capacity, policy, threshold, saturation,
                    saturation, "capacity_reached" if policy == "fill" else "profit_saturated")
        low, high = 1, saturation
        last = 0
        while low <= high:
            check_solver_cancelled()
            middle = (low + high) // 2
            marginal = curve.profit_at(middle) - curve.profit_at(middle - 1)
            if marginal > 0 and marginal >= threshold:
                last = middle
                low = middle + 1
            else:
                high = middle - 1
        reason = curve.limit_reason if last == saturation else "profit_threshold"
        if saturation == 0 and baseline.loaded_quantity == capacity:
            reason = "already_full_without_books"
        return replace(curve, legal_limit=last, limit_reason=reason)

    def profit_at(self, books: int) -> int:
        free, profit = self.capacity, 0
        for unit, _id, _name, lot in self.candidates:
            quantity = min(free, lot * (books + 1))
            profit += int(unit) * quantity
            free -= quantity
            if not free:
                break
        return profit

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
        free, buys = self.capacity, []
        for unit, product_id, name, lot in self.candidates:
            quantity = min(free, lot * (books + 1))
            if quantity:
                buys.append((product_id, name, quantity, unit))
                free -= quantity
            if not free:
                break
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
                       book_stop_reason=reason, legal_book_limit=self.legal_limit,
                       next_book_marginal_profit=Fraction(self.profit_at(books + 1) - profit))


__all__ = ["TradeEdgeOption", "EdgeBookCurve"]
