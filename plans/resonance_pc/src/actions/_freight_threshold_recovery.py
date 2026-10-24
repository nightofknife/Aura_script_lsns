"""Per-city recovery orchestration with observed fatigue and independent thresholds."""
from __future__ import annotations

from copy import deepcopy
from asyncio import CancelledError
from typing import Any

from ._freight_recovery_policy import plan_threshold_water_use, validate_recovery_thresholds
from ._freight_fatigue_ledger import FreightFatigueLedger


class RecoveryStopError(RuntimeError):
    def __init__(self, code: str, detail: dict):
        super().__init__(code)
        self.code, self.detail = code, detail


class FreightRecoverySession:
    def __init__(self, *, water_enabled, bento_enabled, water_start, water_stop,
                 bento_start, bento_stop, priority, refresh_fatigue, water_counts,
                 has_rest, drink_water, consume_bentos, emit=None, travel_cost=None):
        if type(water_enabled) is not bool or type(bento_enabled) is not bool:
            raise ValueError("Recovery switches must be booleans")
        if water_enabled:
            validate_recovery_thresholds(water_start, water_stop, resource="sparkling_water")
        if bento_enabled:
            validate_recovery_thresholds(bento_start, bento_stop, resource="bento")
        self.water_enabled, self.bento_enabled = water_enabled, bento_enabled
        self.water_start, self.water_stop = water_start, water_stop
        self.bento_start, self.bento_stop = bento_start, bento_stop
        self.priority = list(priority or [])
        self.refresh_fatigue, self.water_counts = refresh_fatigue, water_counts
        self.has_rest, self.drink_water = has_rest, drink_water
        self.consume_bentos, self.emit = consume_bentos, emit
        self.events: list[dict] = []
        self.water_plans: list[dict] = []
        self.final_fatigue: int | None = None
        self.ledger = FreightFatigueLedger()
        self.travel_cost = travel_cost

    def invalidate_fatigue(self):
        """An unmeasured operation makes the preceding reading historical."""
        self.final_fatigue = None
        self.ledger.invalidate("unmeasured_operation")

    def accept_trade_reading(self, observation):
        self.invalidate_fatigue()
        if (isinstance(observation, dict) and observation.get("success") is True
                and observation.get("source") == "trade_page"
                and type(observation.get("max_fatigue")) is int
                and observation["max_fatigue"] > 0):
            self.final_fatigue = self.ledger.set_observed(
                observation.get("current_fatigue"), "trade_page", observation["max_fatigue"])
        else:
            self.ledger.invalidate("trade_fatigue_unrecognized")

    def begin_travel(self, leg):
        cost = None
        if self.travel_cost is not None:
            try:
                cost = self.travel_cost(leg)
            except (KeyError, ValueError, TypeError, LookupError, RuntimeError) as exc:
                if "cancel" in str(getattr(exc, "code", "")).lower():
                    raise
                # Missing route data requires an observation after arrival.
                pass
        self.ledger.begin_travel()
        self.final_fatigue = None
        return cost

    def observe_travel(self, travel, cost):
        self.ledger.observe_travel(travel, cost)
        self.final_fatigue = None

    def _set_observed(self, current, source):
        self.final_fatigue = self.ledger.set_observed(current, source)

    def _track_event(self, event):
        tracking = self.ledger.snapshot()
        event.update(current_fatigue=tracking["current_fatigue"],
                     fatigue_source=tracking["source"], final_fatigue=self.final_fatigue,
                     fatigue_tracking=tracking)

    @property
    def enabled(self):
        return self.water_enabled or self.bento_enabled

    async def _emit(self, stage, state, event, **data):
        if self.emit is not None:
            await self.emit(stage, state, city_index=event["city_index"],
                            current_city=event["city_name"],
                            data={"scope": event["scope"], "phase": event["phase"],
                                  "fatigue_tracking": self.ledger.snapshot(), **data})

    @staticmethod
    def _observed(value):
        return type(value) is int and value >= 0

    def _skip(self, reason, page_state, fatigue):
        return {"success": True, "triggered": False, "status": "skipped", "reason": reason,
                "page_state": page_state, "final_fatigue": self.final_fatigue,
                "current_fatigue": fatigue, "fatigue_source": self.ledger.source}

    async def _check_water(self, event, page_state, rest_stop):
        city_name, city_index = event["city_name"], event["city_index"]
        current = self.ledger.current_fatigue
        before_water_source = self.ledger.source
        selection = None
        reason = "disabled"
        if self.water_enabled:
            reason = "below_start_threshold"
            if current > self.water_start:
                if not self.has_rest(city_name):
                    reason = "no_rest_in_city"
                else:
                    remaining, _ = self.water_counts()
                    planned = plan_threshold_water_use(current, self.water_start, self.water_stop, remaining)
                    planned.update(city_name=city_name, city_index=city_index,
                                   scope=event["scope"], phase=event["phase"])
                    planned["fatigue_source"] = self.ledger.source
                    self.water_plans.append(deepcopy(planned))
                    reason = planned["reason"]
                    if planned["planned"]:
                        selection = planned
        event["sparkling_water"] = self._skip(reason, page_state, current)
        if selection:
            event["sparkling_water"] = {"triggered": True, "success": False,
                "status": "running", "requested_count": selection["drink_count"],
                "completed_count": 0, "final_fatigue": None}
            await self._emit("sparkling_water", "started", event, selection=selection)
            # Do not expose a stale pre-consumption reading as the final observation.
            event["final_fatigue"] = None
            self.invalidate_fatigue()
        else:
            await self._emit("sparkling_water", "skipped", event, reason=reason)
        rest = (await rest_stop(selection, page_state) if rest_stop else
                {"sparkling_water": await self.drink_water(selection, page_state)} if selection else
                {"page_state": page_state})
        water = rest.get("sparkling_water")
        page_state = rest.get("page_state", (water or {}).get("page_state", page_state))
        if selection:
            event["sparkling_water"] = {**event["sparkling_water"], **(water or {}), "triggered": True}
            if (not isinstance(water, dict) or water.get("success") is not True
                    or page_state not in {"city_main", "city_panel"}
                    or type(water.get("completed_count")) is not int
                    or not 0 <= water["completed_count"] <= selection["drink_count"]):
                raise RecoveryStopError("sparkling_water_not_completed", event)
            before_water = current
            current = await self.refresh_fatigue(page_state)
            if not self._observed(current):
                raise RecoveryStopError("recovery_fatigue_invalid", event)
            page_state = "city_main"
            event["final_fatigue"] = current
            self._set_observed(current, "water")
            event["sparkling_water"].update(final_fatigue=current,
                                            reached_stop_threshold=current <= self.water_stop)
            if (water["completed_count"] > 0 and current >= before_water
                    and before_water_source != "route_derived"):
                raise RecoveryStopError("sparkling_water_fatigue_not_decreased", event)
            await self._emit("sparkling_water", "completed", event,
                             result=event["sparkling_water"])
        return rest, page_state

    async def _check_bento(self, event, page_state):
        current = self.ledger.current_fatigue
        event["bento_consumption"] = self._skip(
            "disabled" if not self.bento_enabled else "below_start_threshold", page_state, current)
        if self.bento_enabled and current > self.bento_start:
            event["bento_consumption"] = {"triggered": True, "success": False,
                "status": "running", "final_fatigue": None}
            await self._emit("bento", "started", event, current_fatigue=current,
                             fatigue_source=self.ledger.source)
            event["final_fatigue"] = None
            self.invalidate_fatigue()
            params = {"eat_work_meals": "work_meals" in self.priority,
                      "eat_love_bentos": "love_bentos" in self.priority,
                      "bento_priority": self.priority,
                      "bento_start_threshold": self.bento_start,
                      "bento_stop_threshold": self.bento_stop}
            child = await self.consume_bentos(params, page_state)
            child = child if isinstance(child, dict) else {}
            event["bento_consumption"] = {**child, "triggered": True}
            final = child.get("final_fatigue")
            event["final_fatigue"] = final if self._observed(final) else None
            valid = (type(child.get("success")) is bool
                     and type(child.get("consumed_count")) is int and child["consumed_count"] >= 0
                     and type(child.get("completed_count")) is int
                     and 0 <= child["completed_count"] <= child["consumed_count"]
                     and child.get("bento_start_threshold") == self.bento_start
                     and child.get("bento_stop_threshold") == self.bento_stop)
            if not valid:
                event["final_fatigue"] = None
                raise RecoveryStopError("bento_result_invalid", event)
            if self._observed(final):
                self._set_observed(final, "bento")
            if child["success"] is not True:
                raise RecoveryStopError(child.get("reason") or "bento_recovery_failed", event)
            if child["completed_count"] != child["consumed_count"]:
                raise RecoveryStopError("bento_recovery_incomplete", event)
            if not self._observed(final) or child.get("page_state") != "city_main":
                raise RecoveryStopError("bento_final_fatigue_unconfirmed", event)
            # A fresh child entry may legitimately skip after the parent's observation.
            fresh_skip = (child.get("status") == "skipped" and child["consumed_count"] == 0
                          and self._observed(child.get("initial_fatigue"))
                          and child["initial_fatigue"] <= self.bento_start
                          and final == child["initial_fatigue"])
            exhausted = (child.get("reason") == "no_available_meals"
                         and child.get("status") in {"completed", "skipped"}
                         and child["completed_count"] == child["consumed_count"])
            if final > self.bento_stop and not (fresh_skip or exhausted):
                raise RecoveryStopError("bento_recovery_incomplete", event)
            event["bento_consumption"]["reached_stop_threshold"] = final <= self.bento_stop
            current, page_state = final, "city_main"
            await self._emit("bento", "completed", event, result=event["bento_consumption"])
        else:
            await self._emit("bento", "skipped", event,
                             reason=event["bento_consumption"]["reason"])
        return page_state

    async def check(self, *, city_name, city_index, page_state, scope="route", phase="arrival",
                    rest_stop=None):
        """A rest callback may share one visit with unrelated local shopping."""
        if not self.enabled:
            return (await rest_stop(None, page_state) if rest_stop else {"page_state": page_state})
        event = {"scope": scope, "phase": phase, "city_index": city_index, "city_name": city_name,
                 "initial_fatigue": None, "final_fatigue": None}
        self.events.append(event)
        stage = "sparkling_water"
        try:
            if page_state not in {"city_main", "city_panel"}:
                self.invalidate_fatigue()
                raise RecoveryStopError("recovery_invalid_start_page", event)
            current = self.ledger.current_fatigue
            if current is None:
                current = await self.refresh_fatigue(page_state)
                page_state = "city_main"
                self._set_observed(current, "profile")
            if not self._observed(current):
                raise RecoveryStopError("recovery_fatigue_invalid", event)
            event.update(initial_fatigue=current, initial_fatigue_source=self.ledger.source)
            self._track_event(event)
            rest = {"page_state": page_state}
            event["checked_order"] = []
            for resource in ("sparkling_water", "bento"):
                stage = "sparkling_water" if resource == "sparkling_water" else "bento"
                event["checked_order"].append(resource)
                if resource == "sparkling_water":
                    rest, page_state = await self._check_water(event, page_state, rest_stop)
                else:
                    page_state = await self._check_bento(event, page_state)
            event.update(success=True, page_state=page_state)
            self._track_event(event)
            return {**rest, "page_state": page_state, "recovery": event}
        except BaseException as exc:
            self._track_event(event)
            event.update(success=False, reason=getattr(exc, "code", "recovery_cancelled" if isinstance(exc, CancelledError) else "recovery_execution_failed"),
                         page_state="unknown")
            resource = event.get("bento_consumption" if stage == "bento" else "sparkling_water")
            if resource is not None and resource.get("triggered"):
                detail = getattr(exc, "detail", {})
                if isinstance(detail, dict) and detail is not event:
                    for key in ("completed_count", "consumed_count", "work_meals_used", "love_bentos_used"):
                        if type(detail.get(key)) is int and detail[key] >= 0:
                            resource[key] = detail[key]
                resource.update(success=False, status="cancelled" if "cancel" in event["reason"] else "failed",
                                reason=event["reason"])
            await self._emit(stage, "failed", event, result=deepcopy(event))
            if isinstance(exc, (KeyboardInterrupt, SystemExit, CancelledError)):
                raise
            if hasattr(exc, "code"):
                raise
            raise RecoveryStopError("recovery_execution_failed", deepcopy(event)) from exc

    def summary(self):
        def total(rows, key):
            return sum(row[key] for row in rows if type(row.get(key)) is int and row[key] >= 0)

        def summarize(key, enabled):
            rows = [{"scope": e["scope"], "phase": e["phase"], "city_index": e["city_index"],
                     "city_name": e["city_name"], **deepcopy(e[key])}
                    for e in self.events if key in e]
            triggered = [r for r in rows if r.get("triggered")]
            bad = [e for e in self.events if e.get(key, {}).get("triggered")
                   and e[key].get("success") is False]
            result = {"success": not bad, "triggered": bool(triggered),
                      "status": "failed" if bad else "completed" if triggered else "skipped",
                      "reason": bad[-1]["reason"] if bad else
                                None if triggered else "disabled" if not enabled else "not_triggered",
                      "visits": rows}
            if key == "sparkling_water":
                result.update(requested_count=total(triggered, "requested_count"),
                              completed_count=total(triggered, "completed_count"),
                              recovered_fatigue=50 * total(triggered, "completed_count"))
            else:
                result.update({field: total(triggered, field) for field in
                               ("consumed_count", "completed_count", "work_meals_used", "love_bentos_used")})
                recovered = [r.get("recovered_fatigue") for r in triggered]
                result["recovered_fatigue"] = (sum(recovered) if all(type(x) is int for x in recovered) else None)
            result["final_fatigue"] = rows[-1].get("final_fatigue") if rows else None
            return result

        return {"recovery_events": deepcopy(self.events),
                "sparkling_water": summarize("sparkling_water", self.water_enabled),
                "bento_consumption": summarize("bento_consumption", self.bento_enabled),
                "final_fatigue": self.final_fatigue,
                "estimated_fatigue": (self.ledger.current_fatigue
                                      if self.ledger.source == "route_derived" else None),
                "fatigue_tracking": self.ledger.snapshot()}
