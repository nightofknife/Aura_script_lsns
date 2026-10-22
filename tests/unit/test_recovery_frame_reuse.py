"""Capture-boundary regressions through real RecoveryReader and scanner entry points."""
from types import SimpleNamespace as NS

import numpy as np
import pytest

from plans.aura_base.src.services.vision_service import VisionService
from plans.resonance_pc.src.actions import player_recovery_pc_actions as recovery
from plans.resonance_pc.src.actions.love_bento_pc_actions import LoveBentoScanner, load_love_bento_catalog


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(recovery.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(recovery.time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    return now


class NumberedApp:
    def __init__(self, render=None):
        self.captures = []
        self.events = []
        self.render = render

    def capture(self, *, rect):
        number = len(self.captures) + 1
        self.captures.append(tuple(rect))
        self.events.append(("capture", number))
        image = np.full((720, 1280, 3), 40, dtype=np.uint8)
        image[:, :, 0] = number
        if self.render:
            self.render(image, number)
        x, y, w, h = rect
        return NS(success=True, image=image[y:y+h, x:x+w].copy())

    def drag(self, *args, **kwargs):
        self.events.append(("drag", len(self.captures)))
        return NS(success=True)


def hit(found=True, confidence=1.0):
    return NS(found=found, confidence=confidence, debug_info={})


def rig(*, render=None, page_found=None, batch=None):
    layout = recovery.load_recovery_layout(VisionService())
    app = NumberedApp(render)
    matches = []
    batches = []
    keys = {spec["resolved_path"]: key for key, spec in layout["templates"].items()}

    def match(**kwargs):
        image = kwargs["source_image"]
        number = int(image[0, 0, 0])
        key = keys[kwargs["template_image"]]
        matches.append((number, key))
        return hit(page_found(number, key) if page_found else True)

    def match_batch(**kwargs):
        number = int(kwargs["source_image"][0, 0, 0])
        batches.append((number, kwargs))
        if batch:
            return batch(number, kwargs)
        return [hit(True), hit(False)]

    vision = NS(find_template=match, find_templates_batch=match_batch)
    return recovery.RecoveryReader(app, None, vision, layout), app, matches, batches


def assert_full_captures(app, count):
    assert app.captures == [(0, 0, 1280, 720)] * count


def test_work_meals_page_markers_and_three_slots_share_one_capture():
    reader, app, matches, batches = rig()
    assert reader.read_work_meals()["available_count"] == 3
    assert_full_captures(app, 1)
    assert matches == [(1, "bento_page"), (1, "bento_train")]
    assert [number for number, _ in batches] == [1, 1, 1]
    assert [call["source_image"].shape[:2] for _, call in batches] == [(105, 165)] * 3


def test_work_meals_retry_uses_new_frame_and_new_slot_states(clock):
    def classify(number, kwargs):
        if number == 1:
            return [hit(False), hit(False)]
        return [hit(False), hit(True)]
    reader, app, matches, batches = rig(batch=classify)
    result = reader.read_work_meals()
    assert result["available_count"] == 0
    assert "degraded" not in result
    assert_full_captures(app, 2)
    assert [number for number, _ in matches] == [1, 1, 2, 2]
    assert [number for number, _ in batches] == [1, 1, 1, 2, 2, 2]


def test_work_meals_cannot_combine_markers_from_different_frames(clock):
    reader, app, matches, batches = rig(
        page_found=lambda number, key: (key == "bento_page") == (number % 2 == 1),
    )
    reader.layout["bento_ready_timeout_sec"] = .6
    result = reader.read_work_meals()
    assert result["degraded"] is True
    assert batches == []
    assert_full_captures(app, 3)
    assert matches == [(1, "bento_page"), (1, "bento_train"),
                       (2, "bento_page"), (3, "bento_page"), (3, "bento_train")]


def test_water_page_marker_and_both_digit_rois_share_one_capture():
    digits = iter((2, 6))
    def classify(number, kwargs):
        chosen = next(digits)
        return [hit(i == chosen, 1.0 if i == chosen else .1) for i in range(7)]
    reader, app, matches, batches = rig(batch=classify)
    assert reader.read_sparkling_water() == {"remaining_free_uses": 2, "daily_free_limit": 6}
    assert_full_captures(app, 1)
    assert matches == [(1, "popup")]
    assert [number for number, _ in batches] == [1, 1]
    assert [call["source_image"].shape[:2] for _, call in batches] == [(52, 36), (52, 48)]


def test_badge_consecutive_confirmation_observes_changed_count_on_new_frames(clock):
    # Glyph thresholding intentionally removes RGB identity; marker probes and
    # batch calls are correlated with the actual capture counter instead.
    reader, app, matches, _ = rig(page_found=lambda number, key: key != "popup")
    observed = []
    values = [3, 8, 8]
    def classify(**kwargs):
        number = len(app.captures)
        observed.append(number)
        chosen = values[number - 1]
        return [hit(i == chosen, 1.0 if i == chosen else .1) for i in range(13)]
    reader.vision.find_templates_batch = classify
    assert reader.read_bento_count()["count"] == 8
    assert_full_captures(app, 3)
    assert observed == [1, 2, 3]
    assert matches == [(n, key) for n in (1, 2, 3) for key in ("fatigue_page", "popup")]


def test_rewind_keeps_cross_frame_stability_pre_drag_recheck_and_post_drag_capture(clock):
    reader, app, matches, batches = rig()
    reader.layout["bento_navigation"]["stationary_confirmations"] = 2
    reader.prepare_bento_read()
    assert_full_captures(app, 11)
    assert app.events == [
        ("capture", 1), ("capture", 2), ("capture", 3),
        ("capture", 4), ("drag", 4),
        ("capture", 5), ("capture", 6), ("capture", 7),
        ("capture", 8), ("drag", 8),
        ("capture", 9), ("capture", 10), ("capture", 11),
    ]
    assert matches == [(n, key) for n in range(1, 12) for key in ("bento_page", "bento_train")]
    # The two pre-input frames only recheck the page. Content observation and
    # the three top-slot checks continue to use their own round's pixels.
    assert [number for number, _ in batches] == [1, 2, 3, 5, 6, 7, 7, 7, 7, 9, 10, 11, 11, 11, 11]


def test_rewind_page_loss_in_fresh_pre_drag_frame_prevents_input(clock):
    reader, app, matches, _ = rig(page_found=lambda number, key: number < 4)
    with pytest.raises(recovery.StopTaskException, match="disappeared before rewind drag"):
        reader.prepare_bento_read()
    assert_full_captures(app, 4)
    assert not any(kind == "drag" for kind, _ in app.events)
    assert matches[-1] == (4, "bento_page")


def scanner_for(reader):
    catalog = load_love_bento_catalog(VisionService(), navigation_only=True)
    return LoveBentoScanner(reader, catalog)


def test_love_scanner_capture_shares_markers_and_content_then_refreshes_next_call(clock):
    reader, app, matches, _ = rig()
    scanner = scanner_for(reader)
    first = scanner.capture()
    second = scanner.capture()
    assert_full_captures(app, 2)
    assert np.all(first[:, :, 0] == 1)
    assert np.all(second[:, :, 0] == 2)
    assert matches == [(n, key) for n in (1, 2) for key in ("bento_page", "bento_train")]


def test_love_scanner_stability_restarts_after_visible_frame_change(clock):
    def render(image, number):
        image[:, :, 1] = 40 if number == 1 else 100
    reader, app, matches, _ = rig(render=render)
    frame = scanner_for(reader).stable_frame()
    assert_full_captures(app, 4)
    assert np.all(frame[:, :, 0] == 4)
    assert matches == [(n, key) for n in range(1, 5) for key in ("bento_page", "bento_train")]


def test_love_scanner_page_loss_after_drag_is_seen_on_new_capture(clock, monkeypatch):
    reader, app, matches, _ = rig(page_found=lambda number, key: number <= 6)
    scanner = scanner_for(reader)
    # Isolate card classification, retaining real read_page/stability/capture.
    monkeypatch.setattr(scanner, "read_frame", lambda frame: [])
    with pytest.raises(recovery.StopTaskException, match="cabinet disappeared"):
        scanner.read()
    assert_full_captures(app, 7)
    assert app.events[-3:] == [("capture", 6), ("drag", 6), ("capture", 7)]
    assert matches[-1] == (7, "bento_page")
