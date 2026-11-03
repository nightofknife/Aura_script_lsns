"""Passenger batches follow freight visits, with consumption receipts across failure."""
import asyncio
from copy import deepcopy
from threading import Event
from types import SimpleNamespace as NS

import pytest

from plans.resonance_pc.src.actions import _freight_passenger_session as m
from plans.resonance_pc.src.actions.passenger_pc_actions import PassengerPcError


ROUTE = [{"from_city": "A", "to_city": "B"}, {"from_city": "B", "to_city": "C"},
         {"from_city": "C", "to_city": "D"}]


@pytest.fixture
def runtime(monkeypatch):
    r = NS(operations=[], plans=[], events=[], known_balances=[], recruit_error=None, settlement_error=None,
           before=200, after=70, remaining=None, planned=True, inventory=200)
    async def worker(fn, *a, **k):
        return fn(*a, **k)
    async def ensure_main(page_state, app, vision):
        r.operations.append(("main", page_state))
        return "city_main"
    async def emit(stage, state, **fields):
        r.events.append((stage, state, fields))
    def planner(route, options, **kw):
        r.plans.append(deepcopy(kw))
        trips = []
        if r.planned and kw["target_remaining"] > 0 and (kw["inventory"] is None or kw["inventory"] >= 10):
            start = kw["start_index"]
            if start < 3:
                end = 2 if start < 2 else 3
                trips = [{"from_index": start, "to_index": end, "from_city": "ABC"[start],
                          "to_city": "ABCD"[end], "from_city_id": str(start), "to_city_id": str(end)}]
        return {"trips": trips, "target": kw["target_remaining"]}
    def inventory(*a):
        r.operations.append(("inventory",))
        return {"success": True, "page_state": "city_main", "flyers_remaining": r.inventory}
    def recruit(destination, **kw):
        r.known_balances.append(kw['known_flyers_before'])
        r.operations.append(("recruit", destination, kw["read_passenger_count"]))
        if r.recruit_error == "before":
            raise PassengerPcError("destination_not_found", "not found")
        kw["submission_observer"]()
        if r.recruit_error == "after":
            raise PassengerPcError("flyer_balance_unreadable", "unreadable")
        if r.recruit_error == "cancel":
            raise asyncio.CancelledError("cancel after submit")
        if r.recruit_error == "cleanup":
            raise PassengerPcError("main_screen_not_restored", "cleanup failed", {
                "recruitment": {"submission_state": "result_confirmed", "flyers_before": 200,
                                "flyers_remaining": 70, "flyers_used": 130}})
        return {"success": True, "page_state": "city_main", "status": "completed",
                "success_marker_confirmed": True, "flyers_before": r.before,
                "flyers_remaining": r.after, "flyers_used": r.before-r.after}
    def settlement(**kw):
        r.operations.append(("settle",))
        if r.settlement_error:
            raise r.settlement_error
        return {"success": True, "status": "completed", "ticket_revenue": 120,
                "extra_revenue": None, "total_revenue": None}
    def cleanup(*a, **k):
        r.operations.append(("cleanup",))
        return {"success": True}
    monkeypatch.setattr(m, "tracked_to_thread", worker)
    monkeypatch.setattr(m, "build_passenger_plan", planner)
    monkeypatch.setattr(m, "_read_flyer_inventory", inventory)
    monkeypatch.setattr(m, "_recruit_passengers_by_flyer", recruit)
    monkeypatch.setattr(m, "_return_recruitment_pages_to_main", cleanup)
    monkeypatch.setattr(m, "resonance_pc_enter_city_and_settle_passengers", settlement)
    r.session = lambda **kw: m.FreightPassengerSession(deepcopy(ROUTE),
        {"seat_capacity": 100, "flyer_target": 100}, emit=emit, **kw)
    r.call = lambda fn, *args, page_state="city_main": asyncio.run(fn(*args, page_state=page_state,
        app=object(), ocr=object(), vision=object(), ensure_main=ensure_main))
    return r


