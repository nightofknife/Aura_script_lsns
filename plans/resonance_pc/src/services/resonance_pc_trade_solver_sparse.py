"""Sparse integer-tick backend for exact Resonance PC trade planning."""

from __future__ import annotations

import heapq
import time
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Dict, List, Optional, Tuple

from .resonance_pc_trade_solver_common import (
    CompiledEdge,
    ExactSearchResult,
    PreparedSearch,
    SolverProgress,
    check_solver_cancelled,
)


@dataclass(eq=False)
class _Node:
    city_index: int
    city_id: str
    fatigue_ticks: int
    books_used: int
    full_negotiation_used: int
    profit: int
    depth: int
    parent: Optional["_Node"]
    edge: Optional[CompiledEdge]


def _node_paths(
    node: _Node,
    cache: Dict[_Node, Tuple[Tuple[str, ...], Tuple[Any, ...]]],
) -> Tuple[Tuple[str, ...], Tuple[Any, ...]]:
    """Return stable path keys without recursion or cross-run cache retention."""

    current = node
    missing = []
    while current not in cache:
        missing.append(current)
        if current.parent is None or current.edge is None:
            cache[current] = ((), ())
            break
        current = current.parent
    city_path, edge_path = cache[current]
    for item in reversed(missing):
        if item.parent is None or item.edge is None:
            cache[item] = (city_path, edge_path)
            continue
        city_path = city_path + (item.city_id,)
        edge_path = edge_path + (item.edge.option.stable_signature,)
        cache[item] = (city_path, edge_path)
    return cache[node]


def _same_state_better(
    candidate: _Node,
    existing: _Node,
    *,
    path_cache: Dict[_Node, Tuple[Tuple[str, ...], Tuple[Any, ...]]],
) -> bool:
    if candidate.profit != existing.profit:
        return candidate.profit > existing.profit
    if candidate.books_used != existing.books_used:
        return candidate.books_used < existing.books_used
    if candidate.full_negotiation_used != existing.full_negotiation_used:
        return (
            candidate.full_negotiation_used
            < existing.full_negotiation_used
        )
    if candidate.depth != existing.depth:
        return candidate.depth < existing.depth
    return _node_paths(candidate, path_cache) < _node_paths(
        existing, path_cache
    )


def _final_better(
    candidate: _Node,
    existing: Optional[_Node],
    *,
    path_cache: Dict[_Node, Tuple[Tuple[str, ...], Tuple[Any, ...]]],
) -> bool:
    if existing is None:
        return True
    if candidate.profit != existing.profit:
        return candidate.profit > existing.profit
    candidate_cities, candidate_edges = _node_paths(
        candidate, path_cache
    )
    existing_cities, existing_edges = _node_paths(
        existing, path_cache
    )
    return (
        candidate.fatigue_ticks,
        candidate.books_used,
        candidate.full_negotiation_used,
        candidate.depth,
        candidate_cities,
        candidate_edges,
    ) < (
        existing.fatigue_ticks,
        existing.books_used,
        existing.full_negotiation_used,
        existing.depth,
        existing_cities,
        existing_edges,
    )


class _Fenwick2Max:
    def __init__(self, book_budget: int, negotiation_budget: int) -> None:
        self.book_size = int(book_budget) + 1
        self.negotiation_size = int(negotiation_budget) + 1
        self.values = [
            [-1] * (self.negotiation_size + 1)
            for _ in range(self.book_size + 1)
        ]

    def update(self, books: int, negotiation: int, profit: int) -> None:
        book_index = int(books) + 1
        while book_index <= self.book_size:
            negotiation_index = int(negotiation) + 1
            while negotiation_index <= self.negotiation_size:
                if profit > self.values[book_index][negotiation_index]:
                    self.values[book_index][negotiation_index] = profit
                negotiation_index += negotiation_index & -negotiation_index
            book_index += book_index & -book_index

    def query(self, books: int, negotiation: int) -> int:
        result = -1
        book_index = int(books) + 1
        while book_index:
            negotiation_index = int(negotiation) + 1
            while negotiation_index:
                result = max(
                    result,
                    self.values[book_index][negotiation_index],
                )
                negotiation_index -= negotiation_index & -negotiation_index
            book_index -= book_index & -book_index
        return result


