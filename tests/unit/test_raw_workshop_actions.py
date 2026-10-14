"""Workshop maintenance atoms: bounded writes, shortage and unknown are distinct."""
from copy import deepcopy
import inspect
from types import SimpleNamespace

import numpy as np
import pytest

from plans.resonance_pc.src.actions import raw_workshop_pc_actions as actions

ROOM = "动物纤维原料车间"


def fraction(current, capacity):
    return {"current": current, "capacity": capacity, "full": current == capacity,
            "text": f"{current}/{capacity}"}


def snapshot(production="idle", work=(0, 7), reserve=(0, 2), tab="work", start_enabled=True):
    return {"room_name": ROOM, "production": production, "start_enabled": start_enabled,
            "work": fraction(*work), "reserve": fraction(*reserve), "tab": tab}


class App:
    def __init__(self):
        self.clicks = []

    def click(self, *, x, y):
        self.clicks.append((x, y))
        return None

    def capture(self, *, rect):
        return SimpleNamespace(success=True, image=np.zeros((720, 1280, 3), dtype=np.uint8))

    def get_window_size(self):
        return 1280, 720


def patch_snapshot(monkeypatch, value):
    monkeypatch.setattr(actions, "_snapshot", lambda app, room: deepcopy(value))


@pytest.fixture
def scene(monkeypatch):
    """Fake only bounded vision/input surfaces, not the atomic control flow."""
    app = App()
    app.state = snapshot(work=(0, 7))
    app.page = "room"
    app.choice = {"count": fraction(0, 7), "button": "select_all", "confirm_enabled": True}
    app.available = 7
    app.deposit_outcome = "dialog"
    app.maximum = False
    app.max_works = True
    app.submit_works = True

    def clicked(*, x, y):
        point = (x, y)
        app.clicks.append(point)
        name = next(key for key, value in actions.POINTS.items() if tuple(value) == point)
        if name == "deposit":
            app.page = "deposit" if app.deposit_outcome == "dialog" else "room"
        elif name == "deposit_max":
            app.maximum = app.max_works
        elif name == "deposit_confirm" and app.submit_works:
            app.page = "room"
        elif name in {"work_tab", "reserve_tab"}:
            app.state["tab"] = name.removesuffix("_tab")
        elif name == "choose_prisoners":
            app.page = "selection"
        elif name == "select_all":
            app.choice["count"] = fraction(app.available, app.choice["count"]["capacity"])
            app.choice["button"] = "cancel_all"
        elif name == "staff_confirm" and app.submit_works:
            app.state[app.state["tab"]] = deepcopy(app.choice["count"])
            app.page = "room"
        elif name == "selection_back":
            app.page = "room"
        elif name == "start" and app.submit_works:
            app.state["production"] = "running"
        return None

    app.click = clicked
    monkeypatch.setattr(actions.clicks, "_sleep", lambda _: None)
    monkeypatch.setattr(actions.ui, "snapshot", lambda image, room: deepcopy(app.state) if app.page == "room" else None)
    monkeypatch.setattr(actions.ui, "room_ready", lambda image, room: app.page == "room")
    monkeypatch.setattr(actions.ui, "control_visible", lambda image, name: True)
    monkeypatch.setattr(actions.ui, "control_enabled", lambda image, name: True)
    monkeypatch.setattr(actions.ui, "production_state", lambda image: app.state["production"])
    monkeypatch.setattr(actions.ui, "selection_state", lambda image: deepcopy(app.choice) if app.page == "selection" else None)
    monkeypatch.setattr(actions.ui, "deposit_outcome", lambda image, room: "dialog" if app.page == "deposit" else ("already_full" if app.deposit_outcome == "already_full" else None))
    monkeypatch.setattr(actions.ui, "deposit_maximum", lambda image: app.page == "deposit" and app.maximum)
    return app


def controls(app):
    return [next(key for key, point in actions.POINTS.items() if tuple(point) == click) for click in app.clicks]


def test_deposit_maximum_verified_before_single_submit(scene):
    result = actions.resonance_pc_raw_workshop_deposit_max(ROOM, app=scene)
    assert result["success"] and result["data"]["deposit_state"] == "submitted"
    assert controls(scene) == ["deposit", "deposit_max", "deposit_confirm"]
    assert all(click["effect_confirmed"] and click["transition_ready"]
               and click["input_acknowledged"] is None for click in result["data"]["clicks"])


