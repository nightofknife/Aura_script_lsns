"""Shared, strict inputs for passenger service attached to freight."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any


SCORE_NAMES = (
    "comfort", "foodScores", "playScores", "petScores", "fishScores",
    "plantScores", "medicalScores",
)
DEFAULT_PASSENGER_OPTIONS: dict[str, Any] = {
    "seat_capacity": None,
    "flyer_target": 0,
    "transport_level": 100,
    "seat_level": 5,
    "scores": {name: 42000 if name == "comfort" else 7000 for name in SCORE_NAMES},
    "furniture_bonus_bps": 0,
    "cleanliness_bps": 10000,
    "news": "none",
}


def _integer(value: Any, name: str, minimum: int, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        bound = f"{minimum}..{maximum}" if maximum is not None else f">= {minimum}"
        raise ValueError(f"passenger {name} must be an integer {bound}")
    return value


def normalize_passenger_options(options: Any, *, enabled: bool = True) -> dict[str, Any]:
    """Return independent canonical values, without coercing booleans or strings."""
    if options is None:
        options = {}
    if not isinstance(options, Mapping):
        raise ValueError("passenger_options must be an object")
    unknown = set(options) - set(DEFAULT_PASSENGER_OPTIONS)
    if unknown:
        raise ValueError(f"unknown passenger options: {', '.join(sorted(map(str, unknown)))}")
    result = deepcopy(DEFAULT_PASSENGER_OPTIONS)
    result.update(deepcopy(dict(options)))
    capacity = result["seat_capacity"]
    if capacity is None:
        if enabled:
            raise ValueError("passenger seat_capacity is required when auto_passenger is enabled")
    else:
        _integer(capacity, "seat_capacity", 1)
    _integer(result["flyer_target"], "flyer_target", 0)
    _integer(result["transport_level"], "transport_level", 1, 100)
    _integer(result["seat_level"], "seat_level", 1, 5)
    _integer(result["furniture_bonus_bps"], "furniture_bonus_bps", 0)
    _integer(result["cleanliness_bps"], "cleanliness_bps", 0, 10000)
    if result["news"] != "none":
        raise ValueError("passenger news currently supports only 'none'")
    scores = result["scores"]
    if not isinstance(scores, Mapping) or set(scores) - set(SCORE_NAMES):
        raise ValueError("passenger scores must contain only the seven passenger score names")
    result["scores"] = {
        name: _integer(scores.get(name, DEFAULT_PASSENGER_OPTIONS["scores"][name]), name, 0)
        for name in SCORE_NAMES
    }
    return result


def resolve_flyer_target(options: Any, now: datetime | None = None) -> int:
    """Resolve automatic allowance on the UTC+8 server day starting at 05:00.

    Naive datetimes are interpreted as server-local wall time. This computes a
    spending target, not current inventory or a promise of daily replenishment.
    """
    values = normalize_passenger_options(options, enabled=False)
    if values["flyer_target"]:
        return values["flyer_target"]
    server_tz = timezone(timedelta(hours=8))
    instant = datetime.now(server_tz) if now is None else now
    if not isinstance(instant, datetime):
        raise ValueError("now must be a datetime")
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=server_tz)
    server_day = instant.astimezone(server_tz) - timedelta(hours=5)
    return (100 if server_day.weekday() >= 5 else 50) + values["transport_level"]
