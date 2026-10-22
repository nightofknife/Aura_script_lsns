"""Boundary and scene-policy checks for the shared bento observations."""
from types import SimpleNamespace as NS

import numpy as np
import pytest

from plans.resonance_pc.src.actions import bento_auto_recovery_pc_actions as auto
from plans.resonance_pc.src.actions import love_bento_pc_actions as love
from plans.resonance_pc.src.actions import player_recovery_pc_actions as recovery
from plans.resonance_pc.src.actions._bento_observation import (
    ConsecutiveFrameStability, frames_unchanged, work_meal_slot_state,
)


class CaptureApp:
    def __init__(self):
        self.captures = []

    def capture(self, rect):
        self.captures.append(tuple(rect))
        return NS(success=True, image=np.full((rect[3], rect[2], 3),
                                             len(self.captures), dtype=np.uint8))


@pytest.mark.parametrize("require_finite", [False, True])
@pytest.mark.parametrize("hits, expected", [
    ([(True, .7), (True, .89)], "present"),
    ([(True, .69), (True, .9)], "absent"),
    ([(True, .7), (True, .9)], "ambiguous_both"),
    ([(False, 1.), (False, 1.)], "unrecognized_neither"),
    ([(True, .69), (True, .89)], "unrecognized_neither"),
])
def test_slot_uses_each_threshold_and_found_flag(hits, expected, require_finite):
    pair = [NS(found=found, confidence=score) for found, score in hits]
    assert work_meal_slot_state(pair, [.7, .9], require_finite=require_finite) == expected


def test_nonfinite_policy_remains_specific_to_consumption():
    hits = [NS(found=True, confidence=float("inf")), NS(found=False, confidence=0)]
    assert work_meal_slot_state(hits, [.7, .9]) == "present"
    assert work_meal_slot_state(hits, [.7, .9], require_finite=True) == "unrecognized_neither"
    hits[0].confidence = float("nan")
    assert work_meal_slot_state(hits, [.7, .9]) == "unrecognized_neither"


def test_frame_tolerance_boundaries_and_unsigned_subtraction():
    original = np.zeros((10, 10, 3), dtype=np.uint8)
    changed = original.copy()
    changed[0, 0] = 15
    assert frames_unchanged(original, changed)
    changed[0, 0] = 16
    assert not frames_unchanged(original, changed)  # Exactly 1% is movement.
    assert not frames_unchanged(changed, original)
    changed = np.zeros((20, 20, 3), dtype=np.uint8)
    changed[0, 0] = 255
    assert frames_unchanged(np.zeros_like(changed), changed)


def test_missing_content_resets_the_stable_baseline():
    stability = ConsecutiveFrameStability(lambda a, b: a == b)
    assert [stability.observe(value) for value in (1, 1)] == [0, 1]
    stability.reset()
    assert [stability.observe(value) for value in (1, 1, 2, 2, 2)] == [0, 1, 0, 1, 2]


@pytest.fixture
def clock(monkeypatch):
    now, events = [0.], []
    monkeypatch.setattr(love.time, "monotonic", lambda: now[0])
    def sleep(delay):
        events.append(("sleep", delay))
        now[0] += delay
    monkeypatch.setattr(love.time, "sleep", sleep)
    return now, events


def test_love_stability_keeps_initial_capture_then_sleep_and_override(clock):
    now, events = clock
    scanner = object.__new__(love.LoveBentoScanner)
    scanner.deadline = 10
    scanner.cfg = {"page_timeout_sec": 2, "poll_interval_sec": .2}
    pending = iter((1, 1, 2, 2, 2))
    def capture():
        value = next(pending)
        events.append(("capture", value))
        return value
    scanner.capture = capture
    scanner.unchanged = lambda a, b: a == b
    assert scanner.stable_frame() == 2
    assert events == [("capture", 1), ("sleep", .2), ("capture", 1),
                      ("sleep", .2), ("capture", 2), ("sleep", .2),
                      ("capture", 2), ("sleep", .2), ("capture", 2)]
    assert now[0] == pytest.approx(.8)


@pytest.mark.parametrize("interruption", ["missing_page", "unloaded_content"])
def test_recovery_requires_new_stable_baseline_after_loading_interruption(clock, interruption):
    now, _ = clock
    cfg = {"viewport": (0, 0, 20, 20), "total_timeout_sec": 2,
           "max_drags": 1, "stationary_confirmations": 1}
    reader = recovery.RecoveryReader(None, None, None, {
        "bento_navigation": cfg, "bento_ready_timeout_sec": 1, "poll_interval_sec": .1,
    })
    observations = iter((True, True, False, True, True, True))
    ready = lambda *args, **kwargs: next(observations, True)
    reader.is_page = ready if interruption == "missing_page" else lambda page, **kwargs: True
    reader._bento_content_loaded = ready if interruption == "unloaded_content" else lambda frame: True
    reader._bento_top_visible = lambda frame: True
    drags = []
    reader.app = CaptureApp()
    reader.app.drag = lambda *args, **kwargs: drags.append(now[0]) or NS(success=True)
    reader.prepare_bento_read()
    assert drags == [pytest.approx(.5)]


