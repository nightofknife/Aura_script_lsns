"""Native quick-formation flow: single writes and two explicit back operations."""
from copy import deepcopy
import asyncio
from types import SimpleNamespace

import numpy as np
import pytest

from plans.resonance_pc.src.actions import product_workshop_staff_pc_actions as actions

ROOM = "1号生产车间"
POINTS = {name: (100+index*50, 100) for index, name in enumerate(
    ("edit", "quick", "select_all", "confirm", "back", "apply", "cancel_all"))}


class Scene:
    def __init__(self):
        self.page = "product_workshop"
        self.count = 2
        self.initial = 2
        self.capacity = 6
        self.available = 6
        self.applied = False
        self.prompt_works = True
        self.confirm_works = True
        self.save_works = True
        self.clicked = []

    def get_window_size(self):
        return 1280, 720

    def capture(self, *, rect):
        return SimpleNamespace(success=True, image=np.zeros((720, 1280, 3), np.uint8))

    def observe(self):
        if self.page == "selection":
            return {"page": self.page, "count": self.count, "capacity": self.capacity,
                    "button": "cancel_all" if self.count == self.capacity else "select_all"}
        if self.page == "editor":
            return {"page": self.page, "count": self.count}
        return {"page": self.page} if self.page != "unknown" else None

    def click(self, *, x, y):
        name = next(key for key, point in POINTS.items() if point == (x, y))
        self.clicked.append(name)
        if name == "edit":
            self.page = "editor"
        elif name == "quick":
            self.page = "selection"
        elif name == "select_all":
            self.count = max(self.initial, self.available)
        elif name == "confirm" and self.confirm_works:
            self.page = "editor"
        elif name == "back":
            if self.page == "editor" and self.count > self.initial and not self.applied:
                self.page = "save_prompt" if self.prompt_works else "product_workshop"
            elif self.page == "editor" or self.page == "product_workshop":
                self.page = "reforming_center_overview"
        elif name == "apply" and self.save_works:
            self.applied = True
            self.page = "product_workshop"


@pytest.fixture
def scene(monkeypatch):
    app = Scene()
    monkeypatch.setattr(actions.clicks, "_sleep", lambda _: None)
    monkeypatch.setattr(actions.ui, "observe", lambda image, room: deepcopy(app.observe()))
    monkeypatch.setattr(actions.ui, "point", lambda image, control: POINTS.get(control))
    monkeypatch.setattr(actions.ui, "edit_point", lambda image, room: POINTS["edit"])
    return app


def run(app, through=None):
    result = {"success": True, "room_entered": True, "room_name": ROOM}
    rows = []
    for step in actions.STEPS:
        result = actions.resonance_pc_product_workshop_staff_step(ROOM, step, result, app=app)
        rows.append(result)
        if not result["safe_to_continue"] or step == through:
            break
    return rows, actions.resonance_pc_product_workshop_staff_summarize(ROOM, [*rows, *[None]*(7-len(rows))])


def test_full_native_flow_saves_only_after_first_back_and_requires_second_back(scene):
    rows, summary = run(scene)
    assert scene.clicked == ["edit", "quick", "select_all", "confirm", "back", "apply", "back"]
    assert summary["success"] and summary["status"] == "submitted"
    assert summary["initial_count"] == 2 and summary["selected_count"] == 6
    assert summary["selected_added_count"] == 4 and summary["remaining_count"] == 0
    assert summary["final_count"] is summary["added_count"] is None
    assert not summary["save_verified"] and summary["count_scope"] == "local_formation"
    assert summary["returned_to_overview"] and summary["applied"]
    assert rows[4]["page_after"] == "save_prompt"
    assert all(row["effect_confirmed"] for row in summary["data"]["clicks"])


def test_partial_selection_is_saved_and_reported_with_shortage(scene):
    scene.available = 4
    _, result = run(scene)
    assert result["status"] == "partial" and not result["success"]
    assert result["selected_count"] == 4 and result["remaining_count"] == 2
    assert result["final_count"] is None and not result["save_verified"]
    assert result["applied"] and result["returned_to_overview"]


def test_already_full_never_clicks_cancel_all_and_no_popup_is_required(scene):
    scene.initial = scene.count = scene.capacity
    _, result = run(scene)
    assert scene.clicked == ["edit", "quick", "confirm", "back"]
    assert result["success"] and result["added_count"] == 0
    assert not result["applied"] and result["returned_to_overview"]


def test_changed_roster_cannot_bypass_save_popup(scene):
    scene.prompt_works = False
    _, result = run(scene)
    assert result["status"] == "failed" and not result["returned_to_overview"]
    assert scene.clicked == ["edit", "quick", "select_all", "confirm", "back"]
    assert result["page_after"] == "product_workshop" and not result["applied"]


@pytest.mark.parametrize("field,expected", [("confirm_works", "confirm"), ("save_works", "apply")])
def test_missed_write_is_not_retried_or_followed_by_blind_back(scene, field, expected):
    setattr(scene, field, False)
    _, result = run(scene)
    assert result["status"] == "failed"
    assert scene.clicked[-1] == expected and scene.clicked.count(expected) == 1
    assert not result["returned_to_overview"]