def test_deposit_full_toast_skips_amount_dialog(scene):
    scene.deposit_outcome = "already_full"
    result = actions.resonance_pc_raw_workshop_deposit_max(ROOM, app=scene)
    assert result["success"] and result["status"] == "already_full"
    assert controls(scene) == ["deposit"]


def test_maximum_not_confirmed_prevents_deposit_submission(scene):
    scene.max_works = False
    result = actions.resonance_pc_raw_workshop_deposit_max(ROOM, app=scene)
    assert not result["safe_to_continue"]
    assert result["snapshot"] is None
    assert controls(scene) == ["deposit", "deposit_max"]


def test_deposit_unknown_submit_is_not_retried(scene):
    scene.submit_works = False
    result = actions.resonance_pc_raw_workshop_deposit_max(ROOM, app=scene)
    assert not result["safe_to_continue"]
    assert controls(scene) == ["deposit", "deposit_max", "deposit_confirm"]


def test_fill_work_once_confirms_every_click(scene):
    result = actions.resonance_pc_raw_workshop_fill_list_once(ROOM, "work", app=scene)
    assert result["success"] and result["snapshot"]["work"]["full"]
    assert controls(scene) == ["choose_prisoners", "select_all", "staff_confirm"]
    assert all(click["effect_confirmed"] and click["transition_ready"] for click in result["data"]["clicks"])


def test_existing_selected_list_does_not_click_cancel_all(scene):
    scene.state["work"] = fraction(3, 7)
    scene.choice.update(count=fraction(4, 7), button="cancel_all")
    result = actions.resonance_pc_raw_workshop_fill_list_once(ROOM, "work", app=scene)
    assert result["status"] == "partial" and result["safe_to_continue"]
    assert controls(scene) == ["choose_prisoners", "staff_confirm"]


@pytest.mark.parametrize("existing,selected,reason", [(0, 0, "no_available_prisoners"),
                                                     (3, 2, "selection_would_reduce_staff")])
def test_empty_or_reducing_selection_exits_without_submitting(scene, existing, selected, reason):
    scene.state["work"] = fraction(existing, 7)
    scene.choice.update(count=fraction(selected, 7), button="cancel_all")
    result = actions.resonance_pc_raw_workshop_fill_list_once(ROOM, "work", app=scene)
    assert result["reason"] == reason and result["safe_to_continue"]
    assert controls(scene) == ["choose_prisoners", "selection_back"]
    assert result["snapshot"]["work"]["current"] == existing


def test_reserve_switch_confirmed_before_editor(scene):
    scene.state = snapshot("running", work=(7, 7), reserve=(0, 2))
    scene.choice["count"] = fraction(0, 2)
    scene.available = 2
    result = actions.resonance_pc_raw_workshop_fill_list_once(ROOM, "reserve", app=scene)
    assert result["success"]
    assert controls(scene) == ["reserve_tab", "choose_prisoners", "select_all", "staff_confirm"]


def test_staff_unknown_submit_never_confirms_twice(scene):
    scene.submit_works = False
    result = actions.resonance_pc_raw_workshop_fill_list_once(ROOM, "work", app=scene)
    assert not result["safe_to_continue"] and result["snapshot"] is None
    assert controls(scene) == ["choose_prisoners", "select_all", "staff_confirm"]


def test_zero_selection_without_visual_effect_is_unknown_not_success(scene):
    original_click = scene.click
    def no_selection_effect(*, x, y):
        if (x, y) == actions.POINTS["select_all"]:
            scene.clicks.append((x, y))
            return None
        return original_click(x=x, y=y)
    scene.click = no_selection_effect
    result = actions.resonance_pc_raw_workshop_fill_list_once(ROOM, "work", app=scene)
    assert result["status"] == "failed"
    assert result["safe_to_continue"] is False
    assert result["snapshot"] is None
    assert controls(scene) == ["choose_prisoners", "select_all"]


def test_zero_selection_with_confirmed_button_change_cancels_without_empty_save(scene):
    scene.available = 0
    result = actions.resonance_pc_raw_workshop_fill_list_once(ROOM, "work", app=scene)
    assert result["status"] == "partial"
    assert result["safe_to_continue"] is True
    assert result["reason"] == "no_available_prisoners"
    assert controls(scene) == ["choose_prisoners", "select_all", "selection_back"]


