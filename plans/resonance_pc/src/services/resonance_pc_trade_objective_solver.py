"""Exact fixed-sequence and minimum-fatigue target-profit searches.

Target dominance additionally preserves the resource/path ordering. A label
with more profit but a worse path is not enough to dominate an equal-resource
label: after either reaches the target, excess profit is not an objective.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .resonance_pc_trade_candidate_model import EdgeBookCurve, TradeEdgeOption
from .resonance_pc_trade_solver_common import (
    ExactSearchResult, PreparedSearch, SolverProgress, check_solver_cancelled,
)


@dataclass(frozen=True)
class _Label:
    city: str
    ticks: int
    books: int
    negotiations: int
    profit: int
    route: Tuple[TradeEdgeOption, ...]
    cities: Tuple[str, ...]
    signatures: Tuple[Any, ...]

    @property
    def tie(self) -> tuple:
        return self.books, self.negotiations, len(self.route), self.cities, self.signatures

    def extend(self, option: TradeEdgeOption, cost: int) -> "_Label":
        return _Label(option.to_city_id, self.ticks + cost, self.books + option.books_used,
                      self.negotiations + option.full_negotiation_used,
                      self.profit + int(option.expected_profit), self.route + (option,),
                      self.cities + (option.to_city_id,), self.signatures + (option.stable_signature,))


def _ticks(prepared: PreparedSearch, option: TradeEdgeOption) -> int:
    value = option.expected_fatigue_cost * prepared.scale.denominator / prepared.scale.divisor
    if value.denominator != 1:
        raise AssertionError("option is outside the prepared fatigue lattice")
    return int(value)


def _result(prepared: PreparedSearch, label: _Label, backend: str, stats: dict) -> ExactSearchResult:
    return ExactSearchResult(Fraction(label.profit), prepared.scale.to_fraction(label.ticks),
                             label.books, label.negotiations, label.cities, label.route, backend, stats)


def _resources_fit(prepared: PreparedSearch, books: int, negotiations: int) -> bool:
    return ((prepared.unbounded_books or books <= prepared.book_budget)
            and (prepared.all_plan == 1 or negotiations <= prepared.negotiation_budget))


def _dominates_target(a: _Label, b: _Label, prepared: PreparedSearch, goal: int) -> bool:
    if a.ticks > b.ticks or min(a.profit, goal) < min(b.profit, goal):
        return False
    # Feasibility of the same suffix under hard resource limits.
    if not prepared.unbounded_books and a.books > b.books:
        return False
    if prepared.all_plan == 0 and a.negotiations > b.negotiations:
        return False
    # Earlier fatigue is strictly better even with worse secondary resources.
    # At equal fatigue the complete secondary order must survive the suffix.
    return a.ticks < b.ticks or a.tie <= b.tie


def _insert_target(frontier: list, candidate: _Label, prepared: PreparedSearch, goal: int) -> bool:
    if any(_dominates_target(old, candidate, prepared, goal) for old in frontier):
        return False
    frontier[:] = [old for old in frontier if not _dominates_target(candidate, old, prepared, goal)]
    frontier.append(candidate)
    return True


def solve_target(prepared: PreparedSearch, *, curves: Sequence[EdgeBookCurve],
                 target: int, incumbent: ExactSearchResult) -> ExactSearchResult:
    """Process fatigue layers exactly, retaining all nondominated target labels.

    Curves are expanded only when a partial route needs their low-book choices.
    A goal transition uses binary search for its minimum book count. With an
    incumbent fatigue bound, no continuation of that transition can improve
    the answer because every movement has strictly positive fatigue.
    """
    by_city: Dict[str, list] = {city: [] for city in prepared.city_ids}
    for curve in curves:
        if curve.baseline.expected_fatigue_cost > prepared.fatigue_budget or not _resources_fit(
                prepared, 0, curve.baseline.full_negotiation_used):
            continue
        cost = _ticks(prepared, curve.baseline)
        if cost <= prepared.scale.budget_ticks:
            by_city[curve.baseline.from_city_id].append((curve, cost))
    allowed_end = (None if prepared.required_end_indices is None else
                   {prepared.city_ids[index] for index in prepared.required_end_indices})
    start = prepared.city_ids[prepared.start_index]
    initial = _Label(start, 0, 0, 0, 0, (), (start,), ())
    best = initial
    for option in incumbent.route:
        best = best.extend(option, _ticks(prepared, option))
    pending: Dict[int, Dict[str, list]] = {0: {start: [initial]}}
    heap = [0]
    settled: Dict[str, list] = {city: [] for city in prepared.city_ids}
    stats = {"expanded_labels": 0, "generated_labels": 1, "pruned_labels": 0,
             "goal_probes": 0, "peak_pending_labels": 1}
    pending_count = 1
    option_cache = {}
    def option_at(curve: EdgeBookCurve, books: int) -> TradeEdgeOption:
        key = (id(curve), books)
        if key not in option_cache:
            option_cache[key] = curve.option(books, budget=None if prepared.unbounded_books
                                             else prepared.book_budget, target=True)
        return option_cache[key]
    progress = SolverProgress(backend="target_sparse", total=prepared.scale.budget_ticks)
    while heap:
        fatigue = heapq.heappop(heap)
        if fatigue >= best.ticks:
            break
        layer = pending.pop(fatigue)
        pending_count -= sum(len(labels) for labels in layer.values())
        check_solver_cancelled()
        progress.emit(fatigue, **stats)
        nodes = []
        for city, labels in layer.items():
            for label in sorted(labels, key=lambda item: item.tie):
                if not _insert_target(settled[city], label, prepared, target):
                    stats["pruned_labels"] += 1
                    continue
                nodes.append(label)
                stats["expanded_labels"] += 1
        # Probe every goal first. This bounds the second pass without expanding
        # a million-count curve for a one-edge target.
        for node in nodes:
            for curve, cost in by_city[node.city]:
                option = curve.baseline
                next_ticks = node.ticks + cost
                if next_ticks > best.ticks or (allowed_end is not None and option.to_city_id not in allowed_end):
                    continue
                negotiations = node.negotiations + option.full_negotiation_used
                if not _resources_fit(prepared, node.books, negotiations):
                    continue
                limit = curve.legal_limit if prepared.unbounded_books else min(
                    curve.legal_limit, prepared.book_budget - node.books)
                stats["goal_probes"] += 1
                books = curve.minimum_books_for_profit(max(target - node.profit, 0), limit)
                if books is None:
                    continue
                candidate = node.extend(option_at(curve, books), cost)
                if (candidate.ticks, candidate.tie) < (best.ticks, best.tie):
                    best = candidate
        for node in nodes:
            for curve, cost in by_city[node.city]:
                next_ticks = node.ticks + cost
                if next_ticks >= best.ticks:
                    continue
                negotiations = node.negotiations + curve.baseline.full_negotiation_used
                if not _resources_fit(prepared, node.books, negotiations):
                    continue
                limit = curve.legal_limit if prepared.unbounded_books else min(
                    curve.legal_limit, prepared.book_budget - node.books)
                # Once profit is capped, extra books only worsen the objective.
                goal_books = curve.minimum_books_for_profit(max(target - node.profit, 0), limit)
                if goal_books is not None:
                    limit = goal_books
                destination = curve.baseline.to_city_id
                if next_ticks not in pending:
                    pending[next_ticks] = {}
                    heapq.heappush(heap, next_ticks)
                frontier = pending[next_ticks].setdefault(destination, [])
                for books in range(limit + 1):
                    if books % 256 == 0:
                        check_solver_cancelled()
                    candidate = node.extend(option_at(curve, books), cost)
                    if any(_dominates_target(old, candidate, prepared, target) for old in settled[destination]):
                        stats["pruned_labels"] += 1
                        continue
                    before = len(frontier)
                    if _insert_target(frontier, candidate, prepared, target):
                        pending_count += len(frontier) - before
                        stats["generated_labels"] += 1
                        stats["peak_pending_labels"] = max(stats["peak_pending_labels"], pending_count)
                    else:
                        stats["pruned_labels"] += 1
    check_solver_cancelled()
    stats["cached_edge_options"] = len(option_cache)
    progress.emit(best.ticks, force=True, **stats)
    return _result(prepared, best, "target_sparse", stats)


def _dominates_fixed(a: _Label, b: _Label, prepared: PreparedSearch) -> bool:
    # Fatigue is the primary maximization objective. Different fatigue values
    # at the same route position must survive: neither can dominate the other.
    if a.ticks != b.ticks or a.profit < b.profit:
        return False
    if not prepared.unbounded_books and a.books > b.books:
        return False
    if prepared.all_plan == 0 and a.negotiations > b.negotiations:
        return False
    return a.profit > b.profit or a.tie <= b.tie


def solve_fixed(prepared: PreparedSearch, *, city_path: Sequence[str],
                options: Mapping[Tuple[str, str], Sequence[TradeEdgeOption]]) -> Optional[ExactSearchResult]:
    """Maximize legal-prefix fatigue, then profit and the complete tie order.

    An open route is traversed at most once. A closed route repeats in its
    specified order, bounded by budget/minimum positive travel cost.
    """
    start = city_path[0]
    labels = [_Label(start, 0, 0, 0, 0, (), (start,), ())]
    pairs = tuple(zip(city_path, city_path[1:]))
    closed = city_path[0] == city_path[-1]
    options = {pair: tuple(option for option in options.get(pair, ())
               if option.expected_fatigue_cost <= prepared.fatigue_budget
               and _resources_fit(prepared, option.books_used, option.full_negotiation_used))
               for pair in pairs}
    if not options.get(pairs[0]):
        return None
    minimum_cost = min(_ticks(prepared, option) for pair in pairs for option in options[pair])
    if minimum_cost <= 0:
        raise ValueError("fixed routes require strictly positive movement fatigue")
    leg_limit = prepared.scale.budget_ticks // minimum_cost if closed else len(pairs)
    stats = {"generated_labels": 1, "peak_labels": 1, "explored_positions": 0,
             "exact_leg_bound": leg_limit}
    progress = SolverProgress(backend="fixed_sparse", total=max(leg_limit, 1))
    best = None
    for position in range(leg_limit):
        from_city, to_city = pairs[position % len(pairs)]
        check_solver_cancelled()
        next_labels = []
        for node in labels:
            for option in options.get((from_city, to_city), ()):
                candidate = node.extend(option, _ticks(prepared, option))
                if candidate.ticks > prepared.scale.budget_ticks or not _resources_fit(
                        prepared, candidate.books, candidate.negotiations):
                    continue
                if any(_dominates_fixed(old, candidate, prepared) for old in next_labels):
                    continue
                next_labels[:] = [old for old in next_labels if not _dominates_fixed(candidate, old, prepared)]
                next_labels.append(candidate)
                stats["generated_labels"] += 1
        if not next_labels:
            break
        labels = next_labels
        tradable = [label for label in labels if label.profit > 0]
        if tradable:
            candidate_best = min(tradable, key=lambda item: (-item.ticks, -item.profit, item.tie))
            if best is None or (-candidate_best.ticks, -candidate_best.profit, candidate_best.tie) < (
                    -best.ticks, -best.profit, best.tie):
                best = candidate_best
        stats["peak_labels"] = max(stats["peak_labels"], len(labels))
        stats["explored_positions"] = position + 1
        progress.emit(position + 1, **stats)
    if best is None:
        return None
    progress.emit(leg_limit, force=True, **stats)
    return _result(prepared, best, "fixed_sparse", stats)


__all__ = ["solve_fixed", "solve_target"]
