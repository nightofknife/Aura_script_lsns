"""Map gesture regressions using a clock and Unity-style input model only."""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from plans.resonance_pc.src.actions import _intercity_map_drag as gestures


SAFE_RECT = (160, 140, 960, 460)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        assert 0 < seconds <= 0.020000001
        self.now += seconds


class MapInput:
    """BeginDrag records the threshold-crossing point without moving content."""

    def __init__(self, clock, *, bridge=False, fail=None):
        self.clock = clock
        self.events = []
        self.pointer = None
        self.press = None
        self.dragging = False
        self.held = False
        self.content = [0, 0]
        self.fail = fail
        self.scope_active = False
        self.controller = SimpleNamespace(
            mouse_down=self.mouse_down,
            mouse_up=self.mouse_up,
            bridge=SimpleNamespace(enabled=bridge, system_scope=self.system_scope),
        )

    def record(self, kind, value=None):
        self.events.append((kind, self.clock.now, value))
        if self.fail is not None and self.fail(self.events):
            raise RuntimeError("injected input failure")

    @contextmanager
    def system_scope(self):
        self.scope_active = True
        self.record("scope_enter")
        try:
            yield
        finally:
            self.record("scope_exit")
            self.scope_active = False

    def move_to(self, x, y, *, duration):
        assert duration == 0
        next_point = (x, y)
        if self.held:
            if self.dragging:
                self.content[0] += x - self.pointer[0]
                self.content[1] += y - self.pointer[1]
            elif (x - self.press[0]) ** 2 + (y - self.press[1]) ** 2 >= 10 ** 2:
                self.dragging = True
        self.pointer = next_point
        self.record("move", next_point)

    def mouse_down(self, button):
        assert button == "left"
        assert self.scope_active
        self.press = self.pointer
        self.held = True
        self.record("down", self.pointer)

    def mouse_up(self, button):
        assert button == "left"
        assert self.scope_active
        self.held = False
        self.record("up", self.pointer)

    def drag(self, x1, y1, x2, y2, **kwargs):
        self.record("bridge_drag", ((x1, y1), (x2, y2), kwargs))


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(gestures, "time", fake)
    return fake


def perform(app, delta=(100, 0), **kwargs):
    return gestures.perform_map_drag(
        app=app,
        effective_delta=delta,
        safe_rect=SAFE_RECT,
        check_cancelled=kwargs.pop("check_cancelled", lambda: None),
        **kwargs,
    )


@pytest.mark.parametrize("delta", [
    (100, 0), (-100, 0), (0, 200), (0, -200),
    (896, 396), (-896, -396), (1, -1), (100.4, -99.6),
])
def test_system_activation_does_not_consume_effective_displacement(clock, delta):
    app = MapInput(clock)
    result = perform(app, delta)
    expected = [round(delta[0]), round(delta[1])]

    # The model discards threshold-crossing movement, as Unity BeginDrag does.
    assert app.content == expected
    assert result["effective_delta"] == expected
    assert [result["end"][i] - result["drag_start"][i] for i in range(2)] == expected
    assert result["prime_pixels"] == pytest.approx(32, abs=0.6)
    for kind, _, point in app.events:
        if kind == "move":
            assert SAFE_RECT[0] <= point[0] < SAFE_RECT[0] + SAFE_RECT[2]
            assert SAFE_RECT[1] <= point[1] < SAFE_RECT[1] + SAFE_RECT[3]
    assert not app.held


def test_system_waits_for_press_and_begin_drag_frames_then_holds_before_release(clock):
    app = MapInput(clock)
    result = perform(app, duration=0.65, hold_sec=0)
    down_time = next(t for kind, t, _ in app.events if kind == "down")
    moves = [(t, point) for kind, t, point in app.events if kind == "move"]
    up_time = next(t for kind, t, _ in app.events if kind == "up")

    assert moves[1][0] - down_time >= 0.12 - 1e-9
    assert moves[2][0] - moves[1][0] >= 0.12 - 1e-9
    assert moves[1][1] == tuple(result["drag_start"])
    assert moves[-1][1] == tuple(result["end"])
    # The movement schedule consumes its duration before the anti-inertia hold.
    assert up_time >= down_time + 0.12 + 0.12 + 0.65 + 0.55 - 1e-9
    assert up_time - moves[-1][0] >= 0.55 - 1e-9
    assert result["hold_sec"] == 0.55
    assert [kind for kind, _, _ in app.events][-2:] == ["up", "scope_exit"]


