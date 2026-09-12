"""Strict, UI-free contracts for explicitly requested meals and cached inventory.

Catalog is the decoded love_bento.json object (its ``items`` list is authoritative).
Invalid requested data raises ValueError. Love-bento recovery values are
verified FoodFactory bases;
account-dependent RiseBentoEnergy is NOT included. Work recovery is 36 by explicit
caller contract. The parent owns fatigue budgets, floors and ordering; the child
validates all requested identities before the first consumption. This module never plans meals
or substitutes an alternative for a missing requested identity.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


MEAL_KINDS = ("work_meals", "love_bentos")
WORK_MEAL_TIMES = ("05:00", "12:00", "18:00")
WORK_MEAL_RECOVERY = 36


def _integer(value: Any, field: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _mapping(value: Any, field: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _list(value: Any, field: str) -> list:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    return value


def _name(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field} must be a nonempty, unpadded string")
    return value


def meal_identity(candidate: Mapping) -> tuple:
    """Stable observed identity, excluding names, effect metadata and UI state."""
    candidate = _mapping(candidate, "candidate")
    kind = candidate.get("kind")
    if type(kind) is not str:
        raise ValueError("candidate.kind must be work_meals or love_bentos")
    if kind == "work_meals":
        issue_time = candidate.get("issue_time")
        if type(issue_time) is not str or issue_time not in WORK_MEAL_TIMES:
            raise ValueError("candidate.issue_time must be a known work-meal slot")
        return kind, issue_time
    if kind == "love_bentos":
        return (
            kind,
            _integer(candidate.get("role_id"), "candidate.role_id", 1),
            _integer(candidate.get("food_id"), "candidate.food_id", 1),
            _integer(candidate.get("remaining_days"), "candidate.remaining_days"),
        )
    raise ValueError("candidate.kind must be work_meals or love_bentos")


def validate_meals(meals: list) -> list[dict]:
    """Copy exact identity-only requests in caller order; an empty list is a no-op."""
    validated = []
    seen = set()
    for request in _list(meals, "meals"):
        if not isinstance(request, dict):
            raise ValueError("meals entries must be dicts")
        identity = meal_identity(request)
        expected = ({"kind", "issue_time"} if identity[0] == "work_meals" else
                    {"kind", "role_id", "food_id", "remaining_days"})
        if set(request) != expected:
            raise ValueError(f"{identity[0]} request must contain exactly {sorted(expected)}")
        if identity in seen:
            raise ValueError("meals contains a duplicate identity")
        seen.add(identity)
        validated.append(dict(request))
    return validated


def _selected_inventory(inventory: Mapping, kind: str) -> Mapping:
    section = _mapping(inventory.get(kind), f"inventory.{kind}")
    if type(section.get("degraded", False)) is not bool:
        raise ValueError(f"inventory.{kind}.degraded must be a boolean")
    if section.get("degraded", False):
        raise ValueError(f"inventory.{kind} is degraded")
    return section


def _work_candidates(section: Mapping) -> list[dict]:
    count = _integer(section.get("available_count"), "work_meals.available_count")
    slots = _list(section.get("slots"), "work_meals.slots")
    seen = set()
    candidates = []
    for raw in slots:
        slot = _mapping(raw, "work_meals.slot")
        candidate = {"kind": "work_meals", "issue_time": slot.get("issue_time"),
                     "fatigue_recovery": WORK_MEAL_RECOVERY, "rating_stars": None}
        identity = meal_identity(candidate)
        if identity in seen:
            raise ValueError("work_meals.slots contains a duplicate issue_time")
        seen.add(identity)
        if type(slot.get("available")) is not bool:
            raise ValueError("work_meals.slot.available must be a boolean")
        if slot["available"]:
            candidates.append(candidate)
    if len(seen) != len(WORK_MEAL_TIMES):
        raise ValueError("work_meals.slots must include all three known slots")
    if count != len(candidates):
        raise ValueError("work_meals.available_count does not match slots")
    return candidates


def _love_candidates(section: Mapping, catalog: Mapping) -> list[dict]:
    catalog = _mapping(catalog, "catalog")
    foods = {}
    for raw in _list(catalog.get("items"), "catalog.items"):
        food = _mapping(raw, "catalog.item")
        food_id = _integer(food.get("id"), "catalog.item.id", 1)
        if food_id in foods:
            raise ValueError(f"catalog has duplicate food_id {food_id}")
        _name(food.get("name"), "catalog.item.name")
        _integer(food.get("fatigue_recovery"), f"catalog.{food_id}.fatigue_recovery", 1)
        stars = _integer(food.get("rating_stars"), f"catalog.{food_id}.rating_stars", 1)
        if stars not in (1, 5):
            raise ValueError(f"catalog.{food_id}.rating_stars must be 1 or 5")
        foods[food_id] = food
    if not foods:
        raise ValueError("catalog.items must not be empty")
    count = _integer(section.get("count"), "love_bentos.count")
    rows = _list(section.get("items"), "love_bentos.items")
    if count != len(rows):
        raise ValueError("love_bentos.count does not match items")
    candidates = []
    seen = set()
    for raw in rows:
        row = _mapping(raw, "love_bentos.item")
        candidate = {"kind": "love_bentos", **{key: row.get(key) for key in (
            "role_id", "role_name", "food_id", "food_name", "remaining_days"
        )}}
        identity = meal_identity(candidate)
        if identity in seen:
            raise ValueError("love_bentos.items contains a duplicate identity")
        seen.add(identity)
        _name(candidate["role_name"], "love_bentos.item.role_name")
        _name(candidate["food_name"], "love_bentos.item.food_name")
        food = foods.get(candidate["food_id"])
        if food is None:
            raise ValueError(f"unknown food_id {candidate['food_id']}")
        if candidate["food_name"] != food["name"]:
            raise ValueError(f"food_name does not match catalog for {candidate['food_id']}")
        candidate.update(fatigue_recovery=food["fatigue_recovery"], rating_stars=food["rating_stars"])
        candidates.append(candidate)
    return candidates


def validate_inventory(kinds: list[str], inventory: Mapping, catalog: Mapping) -> list[dict]:
    """Return canonical cached candidates in saved order, without ranking.

    Only requested kinds are inspected. Names come from validated cached rows;
    recovery and ratings come from the catalog (or the fixed work-meal contract).
    No requested kinds means no inventory or catalog is needed.
    """
    kinds = _list(kinds, "kinds")
    if any(type(kind) is not str or kind not in MEAL_KINDS for kind in kinds):
        raise ValueError("kinds must contain only work_meals/love_bentos")
    if len(set(kinds)) != len(kinds):
        raise ValueError("kinds must not contain duplicates")
    if not kinds:
        return []
    inventory = _mapping(inventory, "inventory")
    candidates = []
    for kind in kinds:
        section = _selected_inventory(inventory, kind)
        candidates.extend(_work_candidates(section) if kind == "work_meals"
                          else _love_candidates(section, catalog))
    return candidates


def resolve_meal(request: dict, inventory: Mapping, catalog: Mapping) -> dict:
    """Resolve exactly one explicit identity; missing/ambiguous data is an error."""
    request = validate_meals([request])[0]
    identity = meal_identity(request)
    candidates = validate_inventory([request["kind"]], inventory, catalog)
    matches = [candidate for candidate in candidates if meal_identity(candidate) == identity]
    if len(matches) != 1:
        raise ValueError(f"requested meal identity must exist exactly once: {identity!r}")
    return matches[0]
