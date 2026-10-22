"""Button-only staffing with task-private, state-gated click retries."""
from copy import deepcopy
import asyncio
from types import SimpleNamespace

import numpy as np
import pytest

from plans.resonance_pc.src.actions import product_workshop_staff_pc_actions as actions
from packages.aura_core.scheduler import operation_scope
from tests.interaction_clock import advance, install_clock

_CANCELLABLE_SLEEP = actions.clicks._sleep
_RETRY_SLEEP = actions._sleep


@pytest.fixture(autouse=True)
def operation_clock(monkeypatch):
    with install_clock(monkeypatch, actions.clicks) as clock:
        yield clock

ROOM = "1号生产车间"
POINTS = {name:(100+index*50,100) for index,name in enumerate(
    ("edit","quick","select_all","confirm","back","apply","cancel_all"))}


class Scene:
    def __init__(self):
        self.page = "product_workshop"
        self.button = "select_all"
        self.button_after_select = "cancel_all"
        self.selection_applied = False
        self.confirmed = False
        self.applied = False
        self.prompt_works = True
        self.confirm_works = True
        self.save_works = True
        self.clicked = []
        self.selection_exit = "editor"

    def get_window_size(self):
        return 1280,720

    def capture(self, *, rect):
        return SimpleNamespace(success=True,image=np.zeros((720,1280,3),np.uint8))

    def observe(self):
        if self.page == "selection":
            return {"page":self.page,"button":self.button}
        return {"page":self.page} if self.page != "unknown" else None

    def click(self, *, x,y):
        name = next(key for key,point in POINTS.items() if point == (x,y))
        self.clicked.append(name)
        if name == "edit":
            self.page = "editor"
        elif name == "quick":
            self.page = "selection"
        elif name == "select_all":
            self.button = self.button_after_select
            self.selection_applied = True
        elif name == "confirm" and self.confirm_works:
            self.confirmed = True
            self.page = "editor"
        elif name == "back":
            if self.page == "selection":
                self.page = self.selection_exit
            elif self.page == "editor":
                self.page = ("save_prompt" if self.prompt_works else "product_workshop") if self.confirmed and self.selection_applied and not self.applied else "product_workshop"
            elif self.page == "product_workshop":
                self.page = "reforming_center_overview"
        elif name == "apply" and self.save_works:
            self.applied = True
            self.page = "product_workshop"


@pytest.fixture
def scene(monkeypatch):
    app = Scene()
    monkeypatch.setattr(actions.clicks,"_sleep",advance)
    monkeypatch.setattr(actions,"_sleep",advance)
    monkeypatch.setattr(actions.ui,"observe",lambda image,room:deepcopy(app.observe()))
    monkeypatch.setattr(actions.ui,"point",lambda image,control:POINTS.get(control))
    monkeypatch.setattr(actions.ui,"edit_point",lambda image,room:POINTS["edit"])
    return app


def run(app,through=None):
    value = {"success":True,"room_entered":True,"room_name":ROOM}
    rows = []
    for step in actions.STEPS:
        value = actions.resonance_pc_product_workshop_staff_step(ROOM,step,value,app=app)
        rows.append(value)
        if not value["safe_to_continue"] or step == through:
            break
    return rows,actions.resonance_pc_product_workshop_staff_summarize(ROOM,[*rows,*[None]*(7-len(rows))])


def test_select_all_then_confirm_save_and_return_without_any_headcount(scene):
    rows,result = run(scene)
    assert scene.clicked == ["edit","quick","select_all","confirm","back","apply","back"]
    assert result["success"] and result["status"] == "submitted"
    assert result["selection_action"] == "selected"
    assert result["applied"] and result["returned_to_overview"]
    assert not result["save_verified"]
    assert rows[4]["page_after"] == "save_prompt"
    assert all(row["effect_confirmed"] for row in result["data"]["clicks"])


def test_cancel_all_on_entry_exits_without_select_cancel_confirm_or_apply(scene):
    scene.button = "cancel_all"
    _,result = run(scene)
    assert scene.clicked == ["edit","quick","back","back","back"]
    assert result["success"] and result["status"] == "unchanged"
    assert result["selection_action"] == "unchanged"
    assert not result["applied"] and result["returned_to_overview"]
    assert not {"select_all","cancel_all","confirm","apply"}.intersection(scene.clicked)


def test_unchanged_return_can_land_on_overview_without_more_clicks(scene):
    scene.button = "cancel_all"
    scene.selection_exit = "reforming_center_overview"
    _,result = run(scene)
    assert result["status"] == "unchanged" and result["returned_to_overview"]
    assert scene.clicked == ["edit","quick","back"]


def test_cancel_all_after_select_still_confirms_and_saves(scene):
    _,result = run(scene)
    assert scene.button == "cancel_all"
    assert result["status"] == "submitted" and "confirm" in scene.clicked


def test_still_visible_select_all_retries_three_times_without_confirm(scene):
    scene.button_after_select = "select_all"
    _,result = run(scene)
    assert result["status"] == "failed" and result["reason"] == "staff_click_retry_exhausted"
    assert scene.clicked == ["edit","quick","select_all","select_all","select_all"]
    assert [r["attempt"] for r in result["data"]["clicks"] if r["control"] == "select_all"] == [1,2,3]


