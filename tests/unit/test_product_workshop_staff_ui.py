"""Native scenes and labelled live references; these are not live-flow acceptance."""
from copy import deepcopy
import hashlib
import inspect
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import _product_workshop_staff_ui as ui
from plans.resonance_pc.src.actions import product_workshop_staff_pc_actions as actions

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/product_workshop_staff"
ROOM = "1号生产车间"


def frame(name):
    value = cv2.imread(str(FIXTURES / (name+".png")))
    assert value is not None and value.shape == (720, 1280, 3)
    return value


def erase(image, name):
    spec = ui.catalog()["templates"][name]
    for variant in (spec,*spec.get("variants",[])):
        x,y,w,h = variant["bbox"]
        image[max(0,y-2):min(720,y+h+2),max(0,x-2):min(1280,x+w+2)] = (25,31,40)


def test_native_asset_catalog_is_complete_and_honestly_synthetic():
    assert ui.validate_assets()
    data = ui.catalog()
    assert data["kind"] == "native_assets_with_user_reference_templates" and data["live_calibrated"] is False
    assert data["provenance"]["sources"] and data["provenance"]["fonts"]
    assert all(not errors for errors in data["render_errors"].values())
    for spec in data["templates"].values():
        for variant in (spec, *spec.get("variants", [])):
            path = ui.PLAN / variant["image"]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == variant["sha256"]
    assert "editor_digits" not in data and "counters" not in data
    assert "save_prompt" not in data["templates"]


@pytest.mark.parametrize("name,room,page,button", [
    ("editor_1", ROOM, "editor", None),
    ("editor_1_after", ROOM, "editor", None),
    ("editor_2", "2号生产车间", "editor", None),
    ("selection_empty", ROOM, "selection", "select_all"),
    ("selection_full", ROOM, "selection", "cancel_all"),
    ("save_prompt", ROOM, "save_prompt", None),
    ("saved", ROOM, "product_workshop", None),
])
def test_native_scene_classification_and_buttons(name, room, page, button):
    value = ui.observe(frame(name), room)
    assert value["page"] == page and value.get("button") == button
    assert "count" not in value and "capacity" not in value


def test_wrong_numbered_editor_is_unknown():
    assert ui.observe(frame("editor_2"), ROOM) is None
    assert ui.observe(frame("editor_1"), "2号生产车间") is None


def test_real_workshop_entry_click_targets_blue_icon_above_label():
    image = frame("live_room1_entry_20261017")
    assert ui.observe(image, ROOM) == {"page": "product_workshop"}
    point = actions.clicks.rounded_point(ui.edit_point(image, ROOM))
    assert point == [1135, 635]
    # Actual blue icon client bounds supplied by the user's screenshot.
    assert 1102 <= point[0] < 1169 and 602 <= point[1] < 669


def test_rich_reference_blocks_recognize_real_editor_and_preserve_number_identity():
    image = frame("live_room1_editor_20261017")
    assert ui.observe(image, ROOM) == {"page":"editor"}
    assert ui.observe(image, "2号生产车间") is None
    assert ui.control(image,"quick")["score"] > .99
    assert ui.control(image,"room_1_title")["score"] > .99
    assert actions.clicks.rounded_point(ui.point(image,"quick")) == [115,500]
    assert ui.catalog()["templates"]["quick"]["bbox"][2:] == [124,35]
    assert ui.catalog()["templates"]["room_1_title"]["bbox"][2:] == [203,61]


@pytest.mark.parametrize("name,button",[("selection_full","cancel_all"),("live_room1_selection_20261017","select_all")])
def test_selection_needs_no_label_or_headcount(name,button):
    image = frame(name)
    image[640:710,20:260] = (25,31,40)
    assert ui.observe(image,ROOM) == {"page":"selection","button":button}


def test_real_selection_buttons_have_correct_locations_and_high_scores():
    image = frame("live_room1_selection_20261017")
    assert ui.observe(image,ROOM) == {"page":"selection","button":"select_all"}
    for name,point in [("select_all",[930,670]),("confirm",[1150,670])]:
        assert ui.control(image,name)["score"] > .99
        assert actions.clicks.rounded_point(ui.point(image,name)) == point
    spec = ui.catalog()["templates"]["cancel_all"]
    x,y,w,h = spec["bbox"]
    image[y:y+h,x:x+w] = ui.rooms._asset(spec["image"])
    assert ui.observe(image,ROOM) == {"page":"selection","button":"cancel_all"}