def test_cross_station_batch_never_recruits_or_settles_in_the_middle(runtime):
    s = runtime.session(inventory=200)
    runtime.call(s.before_departure, 0)
    runtime.call(s.after_arrival, 1)
    runtime.call(s.before_departure, 1)
    assert not any(op[0] == "settle" for op in runtime.operations)
    assert sum(op[0] == "recruit" for op in runtime.operations) == 1
    assert s.snapshot()["loaded_destination"]["city_index"] == 2
    runtime.call(s.after_arrival, 2)
    result = s.finish()
    assert result["loaded_destination"] is None and result["completed_batches"] == 1
    assert result["stop_reason"] == "target_met"
    assert result["flyers_used"] == 130  # whole max batch may exceed the target
    assert result["extra_revenue"] is result["total_revenue"] is None
    assert result["ticket_revenue"] == 120
    assert ROUTE == s.route


def test_initial_inventory_is_read_once_and_missing_cannot_become_zero(runtime):
    s = runtime.session()
    runtime.call(s.initialize)
    runtime.call(s.initialize)
    assert runtime.operations.count(("inventory",)) == 1
    assert s.inventory == 200 and s.initial_inventory == 200
    runtime.inventory = None
    other = runtime.session()
    with pytest.raises(PassengerPcError, match="flyer_balance_unreadable"):
        runtime.call(other.initialize)
    assert other.inventory is None


def test_multiple_batches_reuse_previous_confirmed_balance_without_reinitializing(runtime):
    s = m.FreightPassengerSession(deepcopy(ROUTE), {'seat_capacity':100, 'flyer_target':500})
    runtime.call(s.before_departure, 0)
    runtime.call(s.after_arrival, 2)
    runtime.before, runtime.after = 70, 20
    runtime.call(s.before_departure, 2)
    assert runtime.operations.count(('inventory',)) == 1
    assert runtime.known_balances == [200, 70]
    assert s.inventory == 20 and s.flyers_used == 180


def test_no_viable_interval_never_opens_passenger_ui(runtime):
    runtime.planned = False
    s = runtime.session()
    result = runtime.call(s.initialize)
    assert result["page_state"] == "city_main" and runtime.operations == []
    assert s.finish()["stop_reason"] == "route_exhausted"


def test_initial_insufficient_inventory_is_normal_stop(runtime):
    runtime.inventory = 9
    s = runtime.session()
    runtime.call(s.before_departure, 0)
    assert s.finish()["stop_reason"] == "flyers_exhausted"
    assert not any(op[0] == "recruit" for op in runtime.operations)


def test_safe_presubmit_miss_restores_main_then_allows_future_city(runtime):
    s = runtime.session(inventory=200)
    runtime.recruit_error = "before"
    result = runtime.call(s.before_departure, 0)
    assert result["triggered"] is False and result["page_state"] == "city_main"
    assert s.loaded_destination is None and s.requires_manual_completion is False
    assert ("cleanup",) in runtime.operations
    runtime.recruit_error = None
    runtime.call(s.before_departure, 1)
    assert s.batches[-1]["status"] == "onboard"


@pytest.mark.parametrize("failure,exception", [("after", PassengerPcError), ("cancel", asyncio.CancelledError)])
def test_submitted_unknown_or_cancelled_is_durable_and_never_repeats(runtime, failure, exception):
    s = runtime.session(inventory=200)
    runtime.recruit_error = failure
    with pytest.raises(exception):
        runtime.call(s.before_departure, 0)
    snap = s.snapshot()
    assert snap["requires_manual_completion"] is True
    assert snap["loaded_destination"]["state"] == "submitted_unknown"
    with pytest.raises(PassengerPcError, match="passenger_state_uncertain"):
        runtime.call(s.before_departure, 0)
    assert sum(op[0] == "recruit" for op in runtime.operations) == 1


