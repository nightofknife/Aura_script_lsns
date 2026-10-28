"""Replay independent captures through the real automatic-bento reading layer."""
from types import SimpleNamespace as NS
from pathlib import Path

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import bento_auto_recovery_pc_actions as auto
from plans.resonance_pc.src.actions import bento_consumption_pc_actions as consumption


PAGE_ROIS = ((10, 10, 2, 2), (20, 10, 2, 2))
SLOT_ROIS = tuple((10 + index * 10, 30, 2, 2) for index in range(3))
FATIGUE_ROI = (1139, 20, 132, 40)
FATIGUE_IMAGE = cv2.imread(str(Path("tests/fixtures/bento_consumption/work_usable.png")))[20:60, 1139:1271]


class ReadingReplay:
    def __init__(self, rounds):
        self.rounds = rounds
        self.captures = []
        self.probes = []
        self.current_round = 0
        self.ocr_side = 0

    def capture(self, rect):
        # Exhausting the script is a failure, not an endlessly repeated last frame.
        number = len(self.captures) + 1
        state = self.rounds[number - 1]
        image = np.zeros((720, 1280, 3), dtype=np.uint8)
        image[:, :, 0] = number
        x, y, w, h = FATIGUE_ROI
        image[y:y+h, x:x+w] = state.get("fatigue_pixels", FATIGUE_IMAGE)
        for index, (x, y, w, h) in enumerate(PAGE_ROIS):
            image[y:y+h, x:x+w, 1] = state.get("page", (True, True))[index]
        for index, (x, y, w, h) in enumerate(SLOT_ROIS):
            image[y:y+h, x:x+w, 1] = index
        x, y, w, h = rect
        self.captures.append((number, tuple(rect)))
        self.current_round, self.ocr_side = number, 0
        return NS(success=True, image=image[y:y+h, x:x+w].copy())

    def find_template(self, *, source_image, template_image, **kwargs):
        number = int(source_image[0, 0, 0])
        self.probes.append((number, template_image))
        return NS(found=bool(source_image[0, 0, 1]), confidence=1., debug_info={})

    def find_templates_batch(self, *, source_image, template_images, **kwargs):
        number, slot = map(int, source_image[0, 0, :2])
        self.probes.append((number, f"slot{slot}"))
        present = self.rounds[number - 1]["slots"][slot]
        return [NS(found=True, confidence=float(present), debug_info={}),
                NS(found=True, confidence=float(not present), debug_info={})]

    def recognize_all(self, *, source_image):
        number = self.current_round
        side = self.ocr_side
        self.ocr_side += 1
        assert side in (0, 1), "Read each digit group once per observation"
        self.probes.append((number, ("fatigue_current", "fatigue_maximum")[side]))
        state = self.rounds[number - 1]
        values = state["fatigue"].split("/")
        if len(values) != 2:
            values = [state["fatigue"]] * 2
        confidence = state.get("confidence", (1., 1.))[side]
        return NS(success=True, results=[NS(text=values[side], confidence=confidence)])


@pytest.fixture
def make_session(monkeypatch):
    now = [0.]
    monkeypatch.setattr(auto.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(auto.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))

    def make(rounds, **layout_options):
        replay = ReadingReplay(rounds)
        replay.clock = now
        layout = {"total_timeout_sec": 10., "timeout_sec": 2., "poll_interval_sec": .1,
                  "fatigue_ratio_roi": FATIGUE_ROI, "ocr_min_confidence": .8,
                  **layout_options}
        recovery_layout = {
            "templates": {
                "bento_page": {"roi": PAGE_ROIS[0], "resolved_path": "cabinet", "threshold": .9},
                "bento_train": {"roi": PAGE_ROIS[1], "resolved_path": "train", "threshold": .9},
                "bento_present": {"resolved_path": "present", "threshold": .7},
                "bento_absent": {"resolved_path": "absent", "threshold": .9},
            },
            "bento_slots": [{"roi": roi} for roi in SLOT_ROIS],
        }
        session = auto.AutoBentoConsumptionSession(
            replay, replay, replay, layout, recovery_layout, {}, priority=("work_meals",),
            bento_start_threshold=700, bento_stop_threshold=200,
        )
        return session, replay
    return make