class _Fenwick1Max:
    """Prefix maximum for the unbounded-book negotiation resource."""

    def __init__(self, negotiation_budget: int) -> None:
        self.size = int(negotiation_budget) + 1
        self.values = [-1] * (self.size + 1)

    def update(self, negotiation: int, profit: int) -> None:
        index = int(negotiation) + 1
        while index <= self.size:
            if profit > self.values[index]:
                self.values[index] = profit
            index += index & -index

    def query(self, negotiation: int) -> int:
        result = -1
        index = int(negotiation) + 1
        while index:
            result = max(result, self.values[index])
            index -= index & -index
        return result


class _Fenwick1Best:
    """Prefix best by profit then fewer books for one fatigue layer."""

    def __init__(self, negotiation_budget: int) -> None:
        self.size = int(negotiation_budget) + 1
        self.values: List[Optional[Tuple[int, int]]] = [
            None
        ] * (self.size + 1)

    def update(self, negotiation: int, value: Tuple[int, int]) -> None:
        index = int(negotiation) + 1
        while index <= self.size:
            if self.values[index] is None or value > self.values[index]:
                self.values[index] = value
            index += index & -index

    def query(self, negotiation: int) -> Optional[Tuple[int, int]]:
        result: Optional[Tuple[int, int]] = None
        index = int(negotiation) + 1
        while index:
            value = self.values[index]
            if value is not None and (result is None or value > result):
                result = value
            index -= index & -index
        return result


def _route_from_node(
    prepared: PreparedSearch, node: _Node
) -> Tuple[Tuple[str, ...], Tuple[Any, ...]]:
    route = []
    current = node
    while current.parent is not None and current.edge is not None:
        route.append(current.edge.option)
        current = current.parent
    route.reverse()
    city_path = [prepared.city_ids[prepared.start_index]]
    city_path.extend(option.to_city_id for option in route)
    return tuple(city_path), tuple(route)