def test_ambiguous_buttons_do_not_authorize_input(monkeypatch):
    original = ui.control
    def ambiguous(image,name):
        if name in {"select_all","cancel_all"}:
            return {"score":.99}
        return original(image,name)
    monkeypatch.setattr(ui,"control",ambiguous)
    assert ui.observe(frame("live_room1_selection_20261017"),ROOM) is None


@pytest.mark.parametrize("missing", ["apply", "discard"])
def test_incomplete_modal_never_exposes_background_editor(missing):
    image = frame("live_room1_save_prompt_20261017")
    # Retain the actual editor controls visible above the native modal body.
    image[:200] = frame("editor_1")[:200]
    image[470:530, :200] = frame("editor_1")[470:530, :200]
    erase(image, missing)
    assert ui.observe(image, ROOM) is None


@pytest.mark.parametrize("name",["save_prompt","live_room1_save_prompt_20261017"])
def test_save_popup_requires_only_two_buttons_and_ignores_message(name):
    image = frame(name)
    image[320:410,450:880] = (25,31,40)
    assert ui.observe(image,ROOM) == {"page":"save_prompt"}
    for key,point in [("apply",[960,505]),("discard",[320,505])]:
        assert ui.control(image,key)["score"] > .99
        assert actions.clicks.rounded_point(ui.point(image,key)) == point


def test_unknown_frame_and_invalid_client_do_not_produce_a_page():
    assert ui.observe(np.zeros((720,1280,3), np.uint8), ROOM) is None
    assert ui.observe(np.zeros((720,1280,3), np.float32), ROOM) is None
    assert ui.observe(frame("editor_1")[:, :1200], ROOM) is None


def test_new_flow_has_no_recognition_service_dependency():
    text = inspect.getsource(ui)+inspect.getsource(actions)
    assert "ocr=" not in text.lower() and "ocr_service" not in text.lower()
    assert 'app="plans/aura_base/app"' in text
    assert "_raw_workshop_digits" not in text and "read_fraction" not in text


def test_whole_flow_replays_native_scenes_with_real_vision_and_no_game_input(monkeypatch):
    class Replay:
        def __init__(self):
            self.name = "saved"
            self.history = []
            self.back_count = 0

        def get_window_size(self):
            return 1280,720

        def capture(self, *, rect):
            image = (cv2.imread(str(ROOT/"tests/fixtures/reforming_center_templates/overview_00.png"))
                     if self.name == "overview" else frame(self.name))
            return SimpleNamespace(success=True, image=cv2.cvtColor(image,cv2.COLOR_BGR2RGB))

        def click(self, *, x, y):
            image = frame(self.name)
            expected_control, next_name = {
                "saved": ("edit", "editor_1") if self.back_count == 0 else ("back", "overview"),
                "editor_1": ("quick", "selection_empty"),
                "selection_empty": ("select_all", "selection_full"),
                "selection_full": ("confirm", "editor_1_after"),
                "editor_1_after": ("back", "save_prompt"),
                "save_prompt": ("apply", "saved"),
            }[self.name]
            point = ui.edit_point(image, ROOM) if expected_control == "edit" else ui.point(image, expected_control)
            assert [x,y] == actions.clicks.rounded_point(point)
            self.history.append(expected_control)
            self.back_count += expected_control == "back"
            self.name = next_name

    monkeypatch.setattr(actions.clicks,"_sleep",lambda _:None)
    monkeypatch.setattr(actions,"_sleep",lambda _:None)
    app = Replay()
    previous = {"success":True,"room_entered":True,"room_name":ROOM}
    rows = []
    for step in actions.STEPS:
        previous = actions.resonance_pc_product_workshop_staff_step(ROOM,step,previous,app=app)
        assert previous["safe_to_continue"], previous
        rows.append(deepcopy(previous))
    result = actions.resonance_pc_product_workshop_staff_summarize(ROOM,rows)
    assert app.history == ["edit","quick","select_all","confirm","back","apply","back"]
    assert result["status"] == "submitted" and result["returned_to_overview"]
    assert result["selection_action"] == "selected" and not result["save_verified"]