def assert_rounds(replay, expected_names, count):
    assert replay.captures == [(number, (0, 0, 1280, 720)) for number in range(1, count + 1)]
    assert replay.probes == [(number, name) for number in range(1, count + 1) for name in expected_names]


def test_three_work_slots_and_page_markers_share_each_round_but_stability_uses_new_frames(make_session):
    session, replay = make_session([
        {"slots": (True, False, True)},
        {"slots": (False, True, False)},
        {"slots": (False, True, False)},
    ])
    assert session.work_slots() == [1]
    assert_rounds(replay, ["cabinet", "train", "slot0", "slot1", "slot2"], 3)


def test_page_loss_on_next_round_is_not_hidden_by_previous_frame(make_session):
    session, replay = make_session([
        {"slots": (True, False, True)},
        {"page": (True, False), "slots": (True, False, True)},
    ])
    with pytest.raises(auto.BentoConsumptionError) as caught:
        session.work_slots()
    assert caught.value.code == "bento_cabinet_missing"
    assert replay.probes == [(1, name) for name in ("cabinet", "train", "slot0", "slot1", "slot2")] + [
        (2, "cabinet"), (2, "train")]
    assert len(replay.captures) == 2


@pytest.mark.parametrize("middle", ["210/600", "unreadable"])
def test_fatigue_ocr_shares_page_frame_and_requires_consecutive_agreement(make_session, middle):
    session, replay = make_session([
        {"fatigue": "200/600"}, {"fatigue": middle},
        {"fatigue": "200/600"}, {"fatigue": "200/600"},
    ])
    assert session.read_fatigue() == 200
    assert_rounds(replay, ["cabinet", "train", "fatigue_current", "fatigue_maximum"], 4)


def test_post_consumption_fatigue_uses_new_frames_instead_of_the_initial_observation(make_session):
    session, replay = make_session([
        {"fatigue": "720/1000"}, {"fatigue": "720/1000"},
        {"fatigue": "610/1000"}, {"fatigue": "610/1000"},
    ])
    assert session.observe_fatigue() == 720
    session.items.append({"meal": {"kind": "work_meals"}, "consumed": False,
                          "completed": False, "phase": "selected"})
    session.record(session.items[0]["meal"], "consumption_pending")
    assert session.final_fatigue is None
    session.record(session.items[0]["meal"], "consumption_confirmed")
    session.record(session.items[0]["meal"], "completed")
    assert session.observe_fatigue() == 610
    assert session.final_fatigue == 610
    assert_rounds(replay, ["cabinet", "train", "fatigue_current", "fatigue_maximum"], 4)


def test_over_capacity_fatigue_is_observed_before_and_after_consumption(make_session):
    session, replay = make_session([
        {"fatigue": "1000/872"}, {"fatigue": "1000/872"},
        {"fatigue": "900/872"}, {"fatigue": "900/872"},
    ])
    assert session.observe_fatigue() == 1000
    session.final_fatigue = None
    assert session.observe_fatigue() == 900
    assert session.final_fatigue == session.last_observed_fatigue == 900
    assert_rounds(replay, ["cabinet", "train", "fatigue_current", "fatigue_maximum"], 4)


@pytest.mark.parametrize("invalid", ["900/0", "-1/872", "unreadable"])
def test_invalid_ratio_cannot_stabilize_into_a_fatigue_reading(make_session, invalid):
    session, replay = make_session([
        {"fatigue": invalid}, {"fatigue": invalid},
        {"fatigue": "900/872"}, {"fatigue": "900/872"},
    ])
    assert session.read_fatigue() == 900
    assert_rounds(replay, ["cabinet", "train", "fatigue_current", "fatigue_maximum"], 4)


@pytest.mark.parametrize("confidence", [(0.79, 1.), (1., 0.79)])
def test_low_confidence_on_either_side_resets_stability(make_session, confidence):
    session, replay = make_session([
        {"fatigue": "200/600"}, {"fatigue": "200/600", "confidence": confidence},
        {"fatigue": "200/600"}, {"fatigue": "200/600"},
    ])
    assert session.read_fatigue() == 200
    assert_rounds(replay, ["cabinet", "train", "fatigue_current", "fatigue_maximum"], 4)


