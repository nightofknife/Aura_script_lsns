"""Backend-authored visits, explicit progress and cumulative resource snapshots."""
from packages.resonance_gui.logic import (
    TRADE_PROGRESS_EVENT, TRADE_PROGRESS_SCHEMA, reduce_workflow_freight_progress,
)


def event(sequence, stage, state="completed", **kwargs):
    return {"name": TRADE_PROGRESS_EVENT, "payload": {"schema": TRADE_PROGRESS_SCHEMA,
            "cid": "freight-1", "sequence": sequence, "stage": stage, "state": state, **kwargs}}


def planned():
    visits = [{"city_index": index, "city_id": city, "city_name": city, "role": role,
               "phases": [{"key": key, "status": status, "reason": "未启用" if status == "skipped" else ""}
                          for key, status in phases]}
              for index, city, role, phases in (
                  (0, "1", "initial", [("sell", "waiting"), ("buy", "waiting"), ("travel", "waiting")]),
                  (1, "2", "intermediate", [("arrival", "waiting"), ("buy", "waiting"), ("travel", "waiting")]),
                  (2, "1", "terminal", [("arrival", "waiting"), ("final_sale", "waiting"), ("bento", "waiting"), ("investment", "skipped")]))]
    return reduce_workflow_freight_progress(None, event(1, "planning", data={
        "route": [{"from_city": "1", "to_city": "2"}, {"from_city": "2", "to_city": "1"}],
        "city_visits": visits, "reposition": {"required": True, "expected_fatigue": 5},
        "progress": {"completed_units": 2, "total_units": 14}}))


def test_visits_preserve_repeat_city_and_backend_phases():
    state = planned()
    assert [city.city_id for city in state.cities] == ["1", "2", "1"]
    assert state.cities[2].phases[-1].state == "skipped"
    assert state.cities[2].phases[-1].detail == "未启用"
    state = reduce_workflow_freight_progress(state, event(2, "final_sale", city_index=2))
    assert state.cities[2].phases[1].state == "completed"
    assert state.cities[0].phases[0].state == "waiting"


def test_reposition_never_changes_trade_visit_or_leg():
    state = planned()
    before = [[phase.state for phase in city.phases] for city in state.cities]
    state = reduce_workflow_freight_progress(state, event(2, "reposition", "started", city_index=0,
        data={"reposition": {"leg_index": 0, "leg_count": 2}, "message": "导航到线路起点"}))
    assert state.active_city_index is None
    assert state.active_phase == "reposition"
    assert state.current_label == "导航到线路起点"
    assert [[phase.state for phase in city.phases] for city in state.cities] == before


def test_progress_is_authoritative_and_resources_are_overwrite_snapshots():
    state = planned()
    state = reduce_workflow_freight_progress(state, event(2, "books", "progress", city_index=0,
        data={"resources": {"books_used": 2, "actual_fatigue": None},
              "progress": {"completed_units": 4, "total_units": 14}}))
    assert state.completed_units == 4
    assert state.total_units == 14
    assert state.percent == 29
    state = reduce_workflow_freight_progress(state, event(3, "books", "progress", city_index=0,
        data={"resources": {"books_used": 3, "actual_fatigue": 150}}))
    assert state.resources == {"books_used": 3, "actual_fatigue": 150}
    duplicate = reduce_workflow_freight_progress(state, event(3, "books", data={"resources": {"books_used": 100}}))
    assert duplicate.resources["books_used"] == 3


def test_route_completion_does_not_skip_bento_or_report_one_hundred_percent():
    state = planned()
    state = reduce_workflow_freight_progress(state, event(2, "route", data={"progress": {"completed_units": 14, "total_units": 14}}))
    assert state.state == "running"
    assert state.percent == 99
    assert state.cities[2].phases[2].state == "waiting"
    state = reduce_workflow_freight_progress(state, event(3, "task", "blocked", data={"message": "便当未完成"}))
    assert state.state == "blocked"
    assert state.cities[2].phases[2].state == "waiting"


def test_only_task_completion_finishes_progress():
    state = reduce_workflow_freight_progress(planned(), event(2, "task"))
    assert state.percent == 100
    assert state.state == "completed"