@pytest.mark.parametrize("hold", [0.55, 1.2])
def test_system_preserves_longer_requested_inertia_hold(clock, hold):
    app = MapInput(clock)
    result = perform(app, duration=0, hold_sec=hold)
    end_move = [t for kind, t, _ in app.events if kind == "move"][-1]
    release = next(t for kind, t, _ in app.events if kind == "up")
    assert release - end_move == pytest.approx(hold)
    assert result["hold_sec"] == hold
    assert app.content == [100, 0]


def test_bridge_uses_direct_begin_drag_and_requests_inertia_stop(clock):
    app = MapInput(clock, bridge=True)
    result = perform(app, (-896, 396), duration=0.7, hold_sec=0.1)
    assert len(app.events) == 1
    kind, _, (start, end, options) = app.events[0]
    assert kind == "bridge_drag"
    assert tuple(result["drag_start"]) == start
    assert (end[0] - start[0], end[1] - start[1]) == (-896, 396)
    assert result["prime_pixels"] == 0
    assert options == {
        "duration": 0.7, "hold_before_release_sec": 0.1, "stop_inertia": True,
    }


@pytest.mark.parametrize("bridge", [False, True])
def test_zero_effective_motion_does_not_press_or_release(clock, bridge):
    app = MapInput(clock, bridge=bridge)
    result = perform(app, (0.1, -0.1))
    assert result["skipped"] is True
    assert app.events == []


@pytest.mark.parametrize("phase", ["before_input", "press_wait", "prime_wait", "movement", "hold"])
def test_cancellation_releases_held_system_button_without_finishing_motion(clock, phase):
    app = MapInput(clock)

    def check_cancelled():
        kinds = [kind for kind, _, _ in app.events]
        moves = kinds.count("move")
        should_cancel = (
            phase == "before_input"
            or (phase == "press_wait" and "down" in kinds)
            or (phase == "prime_wait" and moves >= 2)
            or (phase == "movement" and moves >= 3)
            or (phase == "hold" and clock.now >= 0.24 + 0.65 + 0.02)
        )
        if should_cancel:
            raise asyncio.CancelledError("cancel gesture")

    with pytest.raises(asyncio.CancelledError):
        perform(app, check_cancelled=check_cancelled)
    assert not app.held
    assert not app.scope_active
    kinds = [kind for kind, _, _ in app.events]
    if phase == "before_input":
        assert kinds == []
    else:
        assert kinds.count("down") == kinds.count("up") == 1
        assert kinds[-2:] == ["up", "scope_exit"]
        assert clock.now < 0.24 + 0.65 + 0.55


@pytest.mark.parametrize("failure", ["down", "prime", "movement"])
def test_partially_dispatched_input_failure_still_releases_inside_system_scope(clock, failure):
    def fail(events):
        kinds = [kind for kind, _, _ in events]
        return (
            (failure == "down" and kinds[-1] == "down")
            or (failure == "prime" and kinds[-1] == "move" and kinds.count("move") == 2)
            or (failure == "movement" and kinds[-1] == "move" and kinds.count("move") == 3)
        )

    app = MapInput(clock, fail=fail)
    with pytest.raises(RuntimeError, match="injected input failure"):
        perform(app)
    assert not app.held
    assert not app.scope_active
    assert [kind for kind, _, _ in app.events][-2:] == ["up", "scope_exit"]


@pytest.mark.parametrize("delta,rectangle", [
    ((float("nan"), 1), SAFE_RECT),
    ((1, float("inf")), SAFE_RECT),
    ((1000, 0), SAFE_RECT),
    ((0, -450), SAFE_RECT),
    ((1, 1), (-1, 140, 960, 460)),
    ((1, 1), (160, 140, 1200, 460)),
    ((1, 1), (160, 140, 960, 600)),
    ((1, 1), (160, 140, 1, 460)),
    ((1,), SAFE_RECT),
    ((1, 1), (160, 140, 960)),
])
def test_invalid_or_out_of_bounds_path_fails_before_input(clock, delta, rectangle):
    app = MapInput(clock)
    with pytest.raises(ValueError):
        gestures.perform_map_drag(
            app=app, effective_delta=delta, safe_rect=rectangle, check_cancelled=lambda: None,
        )
    assert app.events == []


@pytest.mark.parametrize("options", [
    {"duration": -1}, {"duration": float("inf")}, {"duration": float("nan")},
    {"hold_sec": -1}, {"hold_sec": float("inf")}, {"hold_sec": float("nan")},
])
def test_invalid_timing_fails_before_input(clock, options):
    app = MapInput(clock)
    with pytest.raises(ValueError):
        perform(app, **options)
    assert app.events == []