def solve_sparse(prepared: PreparedSearch) -> Optional[ExactSearchResult]:
    """Solve exact labels using ordered sparse fatigue layers."""

    started_at = time.perf_counter()
    budget = prepared.scale.budget_ticks
    book_budget = prepared.book_budget
    unbounded_books = prepared.unbounded_books
    negotiation_limit = (
        0
        if prepared.all_plan == 1
        else min(prepared.negotiation_budget, prepared.max_legs * 2)
    )
    city_count = len(prepared.city_ids)
    by_city: List[List[CompiledEdge]] = [[] for _ in range(city_count)]
    for edge in prepared.edges:
        by_city[edge.from_index].append(edge)
    by_city_remaining_books: Optional[
        List[List[Tuple[CompiledEdge, ...]]]
    ] = None
    if not unbounded_books:
        by_city_remaining_books = [
            [tuple() for _ in range(book_budget + 1)]
            for _ in range(city_count)
        ]
        for city_index in range(city_count):
            for remaining_books in range(book_budget + 1):
                by_city_remaining_books[city_index][remaining_books] = tuple(
                    edge
                    for edge in by_city[city_index]
                    if edge.books_used <= remaining_books
                )

    initial = _Node(
        city_index=prepared.start_index,
        city_id=prepared.city_ids[prepared.start_index],
        fatigue_ticks=0,
        books_used=0,
        full_negotiation_used=0,
        profit=0,
        depth=0,
        parent=None,
        edge=None,
    )
    path_cache = {initial: ((), ())}
    if prepared.all_plan == 1:
        initial_key: Tuple[int, ...] = (
            (prepared.start_index,)
            if unbounded_books
            else (prepared.start_index, 0)
        )
    else:
        initial_key = (
            (prepared.start_index, 0)
            if unbounded_books
            else (prepared.start_index, 0, 0)
        )
    pending: Dict[int, Dict[Tuple[int, ...], _Node]] = {
        0: {initial_key: initial}
    }
    fatigue_heap = [0]
    pending_state_count = 1
    max_pending_states = 1
    max_pending_layers = 1
    popped_states = 0
    kept_states = 0
    pruned_states = 0
    transition_attempts = 0
    state_updates = 0
    best: Optional[_Node] = None
    required_end_indices = (
        None
        if prepared.required_end_indices is None
        else frozenset(prepared.required_end_indices)
    )
    progress = SolverProgress(backend="sparse", total=budget)
    progress.emit(0, force=True, pending_states=1)

    if unbounded_books and prepared.all_plan == 1:
        global_dominance: Any = [-1] * city_count
    elif unbounded_books:
        global_dominance = [
            _Fenwick1Max(negotiation_limit)
            for _ in range(city_count)
        ]
    elif prepared.all_plan == 1:
        global_dominance: Any = [
            [-1] * (book_budget + 1) for _ in range(city_count)
        ]
    else:
        global_dominance = [
            _Fenwick2Max(book_budget, negotiation_limit)
            for _ in range(city_count)
        ]

    while fatigue_heap:
        fatigue = heapq.heappop(fatigue_heap)
        layer = pending.pop(fatigue)
        pending_state_count -= len(layer)
        check_solver_cancelled()
        progress.emit(
            fatigue,
            popped_states=popped_states,
            kept_states=kept_states,
            pending_states=pending_state_count,
        )
        grouped: List[List[_Node]] = [[] for _ in range(city_count)]
        for node in layer.values():
            grouped[node.city_index].append(node)

        for city_index, nodes in enumerate(grouped):
            if not nodes:
                continue
            keepers: List[_Node] = []
            if unbounded_books and prepared.all_plan == 1:
                for node in nodes:
                    popped_states += 1
                    if global_dominance[city_index] >= node.profit:
                        pruned_states += 1
                        continue
                    keepers.append(node)
                    kept_states += 1
                for node in keepers:
                    global_dominance[city_index] = max(
                        global_dominance[city_index],
                        node.profit,
                    )
            elif unbounded_books:
                local_dominance = _Fenwick1Best(negotiation_limit)
                for node in sorted(
                    nodes,
                    key=lambda item: (
                        item.full_negotiation_used,
                        item.books_used,
                    ),
                ):
                    popped_states += 1
                    if (
                        global_dominance[city_index].query(
                            node.full_negotiation_used
                        )
                        >= node.profit
                    ):
                        pruned_states += 1
                        continue
                    local_best = local_dominance.query(
                        node.full_negotiation_used
                    )
                    if local_best is not None and local_best >= (
                        node.profit,
                        -node.books_used,
                    ):
                        pruned_states += 1
                        continue
                    local_dominance.update(
                        node.full_negotiation_used,
                        (node.profit, -node.books_used),
                    )
                    keepers.append(node)
                    kept_states += 1
                for node in keepers:
                    global_dominance[city_index].update(
                        node.full_negotiation_used,
                        node.profit,
                    )
            elif prepared.all_plan == 1:
                same_layer_best = -1
                for node in sorted(nodes, key=lambda item: item.books_used):
                    popped_states += 1
                    if (
                        global_dominance[city_index][node.books_used]
                        >= node.profit
                        or same_layer_best >= node.profit
                    ):
                        pruned_states += 1
                        continue
                    same_layer_best = node.profit
                    keepers.append(node)
                    kept_states += 1
                for node in keepers:
                    for limit in range(node.books_used, book_budget + 1):
                        if (
                            node.profit
                            > global_dominance[city_index][limit]
                        ):
                            global_dominance[city_index][limit] = node.profit
            else:
                local_dominance = _Fenwick2Max(
                    book_budget,
                    negotiation_limit,
                )
                for node in sorted(
                    nodes,
                    key=lambda item: (
                        item.books_used,
                        item.full_negotiation_used,
                    ),
                ):
                    popped_states += 1
                    if (
                        global_dominance[city_index].query(
                            node.books_used,
                            node.full_negotiation_used,
                        )
                        >= node.profit
                        or local_dominance.query(
                            node.books_used,
                            node.full_negotiation_used,
                        )
                        >= node.profit
                    ):
                        pruned_states += 1
                        continue
                    local_dominance.update(
                        node.books_used,
                        node.full_negotiation_used,
                        node.profit,
                    )
                    keepers.append(node)
                    kept_states += 1
                for node in keepers:
                    global_dominance[city_index].update(
                        node.books_used,
                        node.full_negotiation_used,
                        node.profit,
                    )

            for node in keepers:
                if (
                    node.depth
                    and node.profit > 0
                    and (
                        required_end_indices is None
                        or node.city_index in required_end_indices
                    )
                    and _final_better(
                        node,
                        best,
                        path_cache=path_cache,
                    )
                ):
                    best = node
                if unbounded_books:
                    outgoing_edges = by_city[city_index]
                else:
                    assert by_city_remaining_books is not None
                    remaining_books = book_budget - node.books_used
                    outgoing_edges = by_city_remaining_books[city_index][
                        remaining_books
                    ]
                for edge in outgoing_edges:
                    transition_attempts += 1
                    next_fatigue = node.fatigue_ticks + edge.fatigue_ticks
                    next_negotiation = (
                        node.full_negotiation_used
                        + edge.full_negotiation_used
                    )
                    if next_fatigue > budget:
                        continue
                    if (
                        prepared.all_plan == 0
                        and next_negotiation > negotiation_limit
                    ):
                        continue
                    next_books = node.books_used + edge.books_used
                    candidate = _Node(
                        city_index=edge.to_index,
                        city_id=prepared.city_ids[edge.to_index],
                        fatigue_ticks=next_fatigue,
                        books_used=next_books,
                        full_negotiation_used=next_negotiation,
                        profit=node.profit + edge.profit,
                        depth=node.depth + 1,
                        parent=node,
                        edge=edge,
                    )
                    if prepared.all_plan == 1:
                        state_key = (
                            (edge.to_index,)
                            if unbounded_books
                            else (edge.to_index, next_books)
                        )
                    else:
                        state_key = (
                            (edge.to_index, next_negotiation)
                            if unbounded_books
                            else (
                                edge.to_index,
                                next_books,
                                next_negotiation,
                            )
                        )
                    target = pending.get(next_fatigue)
                    if target is None:
                        target = {}
                        pending[next_fatigue] = target
                        heapq.heappush(fatigue_heap, next_fatigue)
                        max_pending_layers = max(
                            max_pending_layers,
                            len(pending),
                        )
                    existing = target.get(state_key)
                    if (
                        existing is not None
                        and not _same_state_better(
                            candidate,
                            existing,
                            path_cache=path_cache,
                        )
                    ):
                        continue
                    if existing is None:
                        pending_state_count += 1
                        max_pending_states = max(
                            max_pending_states,
                            pending_state_count,
                        )
                    target[state_key] = candidate
                    state_updates += 1

    check_solver_cancelled()
    progress.emit(
        budget,
        force=True,
        popped_states=popped_states,
        kept_states=kept_states,
        pending_states=0,
    )
    if best is None or best.profit <= 0:
        return None

    city_path, route = _route_from_node(prepared, best)
    return ExactSearchResult(
        expected_profit=Fraction(best.profit, 1),
        expected_fatigue_used=prepared.scale.to_fraction(
            best.fatigue_ticks
        ),
        books_used=best.books_used,
        full_negotiation_used=best.full_negotiation_used,
        city_path=city_path,
        route=route,
        backend="sparse",
        stats={
            "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
            "popped_states": popped_states,
            "kept_states": kept_states,
            "pruned_states": pruned_states,
            "transition_attempts": transition_attempts,
            "state_updates": state_updates,
            "max_pending_states": max_pending_states,
            "max_pending_layers": max_pending_layers,
        },
    )


__all__ = ["solve_sparse"]
