"""Dense NumPy backend for the exact Resonance PC trade recurrence."""

from __future__ import annotations

import time
from functools import lru_cache
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Optional, Tuple

import numpy as np

from .resonance_pc_trade_solver_common import (
    CompiledEdge,
    DenseBackendUnavailable,
    ExactSearchResult,
    PreparedSearch,
    SolverProgress,
    check_solver_cancelled,
)


_NEGATIVE_INFINITY = np.int64(-(2**62))


def _score_weights(prepared: PreparedSearch) -> Tuple[int, int, int]:
    max_full_negotiation = prepared.max_legs * 2
    negotiation_weight = prepared.max_legs + 1
    max_secondary_penalty = (
        max_full_negotiation * negotiation_weight + prepared.max_legs
    )
    profit_weight = max_secondary_penalty + 1
    max_edge_profit = max((edge.profit for edge in prepared.edges), default=0)
    max_score = (
        max_edge_profit * prepared.max_legs * profit_weight
        + max_secondary_penalty
    )
    if max_score >= 2**61:
        raise DenseBackendUnavailable("packed exact objective would overflow int64")
    return profit_weight, negotiation_weight, max_secondary_penalty


def _edge_score(
    edge: CompiledEdge,
    *,
    profit_weight: int,
    negotiation_weight: int,
) -> int:
    return (
        edge.profit * profit_weight
        - edge.full_negotiation_used * negotiation_weight
        - 1
    )


def _canonical_path_all_plan_one(
    *,
    prepared: PreparedSearch,
    score_matrix: np.ndarray,
    final_states: List[Tuple[int, int, int]],
    incoming: List[List[Tuple[CompiledEdge, int]]],
) -> Optional[Tuple[Tuple[str, ...], Tuple[Any, ...], Tuple[Any, ...]]]:
    @lru_cache(maxsize=None)
    def reconstruct(
        fatigue: int, city_index: int, books_used: int
    ) -> Optional[Tuple[Tuple[str, ...], Tuple[Any, ...], Tuple[Any, ...]]]:
        if (
            fatigue == 0
            and city_index == prepared.start_index
            and books_used == 0
        ):
            return ((prepared.city_ids[prepared.start_index],), (), ())

        target_score = int(score_matrix[fatigue, city_index, books_used])
        candidates = []
        for edge, score_delta in incoming[city_index]:
            previous_fatigue = fatigue - edge.fatigue_ticks
            previous_books = books_used - edge.books_used
            if previous_fatigue < 0 or previous_books < 0:
                continue
            previous_score = int(
                score_matrix[
                    previous_fatigue,
                    edge.from_index,
                    previous_books,
                ]
            )
            if (
                previous_score == int(_NEGATIVE_INFINITY)
                or previous_score + score_delta != target_score
            ):
                continue
            parent = reconstruct(
                previous_fatigue,
                edge.from_index,
                previous_books,
            )
            if parent is None:
                continue
            candidates.append(
                (
                    parent[0] + (prepared.city_ids[city_index],),
                    parent[1] + (edge.option.stable_signature,),
                    parent[2] + (edge.option,),
                )
            )
        if not candidates:
            return None
        return min(candidates, key=lambda item: (item[0], item[1]))

    paths = [
        reconstruct(fatigue, city_index, books_used)
        for fatigue, city_index, books_used in final_states
    ]
    valid = [item for item in paths if item is not None]
    if not valid:
        return None
    return min(valid, key=lambda item: (item[0], item[1]))


