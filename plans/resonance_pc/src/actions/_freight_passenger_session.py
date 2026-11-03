"""One-batch passenger state carried along a frozen freight route."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from threading import Event
from typing import Any

from packages.aura_core.observability.runtime_trace import tracked_to_thread
from packages.resonance_shared.passenger_parameters import normalize_passenger_options, resolve_flyer_target
from ._freight_passenger_planner import build_passenger_plan
from .passenger_pc_actions import (
    PassengerPcError, _LOCAL_CANCEL_CHECK, _check_cancelled,
    _return_recruitment_pages_to_main, _read_flyer_inventory, _recruit_passengers_by_flyer,
    resonance_pc_enter_city_and_settle_passengers,
)


class FreightPassengerSession:
    def __init__(self, route, options, city_prestige=None, inventory=None, emit=None):
        self.route = deepcopy(route)
        self.options = normalize_passenger_options(options)
        self.target = resolve_flyer_target(self.options)
        # A weekday boundary must never change the target during this run.
        self.options["flyer_target"] = self.target
        self.city_prestige = deepcopy(city_prestige)
        if inventory is not None and (type(inventory) is not int or inventory < 0):
            raise ValueError("passenger inventory must be a nonnegative observed integer")
        self.inventory = inventory
        self.initial_inventory = inventory
        self.emit = emit
        self.initialized = False
        self.flyers_used = 0
        self.loaded_destination = None
        self.batches: list[dict] = []
        self.plans: list[dict] = []
        self.requires_manual_completion = False
        self.stop_reason = None
        self.failure = None
        self.revenues = {"ticket_revenue": None, "extra_revenue": None, "total_revenue": None}
        self._revenue_observations = {key: [] for key in self.revenues}

    def _plan(self, start_index, *, structural=False):
        inventory = (max(10, self.target) if structural and self.inventory is None else self.inventory)
        plan = build_passenger_plan(
            self.route, self.options, city_prestige=self.city_prestige, inventory=inventory,
            target_remaining=max(self.target - self.flyers_used, 0), start_index=start_index,
        )
        self.plans.append(deepcopy(plan))
        return plan

    async def _emit(self, stage, state, city_index, **fields):
        if self.emit is not None:
            snapshot = fields.pop("_snapshot", None)
            await self.emit(stage, state, city_index=city_index,
                            data={"passenger": self.snapshot() if snapshot is None else snapshot, **fields})

    async def _skip(self, stage, city_index, page_state, reason):
        await self._emit(stage, "skipped", city_index, reason=reason)
        return {"page_state": page_state, "triggered": False, "reason": reason}

    async def _worker(self, function, *args, **kwargs):
        """Join a cancelled tracked worker before exposing a detached final receipt."""
        cancelled = Event()
        def invoke():
            token = _LOCAL_CANCEL_CHECK.set(cancelled.is_set)
            try:
                _check_cancelled()
                return function(*args, **kwargs)
            finally:
                _LOCAL_CANCEL_CHECK.reset(token)
        worker = asyncio.create_task(tracked_to_thread(invoke))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError as cancellation:
            cancelled.set()
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            try:
                result = worker.result()
            except BaseException as worker_error:
                detail = getattr(worker_error, "detail", {}) or {}
                cancellation.detail = deepcopy(detail) if isinstance(detail, dict) else {}
            else:
                cancellation.detail = {"cancelled_operation_result": deepcopy(result)}
            raise

    @staticmethod
    async def _ensure_main(page_state, app, vision, ensure_main):
        state = await ensure_main(page_state, app, vision)
        if state != "city_main":
            raise PassengerPcError("passenger_main_not_restored", "Passenger work requires confirmed city main",
                                   {"page_state": state})
        return state

    async def initialize(self, *, page_state, app, ocr, vision, ensure_main):
        if self.initialized:
            return {"page_state": page_state, "initialized": True}
        if self.target <= 0 or not self.route or not self._plan(0, structural=True).get("trips"):
            self.initialized = True
            self.stop_reason = ("target_met" if self.target <= 0 else "flyers_exhausted"
                                if self.inventory is not None and self.inventory < 10 else "route_exhausted")
            return {"page_state": page_state, "initialized": True}
        if self.inventory is None:
            page_state = await self._ensure_main(page_state, app, vision, ensure_main)
            try:
                observed = await self._worker(_read_flyer_inventory, app, ocr, vision)
            except asyncio.CancelledError as exc:
                observed = (getattr(exc, "detail", {}) or {}).get("cancelled_operation_result", {})
                if (observed.get("success") is True and observed.get("page_state") == "city_main"
                        and type(observed.get("flyers_remaining")) is int and observed["flyers_remaining"] >= 0):
                    self.inventory = self.initial_inventory = observed["flyers_remaining"]
                self.mark_failure(exc)
                raise
            inventory = observed.get("flyers_remaining")
            if (observed.get("success") is not True or observed.get("page_state") != "city_main"
                    or type(inventory) is not int or inventory < 0):
                raise PassengerPcError("flyer_balance_unreadable", "Initial flyer balance was not confirmed", observed)
            self.inventory = self.initial_inventory = inventory
            page_state = "city_main"
        self.initialized = True
        plan = self._plan(0)
        await self._emit("passenger_planning", "completed", 0, plan=deepcopy(plan))
        if self.inventory < 10:
            self.stop_reason = "flyers_exhausted"
        return {"page_state": page_state, "initialized": True, "inventory": self.inventory}

    async def before_departure(self, index, *, page_state, app, ocr, vision, ensure_main):
        if self.requires_manual_completion:
            raise PassengerPcError("passenger_state_uncertain", "Passenger state requires manual completion", self.snapshot())
        if self.loaded_destination is not None:
            return await self._skip("passenger_recruit", index, page_state, "batch_onboard")
        if not self.initialized:
            initialized = await self.initialize(page_state=page_state, app=app, ocr=ocr,
                                                vision=vision, ensure_main=ensure_main)
            page_state = initialized["page_state"]
        if self.flyers_used >= self.target:
            self.stop_reason = "target_met"
            return await self._skip("passenger_recruit", index, page_state, self.stop_reason)
        if self.inventory is not None and self.inventory < 10:
            self.stop_reason = "flyers_exhausted"
            return await self._skip("passenger_recruit", index, page_state, self.stop_reason)
        plan = self._plan(index)
        await self._emit("passenger_planning", "completed", index, plan=deepcopy(plan))
        trip = next((trip for trip in plan.get("trips", []) if trip["from_index"] == index), None)
        if trip is None:
            return await self._skip("passenger_recruit", index, page_state, "not_selected")
        page_state = await self._ensure_main(page_state, app, vision, ensure_main)
        batch = {"trip": deepcopy(trip), "status": "recruiting", "submission_state": "not_submitted",
                 "recruitment": None, "settlement": None, "flyers_used": None}
        self.batches.append(batch)
        await self._emit("passenger_recruit", "started", index, trip=deepcopy(trip))
        loop = asyncio.get_running_loop()
        submission_events = []

        async def flush_submission_events():
            if submission_events:
                await asyncio.gather(*(asyncio.wrap_future(event) for event in submission_events),
                                     return_exceptions=True)

        def submitted():
            batch.update(status="submitted_unknown", submission_state="submitted_unknown")
            self.loaded_destination = {"city_index": trip["to_index"], "city_id": trip.get("to_city_id"),
                                       "city_name": trip["to_city"], "state": "submitted_unknown"}
            if self.emit is not None:
                event = asyncio.run_coroutine_threadsafe(
                    self._emit("passenger_recruit", "submitted", index, _snapshot=self.snapshot()), loop,
                )
                submission_events.append(event)

        try:
            recruitment = await self._worker(
                _recruit_passengers_by_flyer, trip["to_city"], app=app, ocr=ocr, vision=vision,
                read_passenger_count=False, submission_observer=submitted,
                known_flyers_before=self.inventory,
            )
            batch["recruitment"] = deepcopy(recruitment)
            before, after, used = (recruitment.get(key) for key in
                                   ("flyers_before", "flyers_remaining", "flyers_used"))
            if (recruitment.get("success") is not True or recruitment.get("page_state") != "city_main"
                    or recruitment.get("success_marker_confirmed") is not True
                    or any(type(value) is not int or value < 0 for value in (before, after, used))
                    or before - after != used or used <= 0):
                raise PassengerPcError("passenger_recruitment_unconfirmed", "Recruitment consumption was not confirmed",
                                       {"recruitment": recruitment})
            # Max dispatch is deliberate: the last batch may exceed the remaining target.
            self.inventory = after
            self.flyers_used += used
            batch.update(status="onboard", submission_state="result_confirmed", flyers_used=used)
            self.loaded_destination = {"city_index": trip["to_index"], "city_id": trip.get("to_city_id"),
                                       "city_name": trip["to_city"], "state": "onboard"}
            await flush_submission_events()
            await self._emit("passenger_recruit", "completed", index, recruitment=deepcopy(recruitment))
            return {"page_state": "city_main", "triggered": True, "recruitment": recruitment}
        except BaseException as exc:
            batch["error"] = self._error(exc)
            detail = getattr(exc, "detail", {}) or {}
            receipt = detail.get("recruitment") or detail.get("cancelled_operation_result")
            if isinstance(receipt, dict):
                batch["partial_receipt"] = deepcopy(receipt)
                before, after, used = (receipt.get(key) for key in
                                      ("flyers_before", "flyers_remaining", "flyers_used"))
                if (batch["flyers_used"] is None
                        and all(type(v) is int and v >= 0 for v in (before, after, used))
                        and before - after == used):
                    self.inventory = after
                    self.flyers_used += used
                    batch["flyers_used"] = used
                    if receipt.get("success") is True and receipt.get("page_state") == "city_main":
                        batch.update(status="onboard", submission_state="result_confirmed")
                        if self.loaded_destination is not None:
                            self.loaded_destination["state"] = "onboard"
            submitted_unknown = batch["submission_state"] != "not_submitted"
            if submitted_unknown:
                self.mark_failure(exc, city_index=index)
            elif (isinstance(exc, PassengerPcError)
                  and exc.code in {"destination_not_found", "dispatch_location_not_found", "flyers_exhausted"}):
                # No consumption was submitted. Only resume after proving main again.
                try:
                    cleaned = await self._worker(_return_recruitment_pages_to_main, app, vision)
                    if cleaned.get("success") is not True:
                        raise PassengerPcError("passenger_main_not_restored", "Safe recruitment cleanup failed", cleaned)
                    page_state = "city_main"
                except BaseException as cleanup_error:
                    self.mark_failure(cleanup_error, city_index=index)
                    raise
                if exc.code == "flyers_exhausted":
                    observed_before = receipt.get("flyers_before") if isinstance(receipt, dict) else None
                    if type(observed_before) is int and observed_before >= 0:
                        self.inventory = observed_before
                    self.stop_reason = "flyers_exhausted"
                batch.update(status="skipped", reason=exc.code)
                return await self._skip("passenger_recruit", index, page_state, exc.code)
            else:
                self.mark_failure(exc, city_index=index)
            await flush_submission_events()
            await self._emit("passenger_recruit", "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                             index, error=self._error(exc))
            raise

    async def after_arrival(self, city_index, *, page_state, app, ocr, vision, ensure_main):
        loaded = self.loaded_destination
        if loaded is None or city_index != loaded["city_index"]:
            if loaded is not None:
                page_state = await self._ensure_main(page_state, app, vision, ensure_main)
            return await self._skip("passenger_settlement", city_index, page_state, "not_destination")
        if self.requires_manual_completion or loaded["state"] != "onboard":
            raise PassengerPcError("passenger_state_uncertain", "Cannot settle an uncertain passenger batch", self.snapshot())
        batch = self.batches[-1]
        batch["status"] = "settling"
        await self._emit("passenger_settlement", "started", city_index)
        try:
            settled = await self._worker(resonance_pc_enter_city_and_settle_passengers,
                                         app=app, ocr=ocr, vision=vision)
            batch["settlement"] = deepcopy(settled)
            if settled.get("success") is not True or settled.get("status") != "completed":
                raise PassengerPcError("passenger_settlement_unconfirmed", "Passenger settlement was not confirmed", settled)
            # The action confirms closure and stable main internally; do not click before it.
            self._accept_settlement(batch, settled)
            if self.flyers_used >= self.target:
                self.stop_reason = "target_met"
            elif self.inventory is not None and self.inventory < 10:
                self.stop_reason = "flyers_exhausted"
            await self._emit("passenger_settlement", "completed", city_index, settlement=deepcopy(settled))
            plan = self._plan(city_index)
            await self._emit("passenger_planning", "completed", city_index, plan=deepcopy(plan))
            return {"page_state": "city_main", "triggered": True, "settlement": settled}
        except BaseException as exc:
            batch["error"] = self._error(exc)
            settled = (getattr(exc, "detail", {}) or {}).get("cancelled_operation_result", {})
            if settled.get("success") is True and settled.get("status") == "completed":
                self._accept_settlement(batch, settled)
            self.mark_failure(exc, city_index=city_index)
            await self._emit("passenger_settlement", "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                             city_index, error=self._error(exc))
            raise

    def _accept_settlement(self, batch, settled):
        batch.update(status="settled", settlement=deepcopy(settled))
        self.loaded_destination = None
        for key in self.revenues:
            value = settled.get(key)
            self._revenue_observations[key].append(value if type(value) is int else None)
            seen = self._revenue_observations[key]
            self.revenues[key] = sum(seen) if all(type(v) is int for v in seen) else None

    @staticmethod
    def _error(exc):
        return {"code": str(getattr(exc, "code", type(exc).__name__)), "message": str(exc),
                "detail": deepcopy(getattr(exc, "detail", {}) or {})}

    def mark_failure(self, exc, *, travel=None, city_index=None):
        self.failure = self._error(exc)
        self.stop_reason = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
        if self.loaded_destination is not None:
            self.requires_manual_completion = True
        if travel is not None:
            self.failure["travel"] = deepcopy(travel)
        if city_index is not None:
            self.failure["city_index"] = city_index
        return self.snapshot()

    def snapshot(self):
        plan = self.plans[-1] if self.plans else {}
        committed = [batch for batch in self.batches if type(batch.get("flyers_used")) is int and batch["flyers_used"] > 0]
        last_start = max((batch["trip"]["from_index"] for batch in committed), default=-1)
        expected_revenue = sum(batch["trip"].get("expected_revenue", 0) for batch in committed)
        expected_revenue += sum(trip.get("expected_revenue", 0) for trip in plan.get("trips", [])
                                if trip["from_index"] > last_start)
        return deepcopy({"enabled": True, "target": self.target, "target_flyers": self.target, "flyers_used": self.flyers_used,
                         "remaining_target": max(self.target - self.flyers_used, 0),
                         "initial_inventory": self.initial_inventory, "inventory": self.inventory,
                         "loaded_destination": self.loaded_destination,
                         "requires_manual_completion": self.requires_manual_completion,
                         "completed_batches": sum(b["status"] == "settled" for b in self.batches),
                         "batches": self.batches, "plans": self.plans, "plan": plan,
                         "planned_trips": plan.get("trips", []), "expected_revenue": expected_revenue,
                         "expected_remaining_revenue": plan.get("expected_revenue"),
                         "stop_reason": self.stop_reason, "failure": self.failure,
                         "revenue_reading_enabled": False, **self.revenues})

    def finish(self):
        if self.loaded_destination is not None:
            self.requires_manual_completion = True
            self.stop_reason = self.stop_reason or "unsettled_batch"
        elif self.stop_reason is None:
            self.stop_reason = "target_met" if self.flyers_used >= self.target else "route_exhausted"
        return self.snapshot()
