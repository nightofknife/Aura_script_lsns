"""Offline per-city recovery session contracts; callbacks never operate the game."""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from plans.resonance_pc.src.actions._freight_recovery_policy import plan_threshold_water_use
from plans.resonance_pc.src.actions._freight_threshold_recovery import (
    FreightRecoverySession, RecoveryStopError,
)


@pytest.mark.parametrize("fatigue,start,stop,uses,count,reason", [
    (600, 600, 350, 6, 0, "below_start_threshold"),
    (599, 600, 350, 6, 0, "below_start_threshold"),
    (601, 600, 350, 6, 6, "selected"),
    (650, 600, 350, 6, 6, "selected"),
    (630, 600, 350, 6, 6, "selected"),
    (900, 600, 300, 3, 3, "selected"),
    (900, 600, 300, 0, 0, "no_remaining_free_uses"),
    (50, 40, 0, 6, 0, "positive_fatigue_guard"),
    (51, 40, 0, 6, 1, "selected"),
])
def test_water_threshold_boundaries(fatigue, start, stop, uses, count, reason):
    plan = plan_threshold_water_use(fatigue, start, stop, uses)
    assert plan["drink_count"] == count
    assert plan["reason"] == reason
    assert plan["immediate_fatigue"] == fatigue - 50 * count
    assert plan["planned"] is (count > 0)
    assert not {"remaining_consumption", "estimated_endpoint_fatigue", "reserve"} & plan.keys()


@pytest.mark.parametrize("fatigue,start,stop,uses", [
    (True, 600, 300, 6), (700, None, 300, 6), (700, 600, None, 6),
    (700, 600, 600, 6), (700, 600, 601, 6), (700, 600.0, 300, 6),
    (700, 600, True, 6), (700, 600, 300, False), (700, 600, 300, -1),
])
def test_water_threshold_invalid_inputs_are_rejected(fatigue, start, stop, uses):
    with pytest.raises(ValueError):
        plan_threshold_water_use(fatigue, start, stop, uses)


class Harness:
    def __init__(self, readings, *, free_uses=6, rest=True, child=None):
        self.readings = iter(readings)
        self.free_uses = free_uses
        self.rest = rest
        self.child = child
        self.calls = []
        self.emits = []

    async def refresh(self, page):
        self.calls.append(("refresh", page))
        return next(self.readings)

    async def drink(self, selection, page):
        self.calls.append(("water", page, deepcopy(selection)))
        count = selection["drink_count"]
        self.free_uses -= count
        return {"success": True, "status": "completed", "page_state": "city_panel",
                "requested_count": count, "completed_count": count, "recovered_fatigue": count * 50}

    async def consume(self, params, page):
        self.calls.append(("bento", page, deepcopy(params)))
        return deepcopy(self.child)

    async def emit(self, stage, state, **fields):
        self.emits.append((stage, state, deepcopy(fields)))

    def session(self, *, water=True, bento=True, water_start=600, water_stop=300,
                bento_start=700, bento_stop=200, travel_cost=None):
        return FreightRecoverySession(
            water_enabled=water, bento_enabled=bento,
            water_start=water_start, water_stop=water_stop,
            bento_start=bento_start, bento_stop=bento_stop,
            priority=["love_bentos", "work_meals"], refresh_fatigue=self.refresh,
            water_counts=lambda: (self.free_uses, 6), has_rest=lambda _city: self.rest,
            drink_water=self.drink, consume_bentos=self.consume, emit=self.emit,
            travel_cost=travel_cost)


def bento_result(*, start=700, stop=200, initial=750, final=180,
                 consumed=2, completed=2, success=True, status="completed", reason=None):
    return {"success": success, "status": status, "reason": reason,
            "initial_fatigue": initial, "final_fatigue": final,
            "bento_start_threshold": start, "bento_stop_threshold": stop,
            "consumed_count": consumed, "completed_count": completed,
            "work_meals_used": consumed, "love_bentos_used": 0,
            "recovered_fatigue": initial - final if final is not None else None,
            "page_state": "city_main" if success else "unknown"}


def check(session, **kwargs):
    return asyncio.run(session.check(city_name="A", city_index=0, page_state="city_panel", **kwargs))


