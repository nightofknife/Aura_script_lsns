"""Exercise real frame cropping and input boundaries with numbered pixels."""
import asyncio
from collections import defaultdict
import json
from types import SimpleNamespace as NS

import numpy as np
import pytest
from PIL import Image

from plans.resonance_pc.src.actions import sparkling_water_pc_actions as water
from packages.aura_core.observability import failure_evidence


class PixelUI:
    def __init__(self, layout, visible, before_capture=None):
        self.layout = layout
        self.visible = set(visible)
        self.before_capture = before_capture
        self.frames = {}
        self.events = []
        self.reads = defaultdict(list)
        self.scores = {}
        self.centers = {}

    def capture(self, rect):
        number = len(self.frames) + 1
        if self.before_capture:
            self.before_capture(self, number)
        self.frames[number] = set(self.visible)
        self.events.append(("capture", number, tuple(rect)))
        x, y, w, h = rect
        image = np.empty((h, w, 3), dtype=np.uint8)
        image[:, :, 0] = number
        image[:, :, 1] = np.arange(x, x + w, dtype=np.uint16) % 256
        image[:, :, 2] = (np.arange(y, y + h, dtype=np.uint16) % 256)[:, None]
        return NS(success=True, image=image)

    def find_template(self, *, source_image, template_image, **kwargs):
        key = template_image
        number = int(source_image[0, 0, 0])
        x, y, w, h = self.layout["templates"][key]["roi"]
        assert source_image.shape == (h, w, 3)
        assert np.all(source_image[:, :, 0] == number)
        assert list(source_image[0, 0, 1:]) == [x % 256, y % 256]
        self.reads[number].append(key)
        self.events.append(("match", number, key))
        found = key in self.frames[number]
        return NS(found=found, confidence=self.scores.get((number, key), 1.0 if found else 0.0),
                  center_point=self.centers.get(number, (10, 10)) if found else None,
                  debug_info={})

    def click(self, x, y, *args):
        self.events.append(("click", len(self.frames), (x, y)))
        if (x, y) == tuple(self.layout["skip_point"]):
            self.visible = {"sparkling_water"}
        else:
            rx, ry, w, h = self.layout["templates"]["drink_again"]["roi"]
            assert rx <= x < rx + w and ry <= y < ry + h
            self.visible = set()


