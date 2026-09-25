"""Offline checks for the independent, in-session bento recovery task."""

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from plans.aura_base.src.services.vision_service import VisionService
from plans.resonance_pc.src.actions.bento_auto_recovery_pc_actions import (
    AutoBentoConsumptionSession,
    decode_recovery,
    load_auto_layout,
    validate_auto_inputs,
)


FIXTURES = Path("tests/fixtures/bento_recovery_digits")


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