def test_water_first_then_bento_uses_new_actual_fatigue_and_keeps_priority():
    h = Harness([900, 760], free_uses=3, child=bento_result(initial=760))
    session = h.session()
    result = check(session)
    assert [call[0] for call in h.calls] == ["refresh", "water", "refresh", "bento"]
    assert h.calls[1][1] == "city_main"
    assert h.calls[-1][1] == "city_main"
    assert h.calls[-1][2]["bento_priority"] == ["love_bentos", "work_meals"]
    assert result["recovery"]["initial_fatigue"] == 900
    assert result["recovery"]["final_fatigue"] == 180
    assert result["page_state"] == "city_main"
    assert session.summary()["sparkling_water"]["completed_count"] == 3


def test_water_actual_reading_controls_bento_skip_not_arithmetic_estimate():
    h = Harness([900, 650], free_uses=3)
    session = h.session()
    result = check(session)
    assert [call[0] for call in h.calls] == ["refresh", "water", "refresh"]
    assert result["recovery"]["bento_consumption"]["status"] == "skipped"
    assert result["recovery"]["final_fatigue"] == 650


@pytest.mark.parametrize("rest,uses", [(False, 6), (True, 0)])
def test_water_unavailable_never_delays_bento(rest, uses):
    h = Harness([750], free_uses=uses, rest=rest, child=bento_result())
    session = h.session()
    check(session)
    assert [call[0] for call in h.calls] == ["refresh", "bento"]
    assert session.summary()["sparkling_water"]["completed_count"] == 0
    assert session.summary()["bento_consumption"]["consumed_count"] == 2


def test_equal_start_skips_both_without_consumption():
    h = Harness([600])
    session = h.session(bento_start=600)
    check(session)
    assert [call[0] for call in h.calls] == ["refresh"]
    assert session.summary()["final_fatigue"] == 600


def test_equal_600_start_skips_both_in_fixed_water_then_bento_order():
    h = Harness([])
    session = h.session(water_stop=100, bento_start=600, bento_stop=100)
    observe_trade(session, 600)
    result = check(session)
    assert h.calls == []
    assert [(stage, state) for stage, state, _ in h.emits] == [
        ("sparkling_water", "skipped"), ("bento", "skipped")]
    assert "recovery_priority" not in result["recovery"]
    assert result["recovery"]["checked_order"] == ["sparkling_water", "bento"]
    assert result["recovery"]["fatigue_source"] == "trade_page"
    assert session.summary()["final_fatigue"] == 600


@pytest.mark.parametrize("water_final,expected_calls", [
    (600, ["water", "refresh"]), (601, ["water", "refresh", "bento"]),
])
def test_fixed_water_first_rechecks_bento_start_using_postwater_actual(water_final, expected_calls):
    h = Harness([water_final], free_uses=2, child=bento_result(
        start=600, stop=100, initial=water_final, final=81))
    session = h.session(water_stop=100, bento_start=600, bento_stop=100)
    observe_trade(session, 801)
    result = check(session)
    assert [call[0] for call in h.calls] == expected_calls
    assert result["recovery"]["checked_order"] == ["sparkling_water", "bento"]
    assert h.calls[0][2]["current_fatigue"] == 801
    assert h.calls[0][2]["drink_count"] == 2
    water = result["recovery"]["sparkling_water"]
    assert water["completed_count"] == 2 and water["final_fatigue"] == water_final
    if water_final > 600:
        assert result["recovery"]["bento_consumption"]["initial_fatigue"] == water_final
        assert session.summary()["fatigue_tracking"]["source"] == "bento"
        assert session.summary()["final_fatigue"] == 81
    else:
        assert result["recovery"]["bento_consumption"]["triggered"] is False
        assert session.summary()["fatigue_tracking"]["source"] == "water"
        assert session.summary()["final_fatigue"] == 600


def test_only_bento_start_is_met_so_water_check_skips_then_bento_runs():
    h = Harness([], child=bento_result(start=600, stop=100, initial=850, final=70))
    session = h.session(water_start=900, water_stop=100, bento_start=600, bento_stop=100)
    observe_trade(session, 850)
    result = check(session)
    assert [call[0] for call in h.calls] == ["bento"]
    assert result["recovery"]["checked_order"] == ["sparkling_water", "bento"]
    assert result["recovery"]["sparkling_water"]["triggered"] is False
    assert result["recovery"]["sparkling_water"]["current_fatigue"] == 850
    assert [(stage, state) for stage, state, _ in h.emits] == [
        ("sparkling_water", "skipped"), ("bento", "started"), ("bento", "completed")]
    assert h.free_uses == 6
    assert session.summary()["fatigue_tracking"]["source"] == "bento"
    assert session.summary()["final_fatigue"] == 70