def test_start_click_once_and_confirm_running_despite_staff_gap(scene):
    result = actions.resonance_pc_raw_workshop_start_once(ROOM, app=scene)
    assert result["success"] and result["snapshot"]["production"] == "running"
    assert controls(scene) == ["start"]


def test_start_unknown_effect_stops_without_retry(scene):
    scene.submit_works = False
    result = actions.resonance_pc_raw_workshop_start_once(ROOM, app=scene)
    assert not result["safe_to_continue"]
    assert controls(scene) == ["start"]


@pytest.mark.parametrize("after,expected_continue", [
    (snapshot("running", work=(3, 7), reserve=(0, 2)), True),
    (snapshot("running", work=(2, 7), reserve=(0, 2)), True),
    (snapshot("running", work=(2, 7), reserve=(2, 2)), False),
    (snapshot("running", work=(7, 7), reserve=(2, 2)), False),
])
def test_reserve_reports_vacancy_for_bounded_loop_to_decide_progress(monkeypatch, after, expected_continue):
    app = App()
    before = snapshot("running", work=(2, 7), reserve=(0, 2))
    patch_snapshot(monkeypatch, before)
    monkeypatch.setattr(actions, "_fill_list", lambda app, room, kind, data: {
        "success": False, "status": "partial", "reason": "staff_not_full",
        "safe_to_continue": True, "snapshot": deepcopy(before), "data": data})
    monkeypatch.setattr(actions, "_transfer_window", lambda app, room, previous: deepcopy(after))
    result = actions.resonance_pc_raw_workshop_reserve_round(ROOM, app=app)
    assert result["safe_to_continue"]
    assert result["continue_reserve"] is expected_continue
    assert result["data"]["progress"] is (after["work"]["current"] > before["work"]["current"]
                                           or after["reserve"]["current"] > before["reserve"]["current"])
    assert app.clicks == []


def test_idle_reserve_does_not_loop_for_transfer(monkeypatch):
    app = App()
    state = snapshot(work=(7, 7), reserve=(1, 2))
    patch_snapshot(monkeypatch, state)
    monkeypatch.setattr(actions, "_fill_list", lambda app, room, kind, data: {
        "success": False, "status": "partial", "reason": "staff_not_full",
        "safe_to_continue": True, "snapshot": deepcopy(state), "data": data})
    monkeypatch.setattr(actions, "_transfer_window", lambda *args: pytest.fail("Idle must not wait for a running transfer"))
    result = actions.resonance_pc_raw_workshop_reserve_round(ROOM, app=app)
    assert result["continue_reserve"] is True


def test_snapshot_canonicalization_ignores_volatile_digit_match_scores(monkeypatch):
    app = App()
    states = [snapshot(work=(15, 15), reserve=(10, 10)) for _ in range(2)]
    states[0]["work"]["score"], states[1]["work"]["score"] = .94, .99
    values = iter(states)
    monkeypatch.setattr(actions.clicks, "_sleep", lambda _: None)
    monkeypatch.setattr(actions.ui, "snapshot", lambda image, room: next(values))
    result = actions.resonance_pc_raw_workshop_observe(ROOM, app=app)
    assert result["success"]
    assert result["snapshot"]["work"] == {"current": 15, "capacity": 15, "full": True}
    assert app.clicks == []


def test_public_atoms_do_not_inject_ocr_and_points_are_multiples_of_five():
    names = ("observe", "deposit_max", "fill_list_once", "fill_list_until_full", "reserve_round", "start_once", "return_overview")
    for name in names:
        function = getattr(actions, f"resonance_pc_raw_workshop_{name}")
        assert "ocr" not in inspect.signature(function).parameters
    assert actions.POINTS
    for point in actions.POINTS.values():
        assert all(isinstance(value, int) and value % 5 == 0 for value in point)


def test_running_deposit_skips_without_click(monkeypatch):
    app = App()
    patch_snapshot(monkeypatch, snapshot("running", work=(7, 7)))
    result = actions.resonance_pc_raw_workshop_deposit_max(ROOM, app=app)
    assert result["success"] and result["safe_to_continue"]
    assert app.clicks == []


