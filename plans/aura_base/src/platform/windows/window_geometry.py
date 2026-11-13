"""Recover clipped client areas by translation, without changing resolution."""
from __future__ import annotations

import time
from typing import NoReturn, Sequence

import win32api
import win32con
import win32gui

from packages.aura_core.observability.logging.core_logger import logger

from ..contracts import TargetRuntimeError
from ..runtime_config import RuntimeWindowSpecConfig
from .window_target import WindowRecoveryResult, WindowTarget


ScreenRect = tuple[int, int, int, int]


def _covered_by_displays(rect: ScreenRect, displays: Sequence[ScreenRect]) -> bool:
    """Subtract real display rectangles, including gaps in a multi-monitor layout."""
    remaining = [rect]
    for screen in displays:
        uncovered = []
        for left, top, right, bottom in remaining:
            x1, y1 = max(left, screen[0]), max(top, screen[1])
            x2, y2 = min(right, screen[2]), min(bottom, screen[3])
            if x1 >= x2 or y1 >= y2:
                uncovered.append((left, top, right, bottom))
                continue
            if top < y1:
                uncovered.append((left, top, right, y1))
            if y2 < bottom:
                uncovered.append((left, y2, right, bottom))
            if left < x1:
                uncovered.append((left, y1, x1, y2))
            if x2 < right:
                uncovered.append((x2, y1, right, y2))
        remaining = uncovered
        if not remaining:
            return True
    return False


def validate_window_position(target: WindowTarget, spec: RuntimeWindowSpecConfig) -> None:
    """Reject an explicitly clipped position before applying a window spec."""
    if (spec.mode == "off" or spec.position is None
            or not target.config.require_visible or not target.config.visibility_recovery.enabled):
        return
    left, top, width, height = target.get_client_rect_screen()
    window_left, window_top, _, _ = target.get_window_rect_screen()
    width, height = spec.client_size or (width, height)
    client_left = spec.position[0] + left - window_left
    client_top = spec.position[1] + top - window_top
    expected = (client_left, client_top, client_left + width, client_top + height)
    detail = {"hwnd": target.hwnd, "configured_position": list(spec.position),
              "expected_client_rect": list(expected)}
    try:
        displays = [tuple(win32api.GetMonitorInfo(entry[0])["Monitor"])
                    for entry in win32api.EnumDisplayMonitors()]
    except Exception as exc:
        detail["error"] = str(exc)
        raise TargetRuntimeError("window_geometry_unavailable", "Unable to check the configured window position.", detail) from exc
    if not displays:
        raise TargetRuntimeError("window_geometry_unavailable", "No display is available for the configured window position.", detail)
    if not _covered_by_displays(expected, displays):
        detail["display_rects"] = [list(rect) for rect in displays]
        raise TargetRuntimeError(
            "window_position_spec_conflict",
            "The configured window position would clip the game client; remove or correct runtime.window_spec.position.",
            detail,
        )