def test_distinct_thresholds_allow_bento_in_one_city_and_water_after_later_travel():
    h = Harness([700], child=bento_result(start=600, stop=100, initial=850, final=100))
    session = h.session(water_start=900, water_stop=100, bento_start=600, bento_stop=100,
                        travel_cost=lambda _leg: 900)
    observe_trade(session, 850)
    first = check(session)
    assert [call[0] for call in h.calls] == ["bento"]
    assert first["recovery"]["sparkling_water"]["triggered"] is False
    cost = session.begin_travel({"from_city_id": "1", "to_city_id": "2"})
    session.observe_travel({"success": True, "status": "ok"}, cost)
    h.child = bento_result(start=600, stop=100, initial=700, final=100)
    second = asyncio.run(session.check(city_name="B", city_index=1, page_state="city_main"))
    assert [call[0] for call in h.calls] == ["bento", "water", "refresh", "bento"]
    assert h.calls[1][2]["current_fatigue"] == 1000
    assert h.calls[1][2]["fatigue_source"] == "route_derived"
    assert second["recovery"]["initial_fatigue"] == 1000
    assert second["recovery"]["bento_consumption"]["initial_fatigue"] == 700
    assert all(event["checked_order"] == ["sparkling_water", "bento"] for event in session.events)
    summary = session.summary()
    assert summary["sparkling_water"]["completed_count"] == 6
    assert summary["bento_consumption"]["consumed_count"] == 4
    assert summary["final_fatigue"] == 100
    assert summary["fatigue_tracking"]["source"] == "bento"


def test_bento_failure_after_successful_water_keeps_water_receipt_and_partial_bento_count():
    h = Harness([750], free_uses=2, child=bento_result(
        start=600, stop=100, initial=750, final=650, consumed=2, completed=1,
        success=False, status="failed", reason="rating_failed"))
    session = h.session(water_stop=100, bento_start=600, bento_stop=100)
    observe_trade(session, 850)
    with pytest.raises(RecoveryStopError, match="rating_failed"):
        check(session)
    assert [call[0] for call in h.calls] == ["water", "refresh", "bento"]
    summary = session.summary()
    assert summary["sparkling_water"]["success"] is True
    assert summary["sparkling_water"]["completed_count"] == 2
    assert summary["bento_consumption"]["success"] is False
    assert summary["bento_consumption"]["consumed_count"] == 2
    assert summary["bento_consumption"]["completed_count"] == 1
    assert summary["final_fatigue"] == 650
    assert summary["fatigue_tracking"]["source"] == "bento"
    assert summary["recovery_events"][0]["checked_order"] == ["sparkling_water", "bento"]


@pytest.mark.parametrize("water,bento,expected", [
    (True, False, ["water", "refresh"]), (False, True, ["bento"]),
])
def test_disabled_resource_does_not_block_enabled_resource(water, bento, expected):
    h = Harness([550], child=bento_result(start=600, stop=100, initial=850, final=70))
    session = h.session(water=water, bento=bento, water_stop=100,
                        bento_start=600, bento_stop=100)
    observe_trade(session, 850)
    check(session)
    assert [call[0] for call in h.calls] == expected
    assert session.summary()["final_fatigue"] == (550 if water else 70)


def observe_trade(session, fatigue):
    session.accept_trade_reading({"success": True, "source": "trade_page",
                                 "current_fatigue": fatigue, "max_fatigue": 1000})


def test_confirmed_trade_reading_keeps_city_panel_without_profile_navigation():
    h = Harness([])
    session = h.session()
    observe_trade(session, 600)
    result = check(session)
    assert result["page_state"] == "city_panel"
    assert h.calls == []
    assert result["recovery"]["initial_fatigue_source"] == "trade_page"
    assert session.summary()["final_fatigue"] == 600
    assert session.summary()["estimated_fatigue"] is None


def test_confirmed_trade_fatigue_routes_bento_from_current_panel_without_extra_profile():
    h = Harness([], rest=False, child=bento_result())
    session = h.session()
    observe_trade(session, 750)
    result = check(session)
    assert [call[0] for call in h.calls] == ["bento"]
    assert h.calls[0][1] == "city_panel"
    assert result["page_state"] == "city_main"
    assert session.summary()["fatigue_tracking"]["source"] == "bento"
    assert session.summary()["final_fatigue"] == 180


