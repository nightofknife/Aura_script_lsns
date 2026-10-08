"""Additive v1 display metadata is separate from confirmed business completion."""

import copy

import pytest

from packages.resonance_gui.logic import (
    PASSENGER_PROGRESS_EVENT,
    PASSENGER_PROGRESS_SCHEMA,
    TRADE_PROGRESS_EVENT,
    TRADE_PROGRESS_SCHEMA,
    PassengerProgressState,
    WorkflowFreightProgressState,
    reduce_passenger_progress,
    reduce_workflow_freight_progress,
)


def freight(sequence, stage, state="progress", **payload):
    return {"name": TRADE_PROGRESS_EVENT, "payload": {
        "schema": TRADE_PROGRESS_SCHEMA, "cid": "run-1", "sequence": sequence,
        "stage": stage, "state": state, **payload,
    }}


def passenger(sequence, stage, state="progress", **payload):
    event = freight(sequence, stage, state, **payload)
    event["name"] = PASSENGER_PROGRESS_EVENT
    event["payload"]["schema"] = PASSENGER_PROGRESS_SCHEMA
    return event


def plan(sequence=1, revision=1, route=None):
    route = route or [{"from_city": "A", "to_city": "B", "buy_products": ["货物"]},
                      {"from_city": "B", "to_city": "A", "buy_products": ["货物"]}]
    return freight(sequence, "planning", "completed", timestamp="2026-10-09T01:00:00Z",
                   data={"route": route, "route_revision": revision})


REDUCERS = [(reduce_workflow_freight_progress, freight), (reduce_passenger_progress, passenger)]


@pytest.mark.parametrize("reduce,event", REDUCERS)
def test_unknown_progress_resources_and_phase_time_stay_unknown(reduce, event):
    state = reduce(None, event(1, "travel", "started"))
    assert state.percent is None
    assert state.stage_started_at is None
    assert state.operation_started_at is None
    assert state.resources == {}


@pytest.mark.parametrize("reduce,event", REDUCERS)
def test_operation_time_is_stable_within_phase_and_resets_on_next_visit(reduce, event):
    data = {"operation": {"key": "travel.confirming", "label": "确认出发", "state": "started",
                          "detail": {"departure_confirmed": False}}}
    state = reduce(None, event(1, "travel", leg_index=0, timestamp="2026-10-09T01:00:00Z", data=data))
    state = reduce(state, event(2, "travel", leg_index=0, timestamp="2026-10-09T01:00:05Z", data=data))
    assert state.stage_started_at == state.operation_started_at == "2026-10-09T01:00:00Z"
    assert state.current_operation_key == "travel.confirming"
    assert state.current_operation_name == "确认出发"
    assert state.current_operation_state == "started"
    assert state.current_operation_detail == ""
    assert state.current_operation["detail"] == {"departure_confirmed": False}
    data["operation"]["detail"]["departure_confirmed"] = True
    assert state.current_operation["detail"]["departure_confirmed"] is False
    state = reduce(state, event(3, "travel", leg_index=1, data=data))
    assert state.stage_started_at is None
    assert state.operation_started_at is None


def test_child_operation_completion_does_not_complete_parent_or_add_progress_units():
    state = reduce_workflow_freight_progress(None, plan())
    completed_before = state.completed_units
    state = reduce_workflow_freight_progress(state, freight(2, "buy", "completed", city_index=0,
        data={"route_revision": 1, "operation": {"key": "buy.selection", "label": "确认选择",
                                                "state": "completed", "detail": "已选中"}}))
    assert state.cities[0].phases[1].state == "running"
    assert state.completed_units == completed_before
    assert state.current_operation_detail == "已选中"
    state = reduce_workflow_freight_progress(state, freight(3, "buy", "completed", city_index=0))
    assert state.cities[0].phases[1].state == "completed"
    assert state.completed_units == completed_before + 1
    assert not state.current_operation


@pytest.mark.parametrize("reduce,event", REDUCERS)
def test_child_blocked_and_failure_propagate_without_success(reduce, event):
    state = reduce(None, event(1, "travel", data={"operation": {
        "key": "travel.confirming", "label": "确认出发", "state": "blocked", "detail": "请确认"}}))
    assert state.state == "blocked"
    assert state.state_label == "等待确认"
    state = reduce(state, event(2, "travel", data={"operation": {
        "key": "travel.confirming", "label": "确认出发", "state": "failed", "detail": "未能确认"}}))
    assert state.state in {"failed", "error"}
    assert state.state_label == "失败"
    assert state.percent != 100


