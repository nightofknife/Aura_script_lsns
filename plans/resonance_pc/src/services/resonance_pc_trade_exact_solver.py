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

import heapq
import itertools
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


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

    @property
    def full_negotiation_used(self) -> int:
        return int(self.bargain_to_cap) + int(self.raise_to_cap)

    @property
    def expected_negotiation_fatigue(self) -> Fraction:
        return self.expected_bargain_fatigue + self.expected_raise_fatigue

    @property
    def expected_fatigue_cost(self) -> Fraction:
        return Fraction(self.travel_fatigue, 1) + self.expected_negotiation_fatigue

    @property
    def stable_signature(self) -> Tuple[Any, ...]:
        return (
            self.from_city_id,
            self.to_city_id,
            self.books_used,
            int(self.bargain_to_cap),
            int(self.raise_to_cap),
            self.buy_product_ids,
        )


@dataclass(frozen=True)
class _Label:
    city_id: str
    expected_fatigue_used: Fraction
    books_used: int
    full_negotiation_used: int
    expected_profit: Fraction
    city_path: Tuple[str, ...]
    route: Tuple[TradeEdgeOption, ...]


class ResonancePcExactTradeSolver:
    """Exact label solver for one frozen market snapshot."""

    def __init__(
        self,
        *,
        snapshot: Mapping[str, Any],
        fatigue_payload: Mapping[str, Any],
        buy_lot: Mapping[str, Mapping[str, Any]],
        trade_rules: Mapping[str, Any],
        allowed_city_ids: Sequence[str],
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

    def solve(
        self,
        *,
        start_city_id: str,
        fatigue_budget: int,
        cargo_capacity: int,
        book_budget: int,
        book_profit_threshold: Any,
        negotiation_budget: int,
        all_plan: int = 0,
        bargain_success_rates_bps: Optional[Sequence[Any]] = None,
        bargain_step_bps: Optional[Any] = None,
        raise_success_rates_bps: Optional[Sequence[Any]] = None,
        raise_step_bps: Optional[Any] = None,
        trade_level: int = 20,
        city_prestige: Optional[Mapping[str, Any]] = None,
        product_unlocks: Optional[Mapping[str, Any]] = None,
        active_events: Optional[Sequence[Any]] = None,
    ) -> Dict[str, Any]:
        fatigue_limit = _as_non_negative_int("fatigue_budget", fatigue_budget)
        capacity = _as_non_negative_int("cargo_capacity", cargo_capacity)
        if capacity <= 0:
            raise ValueError("cargo_capacity must be greater than 0")
        books_limit = _as_non_negative_int("book_budget", book_budget)
        negotiation_limit = _as_non_negative_int("negotiation_budget", negotiation_budget)
        plan_mode = _as_bounded_int("all_plan", all_plan, minimum=0, maximum=1)
        threshold = _fraction(book_profit_threshold)
        if threshold < 0:
            raise ValueError("book_profit_threshold must be >= 0")

        trade_level_rules = dict(self.rules.get("trade_level") or {})
        min_trade_level = int(trade_level_rules.get("min", 1))
        max_trade_level = int(trade_level_rules.get("max", 20))
        level = _as_bounded_int(
            "trade_level", trade_level, minimum=min_trade_level, maximum=max_trade_level
        )

        start = str(start_city_id or "").strip()
        if not start or start not in self.fatigue_costs:
            raise ValueError(f"start_city_id '{start}' is not present in the fatigue graph")

        bargain_profile = self._normalize_negotiation_profile(
            side="bargain",
            success_rates_bps=bargain_success_rates_bps,
            step_bps=bargain_step_bps,
        )
        raise_profile = self._normalize_negotiation_profile(
            side="raise",
            success_rates_bps=raise_success_rates_bps,
            step_bps=raise_step_bps,
        )
        prestige_by_city = self._normalize_city_prestige(city_prestige)
        unlocked_products = self._normalize_product_unlocks(product_unlocks)
        events = list(active_events or [])
        warnings: List[str] = []
        source_metadata = dict(self.rules.get("source") or {})
        verification_status = str(source_metadata.get("verification_status") or "").strip()
        if verification_status and verification_status != "game_samples_validated":
            warnings.append(
                "trade rule metadata is versioned but still requires validation against game samples"
            )
        if events:
            warnings.append("active_events is accepted but ignored by this planner version")
        if bargain_profile.expected_fatigue is None:
            warnings.append(
                "bargain_to_cap is unavailable because a required success-rate stage is 0 bps"
            )
        if raise_profile.expected_fatigue is None:
            warnings.append(
                "raise_to_cap is unavailable because a required success-rate stage is 0 bps"
            )

        planning_cities = list(self.allowed_city_ids)
        if start not in planning_cities:
            planning_cities.append(start)
        planning_cities = sorted(
            {city for city in planning_cities if city in self.fatigue_costs},
            key=self._sort_key,
        )

        edge_options = self._build_edge_options(
            city_ids=planning_cities,
            cargo_capacity=capacity,
            book_budget=books_limit,
            book_profit_threshold=threshold,
            negotiation_budget=negotiation_limit,
            all_plan=plan_mode,
            bargain_profile=bargain_profile,
            raise_profile=raise_profile,
            prestige_by_city=prestige_by_city,
            unlocked_products=unlocked_products,
        )

        initial = _Label(
            city_id=start,
            expected_fatigue_used=Fraction(0, 1),
            books_used=0,
            full_negotiation_used=0,
            expected_profit=Fraction(0, 1),
            city_path=(start,),
            route=(),
        )
        frontiers: Dict[str, List[_Label]] = {start: [initial]}
        active_label_ids = {id(initial)}
        queue_counter = itertools.count()
        queue: List[Tuple[Fraction, int, int, Tuple[int, Any], int, _Label]] = []
        heapq.heappush(
            queue,
            (
                initial.expected_fatigue_used,
                initial.books_used,
                initial.full_negotiation_used,
                self._sort_key(initial.city_id),
                next(queue_counter),
                initial,
            ),
        )
        best: Optional[_Label] = None
        fatigue_limit_fraction = Fraction(fatigue_limit, 1)

        while queue:
            *_, label = heapq.heappop(queue)
            if id(label) not in active_label_ids:
                continue
            if label.route and label.expected_profit > 0:
                if best is None or self._is_better_final(label, best):
                    best = label
            for to_city in planning_cities:
                if to_city == label.city_id:
                    continue
                for option in edge_options.get((label.city_id, to_city), ()):
                    next_fatigue = label.expected_fatigue_used + option.expected_fatigue_cost
                    next_books = label.books_used + option.books_used
                    next_negotiation = (
                        label.full_negotiation_used + option.full_negotiation_used
                    )
                    if next_fatigue > fatigue_limit_fraction or next_books > books_limit:
                        continue
                    if plan_mode == 0 and next_negotiation > negotiation_limit:
                        continue
                    candidate = _Label(
                        city_id=to_city,
                        expected_fatigue_used=next_fatigue,
                        books_used=next_books,
                        full_negotiation_used=next_negotiation,
                        expected_profit=label.expected_profit + option.expected_profit,
                        city_path=label.city_path + (to_city,),
                        route=label.route + (option,),
                    )
                    if not self._accept_label(
                        candidate,
                        frontiers=frontiers,
                        active_label_ids=active_label_ids,
                        all_plan=plan_mode,
                    ):
                        continue
                    heapq.heappush(
                        queue,
                        (
                            candidate.expected_fatigue_used,
                            candidate.books_used,
                            candidate.full_negotiation_used,
                            self._sort_key(candidate.city_id),
                            next(queue_counter),
                            candidate,
                        ),
                    )

        assumptions = self._build_assumptions(
            all_plan=plan_mode,
            trade_level=level,
            bargain_profile=bargain_profile,
            raise_profile=raise_profile,
        )
        if best is None or best.expected_profit <= 0:
            return self._empty_result(
                start=start,
                fatigue_limit=fatigue_limit,
                books_limit=books_limit,
                negotiation_limit=negotiation_limit,
                all_plan=plan_mode,
                assumptions=assumptions,
                warnings=warnings,
            )

        route = [self._serialize_option(option) for option in best.route]
        remaining_fatigue = fatigue_limit_fraction - best.expected_fatigue_used
        full_bargain_count = sum(int(option.bargain_to_cap) for option in best.route)
        full_raise_count = sum(int(option.raise_to_cap) for option in best.route)
        return {
            "status": "ok",
            "reason": None,
            "snapshot_id": self.snapshot.get("snapshot_id"),
            "all_plan": plan_mode,
            "expected_profit": float(best.expected_profit),
            "expected_profit_exact": self._fraction_text(best.expected_profit),
            "fatigue_budget": fatigue_limit,
            "expected_fatigue_used": float(best.expected_fatigue_used),
            "expected_fatigue_used_exact": self._fraction_text(best.expected_fatigue_used),
            "remaining_expected_fatigue": float(remaining_fatigue),
            "remaining_expected_fatigue_exact": self._fraction_text(remaining_fatigue),
            "books_budget": books_limit,
            "books_used": int(best.books_used),
            "remaining_books": books_limit - int(best.books_used),
            "negotiation_budget": negotiation_limit,
            "negotiation_budget_ignored": plan_mode == 1,
            "full_negotiation_used": int(best.full_negotiation_used),
            "full_bargain_count": full_bargain_count,
            "full_raise_count": full_raise_count,
            "remaining_negotiation": (
                None
                if plan_mode == 1
                else negotiation_limit - int(best.full_negotiation_used)
            ),
            "city_path": [self._city_name(city_id) for city_id in best.city_path],
            "city_path_ids": list(best.city_path),
            "route": route,
            "assumptions": assumptions,
            "warnings": warnings,
        }

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

    def _build_edge_options(
        self,
        *,
        city_ids: Sequence[str],
        cargo_capacity: int,
        book_budget: int,
        book_profit_threshold: Fraction,
        negotiation_budget: int,
        all_plan: int,
        bargain_profile: _NegotiationProfile,
        raise_profile: _NegotiationProfile,
        prestige_by_city: Mapping[str, int],
        unlocked_products: Optional[set[str]],
    ) -> Dict[Tuple[str, str], Tuple[TradeEdgeOption, ...]]:
        table: Dict[Tuple[str, str], Tuple[TradeEdgeOption, ...]] = {}
        bargain_flags = [False]
        raise_flags = [False]
        if bargain_profile.expected_fatigue is not None and (all_plan == 1 or negotiation_budget > 0):
            bargain_flags.append(True)
        if raise_profile.expected_fatigue is not None and (all_plan == 1 or negotiation_budget > 0):
            raise_flags.append(True)

        for from_city in city_ids:
            row = self.fatigue_costs.get(from_city) or {}
            for to_city in city_ids:
                if from_city == to_city:
                    continue
                travel_fatigue = int(row.get(to_city, 0))
                if travel_fatigue <= 0:
                    continue
                options: List[TradeEdgeOption] = []
                for bargain_to_cap in bargain_flags:
                    for raise_to_cap in raise_flags:
                        full_used = int(bargain_to_cap) + int(raise_to_cap)
                        if all_plan == 0 and full_used > negotiation_budget:
                            continue
                        previous_profit: Optional[Fraction] = None
                        threshold_prefix_valid = True
                        for books_used in range(book_budget + 1):
                            option = self._build_edge_option(
                                from_city=from_city,
                                to_city=to_city,
                                books_used=books_used,
                                bargain_to_cap=bargain_to_cap,
                                raise_to_cap=raise_to_cap,
                                travel_fatigue=travel_fatigue,
                                expected_bargain_fatigue=(
                                    bargain_profile.expected_fatigue
                                    if bargain_to_cap
                                    else Fraction(0, 1)
                                ),
                                expected_raise_fatigue=(
                                    raise_profile.expected_fatigue
                                    if raise_to_cap
                                    else Fraction(0, 1)
                                ),
                                cargo_capacity=cargo_capacity,
                                prestige_by_city=prestige_by_city,
                                unlocked_products=unlocked_products,
                            )
                            if previous_profit is not None:
                                marginal = option.expected_profit - previous_profit
                                if marginal < book_profit_threshold:
                                    threshold_prefix_valid = False
                            previous_profit = option.expected_profit
                            if not threshold_prefix_valid:
                                continue
                            if books_used > 0 and option.expected_profit <= 0:
                                continue
                            options.append(option)

                table[(from_city, to_city)] = tuple(
                    sorted(
                        self._prune_edge_options(options, all_plan=all_plan),
                        key=self._edge_sort_key,
                    )
                )
        return table

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
            max_quantity = prestige_lot * (books_used + 1)
            if max_quantity <= 0:
                continue
            product_name = self._product_name(product_id)
            candidates.append((expected_unit_profit, product_id, product_name, max_quantity))

        candidates.sort(key=lambda row: (-row[0], self._sort_key(row[1])))
        free_capacity = int(cargo_capacity)
        expected_profit = Fraction(0, 1)
        buys: List[Tuple[str, str, int, Fraction]] = []
        for unit_profit, product_id, product_name, max_quantity in candidates:
            if free_capacity <= 0:
                break
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

    def _accept_label(
        self,
        candidate: _Label,
        *,
        frontiers: Dict[str, List[_Label]],
        active_label_ids: set[int],
        all_plan: int,
    ) -> bool:
        frontier = frontiers.setdefault(candidate.city_id, [])
        if any(self._label_dominates(existing, candidate, all_plan=all_plan) for existing in frontier):
            return False
        survivors: List[_Label] = []
        for existing in frontier:
            if self._label_dominates(candidate, existing, all_plan=all_plan):
                active_label_ids.discard(id(existing))
            else:
                survivors.append(existing)
        survivors.append(candidate)
        frontiers[candidate.city_id] = survivors
        active_label_ids.add(id(candidate))
        return True

    @classmethod
    def _label_dominates(cls, candidate: _Label, existing: _Label, *, all_plan: int) -> bool:
        if candidate.expected_fatigue_used > existing.expected_fatigue_used:
            return False
        if candidate.books_used > existing.books_used:
            return False
        if all_plan == 0 and candidate.full_negotiation_used > existing.full_negotiation_used:
            return False
        if candidate.expected_profit < existing.expected_profit:
            return False
        strictly_better = (
            candidate.expected_fatigue_used < existing.expected_fatigue_used
            or candidate.books_used < existing.books_used
            or candidate.expected_profit > existing.expected_profit
            or (
                all_plan == 0
                and candidate.full_negotiation_used < existing.full_negotiation_used
            )
        )
        if strictly_better:
            return True
        return cls._label_tie_signature(candidate) <= cls._label_tie_signature(existing)

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
            return product_ids
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

    def _empty_result(
        self,
        *,
        start: str,
        fatigue_limit: int,
        books_limit: int,
        negotiation_limit: int,
        all_plan: int,
        assumptions: Dict[str, Any],
        warnings: List[str],
    ) -> Dict[str, Any]:
        return {
            "status": "no_plan",
            "reason": "no_positive_profit_route",
            "snapshot_id": self.snapshot.get("snapshot_id"),
            "all_plan": all_plan,
            "expected_profit": 0.0,
            "expected_profit_exact": "0",
            "fatigue_budget": fatigue_limit,
            "expected_fatigue_used": 0.0,
            "expected_fatigue_used_exact": "0",
            "remaining_expected_fatigue": float(fatigue_limit),
            "remaining_expected_fatigue_exact": str(fatigue_limit),
            "books_budget": books_limit,
            "books_used": 0,
            "remaining_books": books_limit,
            "negotiation_budget": negotiation_limit,
            "negotiation_budget_ignored": all_plan == 1,
            "full_negotiation_used": 0,
            "full_bargain_count": 0,
            "full_raise_count": 0,
            "remaining_negotiation": None if all_plan == 1 else negotiation_limit,
            "city_path": [self._city_name(start)],
            "city_path_ids": [start],
            "route": [],
            "assumptions": assumptions,
            "warnings": warnings,
        }

    def _serialize_option(self, option: TradeEdgeOption) -> Dict[str, Any]:
        expected_negotiation_fatigue = option.expected_negotiation_fatigue
        expected_fatigue_cost = option.expected_fatigue_cost
        return {
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

    @classmethod
    def _label_tie_signature(cls, label: _Label) -> Tuple[Any, ...]:
        return (
            label.full_negotiation_used,
            len(label.route),
            label.city_path,
            tuple(option.stable_signature for option in label.route),
        )

    @classmethod
    def _is_better_final(cls, candidate: _Label, existing: _Label) -> bool:
        if candidate.expected_profit != existing.expected_profit:
            return candidate.expected_profit > existing.expected_profit
        return (
            candidate.expected_fatigue_used,
            candidate.books_used,
            candidate.full_negotiation_used,
            len(candidate.route),
            candidate.city_path,
            tuple(option.stable_signature for option in candidate.route),
        ) < (
            existing.expected_fatigue_used,
            existing.books_used,
            existing.full_negotiation_used,
            len(existing.route),
            existing.city_path,
            tuple(option.stable_signature for option in existing.route),
        )


__all__ = [
    "ResonancePcExactTradeSolver",
    "TradeEdgeOption",
    "expected_fatigue_to_cap",
    "js_round",
]