def test_route_derived_fatigue_triggers_recovery_without_presenting_it_as_observed():
    h = Harness([])
    session = h.session(travel_cost=lambda _leg: 100)
    observe_trade(session, 500)
    cost = session.begin_travel({"from_city_id": "1", "to_city_id": "2"})
    assert session.summary()["final_fatigue"] is None
    session.observe_travel({"success": True, "status": "ok"}, cost)
    result = check(session)
    assert not h.calls
    assert result["recovery"]["initial_fatigue"] == 600
    assert result["recovery"]["initial_fatigue_source"] == "route_derived"
    summary = session.summary()
    assert summary["final_fatigue"] is None and summary["estimated_fatigue"] == 600
    assert summary["fatigue_tracking"]["last_observed_fatigue"] == 500


def test_cancel_during_route_cost_lookup_is_propagated_before_travel_starts():
    h = Harness([])
    cancelled = RecoveryStopError("trade_cancelled", {"stage": "cost_lookup"})
    def travel_cost(_leg):
        raise cancelled
    session = h.session(travel_cost=travel_cost)
    observe_trade(session, 500)
    with pytest.raises(RecoveryStopError) as raised:
        session.begin_travel({"from_city_id": "1", "to_city_id": "2"})
    assert raised.value is cancelled
    assert session.summary()["fatigue_tracking"]["source"] == "trade_page"
    assert session.summary()["final_fatigue"] == 500
    assert not h.calls


def test_missing_route_data_value_error_requires_arrival_refresh_without_failing_departure():
    h = Harness([620])
    def travel_cost(_leg):
        raise ValueError("route fatigue unavailable")
    session = h.session(water=False, travel_cost=travel_cost)
    observe_trade(session, 500)
    cost = session.begin_travel({"from_city_id": "1", "to_city_id": "2"})
    assert cost is None and session.summary()["final_fatigue"] is None
    session.observe_travel({"success": True, "status": "ok"}, cost)
    result = check(session)
    assert h.calls == [("refresh", "city_panel")]
    assert result["recovery"]["initial_fatigue"] == 620
    assert result["recovery"]["initial_fatigue_source"] == "profile"


def test_derived_bento_started_event_labels_calculated_fatigue_without_actual_field():
    h = Harness([], child=bento_result(initial=750))
    session = h.session(water=False, travel_cost=lambda _leg: 100)
    observe_trade(session, 650)
    cost = session.begin_travel({"from_city_id": "1", "to_city_id": "2"})
    session.observe_travel({"success": True, "status": "ok"}, cost)
    check(session)
    started = next(fields for stage, state, fields in h.emits
                   if stage == "bento" and state == "started")
    data = started["data"]
    assert "actual_fatigue" not in data
    assert data["current_fatigue"] == 750 and data["fatigue_source"] == "route_derived"
    assert data["fatigue_tracking"]["last_observed_fatigue"] == 650
    assert [call[0] for call in h.calls] == ["bento"]


def test_water_actual_reading_can_equal_route_estimate_and_still_calibrate_then_run_bento():
    h = Harness([750], free_uses=3, child=bento_result(initial=750))
    session = h.session(travel_cost=lambda _leg: 100)
    observe_trade(session, 650)
    cost = session.begin_travel({"from_city_id": "1", "to_city_id": "2"})
    session.observe_travel({"success": True, "status": "ok"}, cost)
    result = check(session)
    assert [call[0] for call in h.calls] == ["water", "refresh", "bento"]
    assert h.calls[0][2]["current_fatigue"] == 750
    assert h.calls[0][2]["fatigue_source"] == "route_derived"
    assert result["recovery"]["initial_fatigue_source"] == "route_derived"
    assert result["recovery"]["sparkling_water"]["completed_count"] == 3
    assert result["recovery"]["sparkling_water"]["final_fatigue"] == 750
    assert result["recovery"]["bento_consumption"]["initial_fatigue"] == 750
    summary = session.summary()
    assert summary["sparkling_water"]["success"] is True
    assert summary["sparkling_water"]["completed_count"] == 3
    assert summary["sparkling_water"]["recovered_fatigue"] == 150
    assert summary["bento_consumption"]["consumed_count"] == 2
    assert summary["final_fatigue"] == 180
    assert summary["fatigue_tracking"]["last_observed_source"] == "bento"