def _canonical_path_all_plan_zero(
    *,
    prepared: PreparedSearch,
    score_matrix: np.ndarray,
    final_states: List[Tuple[int, int, int, int]],
    incoming: List[List[Tuple[CompiledEdge, int]]],
) -> Optional[Tuple[Tuple[str, ...], Tuple[Any, ...], Tuple[Any, ...]]]:
    @lru_cache(maxsize=None)
    def reconstruct(
        fatigue: int,
        city_index: int,
        books_used: int,
        full_negotiation_used: int,
    ) -> Optional[Tuple[Tuple[str, ...], Tuple[Any, ...], Tuple[Any, ...]]]:
        if (
            fatigue == 0
            and city_index == prepared.start_index
            and books_used == 0
            and full_negotiation_used == 0
        ):
            return ((prepared.city_ids[prepared.start_index],), (), ())

        target_score = int(
            score_matrix[
                fatigue,
                city_index,
                books_used,
                full_negotiation_used,
            ]
        )
        candidates = []
        for edge, score_delta in incoming[city_index]:
            previous_fatigue = fatigue - edge.fatigue_ticks
            previous_books = books_used - edge.books_used
            previous_negotiation = (
                full_negotiation_used - edge.full_negotiation_used
            )
            if (
                previous_fatigue < 0
                or previous_books < 0
                or previous_negotiation < 0
            ):
                continue
            previous_score = int(
                score_matrix[
                    previous_fatigue,
                    edge.from_index,
                    previous_books,
                    previous_negotiation,
                ]
            )
            if (
                previous_score == int(_NEGATIVE_INFINITY)
                or previous_score + score_delta != target_score
            ):
                continue
            parent = reconstruct(
                previous_fatigue,
                edge.from_index,
                previous_books,
                previous_negotiation,
            )
            if parent is None:
                continue
            candidates.append(
                (
                    parent[0] + (prepared.city_ids[city_index],),
                    parent[1] + (edge.option.stable_signature,),
                    parent[2] + (edge.option,),
                )
            )
        if not candidates:
            return None
        return min(candidates, key=lambda item: (item[0], item[1]))

    paths = [
        reconstruct(fatigue, city_index, books_used, full_negotiation_used)
        for fatigue, city_index, books_used, full_negotiation_used in final_states
    ]
    valid = [item for item in paths if item is not None]
    if not valid:
        return None
    return min(valid, key=lambda item: (item[0], item[1]))


def _find_final_all_plan_one(
    *,
    prepared: PreparedSearch,
    score_matrix: np.ndarray,
    profit_weight: int,
    max_secondary_penalty: int,
) -> Tuple[int, List[Tuple[int, int, int]]]:
    budget = prepared.scale.budget_ticks
    view = score_matrix[: budget + 1]
    max_packed_score = int(view.max())
    if max_packed_score == int(_NEGATIVE_INFINITY):
        return 0, []
    max_profit = (max_packed_score + max_secondary_penalty) // profit_weight
    if max_profit <= 0:
        return 0, []

    for fatigue in range(budget + 1):
        layer = view[fatigue]
        for books_used in range(prepared.book_budget + 1):
            scores = layer[:, books_used]
            valid_scores = scores[scores != _NEGATIVE_INFINITY]
            if valid_scores.size == 0:
                continue
            decoded = (
                valid_scores.astype(np.int64) + max_secondary_penalty
            ) // profit_weight
            if not np.any(decoded == max_profit):
                continue
            candidates = []
            for city_index, score in enumerate(scores):
                normalized = int(score)
                if normalized == int(_NEGATIVE_INFINITY):
                    continue
                if (
                    normalized + max_secondary_penalty
                ) // profit_weight == max_profit:
                    candidates.append((city_index, normalized))
            best_score = max(score for _, score in candidates)
            return max_profit, [
                (fatigue, city_index, books_used)
                for city_index, score in candidates
                if score == best_score
            ]
    return 0, []