def ensure_client_on_screen(
    target: WindowTarget,
    operation: str,
    *,
    locked_position: tuple[int, int] | None = None,
    monitor_index: int | None = None,
) -> WindowRecoveryResult:
    recovery = target.config.visibility_recovery
    if not recovery.enabled or not target.config.require_visible:
        return WindowRecoveryResult()

    target.ensure_valid()
    before = target.get_client_rect_screen()
    left, top, width, height = before
    client_rect = (left, top, left + width, top + height)
    started = time.monotonic()
    detail = {
        "hwnd": target.hwnd,
        "client_rect_before": list(before),
        "visibility_recovery": {"operation": operation, "method": "center_client", "forced": True},
    }

    def fail(code: str, message: str) -> NoReturn:
        detail["visibility_recovery"]["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        logger.error("Window position recovery failed: code=%s detail=%s", code, detail)
        raise TargetRuntimeError(code, message, detail)

    try:
        monitors = [(entry[0], win32api.GetMonitorInfo(entry[0])) for entry in win32api.EnumDisplayMonitors()]
    except Exception as exc:
        detail["error"] = str(exc)
        fail("window_geometry_unavailable", "Unable to read display geometry for window recovery.")
    displays = [tuple(info["Monitor"]) for _, info in monitors]
    if not displays:
        fail("window_geometry_unavailable", "No display is available for window recovery.")
    if _covered_by_displays(client_rect, displays):
        return WindowRecoveryResult()

    detail["display_rects"] = [list(rect) for rect in displays]
    if locked_position is not None:
        detail["configured_position"] = list(locked_position)
        fail("window_position_spec_conflict", "The configured window position clips the client area; remove or correct runtime.window_spec.position.")

    try:
        if monitor_index is not None:
            if not 0 <= monitor_index < len(monitors):
                fail("window_position_spec_conflict", "The configured monitor is unavailable for window recovery.")
            monitor, info = monitors[monitor_index]
        else:
            monitor = win32api.MonitorFromWindow(target.hwnd, win32con.MONITOR_DEFAULTTONEAREST)
            info = win32api.GetMonitorInfo(monitor)
    except TargetRuntimeError:
        raise
    except Exception as exc:
        detail["error"] = str(exc)
        fail("window_geometry_unavailable", "Unable to select a display for window recovery.")

    screen = tuple(info["Monitor"])
    work = tuple(info.get("Work") or screen)
    detail["target_display"] = list(screen)
    if width > screen[2] - screen[0] or height > screen[3] - screen[1]:
        fail("window_client_area_too_large", "The game client does not fit the selected display without changing its resolution.")
    # Prefer the work area, but a client fitting only the full display must
    # retain its resolution rather than being shrunk to make room for a taskbar.
    area = work if width <= work[2] - work[0] and height <= work[3] - work[1] else screen
    window_left, window_top, _, _ = target.get_window_rect_screen()
    new_left = area[0] + (area[2] - area[0] - width) // 2 - (left - window_left)
    new_top = area[1] + (area[3] - area[1] - height) // 2 - (top - window_top)
    detail["window_position_requested"] = [new_left, new_top]
    logger.warning("Window client is off screen; centering without resize: operation=%s detail=%s", operation, detail)
    target.ensure_alive()
    try:
        win32gui.SetWindowPos(
            target.hwnd, None, new_left, new_top, 0, 0,
            win32con.SWP_NOSIZE | win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE,
        )
    except Exception as exc:
        detail["error"] = str(exc)
        fail("window_position_recovery_failed", "Unable to center the game window.")

    deadline = time.monotonic() + recovery.recovery_timeout_ms / 1000.0
    while True:
        target.ensure_valid()
        after = target.get_client_rect_screen()
        detail["client_rect_after"] = list(after)
        if after[2:] != before[2:]:
            fail("window_resolution_changed", "The game client resolution changed during window movement; stopping instead of continuing with altered coordinates.")
        rect = (after[0], after[1], after[0] + width, after[1] + height)
        if _covered_by_displays(rect, [screen]):
            # Let the game redraw before the adapter discards its old WGC session.
            time.sleep(0.2)
            target.ensure_valid()
            settled = target.get_client_rect_screen()
            detail["client_rect_after"] = list(settled)
            if settled[2:] != before[2:]:
                fail("window_resolution_changed", "The game client resolution changed after window movement.")
            settled_rect = (settled[0], settled[1], settled[0] + width, settled[1] + height)
            if _covered_by_displays(settled_rect, [screen]):
                elapsed_ms = int((time.monotonic() - started) * 1000)
                result = WindowRecoveryResult(
                    attempted=True, recovered=True, forced=True,
                    method="center_client", elapsed_ms=elapsed_ms,
                )
                target.last_recovery_result = result
                logger.info("Window position recovery succeeded: operation=%s elapsed_ms=%s detail=%s", operation, elapsed_ms, detail)
                return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            fail("window_position_recovery_failed", "The game client is still clipped after window recovery.")
        time.sleep(min(max(recovery.poll_interval_ms, 10) / 1000.0, remaining))