@pytest.fixture
def make_session(monkeypatch, tmp_path):
    now = [100.0]
    real_sleep = asyncio.sleep

    async def sleep(delay):
        now[0] += delay
        await real_sleep(0)

    monkeypatch.setattr(water.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(water.asyncio, "sleep", sleep)
    session = NS(root=tmp_path, session_id="water-session", available=True,
                              errors_dir=tmp_path / "errors" / "water-session")
    monkeypatch.setattr(water, "get_log_session", lambda: session)
    monkeypatch.setattr(failure_evidence, "get_log_session", lambda: session)

    def create(visible=(), before_capture=None):
        layout = json.loads(water.CONFIG_PATH.read_text(encoding="utf-8"))
        for key, spec in layout["templates"].items():
            spec["resolved_path"] = key
        ui = PixelUI(layout, visible, before_capture)
        return water.SparklingWaterSession(app=ui, vision=ui, layout=layout), ui

    return create


@pytest.mark.parametrize("visible,confirmed,page,reads", [
    ({"drink_again", "rest_menu", "sparkling_water"}, True, None, ["drink_again"]),
    ({"rest_menu", "sparkling_water"}, False, "rest_menu", ["drink_again", "rest_menu"]),
    ({"sparkling_water"}, False, "drink_menu", ["drink_again", "rest_menu", "sparkling_water"]),
    (set(), False, None, ["drink_again", "rest_menu", "sparkling_water"]),
])
def test_round_shares_one_cropped_frame_and_preserves_short_circuit_priority(
    make_session, visible, confirmed, page, reads,
):
    session, ui = make_session(visible)
    confirmation, observed_page = session._observe_animation()
    assert confirmation["found"] is confirmed and observed_page == page
    assert ui.reads == {1: reads}
    assert ui.events[0] == ("capture", 1, (735, 390, 240, 155))
    assert len(ui.frames) == 1


def test_confirmation_relocates_on_fresh_preclick_frame_and_inputs_get_new_followup_frames(make_session):
    session, ui = make_session({"drink_again"})
    ui.centers[2] = (25, 30)
    assert asyncio.run(session.finish_cup()) == ("drink_menu", True)
    assert len(ui.frames) == 5
    assert dict(ui.reads) == {
        1: ["drink_again"], 2: ["drink_again"], 3: ["drink_again"],
        4: ["drink_again", "rest_menu", "sparkling_water"],
        5: ["drink_again", "rest_menu", "sparkling_water"],
    }
    assert [event for event in ui.events if event[0] == "click"] == [
        ("click", 2, (920, 495)), ("click", 4, (1207, 36)),
    ]
    captures = [event for event in ui.events if event[0] == "capture"]
    assert [event[2] for event in captures] == [
        (735, 390, 240, 155), (895, 465, 80, 80), (895, 465, 80, 80),
        (735, 390, 240, 155), (735, 390, 240, 155),
    ]
    for index, event in enumerate(ui.events):
        if event[0] == "click":
            assert ui.events[index + 1][0] == "capture"
            assert ui.events[index + 1][1] > event[1]


def test_disappearing_confirmation_in_preclick_frame_cannot_authorize_click(make_session):
    def disappear(ui, number):
        if number >= 2:
            ui.visible.clear()

    session, ui = make_session({"drink_again"}, disappear)
    with pytest.raises(water.SparklingWaterError) as error:
        asyncio.run(session.finish_cup())
    assert error.value.code == "sparkling_water_transition_timeout"
    assert len(ui.frames) > 2
    assert not any(event[0] == "click" for event in ui.events)
    assert all(reads == ["drink_again"] for reads in ui.reads.values())


@pytest.mark.parametrize("key", ["drink_again", "rest_menu", "sparkling_water"])
@pytest.mark.parametrize("score", [float("nan"), float("inf"), float("-inf")])
def test_uncertain_rounds_skip_input_refresh_and_keep_first_cropped_evidence(make_session, key, score):
    session, ui = make_session({"sparkling_water"})
    ui.scores[1, key] = ui.scores[2, key] = score
    assert asyncio.run(session.finish_cup()) == ("drink_menu", False)
    assert len(ui.frames) == 3
    order = ["drink_again", "rest_menu", "sparkling_water"]
    assert ui.reads[1] == ui.reads[2] == order[:order.index(key) + 1]
    assert ui.reads[3] == order
    assert not any(event[0] == "click" for event in ui.events)
    assert session.uncertain_frames == 2
    evidence = session.first_uncertain_frame
    assert evidence["target"] == key and evidence["stage"] == "drink_animation"
    assert evidence["roi"] == ui.layout["templates"][key]["roi"]
    images = list(water.get_log_session().errors_dir.glob("*.png"))
    assert len(images) == 1
    assert not list(water.get_log_session().errors_dir.glob("*.json"))
    saved = np.array(Image.open(images[0]))
    # cv2 writes the supplied RGB crop as BGR; channel 2 retains capture ID.
    assert np.all(saved[:, :, 2] == 1)
    assert saved.shape[:2] == (evidence["roi"][3], evidence["roi"][2])


def test_unknown_postclick_frame_does_not_authorize_repeat_confirmation(make_session):
    def transition(ui, number):
        if number == 4:
            ui.visible = {"drink_again"}
        elif number == 5:
            ui.visible.clear()

    session, ui = make_session({"drink_again"}, transition)
    ui.scores[3, "drink_again"] = float("nan")
    assert asyncio.run(session.finish_cup()) == ("drink_menu", True)
    assert len(ui.frames) == 7
    assert ui.frames[4] == {"drink_again"}
    assert [event for event in ui.events if event[0] == "click"] == [
        ("click", 2, (905, 475)), ("click", 6, (1207, 36)),
    ]
    assert all(ui.reads[number] == ["drink_again"] for number in (2, 3, 4, 5))
    assert session.uncertain_frames == 1
