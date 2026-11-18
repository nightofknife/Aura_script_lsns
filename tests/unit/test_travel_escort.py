"""Exercise road encounters with real template matching and scripted UI transitions."""

import os
from pathlib import Path
import time
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from plans.aura_base.src.services.observation_service import Frame
from plans.aura_base.src.services.vision_service import VisionService
from plans.resonance_pc.src.actions import _travel_escort as escort
from plans.resonance_pc.src.actions import _travel_gear_recovery as gear
from plans.resonance_pc.src.actions import city_travel_pc_actions as travel


_FIGURES = ("engage", "start", "victory", "defeat", "report", "brake")
_CONTROL_POINTS = {
    "engage": (1007, 229), "start": (1201, 376), "victory": (1178, 624),
    "defeat": (1160, 642), "report": (895, 540), "brake": (1247, 615),
}
_TRAVELING = escort.ScreenTemplate(
    travel._TRAVEL_HORN_TEMPLATE, tuple(travel._TRAVEL_HORN_REGION), .90, False,
)


def _read_rgb(path):
    image = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)


def _paste(frame, pixels, center, mask=None):
    h, w = pixels.shape[:2]
    x, y = center[0] - w // 2, center[1] - h // 2
    target = frame[y:y+h, x:x+w]
    if mask is None:
        target[:] = pixels
    else:
        target[mask > 0] = pixels[mask > 0]


@pytest.fixture
def frames():
    blank = np.full((720, 1280, 3), 36, np.uint8)
    directory = os.environ.get("AURA_ESCORT_REPLAY_DIR")
    if directory:
        result = {name: _read_rgb(Path(directory) / f"{index}.png")
                  for index, name in enumerate(_FIGURES, 1)}
    else:
        result = {name: blank.copy() for name in _FIGURES}
        for name in _FIGURES[:-1]:
            pixels = _read_rgb(escort._PLAN_ROOT / escort._SCREENS[name].path)
            _paste(result[name], pixels, _CONTROL_POINTS[name])
        template, mask = gear._template("b")
        _paste(result["brake"], template, (1193, 638), mask)
        _paste(result["brake"], _read_rgb(escort._PLAN_ROOT / _TRAVELING.path), (997, 677))

    result["drive"] = result["brake"].copy()
    gx, gy, gw, gh = gear._GEAR_REGION
    result["drive"][gy:gy+gh, gx:gx+gw] = 36
    template, mask = gear._template("d")
    _paste(result["drive"], template, (1247, 615), mask)
    result["battle"] = blank.copy()
    clock = escort._battle_clock()
    _paste(result["battle"], np.repeat(clock[:, :, None], 3, axis=2), (541, 26))
    result["loading"] = blank
    return result


@pytest.fixture
def scene(monkeypatch, frames, tmp_path):
    state = SimpleNamespace(now=0., key="engage", clicks=[], captures=[], miss=set(), on_click={},
                            arrival=False, loading_until=0., after_loading=None)
    monkeypatch.setattr(time, "monotonic", lambda: state.now)
    monkeypatch.setattr(time, "sleep", lambda delay: setattr(state, "now", state.now + delay))
    monkeypatch.setattr(escort, "resolve_base_path", lambda: tmp_path)
    state.vision = VisionService()

    def capture(*, rect):
        if state.after_loading is not None and state.now >= state.loading_until:
            state.key, state.after_loading = state.after_loading, None
        state.captures.append((state.now, rect, state.key))
        x, y, w, h = rect
        return SimpleNamespace(success=True, image=frames[state.key][y:y+h, x:x+w])

    def click(*, x, y):
        key = state.key
        state.clicks.append((key, (x, y)))
        assert (x, y) == _CONTROL_POINTS[key]
        if key in state.miss:
            state.miss.remove(key)
            return
        target = state.on_click.get(key)
        if target is not None:
            state.key = target
        if key == "brake":
            state.arrival = state.key == "drive"

    state.app = SimpleNamespace(get_window_size=lambda: (1280, 720), capture=capture, click=click)
    state.handler = escort.TravelEscort(state.app, state.vision, started=0.,
                                         check_cancelled=lambda: None, return_screens={"traveling": _TRAVELING})
    return state


