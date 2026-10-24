"""Measured-fatigue contracts for automatic bentos, without game input."""
from __future__ import annotations

import pytest

from plans.resonance_pc.src.actions import bento_auto_recovery_pc_actions as auto


@pytest.mark.parametrize(("start", "stop"), [
    (None, 200), (700, None), (True, 0), (700, False),
    (700.0, 200), (700, 200.0), (-1, 0), (700, -1), (200, 200), (199, 200),
])
def test_thresholds_must_be_explicit_integers_with_start_above_stop(start, stop):
    with pytest.raises(ValueError, match="threshold"):
        auto.validate_auto_inputs(True, False, ["work_meals"], start, stop)


@pytest.mark.parametrize(("start", "stop"), [(701, 217), (1, 0)])
def test_user_thresholds_have_no_implicit_default(start, stop):
    assert auto.validate_auto_inputs(True, False, ["work_meals"], start, stop) == ("work_meals",)


def scripted_session(monkeypatch, readings, *, kind="work_meals", available=20,
                     start=700, stop=200, limit=24):
    """Run real selection/use/rating orchestration with deterministic observations."""
    session = auto.AutoBentoConsumptionSession(
        object(), None, None, {"total_timeout_sec": 30, "max_portions": limit,
                              "detail_name_roi": [0, 0, 1, 1],
                              "blank_point": [1, 1], "star_points": [[i, 1] for i in range(5)]},
        {}, {}, priority=(kind,), bento_start_threshold=start, bento_stop_threshold=stop,
    )
    events = []
    pending = iter(readings)
    meal = {"kind": kind, "food_name": "Food", "rating_stars": 1,
            "selected_roi": [0, 0, 1, 1], "click_point": [1, 1]}
    scans = [0]

    def enter():
        events.append("enter")
        session.page = "bento_cabinet"

    def read():
        session.stage = "read_fatigue"
        if session.items:
            assert session.items[-1]["phase"] == "completed", "Read only after returning from rating"
        value = next(pending)
        events.append(("read_fatigue", value))
        if isinstance(value, Exception):
            raise value
        return value

    def choose():
        scans[0] += 1
        events.append("choose")
        return ((dict(meal), "selected") if scans[0] <= available
                else (None, "no_available_meals"))

    def wait(probe, label, *args, **kwargs):
        events.append(("wait", label))
        return True

    def finish():
        events.append("return_main")
        session.page = "city_main"

    monkeypatch.setattr(session, "enter", enter)
    monkeypatch.setattr(session, "scroll_top", lambda: events.append("scroll_top"))
    monkeypatch.setattr(session, "read_fatigue", read)
    monkeypatch.setattr(session, "choose_next", choose)
    monkeypatch.setattr(session, "return_main", finish)
    monkeypatch.setattr(session, "snapshot", object)
    monkeypatch.setattr(session, "is_cabinet", lambda **kwargs: True)
    monkeypatch.setattr(session, "text", lambda *args, **kwargs: "Food")
    monkeypatch.setattr(session, "match", lambda key, *args, **kwargs:
                        {"found": key != "use_disabled", "center": [1, 1]})
    monkeypatch.setattr(session, "click", lambda point: events.append(("click", point)))
    monkeypatch.setattr(session, "wait", wait)
    monkeypatch.setattr(session, "_wait_consumption_effect", lambda: events.append("effect_confirmed"))
    return session, events


def public_result(monkeypatch, session):
    monkeypatch.setattr(auto, "is_current_task_cancel_requested", lambda: False)
    monkeypatch.setattr(auto, "resonance_pc_require_client_resolution", lambda **kwargs: None)
    monkeypatch.setattr(auto, "load_auto_layout", lambda vision: {})
    monkeypatch.setattr(auto, "load_recovery_layout", lambda vision: {})
    monkeypatch.setattr(auto, "load_love_bento_catalog", lambda *args, **kwargs: {})
    monkeypatch.setattr(auto, "AutoBentoConsumptionSession", lambda *args, **kwargs: session)
    return auto.resonance_pc_consume_bentos_to_floor(
        eat_work_meals=True, eat_love_bentos=False, bento_priority=["work_meals"],
        bento_start_threshold=700, bento_stop_threshold=200,
        app=object(), ocr=object(), vision=object(),
    )


@pytest.mark.parametrize("initial", [700, 699, 200, 0])
def test_exact_start_threshold_and_lower_fatigue_skip_without_selection(monkeypatch, initial):
    session, events = scripted_session(monkeypatch, [initial])
    result = session.run_auto()
    assert result["status"] == "skipped"
    assert result["reason"] == "start_threshold_not_exceeded"
    assert result["initial_fatigue"] == result["final_fatigue"] == initial
    assert result["recovered_fatigue"] == 0 and result["consumed_count"] == 0
    assert "choose" not in events and result["page_state"] == "city_main"


