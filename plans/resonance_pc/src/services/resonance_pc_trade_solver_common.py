"""Shared exact-search primitives for the Resonance PC trade planner.

The public planner continues to use :class:`ResonancePcExactTradeSolver`.
This module only contains internal execution details shared by the dense
NumPy and sparse label backends.
"""

from __future__ import annotations

import asyncio
import contextvars
import math
import time
from contextlib import contextmanager
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Callable, Dict, Iterable, Iterator, Mapping, Optional, Sequence, Tuple

from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested


ProgressCallback = Callable[[Dict[str, Any]], None]

_PROGRESS_CALLBACK: contextvars.ContextVar[Optional[ProgressCallback]] = contextvars.ContextVar(
    "resonance_pc_trade_solver_progress_callback",
    default=None,
)


class DenseBackendUnavailable(RuntimeError):
    """The exact dense representation is unsafe for this input."""


@contextmanager
def trade_solver_progress(callback: Optional[ProgressCallback]) -> Iterator[None]:
    """Install a thread-propagated solver progress callback."""

    token = _PROGRESS_CALLBACK.set(callback)
    try:
        yield
    finally:
        _PROGRESS_CALLBACK.reset(token)


def check_solver_cancelled() -> None:
    """Stop a synchronous solver at a cooperative cancellation checkpoint."""

    if is_current_task_cancel_requested():
        raise asyncio.CancelledError("Resonance PC trade planning was cancelled")


