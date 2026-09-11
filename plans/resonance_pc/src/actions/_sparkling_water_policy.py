"""Plan one on-route recovery stop without UI, cache, or persistence access."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..services.city_shop_data_pc_service import CityShopDataError


def _object(value: Any, field: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _integer(value: Any, field: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def validate_recovery_snapshot(snapshot: dict) -> dict:
    """Return a deep copy of a valid current-run player_data snapshot.

    Unknown sections are preserved. Only fatigue, sparkling-water quota, and
    persisted metadata are required; over-cap fatigue is valid. The daily
    limit must be in 1..6, while remaining uses may be zero. The caller
    supplies this run's refresh result, never a persisted/legacy cache fallback.
    Missing, invalid, or refresh-required input raises ValueError (including None).
    """
    snapshot = _object(snapshot, "recovery_snapshot")
    status = _object(snapshot.get("status"), "status")
    fatigue = _object(status.get("fatigue"), "status.fatigue")
    _integer(fatigue.get("current"), "status.fatigue.current")
    _integer(fatigue.get("max"), "status.fatigue.max", 1)
    recovery = _object(snapshot.get("recovery"), "recovery")
    water = _object(recovery.get("sparkling_water"), "recovery.sparkling_water")
    if water.get("requires_refresh") is True:
        raise ValueError("recovery.sparkling_water.requires_refresh is true; refresh player data")
    remaining = _integer(water.get("remaining_free_uses"), "remaining_free_uses")
    limit = _integer(water.get("daily_free_limit"), "daily_free_limit", 1)
    if not remaining <= limit <= 6:
        raise ValueError("sparkling_water must satisfy 0 <= remaining <= limit <= 6")
    metadata = _object(snapshot.get("metadata"), "metadata")
    if metadata.get("persisted") is not True:
        raise ValueError("metadata.persisted must be true")
    return deepcopy(snapshot)


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value.strip()


def _validate_travel_fatigue(payload: dict) -> tuple[dict, dict]:
    payload = _object(payload, "travel_fatigue")
    cities = _object(payload.get("cities"), "travel_fatigue.cities")
    costs = _object(payload.get("costs"), "travel_fatigue.costs")
    if not cities or not costs:
        raise ValueError("travel_fatigue cities and costs must not be empty")
    for city_id, name in cities.items():
        _text(city_id, "cities city_id")
        _text(name, f"cities[{city_id}]")
    for from_id, row in costs.items():
        if from_id not in cities:
            raise ValueError(f"Unknown movement origin {from_id!r}")
        for to_id, cost in _object(row, f"costs[{from_id}]").items():
            if to_id not in cities:
                raise ValueError(f"Unknown movement destination {to_id!r}")
            _integer(cost, f"costs[{from_id}][{to_id}]")
    return cities, costs


def _initial_node(initial_city: dict, cities: dict, city_shop_data: object) -> dict:
    initial_city = _object(initial_city, "initial_city")
    name = initial_city.get("city_name") or ""
    key = initial_city.get("city_key") or ""
    _text(name or key, "initial_city city_name or city_key")
    explicit_id = initial_city.get("city_id")
    if explicit_id is not None:
        city_id = _text(explicit_id, "initial_city.city_id")
        if city_id not in cities:
            raise ValueError(f"Unknown initial city id {city_id!r}")
    else:
        matches = {
            city_id for city_id, city_name in cities.items()
            if city_id in (name, key) or city_name in (name, key)
        }
        # The configured resolver handles display aliases (e.g. freeport's
        # numeric versus written-out name) without a second hard-coded catalog.
        if not matches and callable(getattr(city_shop_data, "resolve_city", None)):
            resolved_key = city_shop_data.resolve_city(name or key)["city_key"]
            matches = {
                city_id for city_id, city_name in cities.items()
                if city_shop_data.resolve_city(city_name)["city_key"] == resolved_key
            }
        if len(matches) != 1:
            raise ValueError("initial_city must resolve to exactly one travel city id")
        city_id = next(iter(matches))
    return {"city_id": city_id, "city_key": key or city_id, "city_name": name or cities[city_id]}


def _route_node(leg: dict, side: str, cities: dict) -> dict:
    city_id = _text(leg.get(f"{side}_city_id"), f"{side}_city_id")
    if city_id not in cities:
        raise ValueError(f"Unknown route city id {city_id!r}")
    key = _text(leg.get(f"{side}_city_key") or city_id, f"{side}_city_key")
    name = _text(leg.get(f"{side}_city") or cities[city_id], f"{side}_city")
    return {"city_id": city_id, "city_key": key, "city_name": name}


def select_sparkling_water_stop(
    *,
    route: list[dict],
    initial_city: dict,
    recovery_snapshot: dict | None,
    city_shop_data: object,
    travel_fatigue: dict,
    base_fatigue_reserve: int = 200,
) -> dict:
    """Choose the earliest node with the most cups above the fatigue reserve.

    Nodes are start=0 and arrivals=1..len(route), including repeated cities.
    Candidates includes every node, with rest_available and zero drink_count
    for non-rest nodes. Fatigue estimates are BEFORE drinking; only matrix
    movement costs contribute. No-stop results have null city fields, zero
    recovery, and initial (or unknown) fatigue, not a fictitious selected node.
    Optional route city keys fall back to IDs; rest lookup uses display names.

    Reasons: recovery_snapshot_missing, no_remaining_free_uses,
    no_eligible_tavern_arrival, insufficient_fatigue, or selected.
    Invalid snapshots/routes/costs raise ValueError; lookup failures propagate
    except shop_not_found_in_city, which means that node has no rest point.
    """
    _integer(base_fatigue_reserve, "base_fatigue_reserve")
    result = {
        "base_fatigue_reserve": base_fatigue_reserve,
        "planned": False,
        "reason": "recovery_snapshot_missing",
        "city_index": None,
        "city_id": None,
        "city_key": None,
        "city_name": None,
        "drink_count": 0,
        "remaining_free_uses": None,
        "initial_fatigue": None,
        "cumulative_travel_fatigue": 0,
        "estimated_fatigue": None,
        "recovery_amount": 0,
        "candidates": [],
    }
    if recovery_snapshot is None:
        return result
    snapshot = validate_recovery_snapshot(recovery_snapshot)
    initial = snapshot["status"]["fatigue"]["current"]
    remaining = snapshot["recovery"]["sparkling_water"]["remaining_free_uses"]
    result.update(remaining_free_uses=remaining, initial_fatigue=initial, estimated_fatigue=initial)
    if remaining == 0:
        result["reason"] = "no_remaining_free_uses"
        return result

    cities, costs = _validate_travel_fatigue(travel_fatigue)
    if not isinstance(route, list):
        raise ValueError("route must be a list")
    nodes = []
    cumulative = 0
    for index, raw_leg in enumerate(route):
        leg = _object(raw_leg, f"route[{index}]")
        origin = _route_node(leg, "from", cities)
        destination = _route_node(leg, "to", cities)
        if index == 0:
            nodes.append({**origin, "cumulative_travel_fatigue": 0})
        else:
            previous = nodes[-1]
            # An ID fallback is not a conflicting explicit city key.
            keys_conflict = (
                origin["city_key"] != previous["city_key"]
                and origin["city_key"] != origin["city_id"]
                and previous["city_key"] != previous["city_id"]
            )
            if origin["city_id"] != previous["city_id"] or keys_conflict:
                raise ValueError(f"route[{index}] does not continue the preceding arrival")
        from_id, to_id = origin["city_id"], destination["city_id"]
        if from_id not in costs or to_id not in costs[from_id]:
            raise ValueError(f"Missing deterministic movement cost {from_id!r} -> {to_id!r}")
        cumulative += costs[from_id][to_id]
        nodes.append({**destination, "cumulative_travel_fatigue": cumulative})
    if not nodes:
        nodes.append({**_initial_node(initial_city, cities, city_shop_data), "cumulative_travel_fatigue": 0})

    best = None
    for index, node in enumerate(nodes):
        rest_available = True
        try:
            city_shop_data.resolve_shop_point(node["city_name"] or node["city_key"], "rest")
        except CityShopDataError as exc:
            if exc.code != "shop_not_found_in_city":
                raise
            rest_available = False
        fatigue = initial + node["cumulative_travel_fatigue"]
        cups = min(remaining, max(0, (fatigue - base_fatigue_reserve) // 50)) if rest_available else 0
        candidate = {
            **node,
            "city_index": index,
            "rest_available": rest_available,
            "remaining_free_uses": remaining,
            "initial_fatigue": initial,
            "estimated_fatigue": fatigue,
            "drink_count": cups,
            "recovery_amount": cups * 50,
        }
        result["candidates"].append(candidate)
        if cups > 0 and (best is None or cups > best["drink_count"]):
            best = candidate
    if best is not None:
        result.update(best, planned=True, reason="selected")
    elif any(candidate["rest_available"] for candidate in result["candidates"]):
        result["reason"] = "insufficient_fatigue"
    else:
        result["reason"] = "no_eligible_tavern_arrival"
    return result


__all__ = ["select_sparkling_water_stop", "validate_recovery_snapshot"]