@pytest.mark.parametrize("invalid", [{"consumed_count": True}, {"bento_start_threshold": 999}])
def test_invalid_bento_contract_never_registers_returned_fatigue_as_an_actual_reading(invalid):
    child = bento_result(initial=750, final=180)
    child.update(invalid)
    h = Harness([], rest=False, child=child)
    session = h.session(water=False)
    observe_trade(session, 750)
    with pytest.raises(RecoveryStopError, match="bento_result_invalid"):
        check(session)
    summary = session.summary()
    assert summary["final_fatigue"] is None and summary["estimated_fatigue"] is None
    assert summary["fatigue_tracking"]["current_fatigue"] is None
    assert summary["fatigue_tracking"]["last_observed_fatigue"] == 750
    assert summary["fatigue_tracking"]["last_observed_source"] == "trade_page"


def test_invalid_trade_observation_forgets_prior_value_then_refreshes_once():
    h = Harness([710], rest=False, child=bento_result(initial=710))
    session = h.session()
    observe_trade(session, 500)
    session.accept_trade_reading({"success": False, "current_fatigue": 500,
                                 "max_fatigue": 1000, "source": "trade_page"})
    result = check(session)
    assert [call[0] for call in h.calls] == ["refresh", "bento"]
    assert result["recovery"]["initial_fatigue"] == 710


def test_post_water_observation_is_reused_at_next_unchanged_checkpoint():
    h = Harness([450])
    session = h.session(bento=False)
    observe_trade(session, 750)
    first = check(session)
    second = check(session, phase="pre_departure")
    assert [call[0] for call in h.calls] == ["water", "refresh"]
    assert h.calls[0][1] == "city_panel"
    assert first["recovery"]["final_fatigue"] == second["recovery"]["final_fatigue"] == 450
    assert session.summary()["fatigue_tracking"]["source"] == "water"


def test_post_bento_observation_is_reused_at_next_unchanged_checkpoint():
    h = Harness([], rest=False, child=bento_result())
    session = h.session(water=False)
    observe_trade(session, 750)
    check(session)
    check(session, phase="pre_departure")
    assert [call[0] for call in h.calls] == ["bento"]
    assert session.summary()["fatigue_tracking"]["source"] == "bento"
    assert session.summary()["final_fatigue"] == 180


def test_fresh_bento_skip_above_stop_is_valid_without_consumption():
    h = Harness([750], rest=False, child=bento_result(
        initial=690, final=690, consumed=0, completed=0, status="skipped"))
    session = h.session()
    result = check(session)
    assert result["recovery"]["success"] is True
    assert result["recovery"]["final_fatigue"] == 690
    assert session.summary()["bento_consumption"]["consumed_count"] == 0


@pytest.mark.parametrize("final", [None, True, -1])
def test_invalid_bento_final_never_returns_parent_preconsumption_reading(final):
    h = Harness([750], rest=False, child=bento_result(final=final))
    session = h.session()
    with pytest.raises(RecoveryStopError, match="bento_final_fatigue_unconfirmed"):
        check(session)
    assert session.summary()["final_fatigue"] is None
    assert session.summary()["bento_consumption"]["consumed_count"] == 2


def test_failed_bento_preserves_partial_counts_and_latest_actual_fatigue():
    h = Harness([750], rest=False, child=bento_result(
        initial=750, final=550, consumed=2, completed=1, success=False,
        status="failed", reason="score_failed"))
    session = h.session()
    with pytest.raises(RecoveryStopError, match="score_failed"):
        check(session)
    summary = session.summary()
    assert summary["final_fatigue"] == 550
    assert summary["bento_consumption"]["consumed_count"] == 2
    assert summary["bento_consumption"]["completed_count"] == 1
    assert summary["bento_consumption"]["success"] is False


def test_partial_water_error_preserves_confirmed_cups_and_clears_stale_fatigue():
    h = Harness([900])
    async def partial(_selection, _page):
        raise RecoveryStopError("water_confirmation_failed", {"completed_count": 2})
    h.drink = partial
    session = h.session()
    with pytest.raises(RecoveryStopError, match="water_confirmation_failed"):
        check(session)
    summary = session.summary()
    assert summary["final_fatigue"] is None
    assert summary["sparkling_water"]["completed_count"] == 2
    assert summary["sparkling_water"]["recovered_fatigue"] == 100
    assert not any(call[0] == "bento" for call in h.calls)