def test_consumed_flyers_are_retained_if_main_cleanup_fails(runtime):
    s = runtime.session(inventory=200)
    runtime.recruit_error = "cleanup"
    with pytest.raises(PassengerPcError):
        runtime.call(s.before_departure, 0)
    snap = s.snapshot()
    assert snap["flyers_used"] == 130 and snap["inventory"] == 70
    assert snap["requires_manual_completion"] is True
    assert snap["batches"][0]["partial_receipt"]["flyers_used"] == 130


def test_settlement_failure_keeps_onboard_batch_and_blocks_new_work(runtime):
    s = runtime.session(inventory=200)
    runtime.call(s.before_departure, 0)
    runtime.settlement_error = PassengerPcError("settlement_dismiss_failed", "unknown closure")
    with pytest.raises(PassengerPcError):
        runtime.call(s.after_arrival, 2)
    snap = s.finish()
    assert snap["loaded_destination"]["city_index"] == 2
    assert snap["requires_manual_completion"] is True
    assert snap["completed_batches"] == 0


def test_replanning_uses_actual_remaining_inventory_and_target(runtime):
    s = runtime.session(inventory=200)
    runtime.before, runtime.after = 200, 170
    runtime.call(s.before_departure, 0)
    runtime.call(s.after_arrival, 2)
    runtime.before, runtime.after = 170, 50
    runtime.call(s.before_departure, 2)
    assert runtime.plans[-1]["inventory"] == 170
    assert runtime.plans[-1]["target_remaining"] == 70
    assert runtime.plans[-1]["start_index"] == 2
    assert s.flyers_used == 150


def test_target_is_resolved_once_at_session_creation(runtime, monkeypatch):
    targets = iter([77, 999])
    monkeypatch.setattr(m, "resolve_flyer_target", lambda _: next(targets))
    s = runtime.session(inventory=200)
    runtime.call(s.initialize)
    runtime.call(s.before_departure, 0)
    assert s.target == 77 and s.options["flyer_target"] == 77
    assert next(targets) == 999


def test_failure_receipt_preserves_travel_and_pending_batch(runtime):
    s = runtime.session(inventory=200)
    runtime.call(s.before_departure, 0)
    snap = s.mark_failure(PassengerPcError("arrival_failed", "failed"), travel={"success": True}, city_index=1)
    assert snap["failure"]["travel"] == {"success": True}
    assert snap["requires_manual_completion"] is True and snap["loaded_destination"]


def test_skipped_visit_stages_and_progress_snapshots_are_emitted(runtime):
    s = runtime.session(inventory=200)
    runtime.call(s.before_departure, 0)
    runtime.call(s.after_arrival, 1)
    runtime.call(s.before_departure, 1)
    assert ("passenger_settlement", "skipped") in [(x[0], x[1]) for x in runtime.events]
    assert ("passenger_recruit", "skipped") in [(x[0], x[1]) for x in runtime.events]
    assert all("passenger" in fields["data"] for _, _, fields in runtime.events)


def test_finish_preserves_unsettled_batch(runtime):
    s = runtime.session(inventory=200)
    runtime.call(s.before_departure, 0)
    snap = s.finish()
    assert snap["requires_manual_completion"] is True
    assert snap["stop_reason"] == "unsettled_batch"
    assert snap["loaded_destination"]["city_name"] == "C"


def test_small_target_never_substitutes_for_actual_initial_inventory(runtime, monkeypatch):
    original = m.build_passenger_plan
    def preview_can_reject_small_inventory(route, options, **kw):
        if kw["inventory"] is None or kw["inventory"] < 10:
            return {"trips": []}
        return original(route, options, **kw)
    monkeypatch.setattr(m, "build_passenger_plan", preview_can_reject_small_inventory)
    s = m.FreightPassengerSession(ROUTE, {"seat_capacity": 100, "flyer_target": 1})
    runtime.call(s.before_departure, 0)
    assert ("inventory",) in runtime.operations
    assert s.initial_inventory == 200 and s.flyers_used == 130