@pytest.mark.parametrize("reduce,event", REDUCERS)
def test_duplicate_out_of_order_foreign_and_invalid_events_are_ignored(reduce, event):
    state = reduce(None, event(5, "travel", data={"resources": {"actual_fatigue": None}}))
    for incoming in (event(5, "travel"), event(4, "travel"), event(6, "travel", cid="run-2"),
                     event(True, "travel"), event("6", "travel"), event(6.5, "travel"),
                     event(6, "travel", data={"route_revision": -1})):
        assert reduce(state, incoming) == state
    assert state.resources == {"actual_fatigue": None}
    assert reduce(state, event(6, "travel", cid="run-1"), expected_cid="other") == state


@pytest.mark.parametrize("reduce,event", REDUCERS)
@pytest.mark.parametrize("terminal", ["failed", "cancelled", "stopped", "success", "completed"])
def test_terminal_task_cannot_be_resurrected_by_late_events(reduce, event, terminal):
    state = reduce(None, event(1, "task", terminal))
    assert reduce(state, event(2, "travel", "started", data={"resources": {"books_used": 100}})) == state
    assert reduce(state, event(3, "task", "success")) == state
    assert state.percent == (100 if terminal in {"success", "completed"} else None)


@pytest.mark.parametrize("reduce,event", REDUCERS)
def test_stopping_ignores_child_heartbeats_and_waits_for_terminal_confirmation(reduce, event):
    state = reduce(None, event(1, "task", "stopping"))
    assert state.state_label == "停止中"
    assert reduce(state, event(2, "travel", "started")) == state
    assert reduce(state, event(2, "travel", "completed")) == state
    state = reduce(state, event(3, "task", "cancelled"))
    assert state.state_label == "已停止"
    assert state.percent is None


def test_replanning_archives_each_visit_and_rejects_old_revision_events():
    state = reduce_workflow_freight_progress(None, plan())
    state = reduce_workflow_freight_progress(state, freight(2, "buy", "completed", city_index=0,
                                                          data={"route_revision": 1}))
    previous = copy.deepcopy(state)
    state = reduce_workflow_freight_progress(state, plan(3, 2, [{"from_city": "A", "to_city": "C"}]))
    assert [city.name for city in previous.cities] == ["A", "B", "A"]
    assert len(state.route_history) == 1
    archived = state.history_revisions[0]
    assert archived["route_revision"] == 1
    assert [city.name for city in archived["cities"]] == ["A", "B", "A"]
    assert archived["cities"][0].phases[1].state == "completed"
    archived["cities"][0].name = "changed"
    assert state.route_history[0]["cities"][0].name == "A"
    assert [city.name for city in state.cities] == ["A", "C"]
    assert state.active_city_index is None
    assert state.active_phase == ""
    assert reduce_workflow_freight_progress(state, freight(4, "buy", "completed", city_index=0,
                                                           data={"route_revision": 1})) == state


def test_repeated_plan_snapshot_does_not_reset_confirmed_phases():
    state = reduce_workflow_freight_progress(None, plan())
    state = reduce_workflow_freight_progress(state, freight(2, "buy", "completed", city_index=0))
    state = reduce_workflow_freight_progress(state, plan(3))
    assert not state.route_history
    assert state.cities[0].phases[1].state == "completed"


def test_passenger_parent_completion_is_not_task_success_or_hundred_percent():
    state = reduce_passenger_progress(None, passenger(1, "travel", "completed", leg_index=0, leg_count=1,
        data={"progress": {"completed_units": 2, "total_units": 2}}))
    assert state.percent == 99
    assert state.state_label == "运行中"
    state = reduce_passenger_progress(state, passenger(2, "task", "completed"))
    assert state.percent == 100
    assert state.state_label == "已完成"


def test_passenger_child_task_completion_is_not_task_success():
    state = reduce_passenger_progress(None, passenger(1, "task", "completed", data={"operation": {
        "key": "task.confirming", "label": "确认任务", "state": "completed"}}))
    assert state.percent is None
    assert not state.task_succeeded


def test_passenger_replanning_keeps_independent_revision_snapshots():
    state = reduce_passenger_progress(None, passenger(1, "resolve_start", data={"route_revision": 1,
        "route": [{"from_city": "A", "to_city": "B"}]}))
    state = reduce_passenger_progress(state, passenger(2, "resolve_start", data={"route_revision": 2,
        "route": [{"from_city": "B", "to_city": "C"}]}))
    assert state.history_revisions == [{"route_revision": 1, "route": [{"from_city": "A", "to_city": "B"}],
                                       "timestamp": None, "state": "progress"}]
    assert state.route_revision == 2


def test_initial_model_defaults_remain_unknown():
    assert WorkflowFreightProgressState().percent is None
    assert PassengerProgressState().percent is None
    assert WorkflowFreightProgressState().current_operation_detail == ""
    assert PassengerProgressState().history_revisions == []