def _find_final_all_plan_zero(
    *,
    prepared: PreparedSearch,
    score_matrix: np.ndarray,
    negotiation_limit: int,
    profit_weight: int,
    max_secondary_penalty: int,
) -> Tuple[int, List[Tuple[int, int, int, int]]]:
    budget = prepared.scale.budget_ticks
    view = score_matrix[: budget + 1]
    max_packed_score = int(view.max())
    if max_packed_score == int(_NEGATIVE_INFINITY):
        return 0, []
    max_profit = (max_packed_score + max_secondary_penalty) // profit_weight
    if max_profit <= 0:
        return 0, []

    for fatigue in range(budget + 1):
        layer = view[fatigue]
        for books_used in range(prepared.book_budget + 1):
            for full_negotiation_used in range(negotiation_limit + 1):
                scores = layer[:, books_used, full_negotiation_used]
                candidates = []
                for city_index, score in enumerate(scores):
                    normalized = int(score)
                    if normalized == int(_NEGATIVE_INFINITY):
                        continue
                    if (
                        normalized + max_secondary_penalty
                    ) // profit_weight == max_profit:
                        candidates.append((city_index, normalized))
                if not candidates:
                    continue
                best_score = max(score for _, score in candidates)
                return max_profit, [
                    (
                        fatigue,
                        city_index,
                        books_used,
                        full_negotiation_used,
                    )
                    for city_index, score in candidates
                    if score == best_score
                ]
    return 0, []


