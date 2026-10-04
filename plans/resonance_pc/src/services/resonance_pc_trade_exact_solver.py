"""Exact binary-to-cap route solver for Resonance PC trading.

The optimization model is deliberately explicit:

* market prices stay frozen for the complete route;
* every product consumes one cargo slot;
* goods bought on one edge are sold at that edge's destination;
* bargain/raise decisions are binary: do nothing or reach the 20% cap;
* a full negotiation pays an exact rational expected-fatigue cost;
* crew, events, cash balance, and future market movement are out of scope.

Every travel edge has positive fatigue, so the resource graph is finite even
when cities may repeat.  The solver uses exact labels and strict dominance;
it does not use beam search, top-k filtering, sampling, or heuristic pruning.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction
import heapq
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .resonance_pc_trade_candidate_model import EdgeBookCurve, TradeEdgeOption
from .resonance_pc_trade_objective_solver import solve_fixed, solve_target
from .resonance_pc_trade_solver_common import (
    check_solver_cancelled,
    prepare_search,
    solve_prepared_search,
    trade_solver_progress,
)


def _fraction(value: Any) -> Fraction:
    if isinstance(value, Fraction):
        return value
    if isinstance(value, bool):
        return Fraction(int(value), 1)
    if isinstance(value, int):
        return Fraction(value, 1)
    if isinstance(value, float):
        return Fraction(str(value))
    return Fraction(str(value).strip())


def js_round(value: Fraction) -> int:
    """Match JavaScript ``Math.round`` for exact rational values."""

    value = _fraction(value)
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def _as_integral(name: str, value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        normalized = _fraction(value)
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if normalized.denominator != 1:
        raise ValueError(f"{name} must be an integer")
    return int(normalized)


def _as_bounded_int(name: str, value: Any, *, minimum: int, maximum: Optional[int] = None) -> int:
    normalized = _as_integral(name, value)
    if normalized < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and normalized > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return normalized


def _as_non_negative_int(name: str, value: Any) -> int:
    return _as_bounded_int(name, value, minimum=0)


def _normalize_success_rates(name: str, values: Sequence[Any]) -> Tuple[int, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError(f"{name} must be a non-empty sequence")
    if not values:
        raise ValueError(f"{name} must be a non-empty sequence")
    return tuple(
        _as_bounded_int(f"{name}[{index}]", value, minimum=0, maximum=10_000)
        for index, value in enumerate(values)
    )


def expected_fatigue_to_cap(
    *,
    success_rates_bps: Sequence[Any],
    step_bps: Any,
    max_adjustment_bps: Any = 2000,
    attempt_fatigue: Any = 8,
) -> Optional[Fraction]:
    """Return exact expected fatigue to reach the adjustment cap.

    The probability sequence is indexed by the number of successes already
    achieved.  Failures keep the same stage, and the final sequence value is
    reused when more success stages are required.  ``None`` means a required
    stage has a zero success rate, so reaching the cap is not feasible in this
    idealized model.
    """

    rates = _normalize_success_rates("success_rates_bps", success_rates_bps)
    step = _as_bounded_int("step_bps", step_bps, minimum=1, maximum=2000)
    cap = _as_bounded_int("max_adjustment_bps", max_adjustment_bps, minimum=1, maximum=10_000)
    fatigue = _as_bounded_int("attempt_fatigue", attempt_fatigue, minimum=1)
    required_successes = (cap + step - 1) // step
    expected_attempts = Fraction(0, 1)
    for success_index in range(required_successes):
        success_bps = rates[min(success_index, len(rates) - 1)]
        if success_bps == 0:
            return None
        expected_attempts += Fraction(10_000, success_bps)
    return fatigue * expected_attempts


@dataclass(frozen=True)
class _NegotiationProfile:
    success_rates_bps: Tuple[int, ...]
    step_bps: int
    required_successes: int
    expected_fatigue: Optional[Fraction]





class ResonancePcExactTradeSolver:
    """Exact route solver for one frozen market snapshot."""

    def __init__(
        self,
        *,
        snapshot: Mapping[str, Any],
        fatigue_payload: Mapping[str, Any],
        buy_lot: Mapping[str, Mapping[str, Any]],
        trade_rules: Mapping[str, Any],
        allowed_city_ids: Sequence[str],
        unlockable_product_ids: Optional[Sequence[str]] = None,
    ) -> None:
        self.snapshot = dict(snapshot)
        self.products: Dict[str, Any] = dict(snapshot.get("products") or {})
        self.city_names: Dict[str, str] = {
            str(city_id): str(name)
            for city_id, name in dict(fatigue_payload.get("cities") or {}).items()
        }
        self.fatigue_costs: Dict[str, Dict[str, int]] = {
            str(from_city): {
                str(to_city): int(cost)
                for to_city, cost in dict(row or {}).items()
            }
            for from_city, row in dict(fatigue_payload.get("costs") or {}).items()
        }
        self.buy_lot: Dict[str, Dict[str, int]] = {
            str(city_id): {
                str(product_id): int(value)
                for product_id, value in dict(products or {}).items()
            }
            for city_id, products in dict(buy_lot or {}).items()
        }
        self.rules = dict(trade_rules)
        self.allowed_city_ids = tuple(dict.fromkeys(str(item) for item in allowed_city_ids))
        self.unlockable_product_ids = (
            None
            if unlockable_product_ids is None
            else {str(item) for item in unlockable_product_ids}
        )

    def solve(
        self, *,
        start_city_id: str,
        fatigue_budget: int,
        cargo_capacity: int,
        book_budget: Optional[int] = 0,
        book_profit_threshold: Any = 500000,
        trade_mode: str = "profit",
        book_policy: str = "profit",
        negotiation_policy: str = "auto",
        negotiation_budget: Optional[int] = None,
        required_end_city_ids: Optional[Sequence[str]] = None,
        fixed_route_city_ids: Optional[Sequence[str]] = None,
        reposition_to_route: bool = False,
        target_profit: Any = None,
        bargain_success_rates_bps: Optional[Sequence[Any]] = None,
        bargain_step_bps: Optional[Any] = None,
        raise_success_rates_bps: Optional[Sequence[Any]] = None,
        raise_step_bps: Optional[Any] = None,
        city_prestige: Optional[Mapping[str, Any]] = None,
        product_unlocks: Optional[Mapping[str, Any]] = None,
        _backend: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Plan one complete freight request without compatibility aliases."""
        mode = str(trade_mode).strip().lower()
        if mode not in {"profit", "quick", "fixed", "target"}:
            raise ValueError("trade_mode must be profit, quick, fixed, or target")
        policy = str(book_policy).strip().lower()
        negotiation = str(negotiation_policy).strip().lower()
        if mode == "quick":
            policy, negotiation = "fill", "required"
        if policy not in {"profit", "fill"}:
            raise ValueError("book_policy must be profit or fill")
        if negotiation not in {"auto", "required", "disabled"}:
            raise ValueError("negotiation_policy must be auto, required, or disabled")
        fatigue_limit = _as_non_negative_int("fatigue_budget", fatigue_budget)
        capacity = _as_bounded_int("cargo_capacity", cargo_capacity, minimum=1)
        books_limit = None if book_budget is None else _as_non_negative_int("book_budget", book_budget)
        negotiation_limit = (0 if negotiation_budget is None else
                             _as_non_negative_int("negotiation_budget", negotiation_budget))
        plan_mode = int(negotiation_budget is None)
        negotiation_cap = None if plan_mode else negotiation_limit
        threshold = _fraction(book_profit_threshold)
        if threshold < 0:
            raise ValueError("book_profit_threshold must be >= 0")
        start = str(start_city_id or "").strip()
        if not start or start not in self.fatigue_costs:
            raise ValueError(f"start_city_id '{start}' is not present in the fatigue graph")
        end_ids = None
        fixed_path = None
        actual_start = start
        reposition = []
        reposition_fatigue = 0
        if mode != "fixed":
            reposition_to_route = False
        if not isinstance(reposition_to_route, bool):
            raise ValueError("reposition_to_route must be a boolean")
        if mode == "fixed":
            if isinstance(fixed_route_city_ids, (str, bytes)) or not isinstance(fixed_route_city_ids, Sequence):
                raise ValueError("fixed_route_city_ids must be an ordered city sequence")
            fixed_path = tuple(str(city).strip() for city in fixed_route_city_ids)
            if len(fixed_path) < 2 or any(not city for city in fixed_path):
                raise ValueError("fixed_route_city_ids must contain at least two cities")
            if fixed_path[0] != start and not reposition_to_route:
                raise ValueError("fixed route must begin at the actual start city")
            if any(city not in self.fatigue_costs for city in fixed_path):
                raise ValueError("fixed route contains cities outside the fatigue graph")
            if any(a == b for a, b in zip(fixed_path, fixed_path[1:])):
                raise ValueError("fixed route may not contain adjacent identical cities")
            if any(self.fatigue_costs.get(a, {}).get(b, 0) <= 0 for a, b in zip(fixed_path, fixed_path[1:])):
                raise ValueError("fixed route movement fatigue must be strictly positive on every edge")
            if fixed_path[0] != start:
                reposition = self._plan_reposition(start, fixed_path[0])
                reposition_fatigue = sum(option.travel_fatigue for option in reposition)
                start = fixed_path[0]
            planning_cities = sorted(set(fixed_path), key=self._sort_key)
        else:
            planning_cities = sorted(set(self.allowed_city_ids) | {start}, key=self._sort_key)
            if any(city not in self.fatigue_costs for city in planning_cities):
                raise ValueError("planning cities must be present in the fatigue graph")
            if required_end_city_ids is not None:
                if isinstance(required_end_city_ids, (str, bytes)) or not isinstance(required_end_city_ids, Sequence):
                    raise ValueError("required_end_city_ids must be a city sequence")
                end_ids = tuple(dict.fromkeys(str(city).strip() for city in required_end_city_ids))
                if not end_ids or any(city not in planning_cities for city in end_ids):
                    raise ValueError("required end cities must be selected planning cities")
        target = None
        target_integer = None
        if mode == "target":
            if target_profit is None:
                raise ValueError("target_profit is required in target mode")
            target = _fraction(target_profit)
            if target <= 0:
                raise ValueError("target_profit must be greater than 0")
            target_integer = (target.numerator + target.denominator - 1) // target.denominator

        bargain = self._normalize_negotiation_profile(side="bargain",
                    success_rates_bps=bargain_success_rates_bps, step_bps=bargain_step_bps)
        raising = self._normalize_negotiation_profile(side="raise",
                    success_rates_bps=raise_success_rates_bps, step_bps=raise_step_bps)
        prestige = self._normalize_city_prestige(city_prestige)
        unlocked = self._normalize_product_unlocks(product_unlocks)
        warnings = []
        verification = str((self.rules.get("source") or {}).get("verification_status") or "")
        if verification and verification != "game_samples_validated":
            warnings.append("trade rule metadata still requires validation against game samples")
        for side, profile in (("bargain", bargain), ("raise", raising)):
            if profile.expected_fatigue is None:
                warnings.append(f"{side}_to_cap is unavailable because a required success-rate stage is 0 bps")
        assumptions = self._build_assumptions(all_plan=plan_mode, trade_level=20,
                                             bargain_profile=bargain, raise_profile=raising)
        assumptions.update({"book_threshold_comparison": "marginal > 0 and marginal >= threshold",
                            "book_policy": policy, "negotiation_policy": negotiation})
        route_fatigue_limit = max(fatigue_limit - reposition_fatigue, 0)
        requested_pairs = None if fixed_path is None else set(zip(fixed_path, fixed_path[1:]))
        curves = self._build_edge_curves(city_ids=planning_cities, cargo_capacity=capacity,
                    book_policy=policy, threshold=threshold, negotiation_policy=negotiation,
                    bargain_profile=bargain, raise_profile=raising, prestige_by_city=prestige,
                    unlocked_products=unlocked, requested_pairs=requested_pairs)

        # A provable route-wide bound, not an arbitrary replacement for infinity.
        internal_books = 0
        route_book_bound = 0
        if curves:
            rate = max(Fraction(curve.legal_limit, 1) / curve.baseline.expected_fatigue_cost
                       for curve in curves)
            route_book_bound = int(Fraction(route_fatigue_limit) * rate)
            if books_limit is not None:
                internal_books = min(books_limit, route_book_bound)
        unconstrained_books = books_limit is None or books_limit >= route_book_bound
        single_leg = bool(curves) and route_fatigue_limit < 2 * min(
            curve.baseline.expected_fatigue_cost for curve in curves)
        options = {}
        for curve in curves:
            check_solver_cancelled()
            limit = curve.legal_limit if books_limit is None else min(curve.legal_limit, internal_books)
            counts = (limit,) if unconstrained_books or single_leg else range(limit + 1)
            family = options.setdefault((curve.baseline.from_city_id, curve.baseline.to_city_id), [])
            for count in counts:
                if count % 256 == 0:
                    check_solver_cancelled()
                family.append(curve.option(count, budget=books_limit))
        # This pruning is solely for maximum-profit objectives and the fallback
        # reachable-profit diagnostic. Target search uses all original curves.
        options = {pair: tuple(family if mode == "fixed" else self._prune_edge_options(family, all_plan=plan_mode))
                   for pair, family in options.items()}
        prepared = prepare_search(city_ids=planning_cities, start_city_id=start,
                    edge_options=options, fatigue_budget=route_fatigue_limit, book_budget=internal_books,
                    negotiation_budget=negotiation_limit, all_plan=plan_mode,
                    required_end_city_ids=end_ids, unbounded_books=unconstrained_books or single_leg)
        result = None
        maximum_profit = Fraction(0)
        if prepared is not None and reposition_fatigue <= fatigue_limit:
            if mode == "fixed":
                result = solve_fixed(prepared, city_path=fixed_path, options=options)
            else:
                maximum = solve_prepared_search(prepared, backend=_backend)
                if maximum is not None:
                    maximum_profit = maximum.expected_profit
                if mode != "target":
                    result = maximum
                elif maximum is not None and maximum_profit >= target:
                    base_options = {}
                    for curve in curves:
                        base_options.setdefault((curve.baseline.from_city_id, curve.baseline.to_city_id), []).append(curve.baseline)
                    target_prepared = prepare_search(city_ids=planning_cities, start_city_id=start,
                                edge_options=base_options, fatigue_budget=fatigue_limit,
                                book_budget=internal_books, negotiation_budget=negotiation_limit,
                                all_plan=plan_mode, required_end_city_ids=end_ids,
                                unbounded_books=unconstrained_books)
                    result = solve_target(target_prepared, curves=curves,
                                          target=target_integer, incumbent=maximum)
        status = "ok" if result is not None and (mode == "fixed" or result.expected_profit > 0) else (
                 "fixed_route_infeasible" if mode == "fixed" else
                 "target_unreachable" if mode == "target" else "no_plan")
        actual_profit = Fraction(0) if status != "ok" else result.expected_profit
        route_fatigue_used = Fraction(0) if status != "ok" else result.expected_fatigue_used
        fatigue_used = route_fatigue_used + (reposition_fatigue if status == "ok" else 0)
        books_used = 0 if status != "ok" else result.books_used
        negotiations_used = 0 if status != "ok" else result.full_negotiation_used
        selected = () if status != "ok" else result.route
        path = (start,) if status != "ok" else result.city_path
        route = [self._serialize_option(option) for option in selected]
        for index, leg in enumerate(route):
            leg["route_leg_index"] = index
            if mode == "fixed":
                leg["round_index"] = index // (len(fixed_path) - 1)
                leg["round_leg_index"] = index % (len(fixed_path) - 1)
        increment = sum((option.book_incremental_profit for option in selected), Fraction(0))
        average = None if books_used == 0 else increment / books_used
        remaining = Fraction(fatigue_limit) - fatigue_used
        visits = []
        for index, city in enumerate(path):
            previous = None if index == 0 else route[index - 1]
            following = None if index == len(route) else route[index]
            visits.append({"visit_index": index, "city_id": city, "city_name": self._city_name(city),
                           "sell_intent": None if previous is None or not previous["buys"] else {
                               "source_leg_index": index - 1, "raise_to_cap": previous["raise_to_cap"]},
                           "buy_intent": None if following is None or not following["buys"] else {
                               "source_leg_index": index, "books_used": following["books_used"],
                               "buy_products": following["buy_products"], "buys": following["buys"],
                               "bargain_to_cap": following["bargain_to_cap"]}})
        request = {"trade_mode": mode, "fatigue_budget": fatigue_limit, "cargo_capacity": capacity,
                   "start_city_id": actual_start, "route_start_city_id": start,
                   "book_budget": books_limit, "book_profit_threshold_exact": self._fraction_text(threshold),
                   "book_policy": policy, "negotiation_policy": negotiation,
                   "negotiation_budget": negotiation_cap,
                   "available_city_ids": planning_cities, "required_end_city_ids": list(end_ids or []),
                   "bargain_success_rates_bps": list(bargain.success_rates_bps),
                   "bargain_step_bps": bargain.step_bps,
                   "raise_success_rates_bps": list(raising.success_rates_bps), "raise_step_bps": raising.step_bps,
                   "city_prestige": dict(prestige),
                   "product_unlocks": None if unlocked is None else sorted(unlocked, key=self._sort_key)}
        if mode == "fixed":
            request.update(fixed_route_city_ids=list(fixed_path), reposition_to_route=reposition_to_route)
        if mode == "target":
            request["target_profit_exact"] = self._fraction_text(target)
        response = {
            "status": status, "reason": None if status == "ok" else status,
            "trade_mode": mode, "book_policy": policy, "negotiation_policy": negotiation,
            "request": request, "snapshot_id": self.snapshot.get("snapshot_id"),
            "required_end_city_ids": list(end_ids or []), "start_city_id": actual_start,
            "route_start_city_id": start,
            "route_fatigue_budget": route_fatigue_limit,
            "selected_end_city_id": path[-1] if status == "ok" else None,
            "selected_end_city_name": self._city_name(path[-1]) if status == "ok" else None,
            "expected_profit": self._fraction_number(actual_profit),
            "expected_profit_exact": self._fraction_text(actual_profit),
            "fatigue_budget": fatigue_limit, "expected_fatigue_used": float(fatigue_used),
            "expected_fatigue_used_exact": self._fraction_text(fatigue_used),
            "remaining_expected_fatigue": float(remaining),
            "remaining_expected_fatigue_exact": self._fraction_text(remaining),
            "book_budget": books_limit, "books_budget": books_limit, "books_used": books_used,
            "remaining_books": None if books_limit is None else books_limit - books_used,
            "book_profit_threshold": self._fraction_number(threshold),
            "book_incremental_profit": self._fraction_number(increment),
            "book_incremental_profit_exact": self._fraction_text(increment),
            "average_book_profit": None if average is None else self._fraction_number(average),
            "average_book_profit_exact": None if average is None else self._fraction_text(average),
            "negotiation_budget": negotiation_cap, "negotiation_budget_ignored": plan_mode == 1,
            "full_negotiation_used": negotiations_used,
            "full_bargain_count": sum(int(option.bargain_to_cap) for option in selected),
            "full_raise_count": sum(int(option.raise_to_cap) for option in selected),
            "remaining_negotiation": None if plan_mode else negotiation_limit - negotiations_used,
            "city_path": [self._city_name(city) for city in path], "city_path_ids": list(path),
            "city_visits": visits, "route": route, "assumptions": assumptions, "warnings": warnings,
            "diagnostics": {}, "solver_backend": None if result is None else result.backend,
            "solver_stats": {} if result is None else dict(result.stats),
        }
        if mode == "target":
            response.update(target_profit=self._fraction_number(target),
                            target_profit_exact=self._fraction_text(target), target_reached=status == "ok",
                            target_gap=self._fraction_number(max(target - (actual_profit if status == "ok" else maximum_profit), Fraction(0))),
                            target_gap_exact=self._fraction_text(max(target - (actual_profit if status == "ok" else maximum_profit), Fraction(0))),
                            maximum_reachable_profit=self._fraction_number(maximum_profit),
                            maximum_reachable_profit_exact=self._fraction_text(maximum_profit))
            if status != "ok":
                response["diagnostics"] = {"code": "target_unreachable",
                                          "maximum_reachable_profit_exact": self._fraction_text(maximum_profit),
                                          "execution_allowed": False}
        if mode == "fixed":
            completed, partial = divmod(len(route), len(fixed_path) - 1)
            closed = fixed_path[0] == fixed_path[-1]
            response.update(fixed_route_city_ids=list(fixed_path), fixed_route_closed=closed,
                            completed_circuits=completed if closed else 0,
                            partial_circuit_legs=partial if closed else 0,
                            partial_circuit_city_path_ids=list(path[-partial - 1:]) if closed and partial else [],
                            termination_route_position=(len(route) % (len(fixed_path) - 1)) if closed else len(route),
                            termination_city_id=path[-1] if status == "ok" else None,
                            stop_reason=("open_route_completed" if not closed and len(route) == len(fixed_path) - 1
                                         else "fatigue_budget" if status == "ok" else "fixed_route_infeasible"),
                            reposition_to_route=reposition_to_route,
                            reposition_route=[dict(self._serialize_option(option), leg_type="reposition")
                                              for option in reposition] if status == "ok" else [],
                            reposition_city_path_ids=([actual_start] + [option.to_city_id for option in reposition])
                                                     if status == "ok" else [actual_start],
                            reposition_expected_fatigue=reposition_fatigue if status == "ok" else 0,
                            reposition_expected_fatigue_exact=str(reposition_fatigue if status == "ok" else 0),
                            route_expected_fatigue=float(route_fatigue_used),
                            route_expected_fatigue_exact=self._fraction_text(route_fatigue_used),
                            total_expected_fatigue=float(fatigue_used),
                            total_expected_fatigue_exact=self._fraction_text(fatigue_used))
            if status != "ok":
                response["diagnostics"] = {"code": "fixed_route_infeasible", "execution_allowed": False,
                                          "minimum_first_leg_fatigue": self.fatigue_costs[fixed_path[0]][fixed_path[1]],
                                          "reposition_required_fatigue": reposition_fatigue,
                                          "route_fatigue_budget": route_fatigue_limit}
        return response

    def _plan_reposition(self, start: str, destination: str) -> List[TradeEdgeOption]:
        """Shortest positive-cost navigation, with deterministic full-path ties."""
        heap = [(0, 0, (start,), start)]
        best = {start: (0, 0, (start,))}
        while heap:
            fatigue, depth, path, city = heapq.heappop(heap)
            check_solver_cancelled()
            if best.get(city) != (fatigue, depth, path):
                continue
            if city == destination:
                return [TradeEdgeOption(a, b, 0, False, False, self.fatigue_costs[a][b],
                                        Fraction(0), Fraction(0), Fraction(0), (), (), ())
                        for a, b in zip(path, path[1:])]
            for next_city, cost in self.fatigue_costs.get(city, {}).items():
                if cost <= 0 or next_city not in self.fatigue_costs:
                    continue
                key = (fatigue + cost, depth + 1, path + (next_city,))
                if next_city not in best or key < best[next_city]:
                    best[next_city] = key
                    heapq.heappush(heap, (*key, next_city))
        raise ValueError("fixed route start is unreachable for reposition navigation")

    def _build_edge_curves(
        self, *, city_ids, cargo_capacity, book_policy, threshold, negotiation_policy,
        bargain_profile, raise_profile, prestige_by_city, unlocked_products, requested_pairs=None,
    ):
        profiles = {"disabled": ((False, False),), "required": ((True, True),),
                    "auto": ((False, False), (False, True), (True, False), (True, True))}[negotiation_policy]
        curves = []
        for from_city in city_ids:
            check_solver_cancelled()
            for to_city in city_ids:
                if from_city == to_city or (requested_pairs is not None and (from_city, to_city) not in requested_pairs):
                    continue
                travel = self.fatigue_costs.get(from_city, {}).get(to_city, 0)
                if travel <= 0:
                    continue
                navigation = TradeEdgeOption(from_city, to_city, 0, False, False, travel,
                                             Fraction(0), Fraction(0), Fraction(0), (), (), (),
                                             cargo_capacity=cargo_capacity)
                navigation_index = len(curves)
                has_trade_candidates = False
                curves.append(EdgeBookCurve.create(baseline=navigation, candidates=(),
                              capacity=cargo_capacity, policy=book_policy, threshold=threshold))
                for bargain, raising in profiles:
                    if ((bargain and bargain_profile.expected_fatigue is None)
                            or (raising and raise_profile.expected_fatigue is None)):
                        continue
                    candidates = self._prepare_edge_candidates(from_city=from_city, to_city=to_city,
                                 bargain_to_cap=bargain, raise_to_cap=raising,
                                 prestige_by_city=prestige_by_city, unlocked_products=unlocked_products)
                    if not candidates:
                        continue
                    has_trade_candidates = True
                    baseline = self._build_edge_option_from_candidates(from_city=from_city, to_city=to_city,
                               books_used=0, bargain_to_cap=bargain, raise_to_cap=raising,
                               travel_fatigue=travel, expected_bargain_fatigue=bargain_profile.expected_fatigue if bargain else Fraction(0),
                               expected_raise_fatigue=raise_profile.expected_fatigue if raising else Fraction(0),
                               cargo_capacity=cargo_capacity, candidates=candidates)
                    baseline = replace(baseline, cargo_capacity=cargo_capacity,
                                       loaded_quantity=sum(row[2] for row in baseline.buys))
                    curves.append(EdgeBookCurve.create(baseline=baseline, candidates=candidates,
                                  capacity=cargo_capacity, policy=book_policy, threshold=threshold))
                if has_trade_candidates:
                    curves[navigation_index] = replace(curves[navigation_index], limit_reason="navigation_only")
        return curves

    def _normalize_negotiation_profile(
        self,
        *,
        side: str,
        success_rates_bps: Optional[Sequence[Any]],
        step_bps: Optional[Any],
    ) -> _NegotiationProfile:
        rules = dict(self.rules.get("negotiation") or {})
        defaults = dict(rules.get("defaults") or {})
        rates_key = f"{side}_success_rates_bps"
        step_key = f"{side}_step_bps"
        raw_rates = defaults.get(rates_key) if success_rates_bps is None else success_rates_bps
        raw_step = defaults.get(step_key) if step_bps is None else step_bps
        rates = _normalize_success_rates(rates_key, raw_rates)
        step = _as_bounded_int(step_key, raw_step, minimum=1, maximum=2000)
        cap = _as_bounded_int(
            "negotiation.max_adjustment_bps",
            rules.get("max_adjustment_bps", 2000),
            minimum=1,
            maximum=10_000,
        )
        fatigue = _as_bounded_int(
            "negotiation.attempt_fatigue",
            rules.get("attempt_fatigue", 8),
            minimum=1,
        )
        required_successes = (cap + step - 1) // step
        return _NegotiationProfile(
            success_rates_bps=rates,
            step_bps=step,
            required_successes=required_successes,
            expected_fatigue=expected_fatigue_to_cap(
                success_rates_bps=rates,
                step_bps=step,
                max_adjustment_bps=cap,
                attempt_fatigue=fatigue,
            ),
        )



    def _build_edge_option(
        self,
        *,
        from_city: str,
        to_city: str,
        books_used: int,
        bargain_to_cap: bool,
        raise_to_cap: bool,
        travel_fatigue: int,
        expected_bargain_fatigue: Fraction,
        expected_raise_fatigue: Fraction,
        cargo_capacity: int,
        prestige_by_city: Mapping[str, int],
        unlocked_products: Optional[set[str]],
    ) -> TradeEdgeOption:
        candidates = self._prepare_edge_candidates(
            from_city=from_city,
            to_city=to_city,
            bargain_to_cap=bargain_to_cap,
            raise_to_cap=raise_to_cap,
            prestige_by_city=prestige_by_city,
            unlocked_products=unlocked_products,
        )
        return self._build_edge_option_from_candidates(
            from_city=from_city,
            to_city=to_city,
            books_used=books_used,
            bargain_to_cap=bargain_to_cap,
            raise_to_cap=raise_to_cap,
            travel_fatigue=travel_fatigue,
            expected_bargain_fatigue=expected_bargain_fatigue,
            expected_raise_fatigue=expected_raise_fatigue,
            cargo_capacity=cargo_capacity,
            candidates=candidates,
        )

    def _prepare_edge_candidates(
        self,
        *,
        from_city: str,
        to_city: str,
        bargain_to_cap: bool,
        raise_to_cap: bool,
        prestige_by_city: Mapping[str, int],
        unlocked_products: Optional[set[str]],
    ) -> Tuple[Tuple[Fraction, str, str, int], ...]:
        from_prestige = prestige_by_city[from_city]
        to_prestige = prestige_by_city[to_city]
        buy_tax_bps = self._tax_bps(from_city, from_prestige)
        sell_tax_bps = self._tax_bps(to_city, to_prestige)
        extra_buy_bps = self._prestige_rule(from_prestige)["extra_buy_bps"]
        max_adjustment_bps = int(
            (self.rules.get("negotiation") or {}).get("max_adjustment_bps", 2000)
        )

        candidates: List[Tuple[Fraction, str, str, int]] = []
        city_lots = self.buy_lot.get(from_city) or {}
        for product_id in sorted(city_lots, key=self._sort_key):
            product_id = str(product_id)
            if unlocked_products is not None and product_id not in unlocked_products:
                continue
            base_lot = int(city_lots.get(product_id, 0))
            if base_lot <= 0:
                continue
            buy_price = self._market_price(product_id, "buy", from_city)
            sell_price = self._market_price(product_id, "sell", to_city)
            if buy_price is None or sell_price is None:
                continue
            buy_factor_bps = 10_000 - (max_adjustment_bps if bargain_to_cap else 0)
            sell_factor_bps = 10_000 + (max_adjustment_bps if raise_to_cap else 0)
            adjusted_buy = js_round(buy_price * Fraction(buy_factor_bps, 10_000))
            adjusted_sell = js_round(sell_price * Fraction(sell_factor_bps, 10_000))
            net_profit = (
                Fraction(adjusted_sell * (10_000 - sell_tax_bps), 10_000)
                - Fraction(adjusted_buy * (10_000 + buy_tax_bps), 10_000)
            )
            expected_unit_profit = Fraction(js_round(net_profit), 1)
            if expected_unit_profit <= 0:
                continue
            prestige_lot = js_round(Fraction(base_lot * (10_000 + extra_buy_bps), 10_000))
            if prestige_lot <= 0:
                continue
            product_name = self._product_name(product_id)
            candidates.append(
                (
                    expected_unit_profit,
                    product_id,
                    product_name,
                    prestige_lot,
                )
            )

        candidates.sort(key=lambda row: (-row[0], self._sort_key(row[1])))
        return tuple(candidates)

    @staticmethod
    def _build_edge_option_from_candidates(
        *,
        from_city: str,
        to_city: str,
        books_used: int,
        bargain_to_cap: bool,
        raise_to_cap: bool,
        travel_fatigue: int,
        expected_bargain_fatigue: Fraction,
        expected_raise_fatigue: Fraction,
        cargo_capacity: int,
        candidates: Sequence[Tuple[Fraction, str, str, int]],
    ) -> TradeEdgeOption:
        free_capacity = int(cargo_capacity)
        expected_profit = Fraction(0, 1)
        buys: List[Tuple[str, str, int, Fraction]] = []
        quantity_multiplier = int(books_used) + 1
        for unit_profit, product_id, product_name, prestige_lot in candidates:
            if free_capacity <= 0:
                break
            max_quantity = prestige_lot * quantity_multiplier
            quantity = min(int(max_quantity), free_capacity)
            if quantity <= 0:
                continue
            buys.append((product_id, product_name, quantity, unit_profit))
            expected_profit += unit_profit * quantity
            free_capacity -= quantity

        return TradeEdgeOption(
            from_city_id=from_city,
            to_city_id=to_city,
            books_used=int(books_used),
            bargain_to_cap=bool(bargain_to_cap),
            raise_to_cap=bool(raise_to_cap),
            travel_fatigue=int(travel_fatigue),
            expected_bargain_fatigue=expected_bargain_fatigue,
            expected_raise_fatigue=expected_raise_fatigue,
            expected_profit=expected_profit,
            buy_product_ids=tuple(item[0] for item in buys),
            buy_product_names=tuple(item[1] for item in buys),
            buys=tuple(buys),
        )

    def _prune_edge_options(
        self, options: Sequence[TradeEdgeOption], *, all_plan: int
    ) -> List[TradeEdgeOption]:
        kept: List[TradeEdgeOption] = []
        for candidate in sorted(options, key=self._edge_sort_key):
            if any(
                self._edge_option_dominates(existing, candidate, all_plan=all_plan)
                for existing in kept
            ):
                continue
            kept = [
                existing
                for existing in kept
                if not self._edge_option_dominates(candidate, existing, all_plan=all_plan)
            ]
            kept.append(candidate)
        return kept

    @staticmethod
    def _edge_option_dominates(
        candidate: TradeEdgeOption, existing: TradeEdgeOption, *, all_plan: int
    ) -> bool:
        if candidate.books_used > existing.books_used:
            return False
        if candidate.expected_fatigue_cost > existing.expected_fatigue_cost:
            return False
        if all_plan == 0 and candidate.full_negotiation_used > existing.full_negotiation_used:
            return False
        if candidate.expected_profit < existing.expected_profit:
            return False
        strictly_better = (
            candidate.books_used < existing.books_used
            or candidate.expected_fatigue_cost < existing.expected_fatigue_cost
            or candidate.expected_profit > existing.expected_profit
            or (
                all_plan == 0
                and candidate.full_negotiation_used < existing.full_negotiation_used
            )
        )
        if strictly_better:
            return True
        return (
            candidate.full_negotiation_used,
            candidate.stable_signature,
        ) <= (
            existing.full_negotiation_used,
            existing.stable_signature,
        )

    def _normalize_city_prestige(self, payload: Optional[Mapping[str, Any]]) -> Dict[str, int]:
        raw = dict(payload or {})
        default_level = _as_integral("city_prestige.default", raw.get("default", 20))
        overrides = dict(raw.get("overrides") or {})
        levels = dict(self.rules.get("prestige_levels") or {})
        if str(default_level) not in levels:
            raise ValueError("city_prestige.default must be between 1 and 20")
        result: Dict[str, int] = {}
        for city_id in set(self.allowed_city_ids) | set(self.fatigue_costs):
            value = _as_integral(
                f"city prestige for '{city_id}'",
                overrides.get(str(city_id), default_level),
            )
            if str(value) not in levels:
                raise ValueError(f"city prestige for '{city_id}' must be between 1 and 20")
            result[str(city_id)] = value
        unknown = sorted(set(str(key) for key in overrides) - set(result), key=self._sort_key)
        if unknown:
            raise ValueError(f"city_prestige.overrides contains unknown city ids: {unknown}")
        return result

    def _normalize_product_unlocks(self, payload: Optional[Mapping[str, Any]]) -> Optional[set[str]]:
        raw = dict(payload or {})
        mode = str(raw.get("mode") or "all").strip().lower()
        product_ids = {
            str(item).strip()
            for item in (raw.get("product_ids") or [])
            if str(item).strip()
        }
        if mode == "all":
            return None
        if mode == "only":
            unknown = sorted(product_ids - set(self.products), key=self._sort_key)
            if unknown:
                raise ValueError(f"product_unlocks contains unknown product ids: {unknown}")
            if self.unlockable_product_ids is None:
                return product_ids
            always_available = set(self.products) - self.unlockable_product_ids
            return always_available | (product_ids & self.unlockable_product_ids)
        raise ValueError("product_unlocks.mode must be 'all' or 'only'")

    def _prestige_rule(self, level: int) -> Dict[str, int]:
        payload = (self.rules.get("prestige_levels") or {}).get(str(level))
        if not isinstance(payload, dict):
            raise ValueError(f"prestige rule for level {level} is missing")
        return {
            "general_tax_bps": int(payload.get("general_tax_bps", 0)),
            "extra_buy_bps": int(payload.get("extra_buy_bps", 0)),
        }

    def _tax_bps(self, city_id: str, prestige_level: int) -> int:
        prestige_rule = self._prestige_rule(prestige_level)
        tax_bps = int(prestige_rule["general_tax_bps"])
        tax_rules = dict(self.rules.get("tax") or {})
        special_city_ids = {str(item) for item in list(tax_rules.get("special_city_ids") or [])}
        if str(city_id) in special_city_ids:
            tax_bps += int(tax_rules.get("special_city_delta_bps", 0))
        return max(tax_bps, 0)

    def _market_price(self, product_id: str, side: str, city_id: str) -> Optional[Fraction]:
        product = self.products.get(str(product_id))
        if not isinstance(product, dict):
            return None
        market = product.get("market") or {}
        quote = (market.get(str(side)) or {}).get(str(city_id))
        if not isinstance(quote, dict) or quote.get("price") is None:
            return None
        try:
            price = _fraction(quote.get("price"))
        except (ValueError, ZeroDivisionError):
            return None
        return price if price > 0 else None

    def _product_name(self, product_id: str) -> str:
        product = self.products.get(str(product_id))
        if isinstance(product, dict) and str(product.get("name") or "").strip():
            return str(product.get("name")).strip()
        return f"unknown_{product_id}"

    def _city_name(self, city_id: str) -> str:
        return str(self.city_names.get(str(city_id)) or city_id)

    def _build_assumptions(
        self,
        *,
        all_plan: int,
        trade_level: int,
        bargain_profile: _NegotiationProfile,
        raise_profile: _NegotiationProfile,
    ) -> Dict[str, Any]:
        negotiation_rules = dict(self.rules.get("negotiation") or {})
        return {
            "rule_schema_version": self.rules.get("schema_version"),
            "rule_model_version": self.rules.get("model_version"),
            "rounding_mode": (self.rules.get("rounding") or {}).get("mode"),
            "market_snapshot_frozen": True,
            "crew_effects_included": False,
            "active_events_included": False,
            "cash_constraint_included": False,
            "unit_cargo_size": True,
            "repeat_city_purchase_available": True,
            "negotiation_model": negotiation_rules.get("model"),
            "negotiation_cap_bps": int(negotiation_rules.get("max_adjustment_bps", 2000)),
            "negotiation_attempt_fatigue": int(negotiation_rules.get("attempt_fatigue", 8)),
            "negotiation_attempt_limit_included": False,
            "negotiation_profit_assumes_cap_reached": True,
            "expected_fatigue_is_hard_budget_cost": True,
            "trade_level": trade_level,
            "trade_level_affects_negotiation": False,
            "all_plan": all_plan,
            "bargain_profile": self._serialize_profile(bargain_profile),
            "raise_profile": self._serialize_profile(raise_profile),
            "tax_applied_to_buy_and_sell_amounts": True,
        }



    def _serialize_option(self, option: TradeEdgeOption) -> Dict[str, Any]:
        expected_negotiation_fatigue = option.expected_negotiation_fatigue
        expected_fatigue_cost = option.expected_fatigue_cost
        return {
            "leg_type": "trade" if option.buys else "navigation",
            "cargo_capacity": option.cargo_capacity,
            "loaded_quantity": option.loaded_quantity,
            "is_full_load": option.cargo_capacity > 0 and option.loaded_quantity == option.cargo_capacity,
            "load_ratio": option.loaded_quantity / option.cargo_capacity if option.cargo_capacity else 0,
            "book_stop_reason": option.book_stop_reason,
            "legal_book_limit": option.legal_book_limit,
            "next_book_marginal_profit_exact": self._fraction_text(option.next_book_marginal_profit),
            "from_city": self._city_name(option.from_city_id),
            "to_city": self._city_name(option.to_city_id),
            "from_city_id": option.from_city_id,
            "to_city_id": option.to_city_id,
            "buy_products": list(option.buy_product_names),
            "buy_product_ids": list(option.buy_product_ids),
            "buys": [
                {
                    "product_id": product_id,
                    "product_name": product_name,
                    "quantity": int(quantity),
                    "expected_unit_profit": float(unit_profit),
                    "expected_unit_profit_exact": self._fraction_text(unit_profit),
                }
                for product_id, product_name, quantity, unit_profit in option.buys
            ],
            "books_used": int(option.books_used),
            "bargain_to_cap": bool(option.bargain_to_cap),
            "raise_to_cap": bool(option.raise_to_cap),
            "full_negotiation_used": int(option.full_negotiation_used),
            "travel_fatigue": int(option.travel_fatigue),
            "expected_bargain_fatigue": float(option.expected_bargain_fatigue),
            "expected_bargain_fatigue_exact": self._fraction_text(
                option.expected_bargain_fatigue
            ),
            "expected_raise_fatigue": float(option.expected_raise_fatigue),
            "expected_raise_fatigue_exact": self._fraction_text(option.expected_raise_fatigue),
            "expected_negotiation_fatigue": float(expected_negotiation_fatigue),
            "expected_negotiation_fatigue_exact": self._fraction_text(
                expected_negotiation_fatigue
            ),
            "expected_fatigue_cost": float(expected_fatigue_cost),
            "expected_fatigue_cost_exact": self._fraction_text(expected_fatigue_cost),
            "expected_profit": float(option.expected_profit),
            "expected_profit_exact": self._fraction_text(option.expected_profit),
            "book_incremental_profit": self._fraction_number(
                option.book_incremental_profit
            ),
            "book_incremental_profit_exact": self._fraction_text(
                option.book_incremental_profit
            ),
        }

    @classmethod
    def _serialize_profile(cls, profile: _NegotiationProfile) -> Dict[str, Any]:
        return {
            "success_rates_bps": list(profile.success_rates_bps),
            "step_bps": profile.step_bps,
            "required_successes": profile.required_successes,
            "expected_fatigue": (
                None if profile.expected_fatigue is None else float(profile.expected_fatigue)
            ),
            "expected_fatigue_exact": (
                None
                if profile.expected_fatigue is None
                else cls._fraction_text(profile.expected_fatigue)
            ),
        }

    @staticmethod
    def _fraction_text(value: Fraction) -> str:
        return str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"

    @staticmethod
    def _fraction_number(value: Fraction) -> Any:
        normalized = Fraction(value)
        if normalized.denominator == 1:
            return int(normalized)
        return float(normalized)

    @staticmethod
    def _sort_key(value: Any) -> Tuple[int, Any]:
        text = str(value)
        try:
            return (0, int(text))
        except (TypeError, ValueError):
            return (1, text)

    @classmethod
    def _edge_sort_key(cls, option: TradeEdgeOption) -> Tuple[Any, ...]:
        return (
            option.expected_fatigue_cost,
            option.books_used,
            option.full_negotiation_used,
            -option.expected_profit,
            option.stable_signature,
        )

__all__ = [
    "ResonancePcExactTradeSolver",
    "TradeEdgeOption",
    "expected_fatigue_to_cap",
    "js_round",
    "trade_solver_progress",
]
