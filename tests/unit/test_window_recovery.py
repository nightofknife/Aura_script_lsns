"""Window recovery contracts without touching the user's desktop windows."""
from dataclasses import replace
from types import SimpleNamespace
import threading

import pytest

from plans.aura_base.src.platform.contracts import TargetRuntimeError
from plans.aura_base.src.platform.runtime_config import (
    RuntimeCaptureConfig, RuntimeTargetConfig, RuntimeVisibilityRecoveryConfig,
    RuntimeWindowSpecConfig,
)
from plans.aura_base.src.platform.windows import desktop_adapter as adapter_module
from plans.aura_base.src.platform.windows import window_geometry as geometry
from plans.aura_base.src.platform.windows import window_target as windows


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def config(*, enabled=True):
    return RuntimeTargetConfig(
        mode="hwnd", hwnd=123,
        visibility_recovery=RuntimeVisibilityRecoveryConfig(
            enabled=enabled, grace_period_ms=1000, recovery_timeout_ms=300, poll_interval_ms=25,
        ),
    )


@pytest.fixture
def minimized_window(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(windows, "time", clock)
    state = SimpleNamespace(visible=True, iconic=True, restores=True, commands=[])
    binding = windows.WindowBinding(123, "hwnd", 456, None, None, "fake", None)
    target = windows.WindowTarget(config(), binding)
    monkeypatch.setattr(target, "ensure_alive", lambda: None)
    monkeypatch.setattr(target, "is_foreground", lambda: False)
    monkeypatch.setattr(target, "get_client_rect_screen", lambda: (100, 100, 1280, 720))
    monkeypatch.setattr(windows.win32gui, "IsWindowVisible", lambda _: state.visible)
    monkeypatch.setattr(windows.win32gui, "IsIconic", lambda _: state.iconic)

    def show(command):
        state.commands.append(command)
        if state.restores:
            state.visible, state.iconic = True, False

    monkeypatch.setattr(target, "_show_window_async", show)
    return target, state, clock


def test_visible_minimized_window_is_restored_without_hidden_window_grace(minimized_window):
    target, state, clock = minimized_window
    result = target.ensure_available("capture")
    assert result.recovered and result.method == "sw_restore"
    assert state.commands == [windows.win32con.SW_RESTORE]
    assert state.visible and not state.iconic
    assert clock.now == pytest.approx(0.2)
    assert target.get_client_rect()[2:] == (1280, 720)


def test_minimized_window_never_passes_read_only_validation(minimized_window):
    target, state, _ = minimized_window
    with pytest.raises(TargetRuntimeError) as error:
        target.ensure_valid()
    assert error.value.code == "window_not_visible"
    assert error.value.detail["visible"] is True
    assert error.value.detail["iconic"] is True
    assert state.commands == []


def test_restore_timeout_does_not_accept_visible_but_still_minimized(minimized_window):
    target, state, clock = minimized_window
    state.restores = False
    with pytest.raises(TargetRuntimeError) as error:
        target.ensure_available("capture")
    assert error.value.code == "window_not_visible"
    assert state.iconic and state.commands[0] == windows.win32con.SW_RESTORE
    assert 0.3 <= clock.now <= 0.301


def test_disabled_recovery_does_not_restore_minimized_window(minimized_window):
    target, state, _ = minimized_window
    target.config = config(enabled=False)
    with pytest.raises(TargetRuntimeError):
        target.ensure_available("capture")
    assert state.commands == []


def test_available_window_is_left_alone(minimized_window):
    target, state, clock = minimized_window
    state.iconic = False
    assert not target.ensure_available().attempted
    assert not state.commands and clock.now == 0


def test_hidden_non_minimized_window_keeps_existing_recovery(minimized_window):
    target, state, clock = minimized_window
    state.iconic, state.visible = False, False
    assert target.ensure_available().method == "sw_showna"
    assert state.commands == [windows.win32con.SW_SHOWNA]
    assert clock.now >= 1.2


class GeometryTarget:
    hwnd = 123

    def __init__(self, client):
        self.config = config()
        self.client = tuple(client)
        self.offset = (8, 31)
        self.moves = []
        self.last_recovery_result = windows.WindowRecoveryResult()

    def ensure_valid(self):
        pass

    def ensure_alive(self):
        pass

    def get_client_rect_screen(self):
        return self.client

    def get_window_rect_screen(self):
        x, y, width, height = self.client
        return x - self.offset[0], y - self.offset[1], width + 16, height + 39


@pytest.fixture
def display(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(geometry, "time", clock)
    screens = {1: {"Monitor": (0, 0, 1920, 1080), "Work": (0, 0, 1920, 1040)}}
    state = SimpleNamespace(screens=screens, nearest=1, target=None, move_effect=None, flags=[])
    monkeypatch.setattr(geometry.win32api, "EnumDisplayMonitors",
                        lambda: [(handle, None, info["Monitor"]) for handle, info in screens.items()])
    monkeypatch.setattr(geometry.win32api, "GetMonitorInfo", lambda handle: screens[handle])

    def nearest(hwnd, flag):
        assert flag == geometry.win32con.MONITOR_DEFAULTTONEAREST
        return state.nearest

    def move(hwnd, insert_after, x, y, width, height, flags):
        target = state.target
        assert hwnd == target.hwnd
        target.moves.append((x, y, width, height))
        state.flags.append(flags)
        if state.move_effect is not None:
            state.move_effect(target, x, y)
        else:
            target.client = (x + target.offset[0], y + target.offset[1], *target.client[2:])

    monkeypatch.setattr(geometry.win32api, "MonitorFromWindow", nearest)
    monkeypatch.setattr(geometry.win32gui, "SetWindowPos", move)
    return state, clock


@pytest.mark.parametrize("position", [(-50, 100), (1800, 100), (100, -50), (100, 1000), (4000, 2000)])
def test_clipped_client_is_centered_without_resize_or_activation(display, position):
    state, _ = display
    target = state.target = GeometryTarget((*position, 1280, 720))
    result = geometry.ensure_client_on_screen(target, "capture")
    assert result.recovered and target.client == (320, 160, 1280, 720)
    assert target.moves == [(312, 129, 0, 0)]
    expected = geometry.win32con.SWP_NOSIZE | geometry.win32con.SWP_NOZORDER | geometry.win32con.SWP_NOACTIVATE
    assert state.flags == [expected]
    assert not geometry.ensure_client_on_screen(target, "capture").attempted
    assert len(target.moves) == 1


def test_negative_coordinates_on_secondary_monitor_are_valid(display):
    state, _ = display
    state.screens[2] = {"Monitor": (-1920, 0, 0, 1080), "Work": (-1920, 0, 0, 1040)}
    target = state.target = GeometryTarget((-1800, 100, 1280, 720))
    assert not geometry.ensure_client_on_screen(target, "capture").attempted
    assert not target.moves


def test_offscreen_window_uses_nearest_secondary_monitor(display):
    state, _ = display
    state.screens[2] = {"Monitor": (-1920, 0, 0, 1080), "Work": (-1920, 0, 0, 1040)}
    state.nearest = 2
    target = state.target = GeometryTarget((-2200, 100, 1280, 720))
    geometry.ensure_client_on_screen(target, "capture")
    assert target.client == (-1600, 160, 1280, 720)


def test_visible_window_spanning_adjacent_displays_is_not_moved(display):
    state, _ = display
    state.screens[2] = {"Monitor": (1920, 0, 3840, 1080), "Work": (1920, 0, 3840, 1040)}
    target = state.target = GeometryTarget((1500, 100, 1280, 720))
    assert not geometry.ensure_client_on_screen(target, "capture").attempted
    assert not target.moves


def test_virtual_desktop_gap_does_not_count_as_visible_pixels(display):
    state, _ = display
    state.screens[1] = {"Monitor": (0, 0, 1000, 1080), "Work": (0, 0, 1000, 1040)}
    state.screens[2] = {"Monitor": (1200, 0, 2200, 1080), "Work": (1200, 0, 2200, 1040)}
    state.nearest = 2
    target = state.target = GeometryTarget((900, 100, 400, 200))
    geometry.ensure_client_on_screen(target, "capture")
    assert target.client == (1500, 420, 400, 200)


def test_full_display_fallback_does_not_shrink_to_taskbar_work_area(display):
    state, _ = display
    target = state.target = GeometryTarget((1800, 100, 1280, 1060))
    geometry.ensure_client_on_screen(target, "capture")
    assert target.client == (320, 10, 1280, 1060)


@pytest.mark.parametrize("size", [(1921, 720), (1280, 1081)])
def test_client_too_large_is_rejected_without_resize(display, size):
    state, _ = display
    target = state.target = GeometryTarget((1800, 100, *size))
    with pytest.raises(TargetRuntimeError) as error:
        geometry.ensure_client_on_screen(target, "capture")
    assert error.value.code == "window_client_area_too_large"
    assert not target.moves and target.client[2:] == size


def test_unexpected_dpi_resize_stops_operation(display):
    state, _ = display
    target = state.target = GeometryTarget((1800, 100, 1280, 720))
    state.move_effect = lambda obj, x, y: setattr(obj, "client", (x + 8, y + 31, 1600, 900))
    with pytest.raises(TargetRuntimeError) as error:
        geometry.ensure_client_on_screen(target, "capture")
    assert error.value.code == "window_resolution_changed"


def test_failed_move_has_bounded_wait(display):
    state, clock = display
    target = state.target = GeometryTarget((1800, 100, 1280, 720))
    state.move_effect = lambda *args: None
    with pytest.raises(TargetRuntimeError) as error:
        geometry.ensure_client_on_screen(target, "capture")
    assert error.value.code == "window_position_recovery_failed"
    assert len(target.moves) == 1 and 0.3 <= clock.now <= 0.301


def test_position_conflict_is_rejected_before_any_move(display):
    state, _ = display
    target = state.target = GeometryTarget((100, 100, 1280, 720))
    spec = RuntimeWindowSpecConfig(mode="try_resize_then_verify", position=(1800, 100))
    with pytest.raises(TargetRuntimeError) as error:
        geometry.validate_window_position(target, spec)
    assert error.value.code == "window_position_spec_conflict"
    assert not target.moves and target.client == (100, 100, 1280, 720)


def test_position_preflight_includes_requested_client_size(display):
    state, _ = display
    target = state.target = GeometryTarget((100, 100, 640, 360))
    spec = RuntimeWindowSpecConfig(mode="try_resize_then_verify", position=(900, 100), client_size=(1280, 720))
    with pytest.raises(TargetRuntimeError):
        geometry.validate_window_position(target, spec)
    assert not target.moves


def test_disabled_recovery_leaves_offscreen_position_alone(display):
    state, _ = display
    target = state.target = GeometryTarget((1800, 100, 1280, 720))
    target.config = config(enabled=False)
    assert not geometry.ensure_client_on_screen(target, "capture").attempted
    assert not target.moves


def make_adapter(monkeypatch, target, *, spec=None):
    adapter = adapter_module.WindowsDesktopAdapter.__new__(adapter_module.WindowsDesktopAdapter)
    adapter.target = target
    adapter.target_config = target.config
    adapter.window_spec_config = spec or RuntimeWindowSpecConfig()
    adapter.capture_config = RuntimeCaptureConfig(backend="wgc")
    adapter._operation_lock = threading.RLock()
    adapter._capture_reset_pending = False
    adapter._visibility_recovery_stats = dict(
        enabled=True, attempts=0, successes=0, failures=0,
        last_operation=None, last_method=None, last_elapsed_ms=None, last_timestamp=None,
    )
    events = []
    adapter.capture_backend = SimpleNamespace(
        reset_after_target_recovery=lambda: events.append("reset_capture"),
        capture=lambda **kwargs: events.append("capture") or "new-frame",
    )
    monkeypatch.setattr(adapter_module, "ensure_process_dpi_awareness", lambda: None)
    return adapter, events


def test_restore_and_center_discard_capture_only_after_both_recoveries(monkeypatch, display):
    state, _ = display
    target = state.target = GeometryTarget((1800, 100, 1280, 720))
    target.ensure_available = lambda operation: windows.WindowRecoveryResult(True, True, True, "sw_restore", 20)
    adapter, events = make_adapter(monkeypatch, target)
    monkeypatch.setattr(adapter_module, "ensure_window_spec", lambda *args: SimpleNamespace(applied=False))
    monkeypatch.setattr(adapter_module, "evaluate_window_spec", lambda *args, **kwargs: SimpleNamespace(applied=False))
    assert adapter.capture() == "new-frame"
    assert target.client == (320, 160, 1280, 720)
    assert events == ["reset_capture", "capture"]
    assert adapter._visibility_recovery_stats["successes"] == 2


def test_capture_is_not_run_when_centering_fails(monkeypatch, display):
    state, _ = display
    target = state.target = GeometryTarget((1800, 100, 2000, 720))
    target.ensure_available = lambda operation: windows.WindowRecoveryResult()
    adapter, events = make_adapter(monkeypatch, target)
    monkeypatch.setattr(adapter_module, "ensure_window_spec", lambda *args: SimpleNamespace(applied=False))
    with pytest.raises(TargetRuntimeError):
        adapter.capture()
    assert events == [] and adapter._capture_reset_pending
    assert adapter._visibility_recovery_stats["failures"] == 1


def test_failed_window_spec_leaves_capture_reset_pending_for_retry(monkeypatch, display):
    state, _ = display
    target = state.target = GeometryTarget((100, 100, 1280, 720))
    target.ensure_available = lambda operation: windows.WindowRecoveryResult()
    spec = RuntimeWindowSpecConfig(mode="try_resize_then_verify")
    adapter, events = make_adapter(monkeypatch, target, spec=spec)

    def fail(*args):
        raise TargetRuntimeError("window_spec_apply_failed", "simulated partial move")

    monkeypatch.setattr(adapter_module, "ensure_window_spec", fail)
    with pytest.raises(TargetRuntimeError):
        adapter.capture()
    assert adapter._capture_reset_pending and not events
    monkeypatch.setattr(adapter_module, "ensure_window_spec", lambda *args: SimpleNamespace(applied=False))
    assert adapter.capture() == "new-frame"
    assert events == ["reset_capture", "capture"]