class SolverProgress:
    """Throttled progress publisher used by both exact backends."""

    def __init__(self, *, backend: str, total: int) -> None:
        self.backend = str(backend)
        self.total = max(int(total), 1)
        self.callback = _PROGRESS_CALLBACK.get()
        self.started_at = time.perf_counter()
        self._last_bucket = -1
        self._last_emit = 0.0

    def emit(self, current: int, *, force: bool = False, **stats: Any) -> None:
        if self.callback is None:
            return
        normalized = min(max(int(current), 0), self.total)
        bucket = min((normalized * 10) // self.total, 10)
        now = time.perf_counter()
        if not force and bucket <= self._last_bucket and now - self._last_emit < 0.5:
            return
        self._last_bucket = max(self._last_bucket, bucket)
        self._last_emit = now
        payload = {
            "solver_backend": self.backend,
            "current": normalized,
            "total": self.total,
            "percent": min((normalized * 100.0) / self.total, 100.0),
            "elapsed_ms": int((now - self.started_at) * 1000),
        }
        payload.update(stats)
        self.callback(payload)


@dataclass(frozen=True)
class CompiledEdge:
    option: Any
    from_index: int
    to_index: int
    fatigue_ticks: int
    books_used: int
    full_negotiation_used: int
    profit: int


@dataclass(frozen=True)
class FatigueScale:
    denominator: int
    divisor: int
    budget_ticks: int

    def to_fraction(self, ticks: int) -> Fraction:
        return Fraction(int(ticks) * self.divisor, self.denominator)


@dataclass(frozen=True)
class ExactSearchResult:
    expected_profit: Fraction
    expected_fatigue_used: Fraction
    books_used: int
    full_negotiation_used: int
    city_path: Tuple[str, ...]
    route: Tuple[Any, ...]
    backend: str
    stats: Mapping[str, Any]


@dataclass(frozen=True)
class PreparedSearch:
    city_ids: Tuple[str, ...]
    start_index: int
    required_end_indices: Optional[Tuple[int, ...]]
    edges: Tuple[CompiledEdge, ...]
    scale: FatigueScale
    fatigue_budget: int
    book_budget: int
    negotiation_budget: int
    all_plan: int
    max_legs: int


def _lcm_denominators(values: Iterable[Fraction]) -> int:
    denominator = 1
    for value in values:
        denominator = math.lcm(denominator, value.denominator)
    return denominator


def prepare_search(
    *,
    city_ids: Sequence[str],
    start_city_id: str,
    edge_options: Mapping[Tuple[str, str], Sequence[Any]],
    fatigue_budget: int,
    book_budget: int,
    negotiation_budget: int,
    all_plan: int,
    required_end_city_ids: Optional[Sequence[str]] = None,
) -> Optional[PreparedSearch]:
    """Compile exact edge costs into a normalized integer fatigue lattice."""

    ordered_cities = tuple(str(item) for item in city_ids)
    city_to_index = {city_id: index for index, city_id in enumerate(ordered_cities)}
    if str(start_city_id) not in city_to_index:
        raise ValueError(f"start city '{start_city_id}' is not in the planning city set")
    required_end_indices: Optional[Tuple[int, ...]] = None
    if required_end_city_ids is not None:
        normalized_end_ids = tuple(
            dict.fromkeys(str(city_id).strip() for city_id in required_end_city_ids if str(city_id).strip())
        )
        if not normalized_end_ids:
            raise ValueError("required_end_city_ids must contain at least one city")
        unknown_end_ids = [city_id for city_id in normalized_end_ids if city_id not in city_to_index]
        if unknown_end_ids:
            raise ValueError(
                "required end cities are outside the planning city set: "
                + ", ".join(unknown_end_ids)
            )
        required_end_indices = tuple(city_to_index[city_id] for city_id in normalized_end_ids)

    budget_fraction = Fraction(int(fatigue_budget), 1)
    feasible_options = []
    for (from_city, to_city), options in edge_options.items():
        if from_city not in city_to_index or to_city not in city_to_index:
            continue
        for option in options:
            fatigue = Fraction(option.expected_fatigue_cost)
            if fatigue <= 0 or fatigue > budget_fraction:
                continue
            if int(option.books_used) > int(book_budget):
                continue
            if all_plan == 0 and int(option.full_negotiation_used) > int(
                negotiation_budget
            ):
                continue
            profit = Fraction(option.expected_profit)
            if profit.denominator != 1:
                raise ValueError("edge expected profit must be an integer after game rounding")
            feasible_options.append((from_city, to_city, option, fatigue, int(profit)))

    if not feasible_options:
        return None

    denominator = _lcm_denominators(item[3] for item in feasible_options)
    raw_ticks = [
        fatigue.numerator * (denominator // fatigue.denominator)
        for _, _, _, fatigue, _ in feasible_options
    ]
    divisor = 0
    for ticks in raw_ticks:
        divisor = math.gcd(divisor, ticks)
    divisor = max(divisor, 1)
    budget_ticks = (int(fatigue_budget) * denominator) // divisor

    compiled = []
    for (from_city, to_city, option, _fatigue, profit), raw_tick in zip(
        feasible_options, raw_ticks
    ):
        if raw_tick % divisor:
            raise AssertionError("normalized fatigue tick is not integral")
        compiled.append(
            CompiledEdge(
                option=option,
                from_index=city_to_index[from_city],
                to_index=city_to_index[to_city],
                fatigue_ticks=raw_tick // divisor,
                books_used=int(option.books_used),
                full_negotiation_used=int(option.full_negotiation_used),
                profit=profit,
            )
        )

    min_cost = min(edge.fatigue_ticks for edge in compiled)
    max_legs = budget_ticks // min_cost
    return PreparedSearch(
        city_ids=ordered_cities,
        start_index=city_to_index[str(start_city_id)],
        required_end_indices=required_end_indices,
        edges=tuple(compiled),
        scale=FatigueScale(
            denominator=denominator,
            divisor=divisor,
            budget_ticks=budget_ticks,
        ),
        fatigue_budget=int(fatigue_budget),
        book_budget=int(book_budget),
        negotiation_budget=int(negotiation_budget),
        all_plan=int(all_plan),
        max_legs=max_legs,
    )


def estimate_dense_shape(prepared: PreparedSearch) -> Dict[str, int]:
    """Return conservative dense-backend allocation estimates."""

    negotiation_limit = (
        0
        if prepared.all_plan == 1
        else min(prepared.negotiation_budget, prepared.max_legs * 2)
    )
    max_cost = max(edge.fatigue_ticks for edge in prepared.edges)
    fatigue_layers = prepared.scale.budget_ticks + max_cost + 1
    state_cells = (
        fatigue_layers
        * len(prepared.city_ids)
        * (prepared.book_budget + 1)
        * (negotiation_limit + 1)
    )
    expanded_rows = 0
    for edge in prepared.edges:
        book_rows = prepared.book_budget - edge.books_used + 1
        if prepared.all_plan == 1:
            expanded_rows += book_rows
        else:
            negotiation_rows = (
                negotiation_limit - edge.full_negotiation_used + 1
            )
            if negotiation_rows > 0:
                expanded_rows += book_rows * negotiation_rows
    # One score matrix plus the expanded index/value arrays, sorting indices,
    # and temporary reduction buffers.
    estimated_bytes = state_cells * 8 + expanded_rows * 96
    return {
        "negotiation_limit": negotiation_limit,
        "max_cost": max_cost,
        "fatigue_layers": fatigue_layers,
        "state_cells": state_cells,
        "expanded_rows": expanded_rows,
        "estimated_bytes": estimated_bytes,
    }


def solve_prepared_search(
    prepared: PreparedSearch,
    *,
    backend: Optional[str] = None,
) -> Optional[ExactSearchResult]:
    """Choose one exact execution backend without changing the model."""

    requested = str(backend or "auto").strip().lower()
    if requested not in {"auto", "dense", "sparse"}:
        raise ValueError("solver backend must be 'auto', 'dense', or 'sparse'")

    estimate = estimate_dense_shape(prepared)
    dense_safe = (
        prepared.scale.budget_ticks <= 50_000
        and prepared.max_legs <= 750
        and estimate["expanded_rows"] <= 2_000_000
        and estimate["estimated_bytes"] <= 256 * 1024 * 1024
    )
    if requested == "dense" and not dense_safe:
        raise ValueError(
            "forced dense solver exceeds its exact memory/transition safety limits"
        )

    if requested == "dense" or (requested == "auto" and dense_safe):
        from .resonance_pc_trade_solver_dense import solve_dense

        try:
            return solve_dense(prepared, estimate=estimate)
        except DenseBackendUnavailable:
            if requested == "dense":
                raise ValueError(
                    "forced dense solver cannot encode this input safely"
                ) from None

    from .resonance_pc_trade_solver_sparse import solve_sparse

    return solve_sparse(prepared)


__all__ = [
    "CompiledEdge",
    "DenseBackendUnavailable",
    "ExactSearchResult",
    "FatigueScale",
    "PreparedSearch",
    "SolverProgress",
    "check_solver_cancelled",
    "estimate_dense_shape",
    "prepare_search",
    "solve_prepared_search",
    "trade_solver_progress",
]
