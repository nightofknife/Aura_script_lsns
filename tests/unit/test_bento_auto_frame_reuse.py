"""Replay independent captures through the real automatic-bento reading layer."""
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest
from PIL import Image

from plans.aura_base.src.services.vision_service import VisionService
from plans.resonance_pc.src.actions import bento_auto_recovery_pc_actions as auto


PAGE_ROIS = ((10, 10, 2, 2), (20, 10, 2, 2))
SLOT_ROIS = tuple((10 + index * 10, 30, 2, 2) for index in range(3))
DIGIT_ROI = (60, 100, 22, 16)


class ReadingReplay:
    def __init__(self, rounds):
        self.rounds = rounds
        self.captures = []
        self.probes = []

    def capture(self, rect):
        # Exhausting the script is a failure, not an endlessly repeated last frame.
        number = len(self.captures) + 1
        state = self.rounds[number - 1]
        image = np.zeros((720, 1280, 3), dtype=np.uint8)
        image[:, :, 0] = number
        for index, (x, y, w, h) in enumerate(PAGE_ROIS):
            image[y:y+h, x:x+w, 1] = state.get("page", (True, True))[index]
        for index, (x, y, w, h) in enumerate(SLOT_ROIS):
            image[y:y+h, x:x+w, 1] = index
        if "digits" in state:
            x, y, w, h = DIGIT_ROI
            image[y:y+h, x:x+w] = state["digits"]
        x, y, w, h = rect
        self.captures.append((number, tuple(rect)))
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
        number = int(source_image[0, 0, 0])
        self.probes.append((number, "fatigue"))
        return NS(success=True, results=[NS(text=self.rounds[number - 1]["fatigue"], confidence=1.)])


@pytest.fixture
def make_session(monkeypatch):
    now = [0.]
    monkeypatch.setattr(auto.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(auto.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))

    def make(rounds, **layout_options):
        replay = ReadingReplay(rounds)
        layout = {"total_timeout_sec": 10., "timeout_sec": 2., "poll_interval_sec": .1,
                  "fatigue_ratio_roi": (100, 100, 20, 10), "ocr_min_confidence": .8,
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
            target_recovery_amount=100, allow_exceed_target=False, base_fatigue_reserve=0,
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
    assert session.read_initial_fatigue() == 200
    assert_rounds(replay, ["cabinet", "train", "fatigue"], 4)


def test_recovery_digits_are_cropped_from_same_round_and_confirmed_on_new_frames(make_session):
    fixtures = Path("tests/fixtures/bento_recovery_digits")
    def pixels(name):
        with Image.open(fixtures / name) as image:
            return np.asarray(image.convert("RGB"))
    first = pixels("love_first_51.png")
    second = pixels("love_second_41.png")
    config = auto.load_auto_layout(VisionService())["recovery_digits"]
    session, replay = make_session([
        {"digits": first}, {"digits": second}, {"digits": second},
    ], recovery_digits=config)
    assert session.read_recovery("love", DIGIT_ROI) == 41
    assert_rounds(replay, ["cabinet", "train"], 3)