def test_unknown_button_never_selects_or_confirms(scene):
    scene.button = None
    _,result = run(scene)
    assert result["status"] == "failed"
    assert scene.clicked == ["edit","quick"]


def test_changed_roster_cannot_bypass_save_prompt(scene):
    scene.prompt_works = False
    _,result = run(scene)
    assert result["status"] == "failed" and not result["returned_to_overview"]
    assert scene.clicked == ["edit","quick","select_all","confirm","back"]


@pytest.mark.parametrize("field,expected",[("confirm_works","confirm"),("save_works","apply")])
def test_missed_write_retries_at_most_three_times_then_stops(scene,field,expected):
    setattr(scene,field,False)
    _,result = run(scene)
    assert result["status"] == "failed"
    assert result["reason"] == "staff_click_retry_exhausted"
    assert scene.clicked[-1] == expected and scene.clicked.count(expected) == 3


def test_unknown_initial_page_makes_no_input(scene):
    scene.page = "unknown"
    _,result = run(scene)
    assert result["reason"] == "staff_page_unknown" and not scene.clicked


def test_unchanged_button_changed_before_exit_stops_without_submitting(scene):
    scene.button = "cancel_all"
    rows,_ = run(scene,through="select_all")
    scene.button = "select_all"
    result = actions.resonance_pc_product_workshop_staff_step(ROOM,"confirm",rows[-1],app=scene)
    assert not result["safe_to_continue"] and result["reason"] == "staff_selection_button_changed"
    assert scene.clicked == ["edit","quick"]


def test_wrong_previous_room_stops_without_click(scene):
    prior = {"success":True,"room_entered":True,"room_name":"2号生产车间"}
    result = actions.resonance_pc_product_workshop_staff_step(ROOM,"edit",prior,app=scene)
    assert result["reason"] == "staff_previous_result_invalid" and not scene.clicked


def test_result_requires_overview_and_save_for_selected_branch(scene):
    rows,_ = run(scene)
    before = deepcopy(rows)
    rows[-1]["returned_to_overview"] = False
    assert actions.resonance_pc_product_workshop_staff_summarize(ROOM,rows)["status"] == "failed"
    rows = before
    rows[-1]["applied"] = False
    assert actions.resonance_pc_product_workshop_staff_summarize(ROOM,rows)["status"] == "failed"


def test_assets_failure_is_reported_before_navigation(monkeypatch):
    monkeypatch.setattr(actions.ui,"validate_assets",lambda:(_ for _ in ()).throw(ValueError("missing")))
    preflight = actions.resonance_pc_product_workshop_staff_assets()
    result = actions.resonance_pc_product_workshop_staff_summarize(ROOM,[None]*7,preflight=preflight)
    assert result["reason"] == "staff_templates_invalid"


@pytest.mark.parametrize("step,control,page",[
    ("edit","edit","product_workshop"),
    ("quick","quick","editor"),
    ("select_all","select_all","selection"),
    ("confirm","confirm","selection"),
    ("request_save","back","editor"),
    ("apply","apply","save_prompt"),
    ("return_overview","back","product_workshop"),
])
@pytest.mark.parametrize("misses",[1,2,3])
def test_task_local_retries_recover_missed_clicks_and_stop_at_limit(scene,monkeypatch,step,control,page,misses):
    original = scene.click
    remaining = misses
    waits = []
    def click(*,x,y):
        nonlocal remaining
        if remaining and scene.page == page and (x,y) == POINTS[control]:
            remaining -= 1
            scene.clicked.append(control)
            return None
        return original(x=x,y=y)
    monkeypatch.setattr(scene,"click",click)
    monkeypatch.setattr(actions,"_sleep",waits.append)
    rows,result = run(scene)
    records = [record for record in result["data"]["clicks"] if record["step"] == step]
    assert [record["attempt"] for record in records] == list(range(1,min(misses+1,3)+1))
    assert waits == [1.0]*len(scene.clicked)
    if misses < 3:
        assert result["status"] == "submitted" and result["returned_to_overview"]
        assert records[-1]["effect_confirmed"] and records[-1]["transition_ready"]
        assert all(record["origin_page_present"] and record["origin_button_present"] for record in records[:-1])
    else:
        assert result["status"] == "failed" and result["reason"] == "staff_click_retry_exhausted"
        assert rows[-1]["step"] == step and not rows[-1]["safe_to_continue"]
        assert scene.clicked[-3:] == [control]*3


def test_supplementary_click_relocates_the_button_on_the_fresh_frame(scene,monkeypatch):
    scene.page = "editor"
    shift = 0
    points = []
    def point(image,control):
        return [POINTS[control][0]+shift,POINTS[control][1]]
    def click(*,x,y):
        nonlocal shift
        points.append((x,y))
        if len(points) == 1:
            shift = 20
        else:
            scene.page = "selection"
    monkeypatch.setattr(actions.ui,"point",point)
    monkeypatch.setattr(scene,"click",click)
    previous = {"room_name":ROOM,"step":"edit","safe_to_continue":True}
    result = actions.resonance_pc_product_workshop_staff_step(ROOM,"quick",previous,app=scene)
    assert result["success"] and points == [(150,100),(170,100)]


