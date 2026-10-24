"""Offline checks for the independent, in-session bento recovery task."""

from pathlib import Path
from types import SimpleNamespace

import cv2
import pytest

from plans.resonance_pc.src.actions import bento_auto_recovery_pc_actions as auto_bento
from plans.aura_base.src.services.vision_service import VisionService
from plans.resonance_pc.src.actions.bento_auto_recovery_pc_actions import (
    AutoBentoConsumptionSession,
    load_auto_layout,
    validate_auto_inputs,
)
from plans.resonance_pc.src.actions.bento_consumption_pc_actions import BentoConsumptionError
from plans.resonance_pc.src.actions.love_bento_pc_actions import load_love_bento_catalog


CONSUMPTION_FIXTURES = Path("tests/fixtures/bento_consumption")


def fixed_love_session(monkeypatch, frames):
    class Vision:
        def resolve_template(self, plan, ref, root):
            return str(root / ref)

        def load_image_file(self, path, mode):
            return cv2.imread(str(path), mode)

        def find_templates_batch(self, *, source_image, template_images, threshold,
                                 use_grayscale, match_method, preprocess):
            assert source_image.shape[:2] == (138, 154)
            assert not use_grayscale and match_method == cv2.TM_CCOEFF_NORMED
            assert preprocess == "none"
            hits = []
            for path in template_images:
                template = cv2.imread(path)
                score = float(cv2.minMaxLoc(cv2.matchTemplate(source_image, template, match_method))[1])
                hits.append(SimpleNamespace(found=score >= threshold, confidence=score, debug_info={}))
            return hits

        def find_template(self, **kwargs):
            pytest.fail("First love-bento scan must not match a card background")

    vision = Vision()
    layout = load_auto_layout(vision)
    catalog = load_love_bento_catalog(vision)
    session = AutoBentoConsumptionSession(
        app=object(), ocr=None, vision=vision, layout=layout, recovery_layout={}, catalog=catalog,
        priority=("love_bentos",), bento_start_threshold=700, bento_stop_threshold=200,
    )
    rx, ry, rw, rh = catalog["scanner"]["capture_roi"]
    pending = iter(frame[ry:ry+rh, rx:rx+rw] for frame in frames)

    class Scanner:
        def __init__(self, reader, catalog):
            self.cfg = catalog["scanner"]

        def stable_frame(self):
            return next(pending)

        def classify(self, *args):
            pytest.fail("First love-bento scan must not infer a crop from a card anchor")

    monkeypatch.setattr(auto_bento, "LoveBentoScanner", Scanner)
    recovery_reads = []
    monkeypatch.setattr(session, "read_recovery", lambda *args: recovery_reads.append(args), raising=False)
    return session, recovery_reads


def test_first_love_uses_fixed_food_and_selected_rois(monkeypatch):
    image = cv2.imread(str(CONSUMPTION_FIXTURES / "love_usable.png"))
    session, recovery_reads = fixed_love_session(monkeypatch, [image])
    meal = session.first_love()
    assert meal["food_name"] == "香喷喷浓郁咖喱"
    assert meal["click_point"] == [223, 424]
    assert meal["selected_roi"] == [296, 328, 52, 61]
    assert "fatigue_recovery" not in meal
    assert recovery_reads == []
    marker = cv2.imread("plans/resonance_pc/templates/recovery/bento/controls/selected_marker.png")
    x, y, w, h = meal["selected_roi"]
    selected = 1 - cv2.minMaxLoc(cv2.matchTemplate(image[y:y+h, x:x+w], marker,
                                                   cv2.TM_SQDIFF_NORMED))[0]
    unselected_image = cv2.imread(str(CONSUMPTION_FIXTURES / "work_usable.png"))
    unselected = 1 - cv2.minMaxLoc(cv2.matchTemplate(unselected_image[y:y+h, x:x+w], marker,
                                                     cv2.TM_SQDIFF_NORMED))[0]
    assert selected >= .95 and unselected < .95


def test_first_love_unrecognized_after_stable_frame_means_empty(monkeypatch):
    image = cv2.imread(str(CONSUMPTION_FIXTURES / "love_usable.png"))
    empty = image.copy()
    empty[355:493, 146:300] = 0
    session, recovery_reads = fixed_love_session(monkeypatch, [image, image, empty])
    assert session.first_love() is not None
    assert session.first_love() is not None
    assert session.first_love() is None
    assert recovery_reads == []


