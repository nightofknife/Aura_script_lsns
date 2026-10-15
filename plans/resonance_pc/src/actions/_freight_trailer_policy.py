"""Route-wide towing allocation without pre-reading the game's daily quota."""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json
import math
from pathlib import Path


DISTANCES_FILE = Path(__file__).resolve().parents[2] / "data/meta/trailer_route_distances.json"
QUOTA_STOP_REASONS = frozenset({"quota_exhausted", "daily_limit_reached"})


def validate_trailer_options(enabled, daily_first_n, payment):
    if type(enabled) is not bool:
        raise ValueError("auto_trailer must be a boolean")
    if type(daily_first_n) is not int or daily_first_n < 1:
        raise ValueError("trailer_daily_first_n must be a positive integer")
    if payment not in ("gold", "mileage"):
        raise ValueError("trailer_payment must be gold or mileage")


def verified_quota_skip(result):
    """Only a positive no-input quota decision authorizes ordinary departure."""
    return (isinstance(result, dict) and result.get("success") is True
            and result.get("status") == "not_used" and result.get("submitted") is False
            and result.get("departed") is False and result.get("page_state") == "map"
            and result.get("reason") in QUOTA_STOP_REASONS)


@lru_cache(maxsize=1)
def _distances():
    payload = json.loads(DISTANCES_FILE.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(payload.get("distances_km"), dict):
        raise ValueError("Invalid towing route distance catalog")
    return payload["distances_km"]


class FreightTrailerPlan:
    def __init__(self, *, enabled=False, daily_first_n=1, payment="gold", route=(), reposition=(), distances=None):
        validate_trailer_options(enabled, daily_first_n, payment)
        self.enabled, self.daily_first_n, self.payment = enabled, daily_first_n, payment
        self.stop_reason = None
        self.attempts = []
        self.selected = []
        if enabled and (route or reposition):
            table = _distances() if distances is None else distances
            candidates = []
            for phase, legs in (("reposition", reposition), ("route", route)):
                for index, leg in enumerate(legs):
                    origin, destination = str(leg.get("from_city_id", "")), str(leg.get("to_city_id", ""))
                    try:
                        distance = table[origin][destination]
                    except KeyError as exc:
                        raise ValueError(f"Missing towing distance for {origin}->{destination}") from exc
                    if (type(distance) not in (int, float) or not math.isfinite(distance) or distance <= 0):
                        raise ValueError(f"Invalid towing distance for {origin}->{destination}")
                    candidates.append({"phase": phase, "leg_index": index, "sequence": len(candidates),
                                       "from_city_id": origin, "to_city_id": destination,
                                       "distance_km": distance})
            ranked = sorted(candidates, key=lambda leg: (-leg["distance_km"], leg["sequence"]))
            self.selected = sorted(ranked[:daily_first_n], key=lambda leg: leg["sequence"])
        self._selected_keys = frozenset((leg["phase"], leg["leg_index"]) for leg in self.selected)

    def should_use(self, phase, index):
        return self.enabled and self.stop_reason is None and (phase, index) in self._selected_keys

    def observe(self, phase, index, travel):
        result = travel.get("trailer_service")
        if not isinstance(result, dict):
            return
        self.attempts.append({"phase": phase, "leg_index": index, "result": deepcopy(result)})
        if verified_quota_skip(result):
            self.stop_reason = result["reason"]

    def snapshot(self):
        return {"enabled": self.enabled, "daily_first_n": self.daily_first_n, "payment": self.payment,
                "selected_legs": deepcopy(self.selected), "disabled_reason": self.stop_reason,
                "attempts": deepcopy(self.attempts)}