@pytest.mark.parametrize("transient",["button_missing","unknown_then_source_returns"])
def test_no_more_clicks_after_source_disappears_while_waiting_for_destination(scene,monkeypatch,transient):
    scene.page = "editor"
    captures = 0
    sent = False
    original_capture = scene.capture
    def click(*,x,y):
        nonlocal sent
        scene.clicked.append("quick")
        sent = True
    def capture(*,rect):
        nonlocal captures
        if sent:
            captures += 1
            if transient == "button_missing":
                scene.page = "editor" if captures == 1 else "selection"
            else:
                scene.page = "unknown" if captures == 1 else "editor" if captures == 2 else "selection"
        return original_capture(rect=rect)
    monkeypatch.setattr(scene,"click",click)
    monkeypatch.setattr(scene,"capture",capture)
    monkeypatch.setattr(actions.ui,"point",lambda image,control:None if sent and captures == 1 else POINTS[control])
    previous = {"room_name":ROOM,"step":"edit","safe_to_continue":True}
    result = actions.resonance_pc_product_workshop_staff_step(ROOM,"quick",previous,app=scene)
    assert result["success"] and scene.clicked == ["quick"]
    assert result["clicks"][0]["retry_stopped"] == "origin_disappeared_or_unknown"


def test_unknown_page_after_click_stops_without_using_remaining_attempts(scene,monkeypatch):
    original = scene.click
    def click(*,x,y):
        original(x=x,y=y)
        if (x,y) == POINTS["quick"]:
            scene.page = "unknown"
    monkeypatch.setattr(scene,"click",click)
    _,result = run(scene)
    assert result["status"] == "failed" and result["reason"] == "confirmation_timeout"
    assert scene.clicked == ["edit","quick"]


def test_explicit_input_failure_is_not_a_reason_to_click_again(scene,monkeypatch):
    def rejected(*,x,y):
        scene.clicked.append("edit")
        return False
    monkeypatch.setattr(scene,"click",rejected)
    _,result = run(scene)
    assert result["reason"] == "click_input_failed" and scene.clicked == ["edit"]


def test_failed_capture_after_click_is_not_treated_as_button_disappearance(scene,monkeypatch):
    original_capture = scene.capture
    def capture(*,rect):
        if "quick" in scene.clicked:
            return SimpleNamespace(success=False,image=None)
        return original_capture(rect=rect)
    monkeypatch.setattr(scene,"capture",capture)
    _,result = run(scene)
    assert result["reason"] == "capture_failed" and scene.clicked == ["edit","quick"]


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


def test_cancellation_during_one_second_retry_wait_prevents_supplementary_input(scene, monkeypatch):
    state = {"cancelled": False}
    intervals = []
    monkeypatch.setattr(actions, "is_current_task_cancel_requested", lambda: state["cancelled"])
    monkeypatch.setattr(actions.clicks, "_sleep", _CANCELLABLE_SLEEP)
    monkeypatch.setattr(actions, "_sleep", _RETRY_SLEEP)

    def missed_click(**kwargs):
        scene.clicked.append("edit")

    def cancel_during_sleep(seconds):
        intervals.append(seconds)
        state["cancelled"] = True

    monkeypatch.setattr(scene, "click", missed_click)
    monkeypatch.setattr(operation_scope.time, "sleep", cancel_during_sleep)
    prior = {"success": True, "room_entered": True, "room_name": ROOM}
    with pytest.raises(asyncio.CancelledError):
        actions.resonance_pc_product_workshop_staff_step(ROOM, "edit", prior, app=scene)
    assert scene.clicked == ["edit"]
    assert intervals == [.05]
    assert actions.clicks._CANCEL_CHECK.get() is None


def test_selected_button_reverts_before_confirm_stops_without_input(scene):
    rows, _ = run(scene, through="select_all")
    scene.button = "select_all"
    before = list(scene.clicked)
    result = actions.resonance_pc_product_workshop_staff_step(
        ROOM, "confirm", rows[-1], app=scene)
    assert not result["safe_to_continue"]
    assert result["reason"] == "staff_selection_button_changed"
    assert scene.clicked == before


def test_selected_button_reverts_after_missed_confirm_stops_without_retry(scene, monkeypatch):
    rows, _ = run(scene, through="select_all")
    before = list(scene.clicked)

    def missed_confirm(**kwargs):
        scene.clicked.append("confirm")
        scene.button = "select_all"

    monkeypatch.setattr(scene, "click", missed_confirm)
    result = actions.resonance_pc_product_workshop_staff_step(
        ROOM, "confirm", rows[-1], app=scene)
    assert not result["safe_to_continue"]
    assert result["reason"] == "staff_selection_button_changed"
    assert scene.clicked == before + ["confirm"]
    assert result["clicks"][-1]["attempt"] == 1
    assert not scene.confirmed and not scene.applied
