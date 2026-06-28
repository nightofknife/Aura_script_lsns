# -*- coding: utf-8 -*-
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from typing import Any

import win32api
import win32con
import win32gui
from ctypes import wintypes

from ..contracts import TargetRuntimeError
from ..runtime_config import RuntimeWindowSpecConfig
from .window_target import WindowTarget


_POSITION_TOLERANCE_PX = 2


@dataclass(frozen=True)
class WindowSpecStatus:
    ok: bool
    applied: bool
    mismatches: tuple[str, ...]
    current: dict[str, Any]
    desired: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": bool(self.ok),
            "applied": bool(self.applied),
            "mismatches": list(self.mismatches),
            "current": dict(self.current),
            "desired": dict(self.desired),
        }


def ensure_window_spec(target: WindowTarget, spec: RuntimeWindowSpecConfig) -> WindowSpecStatus:
    status = evaluate_window_spec(target, spec)
    if spec.mode == "off" or status.ok:
        return status

    if spec.mode == "require_exact":
        raise TargetRuntimeError(
            "window_spec_mismatch",
            "The current window does not satisfy runtime.window_spec.",
            status.to_dict(),
        )

    if spec.mode != "try_resize_then_verify":
        raise TargetRuntimeError(
            "window_spec_mode_invalid",
            "Unsupported window spec mode.",
            {"mode": spec.mode},
        )

    apply_window_spec(target, spec)
    verified = evaluate_window_spec(target, spec, applied=True)
    if not verified.ok:
        raise TargetRuntimeError(
            "window_spec_apply_failed",
            "Failed to apply the requested window spec.",
            verified.to_dict(),
        )
    return verified


def evaluate_window_spec(
    target: WindowTarget,
    spec: RuntimeWindowSpecConfig,
    *,
    applied: bool = False,
) -> WindowSpecStatus:
    current_window_rect = target.get_window_rect_screen()
    current_client_rect = target.get_client_rect()
    current_monitor_index = _get_window_monitor_index(target.hwnd)
    current_position = (int(current_window_rect[0]), int(current_window_rect[1]))
    current_client_size = (int(current_client_rect[2]), int(current_client_rect[3]))

    desired = {
        "mode": spec.mode,
        "client_size": list(spec.client_size) if spec.client_size else None,
        "position": list(spec.position) if spec.position else None,
        "monitor_index": spec.monitor_index,
    }
    current = {
        "client_size": list(current_client_size),
        "position": list(current_position),
        "monitor_index": current_monitor_index,
        "window_rect_screen": list(current_window_rect),
    }

    mismatches: list[str] = []
    if spec.client_size is not None and current_client_size != spec.client_size:
        mismatches.append("client_size")
    if spec.position is not None and not _positions_match(current_position, spec.position):
        mismatches.append("position")
    if spec.monitor_index is not None and current_monitor_index != spec.monitor_index:
        mismatches.append("monitor_index")

    return WindowSpecStatus(
        ok=not mismatches,
        applied=applied,
        mismatches=tuple(mismatches),
        current=current,
        desired=desired,
    )


def apply_window_spec(target: WindowTarget, spec: RuntimeWindowSpecConfig) -> None:
    hwnd = target.hwnd
    current_rect = target.get_window_rect_screen()
    left = int(current_rect[0])
    top = int(current_rect[1])
    width = int(current_rect[2])
    height = int(current_rect[3])

    if spec.monitor_index is not None:
        left, top = _default_monitor_origin(spec.monitor_index)
    if spec.position is not None:
        left = int(spec.position[0])
        top = int(spec.position[1])

    if spec.client_size is not None:
        width, height = _client_to_window_size(hwnd, spec.client_size)

    flags = win32con.SWP_NOZORDER
    win32gui.SetWindowPos(hwnd, None, left, top, width, height, flags)


def _client_to_window_size(hwnd: int, client_size: tuple[int, int]) -> tuple[int, int]:
    style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
    exstyle = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    rect = wintypes.RECT(0, 0, int(client_size[0]), int(client_size[1]))
    adjusted = ctypes.windll.user32.AdjustWindowRectEx(ctypes.byref(rect), style, False, exstyle)
    if adjusted == 0:
        raise TargetRuntimeError(
            "window_spec_apply_failed",
            "AdjustWindowRectEx failed while computing target window size.",
            {"hwnd": int(hwnd), "client_size": list(client_size)},
        )
    return int(rect.right - rect.left), int(rect.bottom - rect.top)


def _get_window_monitor_index(hwnd: int) -> int | None:
    try:
        monitor = win32api.MonitorFromWindow(hwnd, 1)
        monitors = win32api.EnumDisplayMonitors()
        for index, monitor_entry in enumerate(monitors):
            if monitor_entry[0] == monitor:
                return int(index)
    except Exception:
        return None
    return None


def _default_monitor_origin(monitor_index: int) -> tuple[int, int]:
    monitors = win32api.EnumDisplayMonitors()
    index = max(int(monitor_index), 0)
    if index >= len(monitors):
        raise TargetRuntimeError(
            "window_spec_apply_failed",
            "Requested monitor_index is out of range.",
            {"monitor_index": monitor_index, "count": len(monitors)},
        )
    monitor_info = win32api.GetMonitorInfo(monitors[index][0])
    work = monitor_info.get("Work") or monitor_info.get("Monitor")
    return int(work[0]) + 50, int(work[1]) + 50


def _positions_match(current: tuple[int, int], expected: tuple[int, int]) -> bool:
    return (
        abs(int(current[0]) - int(expected[0])) <= _POSITION_TOLERANCE_PX
        and abs(int(current[1]) - int(expected[1])) <= _POSITION_TOLERANCE_PX
    )
