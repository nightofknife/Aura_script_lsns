"""Pixel-backed contracts for observation rounds and input freshness.

Keep the production snapshot/capture/match/text/page readers intact. The fake
boundary encodes a monotonically increasing capture ID in every pixel and reads
page state from that captured ID, never from mutable live UI state.
"""
from collections import defaultdict
from types import SimpleNamespace as NS

import numpy as np
import pytest

from packages.aura_core.context.persistence.persistent_data_service import PersistentDataService
from plans.resonance_pc.src.actions import bento_consumption_pc_actions as bento


class PixelUI:
    def __init__(self, *, prompt=True, before_capture=None):
        self.page = "cabinet"
        self.stars = 0
        self.prompt = prompt
        self.before_capture = before_capture
        self.frames = {}
        self.events = []
        self.reads = defaultdict(list)
        self.name = "Food"
        self.disabled = False
        self.use_visible = True

    def capture(self, rect):
        number = len(self.frames) + 1
        if self.before_capture:
            self.before_capture(self, number)
        self.frames[number] = (self.page, self.stars, self.name, self.disabled, self.use_visible)
        self.events.append(("capture", number, self.page, tuple(rect)))
        x, y, w, h = rect
        image = np.empty((h, w, 3), dtype=np.uint8)
        image[:, :, 0] = number
        image[:, :, 1] = np.arange(x, x + w, dtype=np.uint16) % 256
        image[:, :, 2] = (np.arange(y, y + h, dtype=np.uint16) % 256)[:, None]
        return NS(success=True, image=image)

    def find_template(self, *, source_image, template_image, **kwargs):
        number = int(source_image[0, 0, 0])
        assert np.all(source_image[:, :, 0] == number)
        page, stars, _, disabled, use_visible = self.frames[number]
        key = template_image
        x, y = map(int, source_image[0, 0, 1:])
        self.reads[number].append((key, x, y))
        found = False
        if key in ("bento_page", "bento_train", "selected_marker"):
            found = page == "cabinet"
        elif key in ("use_love", "use_work"):
            found = page == "cabinet" and use_visible
        elif key == "use_disabled":
            found = page == "cabinet" and disabled
        elif key == "confirm_use":
            found = page == "confirm"
        elif key == "effect":
            found = page == "effect"
        elif key == "rating_page":
            found = page == "rating"
        elif key == "rating_confirm":
            found = page == "rating" and stars > 0
        elif key in ("star_on", "star_off"):
            index = (x + 2) // 10
            found = page == "rating" and ((index <= stars) == (key == "star_on"))
        else:
            pytest.fail(f"Unexpected template {key}")
        return NS(found=found, confidence=1.0 if found else 0.0,
                  center_point=(2, 2) if found else None, debug_info={})

    def recognize_all(self, *, source_image):
        number = int(source_image[0, 0, 0])
        assert np.all(source_image[:, :, 0] == number)
        self.reads[number].append(("ocr",))
        return NS(results=[NS(text=self.frames[number][2], confidence=1.0)])

    def click(self, x, y):
        self.events.append(("click", len(self.frames), self.page, (x, y)))
        if (x, y) == (10, 10):
            return
        if self.page == "cabinet" and (x, y) == (20, 20):
            self.page = "confirm" if self.prompt else "effect"
        elif self.page == "confirm" and (x, y) == (30, 30):
            self.page = "effect"
        elif self.page == "effect" and (x, y) == (40, 40):
            self.page = "rating"
        elif self.page == "rating" and y == 50:
            self.stars = x // 10
        elif self.page == "rating" and (x, y) == (60, 60):
            self.page = "cabinet"
        else:
            pytest.fail(f"Unexpected click {self.page}: {(x, y)}")


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(bento.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(bento.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    return now


def session_for(tmp_path, monkeypatch, *, before_capture=None):
    ui = PixelUI(before_capture=before_capture)
    keys = ("selected_marker", "use_love", "use_disabled", "confirm_use", "effect",
            "rating_page", "rating_confirm", "star_on", "star_off", "bento_page", "bento_train")
    templates = {key: {"roi": [0, 0, 4, 4], "threshold": .9, "resolved_path": key} for key in keys}
    for key, point in {"use_love": (20, 20), "confirm_use": (30, 30),
                       "rating_confirm": (60, 60)}.items():
        templates[key]["roi"] = [point[0] - 2, point[1] - 2, 4, 4]
    layout = {"total_timeout_sec": 30, "timeout_sec": 1, "animation_timeout_sec": 2,
              "poll_interval_sec": .1, "blank_point": [40, 40],
              "star_points": [[10 * i, 50] for i in range(1, 6)],
              "templates": templates, "detail_name_roi": [100, 100, 20, 10],
              "ocr_min_confidence": .8}
    meal = {"kind": "love_bentos", "role_id": 7, "role_name": "Role", "food_id": 83300011,
            "food_name": "Food", "remaining_days": 1, "fatigue_recovery": 42, "rating_stars": 3}
    catalog = {"items": [{"id": meal["food_id"], "name": "Food", "fatigue_recovery": 42,
                           "rating_stars": 3}]}
    store = PersistentDataService(tmp_path)
    store.merge("user-info.json", ["recovery"], {"love_bentos": {"count": 1, "items": [
        {key: meal[key] for key in ("role_id", "role_name", "food_id", "food_name", "remaining_days")}]}})
    session = bento.BentoConsumptionSession(ui, ui, ui, store, layout, {"templates": templates}, catalog)
    monkeypatch.setattr(session, "locate", lambda chosen: [10, 10])
    session.selected_roi = [80, 80, 4, 4]
    return session, ui, meal


def test_selected_and_rated_rounds_share_pixels_but_all_input_boundaries_refresh(tmp_path, monkeypatch, clock):
    session, ui, meal = session_for(tmp_path, monkeypatch)
    assert session.consume(meal)
    selected_reads = [("bento_page", 0, 0), ("bento_train", 0, 0), ("ocr",),
                      ("selected_marker", 80, 80), ("use_love", 18, 18), ("use_disabled", 0, 0)]
    # Two stable rounds and an independent full selection check are three captures.
    assert [ui.reads[n] for n in (1, 2, 3)] == [selected_reads] * 3
    assert ui.reads[4] == [("use_love", 18, 18)]  # After pending persistence, before use.
    assert [ui.reads[n] for n in (5, 6, 7)] == [[("effect", 0, 0), ("confirm_use", 28, 28)]] * 3
    assert [ui.reads[n] for n in (8, 9)] == [[("effect", 0, 0)]] * 2
    assert ui.reads[10] == [("rating_page", 0, 0)]
    rating_reads = [("rating_page", 0, 0), ("star_on", 8, 48), ("star_on", 18, 48),
                    ("star_on", 28, 48), ("star_off", 38, 48), ("star_off", 48, 48),
                    ("rating_confirm", 58, 58)]
    assert [ui.reads[n] for n in (11, 12)] == [rating_reads] * 2
    assert ui.reads[13] == [("rating_confirm", 58, 58)]  # Fresh immediately before rating input.
    assert [ui.reads[n] for n in (14, 15)] == [[("bento_page", 0, 0), ("bento_train", 0, 0)]] * 2
    assert len(ui.frames) == 15
    assert [event for event in ui.events if event[0] == "click"] == [
        ("click", 0, "cabinet", (10, 10)), ("click", 4, "cabinet", (20, 20)),
        ("click", 7, "confirm", (30, 30)), ("click", 9, "effect", (40, 40)),
        ("click", 10, "rating", (30, 50)), ("click", 13, "rating", (60, 60))]
    # Exact event order proves each next operation observes pixels captured after input.
    for index, event in enumerate(ui.events):
        if event[0] == "click":
            following = ui.events[index + 1]
            assert following[0] == "capture" and following[1] > event[1]
    assert session.items[0]["consumed"] and session.items[0]["completed"]


@pytest.mark.parametrize("changed_at", [3, 4])
def test_selection_and_immediate_use_rechecks_block_changed_ui(tmp_path, monkeypatch, clock, changed_at):
    def change(ui, number):
        if number == changed_at:
            if changed_at == 3:
                ui.name = "OtherDish"
            else:
                ui.use_visible = False
    session, ui, meal = session_for(tmp_path, monkeypatch, before_capture=change)
    with pytest.raises(bento.BentoConsumptionError) as error:
        session.consume(meal)
    assert error.value.code == "bento_target_changed"
    assert len(ui.frames) == changed_at
    assert [event[3] for event in ui.events if event[0] == "click"] == [(10, 10)]
    assert not any(item["consumed"] for item in session.items)


def test_effect_arriving_on_confirmation_input_recheck_prevents_confirm_click(tmp_path, monkeypatch, clock):
    def change(ui, number):
        if number == 7:
            ui.page = "effect"
    session, ui, meal = session_for(tmp_path, monkeypatch, before_capture=change)
    assert session.consume(meal)
    assert ui.reads[5] == ui.reads[6] == [("effect", 0, 0), ("confirm_use", 28, 28)]
    assert ui.reads[7] == ui.reads[8] == [("effect", 0, 0)]
    clicks = [event[3] for event in ui.events if event[0] == "click"]
    assert (30, 30) not in clicks
    assert clicks.count((20, 20)) == 1
    assert session.items[0]["consumed"] and session.items[0]["completed"]


@pytest.mark.parametrize("unstable_phase", ["selection", "rating"])
def test_stability_requires_two_consecutive_new_frames(tmp_path, monkeypatch, clock, unstable_phase):
    def change(ui, number):
        if unstable_phase == "selection":
            if number == 2:
                ui.name = "OtherDish"
            elif number == 3:
                ui.name = "Food"
        else:
            if number == 12:
                ui.stars = 1
            elif number == 13:
                ui.stars = 3
    session, ui, meal = session_for(tmp_path, monkeypatch, before_capture=change)
    assert session.consume(meal)
    clicks = [event for event in ui.events if event[0] == "click"]
    if unstable_phase == "selection":
        # Frame 2 breaks stability; 3 and 4 must both succeed before the full
        # recheck on frame 5 and immediate use-button check on frame 6.
        assert next(event[1] for event in clicks if event[3] == (20, 20)) == 6
        assert ui.reads[2] == [("bento_page", 0, 0), ("bento_train", 0, 0), ("ocr",)]
        assert ui.reads[1] == ui.reads[3] == ui.reads[4] == ui.reads[5]
    else:
        # Frame 12 breaks rating stability; 13 and 14 must succeed and 15
        # supplies the fresh confirmation button immediately before clicking.
        assert next(event[1] for event in clicks if event[3] == (60, 60)) == 15
        assert ui.reads[12] == [("rating_page", 0, 0), ("star_on", 8, 48), ("star_on", 18, 48)]
        assert ui.reads[11] == ui.reads[13] == ui.reads[14]
        assert ui.reads[15] == [("rating_confirm", 58, 58)]