def test_maximum_change_resets_stability_even_when_current_is_unchanged(make_session):
    session, replay = make_session([
        {"fatigue": "200/600"}, {"fatigue": "200/700"},
        {"fatigue": "200/600"}, {"fatigue": "200/600"},
    ])
    assert session.read_fatigue() == 200
    assert_rounds(replay, ["cabinet", "train", "fatigue_current", "fatigue_maximum"], 4)


def test_widened_cabinet_roi_preserves_four_digits_on_both_sides(make_session):
    # Rearrange actual HUD glyphs into 3244/8566, right-aligned at the observed
    # edge. Its first digit lies outside the former ROI that began at x=1163.
    glyphs = [(39, 9), (49, 10), (60, 10), (60, 10), (71, 6),
              (79, 9), (90, 8), (99, 10), (99, 10)]
    pixels = np.zeros_like(FATIGUE_IMAGE)
    cursor = 109 - sum(width for _, width in glyphs) - 2 * (len(glyphs) - 1)
    assert cursor < 24
    for left, width in glyphs:
        pixels[8:20, cursor:cursor+width] = FATIGUE_IMAGE[8:20, left:left+width]
        cursor += width + 2
    session, replay = make_session([
        {"fatigue": "3244/8566", "fatigue_pixels": pixels},
        {"fatigue": "3244/8566", "fatigue_pixels": pixels},
    ])
    assert session.read_fatigue() == 3244
    assert_rounds(replay, ["cabinet", "train", "fatigue_current", "fatigue_maximum"], 2)


def test_fatigue_confirmation_cannot_reuse_a_page_that_disappeared(make_session):
    session, replay = make_session([
        {"fatigue": "200/600"},
        {"fatigue": "200/600", "page": (True, False)},
    ])
    with pytest.raises(auto.BentoConsumptionError) as caught:
        session.read_fatigue()
    assert caught.value.code == "bento_cabinet_missing"
    assert replay.probes == [(1, name) for name in (
        "cabinet", "train", "fatigue_current", "fatigue_maximum")] + [
        (2, "cabinet"), (2, "train")]


def test_slow_confirming_ocr_cannot_commit_after_the_local_read_deadline(make_session, monkeypatch):
    session, replay = make_session([{"fatigue": "200/600"}, {"fatigue": "200/600"}])
    recognize = replay.recognize_all

    def slow_second_frame(**kwargs):
        result = recognize(**kwargs)
        if replay.current_round == 2 and replay.ocr_side == 2:
            replay.clock[0] += 3.
        return result

    monkeypatch.setattr(replay, "recognize_all", slow_second_frame)
    with pytest.raises(auto.BentoConsumptionError) as caught:
        session.observe_fatigue()
    assert caught.value.code == "bento_fatigue_unrecognized"
    assert replay.clock[0] < session.deadline
    assert session.final_fatigue is None
    assert_rounds(replay, ["cabinet", "train", "fatigue_current", "fatigue_maximum"], 2)


@pytest.mark.parametrize("interruption", ["cancel", "total_timeout"])
def test_ocr_interruption_is_not_committed_as_a_stable_observation(make_session, monkeypatch, interruption):
    session, replay = make_session([{"fatigue": "200/600"}, {"fatigue": "200/600"}])
    cancelled = [False]
    monkeypatch.setattr(consumption, "is_current_task_cancel_requested", lambda: cancelled[0])
    recognize = replay.recognize_all

    def interrupt_second_frame(**kwargs):
        result = recognize(**kwargs)
        if replay.current_round == 2 and replay.ocr_side == 2:
            if interruption == "cancel":
                cancelled[0] = True
            else:
                replay.clock[0] += 11.
        return result

    monkeypatch.setattr(replay, "recognize_all", interrupt_second_frame)
    with pytest.raises(auto.BentoConsumptionError) as caught:
        session.observe_fatigue()
    assert caught.value.code == ("bento_cancelled" if interruption == "cancel" else "bento_timeout")
    assert session.final_fatigue is None
    assert_rounds(replay, ["cabinet", "train", "fatigue_current", "fatigue_maximum"], 2)
