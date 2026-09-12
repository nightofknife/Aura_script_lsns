"""Read cached inventory and atomically record portions; never refresh inventory."""
from __future__ import annotations

import copy
from datetime import datetime, timezone

from ._player_data_persistence import USER_INFO_FILE, load_pc_user_info
from ._bento_consumption_policy import validate_inventory


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def load_cached_inventory(store, kinds, catalog):
    document = load_pc_user_info(store)
    inventory = document.get("recovery")
    validate_inventory(kinds, inventory, catalog)
    selected = {}
    for kind in kinds:
        value = inventory[kind]
        if type(value.get("requires_refresh", False)) is not bool or value.get("requires_refresh", False):
            raise ValueError(f"Cached {kind} requires an explicit inventory refresh before consumption")
        selected[kind] = copy.deepcopy(value)
    return selected


def record_portion(store, session_id, number, meal, phase):
    """The same portion can advance phases, but cannot be deducted twice."""
    def update(document):
        metadata = document.setdefault("metadata", {})
        previous = metadata.get("bento_consumption") or {}
        record = copy.deepcopy(previous) if previous.get("session_id") == session_id else {
            "session_id": session_id, "items": [],
        }
        if previous and previous.get("session_id") != session_id:
            record["previous_session"] = {k: copy.deepcopy(v) for k, v in previous.items()
                                          if k != "previous_session"}
        items = record["items"]
        found = next((row for row in items if row["number"] == number), None)
        if found is None:
            if phase != "consumption_pending":
                raise ValueError("Bento portion must be recorded before consumption")
            found = {"number": number, "meal": copy.deepcopy(meal), "consumed": False}
            items.append(found)
        if found["meal"] != meal:
            raise ValueError("Bento portion identity changed")
        if found.get("phase") == phase or (phase == "consumption_confirmed" and found["consumed"]):
            return document
        allowed = {
            None: {"consumption_pending"},
            "consumption_pending": {"consumption_confirmed"},
            "consumption_confirmed": {"rating_pending", "completed"},
            "rating_pending": {"completed"},
            "completed": set(),
        }
        if phase not in allowed.get(found.get("phase"), set()):
            raise ValueError("Invalid bento portion phase transition")
        recovery = document.setdefault("recovery", {})
        resource = recovery.get(meal["kind"])
        if not isinstance(resource, dict):
            raise ValueError("Bento inventory observation is missing")
        if phase == "consumption_confirmed" and not found["consumed"]:
            if meal["kind"] == "work_meals":
                slots = resource["slots"]
                slot = next(row for row in slots if row["issue_time"] == meal["issue_time"])
                if slot["available"] is not True:
                    raise ValueError("Work meal was already unavailable")
                slot["available"] = False
                resource["available_count"] = sum(row["available"] for row in slots)
            else:
                identity = tuple(meal[k] for k in ("role_id", "food_id", "remaining_days"))
                matches = [row for row in resource["items"] if
                           tuple(row[k] for k in ("role_id", "food_id", "remaining_days")) == identity]
                if len(matches) != 1:
                    raise ValueError("Love-bento consumption identity is not unique")
                resource["items"].remove(matches[0])
                resource["count"] = len(resource["items"])
            found["consumed"] = True
        found.update(phase=phase, updated_at=timestamp())
        record.update(phase=phase, requires_refresh=phase != "completed", updated_at=timestamp())
        resource["requires_refresh"] = phase != "completed"
        metadata["bento_consumption"] = record
        return document
    result = store.update(USER_INFO_FILE, update)
    return copy.deepcopy(result.new_value["recovery"][meal["kind"]])