def test_arbitrary_presubmit_observation_failure_is_not_swallowed(runtime, monkeypatch):
    def invalid(*a, **kw):
        raise PassengerPcError("passenger_template_observation_failed", "unknown pixels")
    monkeypatch.setattr(m, "_recruit_passengers_by_flyer", invalid)
    s = runtime.session(inventory=200)
    with pytest.raises(PassengerPcError, match="passenger_template_observation_failed"):
        runtime.call(s.before_departure, 0)
    assert ("cleanup",) not in runtime.operations


def test_intermediate_visit_confirms_main_without_settlement(runtime):
    s = runtime.session(inventory=200)
    runtime.call(s.before_departure, 0)
    runtime.operations.clear()
    runtime.call(s.after_arrival, 1, page_state="city_panel")
    assert runtime.operations == [("main", "city_panel")]


def test_task_cancel_while_worker_queued_joins_without_submitting(runtime, monkeypatch):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def queued_worker(fn, *args, **kwargs):
            entered.set()
            await release.wait()
            return fn(*args, **kwargs)
        monkeypatch.setattr(m, "tracked_to_thread", queued_worker)
        s = runtime.session(inventory=200)
        async def main(*a): return "city_main"
        task = asyncio.create_task(s.before_departure(0, page_state="city_main", app=object(),
                                                       ocr=object(), vision=object(), ensure_main=main))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
        assert s.loaded_destination is None and s.requires_manual_completion is False
        assert not any(row[0] == "recruit" for row in runtime.operations)
        assert runtime.events[-1][1] == "cancelled"
    asyncio.run(scenario())


def test_task_cancel_after_submit_joins_worker_and_publishes_manual_state(runtime, monkeypatch):
    entered, release, exited = Event(), Event(), Event()
    async def thread_worker(fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)
    def recruit(*a, **kwargs):
        kwargs["submission_observer"]()
        entered.set()
        try:
            assert release.wait(3)
            m._check_cancelled()
            pytest.fail("cancelled worker must not perform another input")
        finally:
            exited.set()
    monkeypatch.setattr(m, "tracked_to_thread", thread_worker)
    monkeypatch.setattr(m, "_recruit_passengers_by_flyer", recruit)
    async def scenario():
        s = runtime.session(inventory=200)
        async def main(*a): return "city_main"
        task = asyncio.create_task(s.before_departure(0, page_state="city_main", app=object(),
                                                       ocr=object(), vision=object(), ensure_main=main))
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
        assert exited.is_set()
        assert s.loaded_destination["state"] == "submitted_unknown"
        assert s.requires_manual_completion is True
        assert any(state == "submitted" and fields["data"]["passenger"]["loaded_destination"]
                   for _, state, fields in runtime.events)
        assert runtime.events[-1][1] == "cancelled"
        assert runtime.events[-1][2]["data"]["passenger"]["requires_manual_completion"] is True
    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["initialize", "settle"])
def test_cancel_at_completed_worker_return_preserves_observed_receipt(runtime, monkeypatch, operation):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed_result(fn, *args, **kwargs):
            result = fn(*args, **kwargs)
            entered.set()
            await release.wait()
            return result
        s = runtime.session(inventory=None if operation == "initialize" else 200)
        if operation == "settle":
            async def main(*a): return "city_main"
            await s.before_departure(0, page_state="city_main", app=object(), ocr=object(),
                                     vision=object(), ensure_main=main)
        monkeypatch.setattr(m, "tracked_to_thread", delayed_result)
        async def main(*a): return "city_main"
        kwargs = dict(page_state="city_main", app=object(), ocr=object(), vision=object(), ensure_main=main)
        task = asyncio.create_task(s.initialize(**kwargs) if operation == "initialize" else s.after_arrival(2, **kwargs))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
        assert s.requires_manual_completion is False
        if operation == "initialize":
            assert s.inventory == s.initial_inventory == 200
        else:
            assert s.loaded_destination is None and s.snapshot()["ticket_revenue"] == 120
            assert s.snapshot()["completed_batches"] == 1
    asyncio.run(scenario())