@pytest.mark.parametrize("kind", ["work_meals", "love_bentos"])
def test_started_consumption_continues_below_start_until_exact_stop(monkeypatch, kind):
    session, events = scripted_session(monkeypatch, [701, 689, 420, 200], kind=kind)
    result = session.run_auto()
    assert result["success"] and result["reason"] == "stop_threshold_reached"
    assert result["consumed_count"] == result["completed_count"] == 3
    assert result["final_fatigue"] == 200 and result["recovered_fatigue"] == 501
    assert result["recovered_fatigue_source"] == "measured_difference"
    assert result["work_meals_used"] == (3 if kind == "work_meals" else 0)
    assert result["love_bentos_used"] == (3 if kind == "love_bentos" else 0)
    assert events.count("scroll_top") == 1
    if kind == "love_bentos":
        assert events.count(("wait", "rating page")) == 3
        assert events.count(("wait", "selected rating")) == 3
    assert all("fatigue_recovery" not in row["meal"] for row in result["items"])
    assert "computed_fatigue" not in result and "target_recovery_amount" not in result


def test_unknown_portion_can_cross_stop_without_a_prediction_or_floor(monkeypatch):
    session, _ = scripted_session(monkeypatch, [251, 190], start=250, stop=200)
    result = session.run_auto()
    assert result["success"] and result["consumed_count"] == 1
    assert result["final_fatigue"] == 190 and result["recovered_fatigue"] == 61


@pytest.mark.parametrize("readings", [
    [auto.BentoConsumptionError("bento_fatigue_unrecognized", "unreadable")],
    [701, auto.BentoConsumptionError("bento_fatigue_unrecognized", "unreadable")],
    [701, 610, auto.BentoConsumptionError("bento_fatigue_unrecognized", "unreadable")],
])
def test_unreadable_fatigue_stops_and_never_reports_stale_final(monkeypatch, readings):
    session, events = scripted_session(monkeypatch, readings)
    result = public_result(monkeypatch, session)
    count = len(readings) - 1
    assert result["status"] == "failed" and result["reason"] == "bento_fatigue_unrecognized"
    assert result["consumed_count"] == result["completed_count"] == count
    assert result["final_fatigue"] is None and result["recovered_fatigue"] is None
    assert result["last_observed_fatigue"] == (readings[-2] if count else None)
    assert events.count("choose") == count
    if count:
        assert result["requires_refresh"] is True


@pytest.mark.parametrize("after", [701, 710])
def test_confirmed_portion_without_fatigue_drop_prevents_another_use(monkeypatch, after):
    session, events = scripted_session(monkeypatch, [701, after])
    result = public_result(monkeypatch, session)
    assert result["status"] == "failed" and result["reason"] == "bento_fatigue_not_decreased"
    assert result["consumed_count"] == result["completed_count"] == 1
    assert result["final_fatigue"] == after and result["recovered_fatigue"] == 701 - after
    assert events.count("choose") == 1


def test_cancel_during_rating_preserves_confirmed_consumption_without_stale_final(monkeypatch):
    session, events = scripted_session(monkeypatch, [701], kind="love_bentos")
    wait = session.wait

    def cancel_rating(probe, label, *args, **kwargs):
        if label == "rating page":
            raise auto.BentoConsumptionError("bento_cancelled", "cancelled during rating")
        return wait(probe, label, *args, **kwargs)

    monkeypatch.setattr(session, "wait", cancel_rating)
    result = public_result(monkeypatch, session)
    assert result["status"] == "cancelled"
    assert result["consumed_count"] == 1 and result["completed_count"] == 0
    assert result["last_observed_fatigue"] == 701 and result["final_fatigue"] is None
    assert result["recovered_fatigue"] is None and events.count("choose") == 1


def test_cancel_before_next_portion_keeps_latest_measured_final(monkeypatch):
    session, events = scripted_session(monkeypatch, [701, 610])
    guard = session.guard

    def cancel_after_first_observation():
        if session.last_observed_fatigue == 610:
            raise auto.BentoConsumptionError("bento_cancelled", "cancelled after fatigue read")
        guard()

    monkeypatch.setattr(session, "guard", cancel_after_first_observation)
    result = public_result(monkeypatch, session)
    assert result["status"] == "cancelled" and result["consumed_count"] == 1
    assert result["final_fatigue"] == 610 and result["recovered_fatigue"] == 91
    assert events.count("choose") == 1


@pytest.mark.parametrize("available,readings", [(0, [701]), (1, [701, 610])])
def test_inventory_exhaustion_preserves_actual_partial_result(monkeypatch, available, readings):
    session, events = scripted_session(monkeypatch, readings, available=available)
    result = session.run_auto()
    assert result["success"] and result["reason"] == "no_available_meals"
    assert result["consumed_count"] == result["completed_count"] == available
    assert result["final_fatigue"] == readings[-1]
    assert result["recovered_fatigue"] == readings[0] - readings[-1]
    assert events.count("choose") == available + 1


def test_portion_safety_limit_is_not_reported_as_reaching_stop(monkeypatch):
    session, _ = scripted_session(monkeypatch, [701, 650, 600], limit=2)
    result = public_result(monkeypatch, session)
    assert result["status"] == "failed" and result["reason"] == "bento_portion_limit_reached"
    assert result["consumed_count"] == 2 and result["final_fatigue"] == 600