def test_multi_city_aggregation_does_not_reuse_old_consumption_or_expose_mutable_events():
    h = Harness([750, 450], child=bento_result(initial=850))
    session = h.session()
    check(session)
    # No new fatigue increase: a later check does not replay the first water batch.
    asyncio.run(session.check(city_name="A", city_index=0, page_state="city_main", phase="pre_departure"))
    h.rest = False
    session.accept_trade_reading({"success": True, "current_fatigue": 850,
                                 "max_fatigue": 1000, "source": "trade_page"})
    asyncio.run(session.check(city_name="B", city_index=1, page_state="city_panel"))
    summary = session.summary()
    assert len(summary["recovery_events"]) == 3
    assert summary["sparkling_water"]["completed_count"] == 6
    assert summary["bento_consumption"]["consumed_count"] == 2
    assert summary["final_fatigue"] == 180
    assert sum(call[0] == "water" for call in h.calls) == 1
    summary["recovery_events"][0]["city_name"] = "mutated"
    assert session.events[0]["city_name"] == "A"


def test_disabled_session_does_not_read_fatigue_or_change_rest_result():
    h = Harness([])
    session = h.session(water=False, bento=False)
    async def rest(selection, page):
        assert selection is None and page == "city_panel"
        return {"page_state": "city_main", "black_moon_local_purchase": {"success": True}}
    result = check(session, rest_stop=rest)
    assert result["black_moon_local_purchase"] == {"success": True}
    assert not h.calls
    assert session.summary()["recovery_events"] == []


@pytest.mark.parametrize("consumed,completed,status", [(0, 0, "skipped"), (2, 2, "completed")])
def test_no_available_meals_is_normal_resource_exhaustion_with_actual_fatigue(consumed, completed, status):
    h = Harness([750], rest=False, child=bento_result(
        final=550, consumed=consumed, completed=completed, status=status,
        reason="no_available_meals"))
    session = h.session()
    result = check(session)
    assert result["recovery"]["success"] is True
    assert result["recovery"]["bento_consumption"]["reached_stop_threshold"] is False
    assert result["recovery"]["final_fatigue"] == 550
    assert session.summary()["bento_consumption"]["consumed_count"] == consumed


def test_predeparture_recovery_failure_keeps_confirmed_trade_and_book_usage(monkeypatch):
    from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade

    city_trade = {"success": True, "page_state": "city_main",
                  "buy": {"book_result": {"used": 2}, "success": True},
                  "sell": {"success": True, "sold_confirmed": True}}
    async def execute_city_trade(**_kwargs):
        return deepcopy(city_trade)
    monkeypatch.setattr(trade, "_execute_freight_city_trade", execute_city_trade)
    monkeypatch.setattr(trade, "_check_trade_cancelled", lambda: None)
    async def init(**_kwargs):
        return {"run_key": "offline-threshold-failure"}
    async def summary(*_args, **_kwargs):
        return {"status": "ok", "expected_fatigue_used": 0, "completed_leg_count": 0}
    async def cleanup(*_args, **_kwargs):
        return None
    monkeypatch.setattr(trade, "resonance_pc_trade_route_execution_init", init)
    monkeypatch.setattr(trade, "resonance_pc_trade_route_execution_summary", summary)
    monkeypatch.setattr(trade, "resonance_pc_trade_route_execution_cleanup", cleanup)
    monkeypatch.setattr(trade, "resonance_pc_intercity_depart_and_wait",
                        lambda **_kwargs: pytest.fail("must not depart after failed recovery"))
    class Session:
        def invalidate_fatigue(self):
            pass
        def accept_trade_reading(self, observation):
            pass
        def begin_travel(self, leg):
            pytest.fail("must not begin travel after failed recovery")
        def observe_travel(self, travel, cost):
            pytest.fail("must not observe travel after failed recovery")
        async def check(self, **kwargs):
            if kwargs["phase"] == "start":
                return {"page_state": "city_panel"}
            raise RecoveryStopError("bento_observation_failed", {"consumed_count": 1})
    result = asyncio.run(trade._execute_route(
        route=[{"from_city": "A", "to_city": "B", "from_city_id": "1", "to_city_id": "2",
                "buy_products": ["goods"], "books_used": 2}],
        start_page_state="city_panel", use_fatigue_medicine=False,
        allowed_fatigue_medicines=[], fatigue_medicine_max_uses=0,
        app=object(), ocr=object(), vision=object(), city_shop_data=object(),
        state_store=object(), recovery_session=Session()))
    assert result["status"] == "failed"
    assert result["reason"] == "bento_observation_failed"
    assert result["page_state"] == "unknown"
    assert result["leg_results"][0]["city_trade"] == city_trade
    assert result["leg_results"][0]["city_trade"]["buy"]["book_result"]["used"] == 2
    assert result["error"]["detail"]["consumed_count"] == 1


