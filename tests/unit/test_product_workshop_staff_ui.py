"""Explicitly synthetic native-asset scenes; these are not live acceptance."""
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
    x, y, w, h = ui.catalog()["templates"][name]["bbox"]
    image[max(0,y-2):min(720,y+h+2), max(0,x-2):min(1280,x+w+2)] = (25, 31, 40)


def test_native_asset_catalog_is_complete_and_honestly_synthetic():
    assert ui.validate_assets()
    data = ui.catalog()
    assert data["kind"] == "synthetic_native_game_assets" and data["live_calibrated"] is False
    assert data["provenance"]["sources"] and data["provenance"]["fonts"]
    assert all(not errors for errors in data["render_errors"].values())
    for spec in [*data["templates"].values(), *data["editor_digits"]]:
        path = ui.PLAN / spec["image"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == spec["sha256"]
    assert len(data["editor_digits"]) == 10


@pytest.mark.parametrize("name,room,page,count", [
    ("editor_1", ROOM, "editor", 10),
    ("editor_1_after", ROOM, "editor", 15),
    ("editor_2", "2号生产车间", "editor", 10),
    ("selection_empty", ROOM, "selection", 10),
    ("selection_full", ROOM, "selection", 15),
    ("save_prompt", ROOM, "save_prompt", None),
    ("saved", ROOM, "product_workshop", None),
])
def test_native_scene_classification_and_counts(name, room, page, count):
    value = ui.observe(frame(name), room)
    assert value["page"] == page and value.get("count") == count
    if page == "selection":
        assert value["capacity"] == 15
        assert value["button"] == ("select_all" if count == 10 else "cancel_all")


def test_wrong_numbered_editor_is_unknown():
    assert ui.observe(frame("editor_2"), ROOM) is None
    assert ui.observe(frame("editor_1"), "2号生产车间") is None


def test_full_button_does_not_override_a_missing_count():
    image = frame("selection_full")
    x,y,w,h = ui.catalog()["counters"]["selection"]["region"]
    image[y:y+h,x:x+w] = (25,31,40)
    value = ui.observe(image, ROOM)
    assert value["page"] == "selection" and value["count"] is None


@pytest.mark.parametrize("missing", ["save_prompt", "apply", "discard"])
def test_incomplete_modal_never_exposes_background_editor(missing):
    image = frame("save_prompt")
    # Retain the actual editor controls visible above the native modal body.
    image[:200] = frame("editor_1")[:200]
    image[470:530, :400] = frame("editor_1")[470:530, :400]
    erase(image, missing)
    assert ui.observe(image, ROOM) is None


def test_unknown_frame_and_invalid_client_do_not_produce_a_page():
    assert ui.observe(np.zeros((720,1280,3), np.uint8), ROOM) is None
    assert ui.observe(np.zeros((720,1280,3), np.float32), ROOM) is None
    assert ui.observe(frame("editor_1")[:, :1200], ROOM) is None


def test_native_editor_glyph_bank_recognizes_each_digit():
    for item in ui.catalog()["editor_digits"]:
        image = cv2.imread(str(ui.PLAN/item["image"]))
        value = ui._integer(image, {"region": [0,0,image.shape[1],image.shape[0]]})
        assert value == int(item["character"])


def test_new_flow_has_no_recognition_service_dependency():
    text = inspect.getsource(ui)+inspect.getsource(actions)
    assert "ocr=" not in text.lower() and "ocr_service" not in text.lower()
    assert 'app="plans/aura_base/app"' in text


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
    assert result["selected_count"] == 15 and result["selected_added_count"] == 5
    assert result["final_count"] is None and not result["save_verified"]
