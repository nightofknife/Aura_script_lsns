"""Offline checks for the independent, in-session bento recovery task."""

from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from PIL import Image

from plans.resonance_pc.src.actions import bento_auto_recovery_pc_actions as auto_bento
from plans.aura_base.src.services.vision_service import VisionService
from plans.resonance_pc.src.actions.bento_auto_recovery_pc_actions import (
    AutoBentoConsumptionSession,
    decode_recovery,
    load_auto_layout,
    validate_auto_inputs,
)
from plans.resonance_pc.src.actions.bento_consumption_pc_actions import BentoConsumptionError
from plans.resonance_pc.src.actions.love_bento_pc_actions import load_love_bento_catalog


FIXTURES = Path("tests/fixtures/bento_recovery_digits")
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
        priority=("love_bentos",), target_recovery_amount=2000,
        allow_exceed_target=False, base_fatigue_reserve=-100,
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
    monkeypatch.setattr(session, "read_recovery", lambda style, roi: recovery_reads.append((style, roi)) or 46)
    return session, recovery_reads


def test_first_love_uses_fixed_food_and_selected_rois(monkeypatch):
    image = cv2.imread(str(CONSUMPTION_FIXTURES / "love_usable.png"))
    session, recovery_reads = fixed_love_session(monkeypatch, [image])
    meal = session.first_love()
    assert meal["food_name"] == "香喷喷浓郁咖喱"
    assert meal["click_point"] == [223, 424]
    assert meal["selected_roi"] == [296, 328, 52, 61]
    assert meal["fatigue_recovery"] == 46
    assert recovery_reads == [("love", (298, 513, 22, 16))]
    marker = cv2.imread("plans/resonance_pc/templates/bento_consumption/selected_marker.png")
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
    assert len(recovery_reads) == 2


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


@pytest.mark.parametrize(("name", "style", "expected"), [
    ("work_05_36.png", "work", 36),
    ("work_18_36.png", "work", 36),
    ("fixture_work_second_36.png", "work", 36),
    ("love_first_51.png", "love", 51),
    ("love_second_41.png", "love", 41),
    ("love_third_26.png", "love", 26),
    ("fixture_love_first_46.png", "love", 46),
    ("fixture_love_second_44.png", "love", 44),
    ("fixture_love_third_44.png", "love", 44),
])
def test_real_capture_recovery_digits(name, style, expected):
    layout = load_auto_layout(VisionService())
    frame = np.asarray(Image.open(FIXTURES / name).convert("RGB"))
    cfg = layout["recovery_digits"]
    actual, diagnostics = decode_recovery(
        frame, cfg["banks"][style], threshold=cfg[f"{style}_threshold"],
        margin=cfg["min_score_margin"],
    )
    assert actual == expected
    assert len(diagnostics) == 2


def test_blank_recovery_is_unknown_not_zero():
    cfg = load_auto_layout(VisionService())["recovery_digits"]
    frame = np.zeros((16, 22, 3), dtype=np.uint8)
    actual, _ = decode_recovery(frame, cfg["banks"]["work"],
                                threshold=cfg["work_threshold"], margin=cfg["min_score_margin"])
    assert actual is None


def test_enabled_types_must_match_priority_exactly():
    assert validate_auto_inputs(True, False, ["work_meals"], 100, False, -100) == ("work_meals",)
    with pytest.raises(ValueError, match="priority"):
        validate_auto_inputs(True, False, ["work_meals", "love_bentos"], 100, False, -100)
    with pytest.raises(ValueError, match="at least one"):
        validate_auto_inputs(False, False, [], 100, False, -100)


def test_budget_never_exceeds_target_or_fatigue_floor():
    session = AutoBentoConsumptionSession(
        app=object(), ocr=None, vision=None,
        layout={"total_timeout_sec": 30}, recovery_layout={}, catalog={},
        priority=("work_meals",), target_recovery_amount=100,
        allow_exceed_target=False, base_fatigue_reserve=-100,
    )
    session.computed_fatigue = 20
    assert session.fits(51)
    session.total_recovered = 51
    assert not session.fits(51)
    session.total_recovered = 0
    assert not session.fits(121)
    session.computed_fatigue = -80
    assert not session.fits(36)


def test_only_confirmed_consumption_changes_in_memory_budget():
    session = AutoBentoConsumptionSession(
        app=object(), ocr=None, vision=None,
        layout={"total_timeout_sec": 30}, recovery_layout={}, catalog={},
        priority=("work_meals",), target_recovery_amount=100,
        allow_exceed_target=False, base_fatigue_reserve=-100,
    )
    session.computed_fatigue = 50
    session.items.append({"meal": {"kind": "work_meals", "fatigue_recovery": 36},
                          "consumed": False, "completed": False, "phase": "selected"})
    meal = session.items[-1]["meal"]
    session.record(meal, "consumption_pending")
    assert (session.total_recovered, session.computed_fatigue) == (0, 50)
    session.record(meal, "consumption_confirmed")
    session.record(meal, "consumption_confirmed")
    assert (session.total_recovered, session.computed_fatigue) == (36, 14)


@pytest.mark.parametrize("kind", ["work_meals", "love_bentos"])
def test_multiple_portions_scroll_to_top_only_once_after_enter(monkeypatch, kind):
    session = AutoBentoConsumptionSession(
        app=object(), ocr=None, vision=None,
        layout={"total_timeout_sec": 30, "max_portions": 5}, recovery_layout={}, catalog={},
        priority=(kind,), target_recovery_amount=100,
        allow_exceed_target=False, base_fatigue_reserve=-100,
    )
    events = []
    candidates = iter(({"kind": kind, "fatigue_recovery": 36},
                       {"kind": kind, "fatigue_recovery": 36}, None))
    monkeypatch.setattr(session, "enter", lambda: events.append("enter"))
    monkeypatch.setattr(session, "scroll_top", lambda: events.append("top"))
    monkeypatch.setattr(session, "read_initial_fatigue", lambda: 200)

    def choose_next():
        events.append("scan")
        candidate = next(candidates)
        return candidate, "selected" if candidate else "no_available_meals"

    def consume(candidate):
        events.append("consume")
        session.items.append({"meal": candidate, "consumed": True,
                              "completed": True, "phase": "completed"})
        session.total_recovered += candidate["fatigue_recovery"]
        session.computed_fatigue -= candidate["fatigue_recovery"]

    monkeypatch.setattr(session, "choose_next", choose_next)
    monkeypatch.setattr(session, "consume", consume)

    def return_main():
        events.append("return")
        session.page = "city_main"

    monkeypatch.setattr(session, "return_main", return_main)

    result = session.run_auto()
    assert events == ["enter", "top", "scan", "consume", "scan", "consume", "scan", "return"]
    assert result["success"] is True and result["consumed_count"] == 2
    assert result["recovered_fatigue"] == 72 and result["page_state"] == "city_main"