def solve_dense(
    prepared: PreparedSearch,
    *,
    estimate: Mapping[str, int],
) -> Optional[ExactSearchResult]:
    """Solve the exact recurrence using batched max-plus matrix updates."""

    started_at = time.perf_counter()
    profit_weight, negotiation_weight, max_secondary_penalty = _score_weights(
        prepared
    )
    city_count = len(prepared.city_ids)
    book_count = prepared.book_budget + 1
    budget = prepared.scale.budget_ticks
    max_cost = int(estimate["max_cost"])
    negotiation_limit = int(estimate["negotiation_limit"])
    incoming: List[List[Tuple[CompiledEdge, int]]] = [
        [] for _ in range(city_count)
    ]

    source_city: List[int] = []
    source_books: List[int] = []
    source_negotiation: List[int] = []
    relative_target: List[int] = []
    score_delta: List[int] = []

    if prepared.all_plan == 1:
        stride = city_count * book_count
        for edge in prepared.edges:
            delta = _edge_score(
                edge,
                profit_weight=profit_weight,
                negotiation_weight=negotiation_weight,
            )
            incoming[edge.to_index].append((edge, delta))
            for previous_books in range(
                prepared.book_budget - edge.books_used + 1
            ):
                source_city.append(edge.from_index)
                source_books.append(previous_books)
                relative_target.append(
                    edge.fatigue_ticks * stride
                    + edge.to_index * book_count
                    + previous_books
                    + edge.books_used
                )
                score_delta.append(delta)
        matrix = np.full(
            (budget + max_cost + 1, city_count, book_count),
            _NEGATIVE_INFINITY,
            dtype=np.int64,
        )
        matrix[0, prepared.start_index, 0] = 0
    else:
        negotiation_count = negotiation_limit + 1
        stride = city_count * book_count * negotiation_count
        for edge in prepared.edges:
            if edge.full_negotiation_used > negotiation_limit:
                continue
            delta = _edge_score(
                edge,
                profit_weight=profit_weight,
                negotiation_weight=negotiation_weight,
            )
            incoming[edge.to_index].append((edge, delta))
            for previous_books in range(
                prepared.book_budget - edge.books_used + 1
            ):
                for previous_negotiation in range(
                    negotiation_limit - edge.full_negotiation_used + 1
                ):
                    source_city.append(edge.from_index)
                    source_books.append(previous_books)
                    source_negotiation.append(previous_negotiation)
                    relative_target.append(
                        edge.fatigue_ticks * stride
                        + edge.to_index * book_count * negotiation_count
                        + (previous_books + edge.books_used)
                        * negotiation_count
                        + previous_negotiation
                        + edge.full_negotiation_used
                    )
                    score_delta.append(delta)
        matrix = np.full(
            (
                budget + max_cost + 1,
                city_count,
                book_count,
                negotiation_count,
            ),
            _NEGATIVE_INFINITY,
            dtype=np.int64,
        )
        matrix[0, prepared.start_index, 0, 0] = 0

    if not source_city:
        return None

    source_city_array = np.asarray(source_city, dtype=np.intp)
    source_books_array = np.asarray(source_books, dtype=np.intp)
    relative_target_array = np.asarray(relative_target, dtype=np.int64)
    score_delta_array = np.asarray(score_delta, dtype=np.int64)
    if prepared.all_plan == 0:
        source_negotiation_array = np.asarray(
            source_negotiation, dtype=np.intp
        )

    order = np.argsort(relative_target_array, kind="stable")
    sorted_targets = relative_target_array[order]
    group_starts = np.r_[
        0,
        np.flatnonzero(sorted_targets[1:] != sorted_targets[:-1]) + 1,
    ]
    unique_targets = sorted_targets[group_starts]
    flat_matrix = matrix.reshape(-1)
    progress = SolverProgress(backend="dense", total=budget)
    progress.emit(
        0,
        force=True,
        state_cells=int(estimate["state_cells"]),
        expanded_rows=len(source_city),
    )
    active_layers = 0

    for fatigue in range(budget + 1):
        if fatigue % 16 == 0:
            check_solver_cancelled()
            progress.emit(fatigue, active_layers=active_layers)
        if prepared.all_plan == 1:
            base = matrix[
                fatigue,
                source_city_array,
                source_books_array,
            ]
        else:
            base = matrix[
                fatigue,
                source_city_array,
                source_books_array,
                source_negotiation_array,
            ]
        if not np.any(base != _NEGATIVE_INFINITY):
            continue
        active_layers += 1
        candidate_scores = np.where(
            base == _NEGATIVE_INFINITY,
            _NEGATIVE_INFINITY,
            base + score_delta_array,
        )
        reduced = np.maximum.reduceat(candidate_scores[order], group_starts)
        destination = fatigue * stride + unique_targets
        flat_matrix[destination] = np.maximum(
            flat_matrix[destination],
            reduced,
        )

    check_solver_cancelled()
    if prepared.all_plan == 1:
        max_profit, final_states = _find_final_all_plan_one(
            prepared=prepared,
            score_matrix=matrix,
            profit_weight=profit_weight,
            max_secondary_penalty=max_secondary_penalty,
        )
        canonical = _canonical_path_all_plan_one(
            prepared=prepared,
            score_matrix=matrix,
            final_states=final_states,
            incoming=incoming,
        )
    else:
        max_profit, final_states = _find_final_all_plan_zero(
            prepared=prepared,
            score_matrix=matrix,
            negotiation_limit=negotiation_limit,
            profit_weight=profit_weight,
            max_secondary_penalty=max_secondary_penalty,
        )
        canonical = _canonical_path_all_plan_zero(
            prepared=prepared,
            score_matrix=matrix,
            final_states=final_states,
            incoming=incoming,
        )

    if max_profit <= 0 or canonical is None:
        progress.emit(budget, force=True, active_layers=active_layers)
        return None

    city_path, _edge_signatures, route = canonical
    expected_profit = sum(
        (Fraction(option.expected_profit) for option in route),
        Fraction(0, 1),
    )
    fatigue_ticks = sum(
        next(
            edge.fatigue_ticks
            for edge in prepared.edges
            if edge.option is option
        )
        for option in route
    )
    books_used = sum(int(option.books_used) for option in route)
    full_negotiation_used = sum(
        int(option.full_negotiation_used) for option in route
    )
    progress.emit(
        budget,
        force=True,
        active_layers=active_layers,
        route_length=len(route),
    )
    return ExactSearchResult(
        expected_profit=expected_profit,
        expected_fatigue_used=prepared.scale.to_fraction(fatigue_ticks),
        books_used=books_used,
        full_negotiation_used=full_negotiation_used,
        city_path=city_path,
        route=route,
        backend="dense",
        stats={
            "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
            "state_cells": int(estimate["state_cells"]),
            "expanded_rows": len(source_city),
            "target_groups": len(group_starts),
            "active_fatigue_layers": active_layers,
        },
    )


__all__ = ["solve_dense"]
