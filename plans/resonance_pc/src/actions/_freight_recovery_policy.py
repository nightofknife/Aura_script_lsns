"""Pure freight recovery planning; no cache, UI, solver or account-bonus access.

Routes are planner leg dictionaries, with string from/to_city_id fields.
Arrival indices are city-node indices 1..len(route); leg indices are zero-based.
Invalid inputs raise ValueError, while valid no-op plans have planned=False.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ._bento_consumption_policy import (
    MEAL_KINDS,
    meal_identity,
    resolve_meal,
    validate_inventory,
)


def _integer(value: Any, field: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field} must be a nonempty, unpadded string")
    return value


def _route(route: list[dict]) -> list[dict]:
    if not isinstance(route, list):
        raise ValueError("route must be a list")
    for index, leg in enumerate(route):
        if not isinstance(leg, Mapping):
            raise ValueError(f"route[{index}] must be an object")
        for side in ("from", "to"):
            _text(leg.get(f"{side}_city_id"), f"route[{index}].{side}_city_id")
            for suffix in ("city", "city_key"):
                field = f"{side}_{suffix}"
                if field in leg:
                    _text(leg[field], f"route[{index}].{field}")
        if index:
            previous = route[index - 1]
            if leg["from_city_id"] != previous["to_city_id"]:
                raise ValueError(f"route[{index}] does not continue the preceding arrival")
            if ("from_city_key" in leg and "to_city_key" in previous
                    and leg["from_city_key"] != previous["to_city_key"]):
                raise ValueError(f"route[{index}] has conflicting city keys")
    return route


def select_last_water_arrival(
    route: list[dict], rest_lookup: Callable[[str], bool],
) -> dict:
    """Select the last actual arrival with rest, never the initial departure.

    rest_lookup receives the display city name (ID if absent) and must return
    bool. The caller adapts configured rest points and handles known no-shop
    errors; unexpected lookup exceptions propagate. Empty routes need no lookup.
    The returned city_index is the arrival-node index, not a leg index.
    """
    route = _route(route)
    result = {
        "planned": False, "reason": "no_route" if not route else "no_eligible_rest_arrival",
        "city_index": None, "city_id": None, "city_key": None, "city_name": None,
    }
    if not route:
        return result
    if not callable(rest_lookup):
        raise ValueError("rest_lookup must be callable")
    for index in range(len(route) - 1, -1, -1):
        leg = route[index]
        city_id = leg["to_city_id"]
        name = leg.get("to_city", city_id)
        available = rest_lookup(name)
        if type(available) is not bool:
            raise ValueError("rest_lookup must return a boolean")
        if available:
            return {
                "planned": True, "reason": "selected", "city_index": index + 1,
                "city_id": city_id, "city_key": leg.get("to_city_key", city_id),
                "city_name": name,
            }
    return result


def estimate_remaining_consumption(
    route: list[dict], arrival_index: int, travel_costs: Mapping,
) -> dict:
    """Estimate future movement and explicitly scheduled negotiation only.

    travel_costs is the decoded movement catalog's ``costs`` matrix, not the
    enclosing document. Only unexecuted edges require costs. Selling the cargo
    just delivered is still pending, including at the endpoint. Missing flags
    mean no scheduled negotiation; present pending flags must be booleans.
    """
    route = _route(route)
    _integer(arrival_index, "arrival_index", 1)
    if arrival_index > len(route):
        raise ValueError("arrival_index must identify an actual route arrival")
    if not isinstance(travel_costs, Mapping):
        raise ValueError("travel_costs must be an object")
    travel_legs = []
    for index in range(arrival_index, len(route)):
        leg = route[index]
        origin, destination = leg["from_city_id"], leg["to_city_id"]
        row = travel_costs.get(origin)
        if not isinstance(row, Mapping) or destination not in row:
            raise ValueError(f"Missing deterministic movement cost {origin!r} -> {destination!r}")
        cost = _integer(row[destination], f"travel_costs[{origin}][{destination}]")
        travel_legs.append({
            "leg_index": index, "from_city_id": origin, "to_city_id": destination, "cost": cost,
        })

    indices = {}
    for field, start in (("bargain_to_cap", arrival_index), ("raise_to_cap", arrival_index - 1)):
        indices[field] = []
        for index in range(start, len(route)):
            enabled = route[index].get(field, False)
            if type(enabled) is not bool:
                raise ValueError(f"route[{index}].{field} must be a boolean")
            if enabled:
                indices[field].append(index)
    bargains, raises = indices["bargain_to_cap"], indices["raise_to_cap"]
    rounds = len(bargains) + len(raises)
    steps = 2 * rounds
    failures = (steps + 3) // 4
    negotiation_cost = 8 * (steps + failures)
    travel_cost = sum(leg["cost"] for leg in travel_legs)
    return {
        "arrival_index": arrival_index, "remaining_travel_cost": travel_cost,
        "bargain_rounds": len(bargains), "raise_rounds": len(raises),
        "negotiation_rounds": rounds, "negotiation_steps": steps,
        "estimated_failures": failures, "estimated_negotiation_cost": negotiation_cost,
        "remaining_consumption": travel_cost + negotiation_cost, "travel_legs": travel_legs,
        "bargain_leg_indices": bargains, "raise_leg_indices": raises,
    }


def plan_water_use(
    current_fatigue: int, remaining_consumption: int, reserve: int, free_uses: int,
) -> dict:
    """Maximize free cups with positive immediate fatigue and endpoint >= reserve.

    A zero-cup result is a normal skip even if the existing route's endpoint
    estimate is below reserve. This function does not reject or repair budgets.
    """
    for field, value in (("current_fatigue", current_fatigue),
                         ("remaining_consumption", remaining_consumption),
                         ("reserve", reserve), ("free_uses", free_uses)):
        _integer(value, field)
    count = max(0, min(free_uses, (current_fatigue - 1) // 50,
                       (current_fatigue + remaining_consumption - reserve) // 50))
    recovery = 50 * count
    immediate = current_fatigue - recovery
    return {
        "planned": count > 0,
        "reason": "selected" if count else (
            "no_remaining_free_uses" if free_uses == 0 else "insufficient_fatigue"),
        "current_fatigue": current_fatigue, "remaining_consumption": remaining_consumption,
        "reserve": reserve, "free_uses": free_uses, "drink_count": count,
        "recovery_amount": recovery, "immediate_fatigue": immediate,
        "estimated_endpoint_fatigue": immediate + remaining_consumption,
    }


def _request(candidate: Mapping) -> dict:
    identity = meal_identity(candidate)
    fields = (("kind", "issue_time") if identity[0] == "work_meals" else
              ("kind", "role_id", "food_id", "remaining_days"))
    return dict(zip(fields, identity))


def validate_bento_priority(priority: list[str]) -> list[str]:
    """Copy a nonempty ordered subset; the parent supplies defaults when enabled."""
    if not isinstance(priority, list) or not priority:
        raise ValueError("priority must be a nonempty list")
    if any(type(kind) is not str or kind not in MEAL_KINDS for kind in priority):
        raise ValueError("priority must contain only work_meals/love_bentos")
    if len(set(priority)) != len(priority):
        raise ValueError("priority must not contain duplicates")
    return list(priority)


def plan_bento_meals(
    current_fatigue: int, reserve: int, priority: list[str],
    cached_inventory: Mapping, food_catalog: Mapping,
) -> dict:
    """Greedily select explicit identities by type, then slot or expiry order.

    cached_inventory is the player's recovery object or selected-kind subset;
    food_catalog is decoded love_bento.json. No cache/catalog is inspected for
    fatigue <= reserve. All enabled types are validated before
    selection, even when earlier types exhaust the allowance. Timestamps are
    reported as cached strings (or None), not interpreted as a freshness clock.
    """
    _integer(current_fatigue, "current_fatigue")
    _integer(reserve, "reserve")
    priority = validate_bento_priority(priority)
    result = {
        "planned": False, "reason": "fatigue_at_or_below_reserve",
        "current_fatigue": current_fatigue, "reserve": reserve, "priority": list(priority),
        "inventory_updated_at": {}, "meals": [], "recovery_amount": 0,
        "planned_remaining_fatigue": current_fatigue,
        "remaining_allowance": max(0, current_fatigue - reserve), "skipped": [],
    }
    if current_fatigue <= reserve:
        return result
    candidates = validate_inventory(priority, cached_inventory, food_catalog)
    for kind in priority:
        section = cached_inventory[kind]
        refresh = section.get("requires_refresh", False)
        if type(refresh) is not bool or refresh:
            raise ValueError(f"Cached {kind} requires an explicit inventory refresh before consumption")
        updated_at = section.get("updated_at")
        if updated_at is not None:
            _text(updated_at, f"cached_inventory.{kind}.updated_at")
        result["inventory_updated_at"][kind] = updated_at

    def rank(candidate: Mapping) -> tuple:
        kind = candidate["kind"]
        within_type = ((candidate["issue_time"],) if kind == "work_meals" else
                       (candidate["remaining_days"], candidate["role_id"], candidate["food_id"]))
        return (priority.index(kind), *within_type)

    for candidate in sorted(candidates, key=rank):
        meal = _request(candidate)
        recovery = candidate["fatigue_recovery"]
        if recovery > result["remaining_allowance"]:
            result["skipped"].append({
                "meal": meal, "reason": "does_not_fit", "recovery_amount": recovery,
                "remaining_allowance": result["remaining_allowance"],
            })
            continue
        # Verify the emitted identity through the exact same resolver as the child.
        resolve_meal(meal, cached_inventory, food_catalog)
        result["meals"].append(meal)
        result["recovery_amount"] += recovery
        result["remaining_allowance"] -= recovery
    result["planned_remaining_fatigue"] = current_fatigue - result["recovery_amount"]
    result["planned"] = bool(result["meals"])
    result["reason"] = ("selected" if result["planned"] else
                        "no_available_meals" if not candidates else "no_fitting_meals")
    return result


__all__ = [
    "select_last_water_arrival", "estimate_remaining_consumption",
    "plan_water_use", "plan_bento_meals", "validate_bento_priority",
]
