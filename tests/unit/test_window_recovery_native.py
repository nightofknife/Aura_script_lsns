"""Exercise real Win32 recovery using an owned, disposable test window only."""
from concurrent.futures import Future
import ctypes
from ctypes import wintypes
import os
import threading

import pytest
import win32api
import win32con
import win32gui

from plans.aura_base.src.platform.runtime_config import RuntimeTargetConfig, RuntimeVisibilityRecoveryConfig
from plans.aura_base.src.platform.windows.dpi import ensure_process_dpi_awareness
from plans.aura_base.src.platform.windows.window_geometry import ensure_client_on_screen
from plans.aura_base.src.platform.windows.window_target import WindowBinding, WindowTarget


@pytest.fixture
def native_target():
    ensure_process_dpi_awareness()
    # Crossing an internal monitor seam is valid; use the outer desktop edge
    # so this test really removes pixels from the union of all displays.
    info = max(
        (win32api.GetMonitorInfo(entry[0]) for entry in win32api.EnumDisplayMonitors()),
        key=lambda item: item["Monitor"][2],
    )
    screen = info["Monitor"]
    if screen[2] - screen[0] < 1280 or screen[3] - screen[1] < 720:
        pytest.skip("The native test needs a display that fits a 1280x720 client.")
    ready = Future()
    stop = threading.Event()
    errors = []

    def own_window():
        hwnd = None
        try:
            style = win32con.WS_OVERLAPPEDWINDOW
            frame = wintypes.RECT(0, 0, 1280, 720)
            assert ctypes.windll.user32.AdjustWindowRectEx(
                ctypes.byref(frame), style, False, win32con.WS_EX_TOOLWINDOW,
            )
            hwnd = win32gui.CreateWindowEx(
                win32con.WS_EX_TOOLWINDOW, "STATIC", "Aura window recovery test (temporary)",
                style, screen[0] + 50, screen[1] + 50,
                frame.right - frame.left, frame.bottom - frame.top,
                0, 0, 0, None,
            )
            win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
            ready.set_result(hwnd)
            while not stop.wait(0.01):
                win32gui.PumpWaitingMessages()
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            else:
                errors.append(exc)
        finally:
            if hwnd is not None and win32gui.IsWindow(hwnd):
                win32gui.DestroyWindow(hwnd)

    owner = threading.Thread(target=own_window, name="owned-window-recovery-test")
    owner.start()
    try:
        hwnd = ready.result(timeout=5)
        binding = WindowBinding(hwnd, "hwnd", os.getpid(), None, None, "owned-test", "Static")
        config = RuntimeTargetConfig(
            mode="hwnd", hwnd=hwnd,
            visibility_recovery=RuntimeVisibilityRecoveryConfig(
                enabled=True, grace_period_ms=1000, recovery_timeout_ms=3000, poll_interval_ms=25,
            ),
        )
        target = WindowTarget(config, binding)
        assert target.get_client_rect()[2:] == (1280, 720)
        yield target, info
    finally:
        stop.set()
        owner.join(timeout=5)
        assert not owner.is_alive(), "The owned test-window thread did not exit."
        assert not errors


def test_native_visible_minimized_window_is_restored_at_original_resolution(native_target):
    target, _ = native_target
    win32gui.ShowWindow(target.hwnd, win32con.SW_MINIMIZE)
    assert win32gui.IsWindowVisible(target.hwnd)
    assert win32gui.IsIconic(target.hwnd)
    result = target.ensure_available("native_minimize_test")
    assert result.recovered and result.method == "sw_restore"
    assert not win32gui.IsIconic(target.hwnd)
    assert target.get_client_rect()[2:] == (1280, 720)


def test_native_clipped_window_is_centered_without_resizing_or_stealing_focus(native_target):
    target, info = native_target
    screen = info["Monitor"]
    win32gui.SetWindowPos(
        target.hwnd, None, screen[2] - 100, screen[1] + 100, 0, 0,
        win32con.SWP_NOSIZE | win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE,
    )
    left, _, width, _ = target.get_client_rect_screen()
    assert left + width > screen[2]
    foreground = win32gui.GetForegroundWindow()
    result = ensure_client_on_screen(target, "native_center_test")
    assert result.recovered
    x, y, width, height = target.get_client_rect_screen()
    assert (width, height) == (1280, 720)
    assert screen[0] <= x and x + width <= screen[2]
    assert screen[1] <= y and y + height <= screen[3]
    work = info.get("Work") or screen
    area = work if width <= work[2] - work[0] and height <= work[3] - work[1] else screen
    assert abs((2 * x + width) - (area[0] + area[2])) <= 1
    assert abs((2 * y + height) - (area[1] + area[3])) <= 1
    assert win32gui.GetForegroundWindow() == foreground
