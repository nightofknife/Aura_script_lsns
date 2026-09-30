from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from plans.aura_base.src.services.vision_service import VisionService
from plans.resonance_pc.src.actions import player_data_pc_actions as navigation


@pytest.fixture
def templates():
    return navigation._load_warehouse_page_templates(VisionService())


def warehouse_frame(templates, *, selected="items", missing=None):
    frame = np.full((720, 1280, 3), 40, dtype=np.uint8)
    for category, (x, y, width, height) in navigation._WAREHOUSE_PAGE_ICON_REGIONS.items():
        if category == missing:
            continue
        icon = templates[category][0 if category == selected else 1]
        left = x + (width - icon.shape[1]) // 2
        top = y + (height - icon.shape[0]) // 2
        frame[top:top + icon.shape[0], left:left + icon.shape[1]] = cv2.cvtColor(icon, cv2.COLOR_GRAY2BGR)
    return frame


class Frames:
    def __init__(self, frames):
        self.frames = frames
        self.captures = 0
        self.clicks = []

    def capture(self, *, rect):
        frame = self.frames[min(self.captures, len(self.frames) - 1)]
        self.captures += 1
        if frame is None:
            return SimpleNamespace(success=False, image=None)
        x, y, width, height = rect
        return SimpleNamespace(success=True, image=frame[y:y + height, x:x + width].copy())

    def click(self, *, x, y):
        self.clicks.append((x, y))


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(navigation.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(navigation.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(navigation, "_capture_ocr_items", lambda *args, **kwargs: pytest.fail("page confirmation must not use OCR"))
    return now


@pytest.mark.parametrize("selected", ["items", "materials", "equipment"])
def test_native_icons_confirm_already_open_warehouse_without_entry_click(templates, clock, monkeypatch, selected):
    app = Frames([warehouse_frame(templates, selected=selected)])
    monkeypatch.setattr(navigation, "_match_warehouse_entry", lambda *args: pytest.fail("must observe open page before entry input"))
    navigation._enter_warehouse_page(app, VisionService())
    assert app.captures == 2
    assert app.clicks == []


def test_transient_warehouse_frame_is_not_enough_to_start_scanning(templates, clock, monkeypatch):
    profile = np.full((720, 1280, 3), 40, dtype=np.uint8)
    warehouse = warehouse_frame(templates)
    app = Frames([profile, warehouse, profile, warehouse, warehouse])
    monkeypatch.setattr(navigation, "_match_warehouse_entry", lambda *args: {
        "found": not app.clicks, "center": [165, 615], "confidence": .998,
    })
    navigation._enter_warehouse_page(app, VisionService())
    assert app.captures == 5
    assert app.clicks == [(165, 615)]


@pytest.mark.parametrize("failed_frame", [None, np.zeros((100, 100, 3), dtype=np.uint8)])
def test_failed_capture_resets_confirmation_and_only_retries_observation(templates, clock, monkeypatch, failed_frame):
    warehouse = warehouse_frame(templates)
    app = Frames([warehouse, failed_frame, warehouse, warehouse])
    monkeypatch.setattr(navigation, "_match_warehouse_entry", lambda *args: pytest.fail("capture failure must not cause input"))
    navigation._enter_warehouse_page(app, VisionService())
    assert app.captures == 4
    assert app.clicks == []


def test_slow_capture_cannot_trigger_entry_input_after_deadline(templates, clock, monkeypatch):
    app = Frames([np.full((720, 1280, 3), 40, dtype=np.uint8)])
    capture = app.capture

    def slow_capture(**kwargs):
        clock[0] += 4.0
        return capture(**kwargs)

    app.capture = slow_capture
    monkeypatch.setattr(navigation, "_match_warehouse_entry", lambda *args: pytest.fail("deadline passed"))
    with pytest.raises(navigation.StopTaskException, match="category icons were not confirmed"):
        navigation._enter_warehouse_page(app, VisionService())
    assert app.clicks == []


@pytest.mark.parametrize("missing", ["items", "materials", "equipment"])
def test_incomplete_sidebar_times_out_without_blind_entry_click(templates, clock, monkeypatch, missing):
    app = Frames([warehouse_frame(templates, missing=missing)])
    monkeypatch.setattr(navigation, "_match_warehouse_entry", lambda *args: {"found": False, "confidence": .1})
    with pytest.raises(navigation.StopTaskException, match="category icons were not confirmed twice consecutively"):
        navigation._enter_warehouse_page(app, VisionService(), timeout_sec=.6)
    assert app.clicks == []
    assert clock[0] < 1.0