def test_running_work_list_cannot_be_edited(monkeypatch):
    app = App()
    patch_snapshot(monkeypatch, snapshot("running", work=(2, 15), reserve=(0, 10)))
    result = actions.resonance_pc_raw_workshop_fill_list_once(ROOM, "work", app=app)
    assert result["safe_to_continue"]
    assert app.clicks == []


@pytest.mark.parametrize("kind,work,reserve", [("work", (15, 15), (0, 10)),
                                               ("reserve", (6, 6), (10, 10))])
def test_dynamic_capacity_full_list_skips(monkeypatch, kind, work, reserve):
    app = App()
    patch_snapshot(monkeypatch, snapshot(work=work, reserve=reserve, tab=kind))
    result = actions.resonance_pc_raw_workshop_fill_list_once(ROOM, kind, app=app)
    assert result["safe_to_continue"]
    assert app.clicks == []


def test_start_already_running_never_terminates_or_accelerates(monkeypatch):
    app = App()
    patch_snapshot(monkeypatch, snapshot("running", work=(7, 7), reserve=(2, 2)))
    result = actions.resonance_pc_raw_workshop_start_once(ROOM, app=app)
    assert result["success"] and result["safe_to_continue"]
    assert app.clicks == []


def test_disabled_start_is_known_blocker_without_click(monkeypatch):
    app = App()
    patch_snapshot(monkeypatch, snapshot(start_enabled=False))
    result = actions.resonance_pc_raw_workshop_start_once(ROOM, app=app)
    assert result["status"] == "blocked"
    assert result["safe_to_continue"] is True
    assert app.clicks == []


@pytest.mark.parametrize("production,work,reserve,expected", [
    ("running", (15, 15), (10, 10), "completed"),
    ("running", (14, 15), (10, 10), "partial"),
    ("running", (15, 15), (9, 10), "partial"),
    ("idle", (15, 15), (10, 10), "blocked"),
])
def test_summary_uses_business_state_not_node_success(production, work, reserve, expected):
    observed = {"success": True, "safe_to_continue": True,
                "snapshot": snapshot(production, work, reserve)}
    result = actions.resonance_pc_raw_workshop_summarize(
        ROOM, navigation={"room_entered": True}, observation=observed)
    assert result["status"] == expected
    assert result["success"] is (expected == "completed")


def test_unsafe_submit_invalidates_current_snapshot_in_summary():
    previous = snapshot("running", (7, 7), (2, 2))
    result = actions.resonance_pc_raw_workshop_summarize(
        ROOM, navigation={"room_entered": True},
        observation={"success": True, "safe_to_continue": True, "snapshot": previous},
        deposit={"success": False, "status": "failed", "safe_to_continue": False,
                 "reason": "submit_result_unknown", "snapshot": None})
    assert result["status"] == "failed"
    assert result["success"] is False
    assert result["snapshot"] is None


def test_final_observation_takes_priority_over_start_snapshot():
    result = actions.resonance_pc_raw_workshop_summarize(
        ROOM, navigation={"room_entered": True},
        start={"success": True, "safe_to_continue": True,
               "snapshot": snapshot("running", (7, 7), (2, 2))},
        final_observe={"success": True, "safe_to_continue": True,
                       "snapshot": snapshot("running", (6, 7), (1, 2))})
    assert result["status"] == "partial"
    assert result["snapshot"]["work"]["current"] == 6
    assert result["snapshot"]["reserve"]["current"] == 1


def test_final_observation_unknown_does_not_reuse_stale_success():
    result = actions.resonance_pc_raw_workshop_summarize(
        ROOM, navigation={"room_entered": True},
        start={"success": True, "safe_to_continue": True,
               "snapshot": snapshot("running", (7, 7), (2, 2))},
        final_observe={"success": False, "safe_to_continue": False, "snapshot": None,
                       "reason": "confirmation_timeout"})
    assert result["status"] == "failed"
    assert result["snapshot"] is None
    assert result["data"]["last_confirmed_snapshot"]["production"] == "running"


