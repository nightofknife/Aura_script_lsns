"""Keep observed freight fatigue separate from confirmed route additions.

Only completed normal travel has a predictable addition. Negotiation is already
included in the trade-page observation and must never be added here again.
"""
from __future__ import annotations

from typing import Any


class FreightFatigueLedger:
    OBSERVATION_SOURCES = frozenset({"trade_page", "profile", "water", "bento"})

    def __init__(self):
        self.current_fatigue: int | None = None
        self.source: str | None = None
        self.last_observed_fatigue: int | None = None
        self.last_observed_source: str | None = None
        self.added_travel_fatigue = 0
        self.max_fatigue: int | None = None
        self.invalid_reason: str | None = "no_observed_baseline"
        self._travel_baseline: int | None = None

    @staticmethod
    def _nonnegative_int(value: Any) -> bool:
        return type(value) is int and value >= 0

    def invalidate(self, reason: str) -> None:
        """Retain the historical reading, but forbid using it as current fatigue."""
        self.current_fatigue = None
        self.source = None
        self.invalid_reason = str(reason)
        self._travel_baseline = None

    def begin_travel(self) -> None:
        """Hide departure fatigue until arrival is confirmed, retaining it privately."""
        if self._travel_baseline is None:
            self._travel_baseline = self.current_fatigue
        self.current_fatigue = None
        self.source = None
        self.invalid_reason = "travel_in_progress"

    def set_observed(self, current: Any, source: str,
                     max_fatigue: int | None = None) -> int | None:
        """Replace the baseline with a measured value; never clamp invalid OCR."""
        if not isinstance(source, str) or source not in self.OBSERVATION_SOURCES:
            self.invalidate("invalid_observation_source")
            return None
        if not self._nonnegative_int(current):
            self.invalidate("invalid_observed_fatigue")
            return None
        if max_fatigue is not None and (
                not self._nonnegative_int(max_fatigue) or max_fatigue == 0):
            self.invalidate("invalid_max_fatigue")
            return None
        limit = self.max_fatigue if max_fatigue is None else max_fatigue
        self.max_fatigue = limit
        self.current_fatigue = self.last_observed_fatigue = current
        self.source = self.last_observed_source = source
        self.added_travel_fatigue = 0
        self.invalid_reason = None
        self._travel_baseline = None
        return current

    def observe_travel(self, travel: Any, cost: Any) -> int | None:
        """Apply one confirmed leg once. The caller owns leg deduplication."""
        if not isinstance(travel, dict) or travel.get("success") is not True:
            self.invalidate("travel_arrival_unconfirmed")
            return None
        if travel.get("status") not in {"ok", "completed", "arrived"}:
            self.invalidate("travel_arrival_unconfirmed")
            return None
        if "arrival_status" in travel and travel["arrival_status"] != "arrived":
            self.invalidate("travel_arrival_unconfirmed")
            return None
        medicine_count = travel.get("fatigue_medicine_use_count", 0)
        encounter_count = travel.get("encounter_actions", 0)
        if not self._nonnegative_int(medicine_count):
            self.invalidate("travel_medicine_usage_unknown")
            return None
        if medicine_count or travel.get("fatigue_medicine_used"):
            self.invalidate("travel_fatigue_medicine_used")
            return None
        if not self._nonnegative_int(encounter_count):
            self.invalidate("travel_encounter_usage_unknown")
            return None
        if encounter_count:
            self.invalidate("travel_encounter_actions")
            return None
        method = travel.get("travel_method", "normal")
        trailer = travel.get("trailer_service")
        if method == "trailer" or (isinstance(trailer, dict) and (
                trailer.get("departed") is True or trailer.get("status") == "departed")):
            self.invalidate("travel_trailer_used")
            return None
        if method != "normal":
            self.invalidate("travel_method_unknown")
            return None
        if not self._nonnegative_int(cost):
            self.invalidate("travel_fatigue_cost_unknown")
            return None
        baseline = (self._travel_baseline if self._travel_baseline is not None
                    else self.current_fatigue)
        if baseline is None:
            # A later ordinary leg cannot repair an earlier unknown transition.
            return None
        current = baseline + cost
        self.added_travel_fatigue += cost
        self.current_fatigue = current
        self.source = "route_derived"
        self.invalid_reason = None
        self._travel_baseline = None
        return current

    def snapshot(self) -> dict:
        return {
            "current_fatigue": self.current_fatigue,
            "source": self.source,
            "last_observed_fatigue": self.last_observed_fatigue,
            "last_observed_source": self.last_observed_source,
            "added_travel_fatigue": self.added_travel_fatigue,
            "max_fatigue": self.max_fatigue,
            "invalid_reason": self.invalid_reason,
        }
