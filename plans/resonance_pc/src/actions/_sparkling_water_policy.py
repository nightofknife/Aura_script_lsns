"""Validate current-run sparkling-water snapshots without UI or persistence."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


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


__all__ = ["validate_recovery_snapshot"]