def test_failed_navigation_cannot_be_overridden_by_later_success():
    result = actions.resonance_pc_raw_workshop_summarize(
        ROOM, navigation={"room_entered": False, "reason": "room_anchor_not_found"},
        observation={"success": True, "safe_to_continue": True,
                     "snapshot": snapshot("running", (7, 7), (2, 2))})
    assert result["status"] == "failed"
    assert result["reason"] == "room_anchor_not_found"
    assert result["snapshot"] is None


def test_summary_reports_explicit_reserve_round_budget_exhaustion():
    state = snapshot("running", (6, 7), (0, 2))
    round_result = {"success": False, "status": "partial", "safe_to_continue": True, "snapshot": state,
                    "reason": "reserve_round_limit", "data": {"round_count": 8}}
    result = actions.resonance_pc_raw_workshop_summarize(
        ROOM, navigation={"room_entered": True}, reserve_rounds=[round_result])
    assert result["status"] == "partial"
    assert result["reason"] == "reserve_round_limit"
    assert result["data"]["reserve_round_count"] == 8


@pytest.mark.parametrize("production", ["idle", "running"])
def test_return_overview_clicks_once_then_confirms_effect_and_two_ready_frames(monkeypatch, production):
    app = App()
    patch_snapshot(monkeypatch, snapshot(production))
    monkeypatch.setattr(actions.clicks, "_sleep", lambda _: None)
    monkeypatch.setattr(actions.ui, "room_ready", lambda *args: True)
    monkeypatch.setattr(actions.ui, "control_visible", lambda image, name: name == "selection_back")
    probes = []
    monkeypatch.setattr(actions.ui, "overview_state", lambda image: probes.append(True) or {
        "page": "reforming_center_overview", "anchors": [ROOM], "offset": [0, 0]})
    result = actions.resonance_pc_raw_workshop_return_overview(ROOM, app=app)
    assert result["success"] and result["returned_to_overview"]
    assert result["page_after"] == "reforming_center_overview"
    assert app.clicks == [(95, 40)] and len(probes) == 3
    assert result["data"]["clicks"][0]["effect_confirmed"]
    assert result["data"]["clicks"][0]["transition_ready"]


def test_return_overview_unknown_room_does_not_click(monkeypatch):
    app = App()
    def unknown(*args):
        raise actions.clicks.CheckedClickError("confirmation_timeout")
    monkeypatch.setattr(actions, "_snapshot", unknown)
    result = actions.resonance_pc_raw_workshop_return_overview(ROOM, app=app)
    assert not result["returned_to_overview"] and not result["safe_to_continue"]
    assert app.clicks == []


@pytest.mark.parametrize("back_visible", [False, True])
def test_return_overview_missing_arrow_or_missing_destination_never_retries(monkeypatch, back_visible):
    app = App()
    patch_snapshot(monkeypatch, snapshot("running"))
    monkeypatch.setattr(actions.clicks, "_sleep", lambda _: None)
    monkeypatch.setattr(actions.ui, "room_ready", lambda *args: True)
    monkeypatch.setattr(actions.ui, "control_visible", lambda *args: back_visible)
    monkeypatch.setattr(actions.ui, "overview_state", lambda image: None)
    result = actions.resonance_pc_raw_workshop_return_overview(ROOM, app=app)
    assert not result["safe_to_continue"] and not result["returned_to_overview"]
    assert app.clicks == ([(95, 40)] if back_visible else [])


def test_return_overview_unknown_after_effect_stops_without_second_click(monkeypatch):
    app = App()
    patch_snapshot(monkeypatch, snapshot())
    monkeypatch.setattr(actions.clicks, "_sleep", lambda _: None)
    monkeypatch.setattr(actions.ui, "room_ready", lambda *args: True)
    monkeypatch.setattr(actions.ui, "control_visible", lambda *args: True)
    values = iter([{"page": "reforming_center_overview"}] + [None] * 10)
    monkeypatch.setattr(actions.ui, "overview_state", lambda image: next(values))
    result = actions.resonance_pc_raw_workshop_return_overview(ROOM, app=app)
    assert result["reason"] == "click_transition_not_confirmed"
    assert app.clicks == [(95, 40)]