def slot_layout():
    return {
        "templates": {"bento_present": {"resolved_path": "present", "threshold": .7},
                      "bento_absent": {"resolved_path": "absent", "threshold": .9}},
        "bento_region": (0, 0, 6, 2),
        "bento_slots": [{"issue_time": issue, "roi": (i * 2, 0, 2, 2)}
                        for i, issue in enumerate(("05:00", "12:00", "18:00"))],
        "bento_ready_timeout_sec": .6, "poll_interval_sec": .1,
    }


def pair(available):
    return [NS(found=True, confidence=.7 if available else .69, debug_info={}),
            NS(found=True, confidence=.89 if available else .9, debug_info={})]


def test_recovery_accepts_one_round_captured_as_one_frame(clock, monkeypatch):
    layout = slot_layout()
    matches, observations = [], []
    pending = iter((True, False, True))
    def batch(**kwargs):
        matches.append(kwargs)
        return pair(next(pending))
    app = CaptureApp()
    reader = recovery.RecoveryReader(app, None, NS(find_templates_batch=batch), layout)
    reader.is_page = lambda page, **kwargs: True
    def info(message, *args):
        if "observation=%s" in message:
            observations.append(args[-1])
    monkeypatch.setattr(recovery.logger, "info", info)
    result = reader.read_work_meals()
    assert result["available_count"] == 2 and "degraded" not in result
    assert app.captures == [(0, 0, 1280, 720)]
    assert all(row["threshold"] == .7 and row["source_image"].shape == (2, 2, 3)
               for row in matches)
    assert [int(row["source_image"][0, 0, 0]) for row in matches] == [1, 1, 1]
    assert observations[1] == {
        "issue_time": "12:00", "roi": [2, 0, 2, 2], "result": "absent",
        "matches": {"bento_present": {"found": True, "confidence": .69, "threshold": .7},
                    "bento_absent": {"found": True, "confidence": .9, "threshold": .9}},
    }


def auto_session(clock, hit_rounds):
    now, _ = clock
    session = object.__new__(auto.AutoBentoConsumptionSession)
    session.reader = NS(layout=slot_layout())
    session.layout = {"timeout_sec": .6, "poll_interval_sec": .1}
    session.deadline = 10
    session.guard = lambda: None
    session.is_cabinet = lambda **kwargs: True
    session.sleep = lambda: now.__setitem__(0, now[0] + .1)
    session.app = CaptureApp()
    session.match_frames = []
    pending = iter(hit_rounds)
    def batch(**kwargs):
        session.match_frames.append(int(kwargs["source_image"][0, 0, 0]))
        return next(pending)
    session.vision = NS(find_templates_batch=batch)
    return session, session.app.captures


def test_auto_slots_reset_between_rounds_and_share_one_capture_per_round(clock):
    good = [pair(True), pair(False), pair(True)]
    ambiguous = [NS(found=True, confidence=1, debug_info={})] * 2
    session, captures = auto_session(clock, good + [ambiguous] + good + good)
    assert session.work_slots() == [0, 2]
    assert captures == [(0, 0, 1280, 720)] * 4
    assert session.match_frames == [1, 1, 1, 2, 3, 3, 3, 4, 4, 4]
    assert clock[0][0] == pytest.approx(.3)


@pytest.mark.parametrize("broken", [[], [NS(found=True, confidence=1, debug_info={"error": "failed"})] * 2])
def test_matcher_failure_preserves_each_scene_error(clock, broken):
    session, _ = auto_session(clock, [broken])
    with pytest.raises(auto.BentoConsumptionError) as caught:
        session.work_slots()
    assert caught.value.code == "bento_work_slot_match_failed"
    reader = recovery.RecoveryReader(CaptureApp(), None, NS(find_templates_batch=lambda **kwargs: broken), slot_layout())
    reader.is_page = lambda page, **kwargs: True
    expected = "bento template result count mismatch" if not broken else "bento template matching failed"
    with pytest.raises(recovery.StopTaskException, match=expected):
        reader.read_work_meals()


def test_auto_timeout_never_inherits_recovery_assumed_empty(clock):
    ambiguous = [NS(found=True, confidence=1, debug_info={})] * 2
    session, _ = auto_session(clock, [ambiguous] * 6)
    with pytest.raises(auto.BentoConsumptionError) as caught:
        session.work_slots()
    assert caught.value.code == "bento_work_slots_unrecognized"
