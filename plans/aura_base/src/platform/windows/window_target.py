# -*- coding: utf-8 -*-
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import win32api
import win32con
import win32gui
import win32process

from ..contracts import TargetRuntimeError
from ..runtime_config import RuntimeTargetConfig
from .window_selector import resolve_window_candidate


@dataclass(frozen=True)
class WindowBinding:
    hwnd: int
    mode: str
    pid: int | None
    process_name: str | None
    exe_path: str | None
    title: str | None
    class_name: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "hwnd": int(self.hwnd),
            "mode": self.mode,
            "pid": self.pid,
            "process_name": self.process_name,
            "exe_path": self.exe_path,
            "title": self.title,
            "class_name": self.class_name,
        }


class WindowTarget:
    def __init__(self, config: RuntimeTargetConfig, binding: WindowBinding):
        self.config = config
        self.binding = binding

    @classmethod
    def create(cls, config: RuntimeTargetConfig) -> "WindowTarget":
        candidate = resolve_window_candidate(config)
        binding = WindowBinding(
            hwnd=int(candidate.hwnd),
            mode=config.mode,
            pid=candidate.pid,
            process_name=candidate.process_name,
            exe_path=candidate.exe_path,
            title=candidate.title,
            class_name=candidate.class_name,
        )
        target = cls(config=config, binding=binding)
        target.ensure_valid()
        return target

    @property
    def hwnd(self) -> int:
        return int(self.binding.hwnd)

    def ensure_valid(self) -> None:
        hwnd = self.hwnd
        if not win32gui.IsWindow(hwnd):
            raise TargetRuntimeError(
                "window_target_lost",
                "The configured target window is no longer valid.",
                {"hwnd": hwnd, "mode": self.binding.mode},
            )
        if self.config.require_visible and not win32gui.IsWindowVisible(hwnd):
            raise TargetRuntimeError(
                "window_not_visible",
                "The configured target window is not visible.",
                self.to_summary(),
            )
        if not self.config.allow_borderless and _is_borderless_window(hwnd):
            raise TargetRuntimeError(
                "window_borderless_not_allowed",
                "The configured target window is borderless but runtime.target.allow_borderless=false.",
                self.to_summary(),
            )

        _, _, client_width, client_height = self.get_client_rect_screen()
        if client_width <= 0 or client_height <= 0:
            raise TargetRuntimeError(
                "window_client_rect_invalid",
                "The configured target window has an invalid client area.",
                self.to_summary(),
            )
        if self.config.require_foreground and not self.is_foreground():
            raise TargetRuntimeError(
                "window_not_foreground",
                "The configured target window is not in the foreground.",
                self.to_summary(),
            )

    def get_client_rect(self) -> tuple[int, int, int, int]:
        _, _, width, height = self.get_client_rect_screen()
        return 0, 0, width, height

    def get_client_rect_screen(self) -> tuple[int, int, int, int]:
        self.ensure_alive()
        left_top = win32gui.ClientToScreen(self.hwnd, (0, 0))
        rect = win32gui.GetClientRect(self.hwnd)
        width = max(int(rect[2] - rect[0]), 0)
        height = max(int(rect[3] - rect[1]), 0)
        return int(left_top[0]), int(left_top[1]), width, height

    def get_window_rect_screen(self) -> tuple[int, int, int, int]:
        self.ensure_alive()
        left, top, right, bottom = win32gui.GetWindowRect(self.hwnd)
        return int(left), int(top), int(right - left), int(bottom - top)

    def focus(self) -> bool:
        self.ensure_alive()
        hwnd = self.hwnd
        if self.is_foreground():
            return True

        try:
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            else:
                win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        except Exception:
            pass

        try:
            foreground = win32gui.GetForegroundWindow()
            current_thread = win32api.GetCurrentThreadId()
            foreground_thread = win32process.GetWindowThreadProcessId(foreground)[0] if foreground else 0
            target_thread = win32process.GetWindowThreadProcessId(hwnd)[0]
            attached = False
            if foreground_thread and foreground_thread != current_thread:
                try:
                    win32process.AttachThreadInput(foreground_thread, current_thread, True)
                    attached = True
                except Exception:
                    attached = False
            try:
                win32gui.BringWindowToTop(hwnd)
                win32gui.SetForegroundWindow(hwnd)
                win32gui.SetActiveWindow(hwnd)
            finally:
                if attached:
                    try:
                        win32process.AttachThreadInput(foreground_thread, current_thread, False)
                    except Exception:
                        pass
        except Exception:
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                pass

        time.sleep(0.05)
        return self.is_foreground()

    def is_foreground(self) -> bool:
        self.ensure_alive()
        return int(win32gui.GetForegroundWindow() or 0) == self.hwnd

    def ensure_alive(self) -> None:
        if not win32gui.IsWindow(self.hwnd):
            raise TargetRuntimeError(
                "window_target_lost",
                "The configured target window is no longer available.",
                self.to_summary(),
            )

    def to_summary(self) -> dict[str, Any]:
        try:
            client_rect = list(self.get_client_rect())
        except Exception:
            client_rect = None
        try:
            client_rect_screen = list(self.get_client_rect_screen())
        except Exception:
            client_rect_screen = None
        return {
            "mode": self.binding.mode,
            "hwnd": int(self.hwnd),
            "pid": self.binding.pid,
            "title": self.binding.title,
            "process_name": self.binding.process_name,
            "exe_path": self.binding.exe_path,
            "class_name": self.binding.class_name,
            "client_rect": client_rect,
            "client_rect_screen": client_rect_screen,
            "foreground": self.is_foreground() if win32gui.IsWindow(self.hwnd) else False,
        }


def _is_borderless_window(hwnd: int) -> bool:
    try:
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
    except Exception:
        return False
    return (style & win32con.WS_CAPTION) == 0 and (style & win32con.WS_THICKFRAME) == 0