def test_single_template_identifies_each_of_the_six_figures(scene, frames):
    for key in (*_FIGURES, "battle"):
        hits = [name for name in _FIGURES if scene.handler._match(
            Frame.from_image(frames[name]), key, scene.handler.scope,
        ).state == "present"]
        assert hits == ([key] if key in _FIGURES else [])


def test_encounter_scan_only_uses_its_region_at_five_second_intervals(scene):
    scene.key = "brake"
    for now in (0., 1., 4.99, 5., 8., 10.):
        scene.now = now
        assert not scene.handler.tick(30.)
    assert scene.captures == [(now, escort._SCREENS["engage"].roi, "brake") for now in (0., 5., 10.)]
    assert scene.clicks == []


@pytest.mark.parametrize("use_battle_hud", [False, True])
def test_victory_rechecks_and_supplements_each_click(scene, use_battle_hud):
    scene.on_click.update(engage="start", start="battle" if use_battle_hud else "victory", victory="brake")
    scene.miss.update(("engage", "start", "victory"))
    if use_battle_hud:
        native_capture = scene.app.capture

        def capture(*, rect):
            if scene.key == "battle" and scene.after_loading is None:
                scene.loading_until, scene.after_loading = scene.now + 1.5, "victory"
            return native_capture(rect=rect)

        scene.app.capture = capture
    assert scene.handler.tick(60.)
    assert scene.handler.count == 1
    assert [name for name, _ in scene.clicks] == ["engage", "engage", "start", "start", "victory", "victory"]


def test_defeat_reports_and_confirms_d_with_pickup_off(scene, monkeypatch):
    scene.on_click.update(engage="start", start="defeat", defeat="report", report="brake", brake="drive")
    scene.miss.update(("defeat", "report", "brake"))
    monkeypatch.setattr(travel, "_click_arrival_template_until_absent", lambda **_:
                        {"confirmed": True, "click_attempts": 1, "arrival_point": (900, 350)}
                        if scene.arrival else {"confirmed": False})
    monkeypatch.setattr(travel, "_detect_city_main_entry", lambda **_: {"found": False})
    outcome = travel.resonance_pc_wait_intercity_arrival(
        app=scene.app, vision=scene.vision, auto_escort=True, timeout_sec=60.,
    )
    assert outcome["encounter_actions"] == 1
    assert outcome["pickup_click_count"] == 0
    assert [name for name, _ in scene.clicks] == ["engage", "start", "defeat", "defeat",
                                                 "report", "report", "brake", "brake"]
    assert len([entry for entry in scene.captures if entry[2] == "drive"]) >= 2


def test_supplements_stop_when_original_screen_is_lost(scene):
    scene.on_click.update(engage="start", start="defeat", defeat="report", report="loading")
    with pytest.raises(escort.TravelEscortError, match="travel_escort_report_unconfirmed") as error:
        scene.handler.tick(60.)
    assert [name for name, _ in scene.clicks].count("report") == 1
    assert Path(error.value.detail["screenshot"]).is_file()


def test_already_in_d_and_multiple_encounters(scene):
    scene.on_click.update(engage="start", start="defeat", defeat="report", report="drive")
    assert scene.handler.tick(60.)
    assert scene.handler.count == 1
    assert scene.key == "drive"
    assert "brake" not in [name for name, _ in scene.clicks]

    scene.key = "engage"
    assert not scene.handler.tick(60.)
    scene.now = scene.handler.next_check_at
    assert scene.handler.tick(60.)
    assert scene.handler.count == 2


def test_default_skips_encounter_capture(scene, monkeypatch):
    monkeypatch.setattr(travel, "_click_arrival_template_until_absent", lambda **_: {"confirmed": False})
    monkeypatch.setattr(travel, "_detect_city_main_entry", lambda **_: {"found": scene.now >= 5})
    result = travel.resonance_pc_wait_intercity_arrival(app=scene.app, vision=scene.vision, timeout_sec=15.)
    assert result["status"] == "arrived"
    assert scene.captures == scene.clicks == []