def test_unknown_initial_page_makes_no_input(scene):
    scene.page = "unknown"
    _, result = run(scene)
    assert result["reason"] == "staff_page_unknown"
    assert not scene.clicked and result["page_after"] is None


def test_cancel_all_before_full_is_not_clicked(scene, monkeypatch):
    original = scene.observe
    def observe():
        value = original()
        if value and value["page"] == "selection":
            value["button"] = "cancel_all"
        return value
    monkeypatch.setattr(scene, "observe", observe)
    _, result = run(scene)
    assert result["reason"] == "staff_select_all_not_available"
    assert scene.clicked == ["edit", "quick"]


def test_no_candidate_never_submits_empty_selection(scene):
    scene.initial = scene.count = scene.available = 0
    _, result = run(scene)
    assert result["status"] == "failed" and not result["applied"]
    assert scene.clicked == ["edit", "quick", "select_all"]


def test_same_count_is_not_confirmed_as_select_effect(scene):
    scene.available = scene.initial
    rows, result = run(scene)
    assert result["status"] == "failed"
    assert not rows[-1]["clicks"][-1]["effect_confirmed"]
    assert scene.clicked == ["edit", "quick", "select_all"]


def test_selection_count_change_before_confirm_stops_input(scene):
    rows, _ = run(scene, through="select_all")
    scene.count -= 1
    result = actions.resonance_pc_product_workshop_staff_step(ROOM, "confirm", rows[-1], app=scene)
    assert not result["safe_to_continue"] and result["reason"] == "staff_selection_changed"
    assert "confirm" not in scene.clicked


def test_wrong_previous_room_stops_without_click(scene):
    prior = {"success": True, "room_entered": True, "room_name": "2号生产车间"}
    result = actions.resonance_pc_product_workshop_staff_step(ROOM, "edit", prior, app=scene)
    assert result["reason"] == "staff_previous_result_invalid" and not scene.clicked


def test_result_is_immutable_and_does_not_claim_completion_from_counts_alone(scene):
    rows, _ = run(scene)
    before = deepcopy(rows)
    rows[-1]["returned_to_overview"] = False
    result = actions.resonance_pc_product_workshop_staff_summarize(ROOM, rows)
    assert result["status"] == "failed"
    assert rows[:-1] == before[:-1]
    rows[-1] = before[-1]
    rows[-1]["applied"] = False
    assert actions.resonance_pc_product_workshop_staff_summarize(ROOM, rows)["status"] == "failed"


def test_assets_failure_is_reported_before_navigation(monkeypatch):
    monkeypatch.setattr(actions.ui, "validate_assets", lambda: (_ for _ in ()).throw(ValueError("missing")))
    preflight = actions.resonance_pc_product_workshop_staff_assets()
    result = actions.resonance_pc_product_workshop_staff_summarize(ROOM, [None]*7, preflight=preflight)
    assert not preflight["success"] and result["reason"] == "staff_templates_invalid"


def test_saved_page_cannot_prove_async_server_count(scene):
    rows, _ = run(scene)
    # Even a fabricated save_verified flag must not turn a pre-save local count
    # into evidence of a server result absent a separate readback operation.
    rows[-1]["save_verified"] = True
    rows[-1]["final_count"] = 6
    result = actions.resonance_pc_product_workshop_staff_summarize(ROOM, rows)
    assert result["status"] == "submitted" and result["final_count"] is None
    assert not result["save_verified"]


@pytest.mark.parametrize("stage", ["before", "capture", "observation"])
def test_cancellation_before_input_never_clicks(scene, monkeypatch, stage):
    state = {"cancelled": stage == "before"}
    monkeypatch.setattr(actions, "is_current_task_cancel_requested", lambda: state["cancelled"])
    original_capture, original_observe = scene.capture, scene.observe

    def capture(**kwargs):
        state["cancelled"] = stage == "capture"
        return original_capture(**kwargs)

    def observe():
        state["cancelled"] = stage == "observation"
        return original_observe()

    scene.capture, scene.observe = capture, observe
    prior = {"success": True, "room_entered": True, "room_name": ROOM}
    with pytest.raises(asyncio.CancelledError):
        actions.resonance_pc_product_workshop_staff_step(ROOM, "edit", prior, app=scene)
    assert not scene.clicked
    assert actions.clicks._CANCEL_CHECK.get() is None


def test_cancellation_in_click_effect_never_advances_step(scene, monkeypatch):
    state = {"cancelled": False}
    monkeypatch.setattr(actions, "is_current_task_cancel_requested", lambda: state["cancelled"])
    original_click = scene.click

    def click(**kwargs):
        original_click(**kwargs)
        state["cancelled"] = True

    scene.click = click
    prior = {"success": True, "room_entered": True, "room_name": ROOM}
    with pytest.raises(asyncio.CancelledError):
        actions.resonance_pc_product_workshop_staff_step(ROOM, "edit", prior, app=scene)
    assert scene.clicked == ["edit"]
    assert actions.clicks._CANCEL_CHECK.get() is None