def test_freight_finalization_preserves_real_recovery_and_original_result():
    from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade
    source = {"success": True, "status": "completed", "page_state": "city_main",
              "final_fatigue": 251, "expected_fatigue_used": 333,
              "resources": {"actual_fatigue": 251},
              "sparkling_water": {"completed_count": 3, "visits": [{"city_name": "A"}]},
              "bento_consumption": {"consumed_count": 2, "final_fatigue": 251},
              "recovery_events": [{"final_fatigue": 251}],
              "execution": {"status": "completed", "completed_leg_count": 2}}
    before = deepcopy(source)
    result = asyncio.run(trade.resonance_pc_finish_auto_cycle_trade(source))
    assert result == before
    assert source == before
    assert result is not source


def test_successful_water_with_unchanged_fatigue_stops_before_bento():
    h = Harness([900, 900])
    session = h.session()
    with pytest.raises(RecoveryStopError, match="sparkling_water_fatigue_not_decreased"):
        check(session)
    summary = session.summary()
    assert summary["final_fatigue"] == 900
    assert summary["sparkling_water"]["completed_count"] == 6
    assert not any(call[0] == "bento" for call in h.calls)


def test_successful_bento_cannot_leave_an_uncompleted_portion():
    h = Harness([750], child=bento_result(consumed=2, completed=1))
    session = h.session(water=False)
    with pytest.raises(RecoveryStopError, match="bento_recovery_incomplete"):
        check(session)
    assert session.summary()["bento_consumption"]["consumed_count"] == 2
    assert session.summary()["bento_consumption"]["completed_count"] == 1


@pytest.mark.parametrize("typed_error", [True, False])
def test_shared_rest_return_failure_preserves_confirmed_water_cups(monkeypatch, typed_error):
    from types import SimpleNamespace
    from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade

    calls = []
    monkeypatch.setattr(trade, "_check_trade_cancelled", lambda: None)
    monkeypatch.setattr(trade, "_black_moon_purchase_gate", lambda **_kwargs: {
        "success": True, "triggered": True, "status": "pending", "city_key": "a"})
    monkeypatch.setattr(trade, "_retain_black_moon_purchase_receipt", lambda *_args, **_kwargs: None)
    async def capture(*_args, **_kwargs):
        return None
    monkeypatch.setattr(trade, "capture_failure", capture)
    async def open_city(*_args, **_kwargs):
        return {"success": True, "page_state": "city_panel"}
    monkeypatch.setattr(trade, "_call_recovery_action", open_city)
    monkeypatch.setattr(trade, "load_sparkling_water_layout", lambda _vision: {})
    class SharedRest:
        def __init__(self, **_kwargs):
            self.completed = 0
        async def enter_rest(self, _point):
            calls.append("enter_rest")
        async def return_to_city(self, _reader, _key):
            calls.append("return_failure")
            if typed_error:
                raise trade.CityTradeFlowError("city_not_restored", "return failed", {})
            raise RuntimeError("return failed")
    monkeypatch.setattr(trade, "SparklingWaterSession", SharedRest)
    async def purchase(**_kwargs):
        calls.append("purchase")
        return {"success": True, "status": "completed", "page_state": "rest_menu"}
    monkeypatch.setattr(trade, "_run_black_moon_local_purchase_task", purchase)
    async def drink(*, session, drink_count, **_kwargs):
        assert drink_count == 2
        calls.append("water")
        session.completed = 2
        return {"success": True, "page_state": "rest_menu", "requested_count": 2, "completed_count": 2}
    monkeypatch.setattr(trade, "_drink_sparkling_water_from_rest_menu", drink)
    h = Harness([750], free_uses=2)
    session = h.session(bento=False)
    cities = SimpleNamespace(resolve_shop_point=lambda **_kwargs: {"city_name": "A", "city_key": "a"})
    async def rest(selection, page):
        return await trade._execute_freight_rest_stop(
            city_name="A", city_index=0, page_state=page,
            purchase_table={"order_book": True}, record_scope="default", catalog={},
            purchase_visits=[], progress_context={}, water_selection=selection,
            route=[], travel_costs=None, context=object(), engine=object(),
            app=object(), ocr=object(), vision=object(), city_shop_data=cities,
            persistent_data=object())
    with pytest.raises(trade.CityTradeFlowError) as raised:
        check(session, rest_stop=rest)
    assert raised.value.detail["requested_count"] == 2
    assert raised.value.detail["completed_count"] == 2
    assert raised.value.detail["sparkling_water"]["completed_count"] == 2
    assert calls == ["enter_rest", "purchase", "water", "return_failure"]
    summary = session.summary()
    assert summary["sparkling_water"]["success"] is False
    assert summary["sparkling_water"]["completed_count"] == 2
    assert summary["sparkling_water"]["recovered_fatigue"] == 100
    assert summary["final_fatigue"] is None



