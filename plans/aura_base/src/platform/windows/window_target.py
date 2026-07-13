# -*- coding: utf-8 -*-
from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass, replace
from typing import Any

import win32api
import win32con
import win32gui
import win32process

from packages.aura_core.observability.logging.core_logger import logger

from ..contracts import TargetRuntimeError
from ..runtime_config import RuntimeTargetConfig
from .window_selector import resolve_window_candidate


_FORCED_SHOW_ESCALATION_MS = 500
_POST_RECOVERY_SETTLE_SEC = 0.2


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


@dataclass(frozen=True)
class WindowRecoveryResult:
    attempted: bool = False
    recovered: bool = False
    forced: bool = False
    method: str | None = None
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "recovered": self.recovered,
            "forced": self.forced,
            "method": self.method,
            "elapsed_ms": self.elapsed_ms,
        }


class WindowTarget:
    def __init__(self, config: RuntimeTargetConfig, binding: WindowBinding):
        self.config = config
        self.binding = binding
        self.last_recovery_result = WindowRecoveryResult()

    @classmethod
    def create(cls, config: RuntimeTargetConfig) -> "WindowTarget":
        try:
            candidate = resolve_window_candidate(config)
        except TargetRuntimeError as exc:
            recovery = config.visibility_recovery
            if exc.code != "window_not_found" or not recovery.enabled or not config.require_visible:
                raise
            relaxed_config = replace(config, require_visible=False, require_foreground=False)
            candidate = resolve_window_candidate(relaxed_config)
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
        target.ensure_available("runtime_start")
        return target

    @property
    def hwnd(self) -> int:
        return int(self.binding.hwnd)

    def ensure_valid(self) -> None:
        self.ensure_alive()
        hwnd = self.hwnd
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

    def ensure_available(self, operation: str = "runtime_operation") -> WindowRecoveryResult:
        self.ensure_alive()
        if not self.config.require_visible or win32gui.IsWindowVisible(self.hwnd):
            self.ensure_valid()
            return WindowRecoveryResult()

        recovery = self.config.visibility_recovery
        if not recovery.enabled:
            self.ensure_valid()

        started = time.monotonic()
        initial_iconic = bool(win32gui.IsIconic(self.hwnd))
        logger.warning(
            "Window visibility recovery started: operation=%s hwnd=%s pid=%s visible=false iconic=%s",
            operation,
            self.hwnd,
            self.binding.pid,
            initial_iconic,
        )

        if self._wait_until_visible(recovery.grace_period_ms, recovery.poll_interval_ms):
            return self._complete_recovery(started, operation, forced=False, method="natural")

        forced_method = "sw_restore" if win32gui.IsIconic(self.hwnd) else "sw_showna"
        forced_command = win32con.SW_RESTORE if forced_method == "sw_restore" else win32con.SW_SHOWNA
        self._show_window_async(forced_command)

        deadline = time.monotonic() + float(recovery.recovery_timeout_ms) / 1000.0
        first_wait_ms = min(_FORCED_SHOW_ESCALATION_MS, self._remaining_ms(deadline))
        if self._wait_until_visible(first_wait_ms, recovery.poll_interval_ms):
            return self._complete_recovery(started, operation, forced=True, method=forced_method)

        if self._remaining_ms(deadline) > 0:
            forced_method = f"{forced_method}+sw_show"
            self._show_window_async(win32con.SW_SHOW)
            if self._wait_until_visible(self._remaining_ms(deadline), recovery.poll_interval_ms):
                return self._complete_recovery(started, operation, forced=True, method=forced_method)

        elapsed_ms = int((time.monotonic() - started) * 1000.0)
        detail = self.to_summary()
        detail["visibility_recovery"] = {
            "operation": operation,
            "elapsed_ms": elapsed_ms,
            "forced": True,
            "method": forced_method,
        }
        logger.error(
            "Window visibility recovery failed: operation=%s hwnd=%s pid=%s elapsed_ms=%s method=%s",
            operation,
            self.hwnd,
            self.binding.pid,
            elapsed_ms,
            forced_method,
        )
        raise TargetRuntimeError(
            "window_not_visible",
            "The configured target window is not visible after recovery.",
            detail,
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
                {"hwnd": self.hwnd, "mode": self.binding.mode, "pid": self.binding.pid},
            )
        if self.binding.pid is not None:
            _, current_pid = win32process.GetWindowThreadProcessId(self.hwnd)
            if int(current_pid) != int(self.binding.pid):
                raise TargetRuntimeError(
                    "window_target_lost",
                    "The configured hwnd now belongs to a different process.",
                    {
                        "hwnd": self.hwnd,
                        "expected_pid": int(self.binding.pid),
                        "actual_pid": int(current_pid),
                    },
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
            "foreground": self._foreground_or_false(),
        }

    def _wait_until_visible(self, timeout_ms: int, poll_interval_ms: int) -> bool:
        deadline = time.monotonic() + max(int(timeout_ms), 0) / 1000.0
        while True:
            self.ensure_alive()
            if win32gui.IsWindowVisible(self.hwnd) and self._has_valid_client_area():
                return True
            if time.monotonic() >= deadline:
                return False
            sleep_sec = min(max(int(poll_interval_ms), 10) / 1000.0, max(deadline - time.monotonic(), 0.0))
            if sleep_sec > 0:
                time.sleep(sleep_sec)

    def _complete_recovery(
        self,
        started: float,
        operation: str,
        *,
        forced: bool,
        method: str,
    ) -> WindowRecoveryResult:
        time.sleep(_POST_RECOVERY_SETTLE_SEC)
        self.ensure_valid()
        elapsed_ms = int((time.monotonic() - started) * 1000.0)
        logger.info(
            "Window visibility recovery succeeded: operation=%s hwnd=%s pid=%s elapsed_ms=%s forced=%s method=%s",
            operation,
            self.hwnd,
            self.binding.pid,
            elapsed_ms,
            forced,
            method,
        )
        result = WindowRecoveryResult(
            attempted=True,
            recovered=True,
            forced=forced,
            method=method,
            elapsed_ms=elapsed_ms,
        )
        self.last_recovery_result = result
        return result

    def _has_valid_client_area(self) -> bool:
        try:
            _, _, width, height = self.get_client_rect_screen()
        except Exception:
            return False
        return width > 0 and height > 0

    def _show_window_async(self, command: int) -> None:
        try:
            ctypes.windll.user32.ShowWindowAsync(int(self.hwnd), int(command))
        except Exception as exc:
            logger.warning(
                "ShowWindowAsync failed during visibility recovery: hwnd=%s command=%s error=%s",
                self.hwnd,
                command,
                exc,
            )

    @staticmethod
    def _remaining_ms(deadline: float) -> int:
        return max(int((deadline - time.monotonic()) * 1000.0), 0)

    def _foreground_or_false(self) -> bool:
        try:
            return self.is_foreground()
        except Exception:
            return False


def _is_borderless_window(hwnd: int) -> bool:
    try:
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
    except Exception:
        return False
    return (style & win32con.WS_CAPTION) == 0 and (style & win32con.WS_THICKFRAME) == 0
