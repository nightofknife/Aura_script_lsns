"""Drive recovery and pickup run under the same arrival loop."""

import asyncio
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from plans.aura_base.src.services.vision_service import VisionService
from plans.resonance_pc.src.actions import _travel_gear_recovery as gear
from plans.resonance_pc.src.actions import city_travel_pc_actions as travel


@pytest.fixture
def leg(monkeypatch):
    state = SimpleNamespace(
        now=0.0, arrive_at=44.0, cancel_at=float("inf"), gear_at=lambda _: "b",
        clicks=[], captures=[], size=(1280, 720),
    )

    # Render the packaged template at its measured client position on an RGB capture.
    frames = {}
    for name, center in (("b", (1193, 638)), ("d", (1247, 615))):
        path = gear._TEMPLATES / f"gear_{name}_selected.png"
        rgba = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_UNCHANGED)
        x, y, width, height = gear._GEAR_REGION
        frame = np.full((height, width, 3), 64, dtype=np.uint8)
        left, top = center[0] - x - 19, center[1] - y - 19
        sprite = frame[top:top + 38, left:left + 38]
        opaque = rgba[:, :, 3] == 255
        sprite[opaque] = cv2.cvtColor(rgba, cv2.COLOR_BGRA2RGB)[opaque]
        frames[name] = frame

    def capture(*, rect):
        assert rect == gear._GEAR_REGION
        state.captures.append(state.now)
        image = frames.get(state.gear_at(state.now))
        return SimpleNamespace(success=image is not None, image=image)

    def cancel():
        if state.now >= state.cancel_at:
            raise asyncio.CancelledError

    def arrive(**kwargs):
        if state.now >= state.arrive_at:
            return {"confirmed": True, "click_attempts": 1, "arrival_point": (900, 350)}
        return {"confirmed": False}

    monkeypatch.setattr(travel.time, "monotonic", lambda: state.now)
    monkeypatch.setattr(travel.time, "sleep", lambda duration: setattr(state, "now", state.now + duration))
    monkeypatch.setattr(travel, "_check_intercity_cancelled", cancel)
    monkeypatch.setattr(travel, "_click_arrival_template_until_absent", arrive)
    monkeypatch.setattr(travel, "_detect_city_main_entry", lambda **kwargs: {"found": False})
    monkeypatch.setattr(travel, "_match_template_in_region", lambda **kwargs: {"found": True})
    app = SimpleNamespace(
        click=lambda *, x, y: state.clicks.append((state.now, (x, y))),
        get_window_size=lambda: state.size,
        capture=capture,
    )
    state.run = lambda **kwargs: travel.resonance_pc_wait_intercity_arrival(
        app=app, vision=VisionService(), timeout_sec=80.0, interval_sec=0.5, **kwargs,
    )
    return state


DRIVE_POINT = (1247, 615)


def click_times(leg, point):
    return [when for when, target in leg.clicks if target == point]


@pytest.mark.parametrize(("succeed_after", "drive_times", "resumed_at"), [
    (1, [20.0], 20.5),
    (2, [20.0, 20.5], 21.0),
    (3, [20.0, 20.5, 41.0], 41.5),
])
def test_only_confirmed_d_resumes_pickup(leg, succeed_after, drive_times, resumed_at):
    leg.gear_at = lambda _: "d" if len(click_times(leg, DRIVE_POINT)) >= succeed_after else "b"
    result = leg.run(auto_pickup=True)
    assert result["status"] == "arrived"
    assert leg.captures[:3] == [10.0, 20.0, 20.0]  # Scheduled samples and fresh B before the click.
    assert click_times(leg, DRIVE_POINT) == drive_times
    pickups = click_times(leg, travel._AUTO_PICKUP_POINT)
    assert 10.0 in pickups and 19.5 in pickups
    assert next(when for when in pickups if when >= 20.0) == resumed_at


@pytest.mark.parametrize("interruption", [None, "d"])
def test_interrupted_b_samples_start_again(leg, interruption):
    leg.gear_at = lambda now: interruption if now == 20.0 else "b"
    leg.run(auto_pickup=True)
    assert click_times(leg, DRIVE_POINT) == [40.0, 40.5]


def test_manual_d_confirmation_after_retry_resumes_pickup(leg):
    leg.gear_at = lambda now: "d" if now >= 25.0 else "b"
    leg.run(auto_pickup=True)
    assert click_times(leg, DRIVE_POINT) == [20.0, 20.5]
    pickups = click_times(leg, travel._AUTO_PICKUP_POINT)
    assert next(when for when in pickups if when >= 20.0) == 31.0


def test_unknown_confirmation_does_not_authorize_retry_or_pickup(leg):
    leg.arrive_at = 22.0
    leg.gear_at = lambda now: "b" if now <= 20.0 else None
    leg.run(auto_pickup=True)
    assert click_times(leg, DRIVE_POINT) == [20.0]
    assert max(click_times(leg, travel._AUTO_PICKUP_POINT)) == 19.5


def test_long_pickup_cooldown_keeps_gear_schedule_independent(leg):
    leg.gear_at = lambda _: "d" if click_times(leg, DRIVE_POINT) else "b"
    leg.run(auto_pickup=True, auto_pickup_interval_sec=60.0)
    assert click_times(leg, DRIVE_POINT) == [20.0]
    assert click_times(leg, travel._AUTO_PICKUP_POINT) == [0.0]


@pytest.mark.parametrize("end", ["arrival", "cancel"])
def test_arrival_or_cancel_takes_priority_over_pending_retry(leg, end):
    if end == "arrival":
        leg.arrive_at = 20.5
        assert leg.run(auto_pickup=True)["status"] == "arrived"
    else:
        leg.cancel_at = 20.25
        with pytest.raises(asyncio.CancelledError):
            leg.run(auto_pickup=True)
    assert click_times(leg, DRIVE_POINT) == [20.0]
    assert max(click_times(leg, travel._AUTO_PICKUP_POINT)) == 19.5


@pytest.mark.parametrize(("auto_pickup", "size"), [(False, (1280, 720)), (True, (1600, 900))])
def test_recovery_requires_pickup_and_reference_viewport(leg, auto_pickup, size):
    leg.size = size
    leg.run(auto_pickup=auto_pickup)
    assert leg.captures == []
    assert click_times(leg, DRIVE_POINT) == []