@pytest.mark.parametrize("failure", ["service_error", "nonfinite"])
def test_first_love_matching_failure_is_not_treated_as_empty(monkeypatch, failure):
    image = cv2.imread(str(CONSUMPTION_FIXTURES / "love_usable.png"))
    session, recovery_reads = fixed_love_session(monkeypatch, [image])
    score = float("nan") if failure == "nonfinite" else 0.0
    def failed_batch(**kwargs):
        return [SimpleNamespace(found=False, confidence=score,
                                debug_info={"error": "matcher failed"} if failure == "service_error" else {})
                for _ in kwargs["template_images"]]
    monkeypatch.setattr(session.vision, "find_templates_batch", failed_batch)
    with pytest.raises(BentoConsumptionError) as exc:
        session.first_love()
    assert exc.value.code == "bento_first_food_match_failed"
    assert recovery_reads == []


def test_enabled_types_must_match_priority_exactly():
    assert validate_auto_inputs(True, False, ["work_meals"], 700, 200) == ("work_meals",)
    with pytest.raises(ValueError, match="priority"):
        validate_auto_inputs(True, False, ["work_meals", "love_bentos"], 700, 200)
    with pytest.raises(ValueError, match="at least one"):
        validate_auto_inputs(False, False, [], 700, 200)


def test_auto_layout_does_not_load_recovery_digit_templates():
    class Vision(VisionService):
        def resolve_template(self, plan, ref, root):
            assert "recovery_digits" not in str(ref)
            return super().resolve_template(plan, ref, root)
    layout = load_auto_layout(Vision())
    assert "banks" not in layout["recovery_digits"]
    # Four digits on both sides need the extra left margin of the HUD crop.
    assert layout["fatigue_ratio_roi"] == (1139, 20, 132, 40)


def test_work_meal_selection_does_not_need_a_recovery_amount(monkeypatch):
    session = AutoBentoConsumptionSession(
        object(), None, None,
        {"total_timeout_sec": 30, "work_selected_rois": [[1, 2, 3, 4]] * 3},
        {"bento_slots": [{"issue_time": time, "roi": [20 * index, 0, 10, 10]}
                         for index, time in enumerate(("05:00", "12:00", "18:00"))]},
        {}, priority=("work_meals", "love_bentos"),
        bento_start_threshold=700, bento_stop_threshold=200,
    )
    monkeypatch.setattr(session, "work_slots", lambda: [1, 2])
    monkeypatch.setattr(session, "first_love", lambda: pytest.fail("Priority work meal must be selected first"))
    meal, reason = session.choose_next()
    assert reason == "selected" and meal["issue_time"] == "12:00"
    assert "fatigue_recovery" not in meal


@pytest.mark.parametrize(("priority", "work_available", "love_available", "expected", "scanned"), [
    (("love_bentos", "work_meals"), True, True, "love_bentos", ["love_bentos"]),
    (("love_bentos", "work_meals"), True, False, "work_meals", ["love_bentos", "work_meals"]),
    (("work_meals", "love_bentos"), False, True, "love_bentos", ["work_meals", "love_bentos"]),
    (("love_bentos", "work_meals"), False, False, None, ["love_bentos", "work_meals"]),
])
def test_type_priority_controls_selection_and_exhaustion_fallback(
    monkeypatch, priority, work_available, love_available, expected, scanned,
):
    session = AutoBentoConsumptionSession(
        object(), None, None,
        {"total_timeout_sec": 30, "work_selected_rois": [[1, 2, 3, 4]] * 3},
        {"bento_slots": [{"issue_time": "12:00", "roi": [0, 0, 10, 10]}]},
        {}, priority=priority, bento_start_threshold=600, bento_stop_threshold=100,
    )
    calls = []

    def work_slots():
        calls.append("work_meals")
        return [0] if work_available else []

    def first_love():
        calls.append("love_bentos")
        return {"kind": "love_bentos", "food_name": "Food"} if love_available else None

    monkeypatch.setattr(session, "work_slots", work_slots)
    monkeypatch.setattr(session, "first_love", first_love)
    meal, reason = session.choose_next()
    assert (meal["kind"] if meal else None) == expected
    assert reason == ("selected" if expected else "no_available_meals")
    assert calls == scanned