@pytest.mark.parametrize("complete", [True, False])
def test_summary_return_success_preserves_maintenance_completion_or_shortage(complete):
    observed = {"safe_to_continue": True, "snapshot": snapshot("running", (7, 7), (2 if complete else 0, 2))}
    result = actions.resonance_pc_raw_workshop_summarize(
        ROOM, navigation={"room_entered": True}, final_observe=observed, require_return=True,
        return_overview={"success": True, "returned_to_overview": True})
    assert result["returned_to_overview"]
    assert result["page_after"] == "reforming_center_overview"
    assert result["status"] == ("completed" if complete else "partial")
    assert result["data"]["maintenance"]["status"] == result["status"]


@pytest.mark.parametrize("returned", [None, {"success": False, "returned_to_overview": False,
                                          "reason": "click_effect_not_confirmed"}])
def test_summary_return_failure_is_not_hidden_by_successful_maintenance(returned):
    result = actions.resonance_pc_raw_workshop_summarize(
        ROOM, navigation={"room_entered": True}, require_return=True, return_overview=returned,
        final_observe={"safe_to_continue": True, "snapshot": snapshot("running", (7, 7), (2, 2))})
    assert result["status"] == "failed" and not result["success"]
    assert not result["returned_to_overview"] and result["snapshot"] is None
    assert result["page_after"] is None
    assert result["data"]["maintenance"]["status"] == "completed"


@pytest.mark.parametrize("stage", ["before", "snapshot", "require"])
def test_return_overview_cancellation_before_click_never_inputs(monkeypatch, stage):
    import asyncio

    state = {"cancelled": stage == "before"}
    app = App()
    monkeypatch.setattr(actions, "is_current_task_cancel_requested", lambda: state["cancelled"])
    monkeypatch.setattr(actions.clicks, "_sleep", lambda _: None)
    captures = []
    original_capture = app.capture

    def capture(**kwargs):
        captures.append(True)
        if stage == "require":
            state["cancelled"] = True
        return original_capture(**kwargs)

    app.capture = capture
    if stage != "snapshot":
        patch_snapshot(monkeypatch, snapshot())
    else:
        def probe(*args):
            state["cancelled"] = True
            return snapshot()
        monkeypatch.setattr(actions, "_probe", probe)
    monkeypatch.setattr(actions.ui, "room_ready", lambda *args: True)
    monkeypatch.setattr(actions.ui, "control_visible", lambda *args: True)
    with pytest.raises(asyncio.CancelledError):
        actions.resonance_pc_raw_workshop_return_overview(ROOM, app=app)
    assert app.clicks == []
    if stage == "before":
        assert captures == []
    assert actions.clicks._CANCEL_CHECK.get() is None


@pytest.mark.parametrize("cancel_probe", [1, 2])
def test_return_overview_cancellation_during_effect_or_ready_stops_confirmation(monkeypatch, cancel_probe):
    import asyncio

    app = App()
    state = {"cancelled": False, "probes": 0}
    monkeypatch.setattr(actions, "is_current_task_cancel_requested", lambda: state["cancelled"])
    patch_snapshot(monkeypatch, snapshot())
    monkeypatch.setattr(actions.clicks, "_sleep", lambda _: None)
    monkeypatch.setattr(actions.ui, "room_ready", lambda *args: True)
    monkeypatch.setattr(actions.ui, "control_visible", lambda *args: True)

    def overview(image):
        state["probes"] += 1
        if state["probes"] == cancel_probe:
            state["cancelled"] = True
        return {"page": "reforming_center_overview"}

    monkeypatch.setattr(actions.ui, "overview_state", overview)
    with pytest.raises(asyncio.CancelledError):
        actions.resonance_pc_raw_workshop_return_overview(ROOM, app=app)
    assert app.clicks == [(95, 40)]
    assert state["probes"] == cancel_probe
    assert actions.clicks._CANCEL_CHECK.get() is None


def test_return_confirmation_wait_polls_cancellation_at_fifty_ms(monkeypatch):
    import asyncio

    state = {"cancelled": False}
    sleeps = []
    monkeypatch.setattr(actions, "is_current_task_cancel_requested", lambda: state["cancelled"])

    def sleep(seconds):
        sleeps.append(seconds)
        state["cancelled"] = True

    monkeypatch.setattr(actions.clicks.time, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        with actions.clicks.cancellation_checks(actions._check_return_cancelled):
            actions.clicks.wait_confirmed(App(), lambda image: True)
    assert sleeps == [.05]
    assert actions.clicks._CANCEL_CHECK.get() is None
