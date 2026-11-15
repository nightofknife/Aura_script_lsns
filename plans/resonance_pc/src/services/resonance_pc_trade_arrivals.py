"""Minimum arrival requirements for the free freight route search."""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Mapping, Sequence

from .resonance_pc_trade_solver_common import check_solver_cancelled


def normalize_required_arrivals(value: object) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("required_arrivals must map city IDs to positive integer counts")
    result: dict[str, int] = {}
    for city, count in value.items():
        if not isinstance(city, str) or not city.strip() or city.strip() in result:
            raise ValueError("required_arrivals must use distinct nonempty city IDs")
        if type(count) is not int or count < 1:
            raise ValueError("required_arrivals counts must be positive integers")
        result[city.strip()] = count
    return dict(sorted(result.items()))


@dataclass(frozen=True)
class ArrivalSearch:
    initial: tuple[int, ...]
    complete: tuple[int, ...]
    slots: dict[str, int]
    remaining_movement: dict[tuple[str, tuple[int, ...]], int]

    def arrive(self, counts: tuple[int, ...], city: str) -> tuple[int, ...]:
        slot = self.slots.get(city)
        if slot is None or counts[slot] == 0:
            return counts
        return counts[:slot] + (counts[slot] - 1,) + counts[slot + 1:]


def prepare_arrivals(
    required: Mapping[str, int],
    *,
    cities: Sequence[str],
    costs: Mapping[str, Mapping[str, int]],
    start: str,
    ends: Sequence[str] | None,
    budget: int,
) -> tuple[ArrivalSearch | None, dict]:
    """Reverse shortest paths for the remaining arrival counts and legal end.

    Distances are in movement fatigue. They bound the full trade search, which
    can additionally spend fatigue on negotiation. Once met, a count stays at
    zero, so any number of extra arrivals remains legal.
    """
    missing = [city for city in required if city not in cities]
    if missing:
        return None, {"code": "arrival_city_not_selected", "city_ids": missing}
    incoming = {city: [(other, costs[other][city]) for other in cities
                       if other != city and costs.get(other, {}).get(city, 0) > 0]
                for city in cities}
    isolated = [city for city in required if not incoming[city]]
    if isolated:
        return None, {"code": "arrival_city_unreachable", "city_ids": isolated}
    initial = tuple(required.values())
    complete = (0,) * len(initial)
    slots = {city: index for index, city in enumerate(required)}
    cheapest = [min(cost for _, cost in incoming[city]) for city in required]
    total_min = sum(count * cost for count, cost in zip(initial, cheapest))
    if total_min > budget:
        return None, {"code": "arrival_movement_budget", "movement_lower_bound": total_min}
    # Search a little past the budget to report an exact minimum for a near
    # miss, without expanding arbitrarily long routes to diagnose failure.
    ceiling = budget + max((cost for edges in incoming.values() for _, cost in edges), default=0)
    best = {(city, complete): 0 for city in (ends or cities)}
    queue = [(0, city, complete, total_min) for city, _ in best]
    heapq.heapify(queue)
    distances: dict[tuple[str, tuple[int, ...]], int] = {}
    beyond_ceiling = False
    while queue:
        check_solver_cancelled()
        cost, city, counts, prefix_bound = heapq.heappop(queue)
        key = (city, counts)
        if cost != best[key]:
            continue
        distances[key] = cost
        if key == (start, initial) and cost > budget:
            return None, {"code": "arrival_movement_budget", "minimum_movement_fatigue": cost}
        slot = slots.get(city)
        previous = []
        if slot is None or counts[slot] == 0:
            previous.append((counts, prefix_bound))
        if slot is not None and counts[slot] < initial[slot]:
            previous.append((counts[:slot] + (counts[slot] + 1,) + counts[slot + 1:],
                             prefix_bound - cheapest[slot]))
        for other, step in incoming[city]:
            total = cost + step
            for previous_counts, bound in previous:
                prev = (other, previous_counts)
                if prev in best and best[prev] <= total:
                    continue
                if total + bound > ceiling:
                    beyond_ceiling = True
                    continue
                best[prev] = total
                heapq.heappush(queue, (total, other, previous_counts, bound))
    minimum = distances.get((start, initial))
    if minimum is None:
        return None, {"code": "arrival_movement_budget" if beyond_ceiling else "arrival_route_unreachable"}
    return ArrivalSearch(initial, complete, slots, distances), {"minimum_movement_fatigue": minimum}