def test_invalidation_clears_current_fatigue_but_keeps_observation_history():
    h = Harness([600, 500])
    session = h.session()
    check(session)
    assert session.final_fatigue == 600
    history = deepcopy(session.events)
    session.invalidate_fatigue()
    summary = session.summary()
    assert session.final_fatigue is None
    assert summary["final_fatigue"] is None
    assert summary["recovery_events"] == history
    assert summary["recovery_events"][0]["final_fatigue"] == 600
    # A later actual observation becomes the new current value.
    asyncio.run(session.check(city_name="B", city_index=1, page_state="city_main"))
    assert session.summary()["final_fatigue"] == 500
    assert session.events[0]["final_fatigue"] == 600


def test_reposition_arrival_recovery_failure_keeps_confirmed_travel_and_trailer_receipt(monkeypatch):
    from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade
    monkeypatch.setattr(trade, "_check_trade_cancelled", lambda: None)
    travel = {"success": True, "status": "completed", "page_state": "city_main",
              "trailer_used": True, "fatigue_medicine_use_count": 0}
    calls = []
    class Session:
        def invalidate_fatigue(self):
            calls.append("invalidate")
        def begin_travel(self, leg):
            calls.append("begin_travel")
            return 100
        def observe_travel(self, result, cost):
            assert result == travel and cost == 100
            calls.append("observe_travel")
        async def check(self, **kwargs):
            calls.append(kwargs["phase"])
            if kwargs["phase"] == "arrival":
                raise RecoveryStopError("recovery_fatigue_invalid", {})
            return {"page_state": "city_main"}
    class Trailer:
        daily_first_n = 1
        payment = "gold"
        def should_use(self, scope, index):
            assert scope == "reposition" and index == 0
            return True
        def observe(self, scope, index, result):
            assert scope == "reposition" and index == 0 and result == travel
            calls.append("trailer_receipt")
    async def tracked(function, **kwargs):
        assert function is trade.resonance_pc_intercity_depart_and_wait
        assert kwargs["use_trailer"] is True
        calls.append("travel")
        return deepcopy(travel)
    monkeypatch.setattr(trade, "tracked_to_thread", tracked)
    leg = {"from_city": "A", "to_city": "B", "from_city_id": "1", "to_city_id": "2"}
    result = asyncio.run(trade._execute_freight_reposition(
        [leg], page_state="city_main", app=object(), ocr=object(), vision=object(),
        arrival_timeout_seconds=30, use_fatigue_medicine=False,
        allowed_fatigue_medicines=[], fatigue_medicine_max_uses=0,
        trailer_plan=Trailer(), recovery_session=Session()))
    assert calls[0] == "pre_departure" and calls[-1] == "arrival"
    assert calls.index("begin_travel") < calls.index("travel")
    assert calls.index("travel") < calls.index("trailer_receipt") < calls.index("arrival")
    assert calls.index("travel") < calls.index("observe_travel") < calls.index("arrival")
    assert result["success"] is False
    assert result["reason"] == "recovery_fatigue_invalid"
    assert result["leg_results"] == [{"leg": leg, "travel": travel}]
    assert result["page_state"] == "unknown"
