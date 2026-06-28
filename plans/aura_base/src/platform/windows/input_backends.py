# -*- coding: utf-8 -*-
from __future__ import annotations

import ctypes
import math
import os
import time
from typing import Any

import win32api
import win32con
import win32gui

from ..contracts import TargetRuntimeError
from .window_target import WindowTarget


_ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", _ULONG_PTR),
    )


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", _ULONG_PTR),
    )


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = (
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_short),
        ("wParamH", ctypes.c_ushort),
    )


class _INPUTUNION(ctypes.Union):
    _fields_ = (
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    )


class _INPUT(ctypes.Structure):
    _fields_ = (
        ("type", ctypes.c_ulong),
        ("union", _INPUTUNION),
    )


_BUTTON_MESSAGES = {
    "left": (win32con.WM_LBUTTONDOWN, win32con.WM_LBUTTONUP, win32con.MK_LBUTTON),
    "right": (win32con.WM_RBUTTONDOWN, win32con.WM_RBUTTONUP, win32con.MK_RBUTTON),
    "middle": (win32con.WM_MBUTTONDOWN, win32con.WM_MBUTTONUP, win32con.MK_MBUTTON),
}

_BUTTON_SENDINPUT_FLAGS = {
    "left": (win32con.MOUSEEVENTF_LEFTDOWN, win32con.MOUSEEVENTF_LEFTUP),
    "right": (win32con.MOUSEEVENTF_RIGHTDOWN, win32con.MOUSEEVENTF_RIGHTUP),
    "middle": (win32con.MOUSEEVENTF_MIDDLEDOWN, win32con.MOUSEEVENTF_MIDDLEUP),
}

_SPECIAL_KEYS = {
    "enter": win32con.VK_RETURN,
    "tab": win32con.VK_TAB,
    "space": win32con.VK_SPACE,
    "esc": win32con.VK_ESCAPE,
    "escape": win32con.VK_ESCAPE,
    "backspace": win32con.VK_BACK,
    "delete": win32con.VK_DELETE,
    "up": win32con.VK_UP,
    "down": win32con.VK_DOWN,
    "left": win32con.VK_LEFT,
    "right": win32con.VK_RIGHT,
    "shift": win32con.VK_SHIFT,
    "ctrl": win32con.VK_CONTROL,
    "alt": win32con.VK_MENU,
    "home": win32con.VK_HOME,
    "end": win32con.VK_END,
    "pageup": win32con.VK_PRIOR,
    "pagedown": win32con.VK_NEXT,
}

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_QUERY = 0x0008
_TOKEN_INTEGRITY_LEVEL = 25
_INTEGRITY_RID_LOW = 0x1000
_INTEGRITY_RID_MEDIUM = 0x2000
_INTEGRITY_RID_HIGH = 0x3000
_INTEGRITY_RID_SYSTEM = 0x4000


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = (
        ("Sid", ctypes.c_void_p),
        ("Attributes", ctypes.c_ulong),
    )


class _TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = (("Label", _SID_AND_ATTRIBUTES),)


def _integrity_label_from_rid(rid: int) -> str:
    normalized = int(rid)
    if normalized >= _INTEGRITY_RID_SYSTEM:
        return "system"
    if normalized >= _INTEGRITY_RID_HIGH:
        return "high"
    if normalized >= _INTEGRITY_RID_MEDIUM:
        return "medium"
    if normalized >= _INTEGRITY_RID_LOW:
        return "low"
    return "untrusted"


def _get_process_integrity_level(pid: int | None) -> dict[str, Any] | None:
    if pid is None:
        return None

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    open_process = kernel32.OpenProcess
    open_process.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    open_process.restype = ctypes.c_void_p

    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int

    open_process_token = advapi32.OpenProcessToken
    open_process_token.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)]
    open_process_token.restype = ctypes.c_int

    get_token_information = advapi32.GetTokenInformation
    get_token_information.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    get_token_information.restype = ctypes.c_int

    get_sid_sub_authority_count = advapi32.GetSidSubAuthorityCount
    get_sid_sub_authority_count.argtypes = [ctypes.c_void_p]
    get_sid_sub_authority_count.restype = ctypes.POINTER(ctypes.c_ubyte)

    get_sid_sub_authority = advapi32.GetSidSubAuthority
    get_sid_sub_authority.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    get_sid_sub_authority.restype = ctypes.POINTER(ctypes.c_ulong)

    process_handle = open_process(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not process_handle:
        return None

    try:
        token_handle = ctypes.c_void_p()
        if not open_process_token(process_handle, _TOKEN_QUERY, ctypes.byref(token_handle)):
            return None

        try:
            needed = ctypes.c_ulong(0)
            get_token_information(token_handle, _TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(needed))
            if needed.value <= 0:
                return None

            buffer = ctypes.create_string_buffer(needed.value)
            if not get_token_information(
                token_handle,
                _TOKEN_INTEGRITY_LEVEL,
                buffer,
                needed.value,
                ctypes.byref(needed),
            ):
                return None

            token_label = _TOKEN_MANDATORY_LABEL.from_buffer(buffer)
            sid = token_label.Label.Sid
            if not sid:
                return None

            sub_auth_count = int(get_sid_sub_authority_count(sid)[0])
            if sub_auth_count <= 0:
                return None
            rid = int(get_sid_sub_authority(sid, sub_auth_count - 1)[0])
            return {
                "pid": int(pid),
                "rid": rid,
                "label": _integrity_label_from_rid(rid),
            }
        finally:
            close_handle(token_handle)
    finally:
        close_handle(process_handle)


class BaseWindowsInputBackend:
    backend_name = ""

    def __init__(self, target: WindowTarget, config: dict[str, Any] | None = None):
        self.target = target
        self.config = dict(config or {})
        self.focus_before_input = bool(self.config.get("focus_before_input", True))
        self.default_move_duration_sec = max(float(self.config.get("mouse_move_duration_ms", 120)) / 1000.0, 0.0)
        self.default_key_interval_sec = max(float(self.config.get("key_interval_ms", 40)) / 1000.0, 0.0)
        self.click_post_delay_sec = max(float(self.config.get("click_post_delay_ms", 30)) / 1000.0, 0.0)
        look_config = dict(self.config.get("look", {}) or {})
        self.look_tick_ms = max(int(look_config.get("tick_ms", 16) or 16), 1)
        self.look_base_delta = max(int(look_config.get("base_delta", 24) or 24), 1)
        self.look_max_delta_per_tick = max(int(look_config.get("max_delta_per_tick", 96) or 96), 1)
        self.look_scale_x = max(float(look_config.get("scale_x", 1.0) or 1.0), 0.0001)
        self.look_scale_y = max(float(look_config.get("scale_y", 1.0) or 1.0), 0.0001)
        self.look_invert_y = bool(look_config.get("invert_y", False))
        self.allow_integrity_mismatch = bool(self.config.get("allow_integrity_mismatch", False))
        self._held_keys: set[int] = set()
        self._held_mouse_buttons: set[str] = set()
        self._cursor_position: tuple[int, int] | None = None
        self._integrity_error: TargetRuntimeError | None = None
        self._integrity_checked = False

    def close(self) -> None:
        self.release_all()

    def capabilities(self) -> dict[str, Any]:
        return {
            "absolute_pointer": True,
            "relative_look": False,
            "keyboard": True,
            "text_input": True,
            "background_input": False,
        }

    def self_check(self) -> dict[str, Any]:
        return {
            "ok": True,
            "backend": self.backend_name,
            "focus_before_input": self.focus_before_input,
            "look": {
                "tick_ms": self.look_tick_ms,
                "base_delta": self.look_base_delta,
                "max_delta_per_tick": self.look_max_delta_per_tick,
                "scale_x": self.look_scale_x,
                "scale_y": self.look_scale_y,
                "invert_y": self.look_invert_y,
            },
            "cursor_position": list(self._cursor_position) if self._cursor_position else None,
            "held_mouse_buttons": sorted(self._held_mouse_buttons),
            "held_keys": sorted(self._held_keys),
            "capabilities": self.capabilities(),
        }

    def _resolve_duration(self, duration: float | None) -> float:
        return self.default_move_duration_sec if duration is None else max(float(duration), 0.0)

    def _resolve_interval(self, interval: float | None) -> float:
        return self.default_key_interval_sec if interval is None else max(float(interval), 0.0)

    def _apply_click_post_delay(self) -> None:
        if self.click_post_delay_sec > 0:
            time.sleep(self.click_post_delay_sec)

    def _ensure_integrity_compatible(self) -> None:
        if self.allow_integrity_mismatch:
            return
        if self._integrity_checked:
            if self._integrity_error is not None:
                raise self._integrity_error
            return

        self._integrity_checked = True
        target_binding = getattr(self.target, "binding", None)
        target_pid = getattr(target_binding, "pid", None)
        current_integrity = _get_process_integrity_level(os.getpid())
        target_integrity = _get_process_integrity_level(target_pid)

        if current_integrity is None or target_integrity is None:
            return
        if int(target_integrity["rid"]) <= int(current_integrity["rid"]):
            return

        target_title = getattr(target_binding, "title", None)
        target_process_name = getattr(target_binding, "process_name", None)
        detail = {
            "backend": self.backend_name,
            "current_process_pid": os.getpid(),
            "current_process_integrity": current_integrity["label"],
            "target_pid": target_pid,
            "target_process_integrity": target_integrity["label"],
            "target_process_name": target_process_name,
            "target_title": target_title,
            "suggestion": "Run Aura elevated or route desktop input through an elevated helper process.",
        }
        self._integrity_error = TargetRuntimeError(
            "input_integrity_mismatch",
            (
                f"Backend '{self.backend_name}' cannot drive the target window because the current process "
                f"runs at {current_integrity['label']} integrity while the target process runs at "
                f"{target_integrity['label']} integrity."
            ),
            detail,
        )
        raise self._integrity_error

    def _resolve_point(self, x: int | None, y: int | None) -> tuple[int, int]:
        if x is not None and y is not None:
            return self._clamp_client_point(int(x), int(y))
        if self._cursor_position is not None:
            return self._cursor_position
        client_rect = self.target.get_client_rect()
        return int(client_rect[2] / 2), int(client_rect[3] / 2)

    def _clamp_client_point(self, x: int, y: int) -> tuple[int, int]:
        _, _, width, height = self.target.get_client_rect()
        clamped_x = min(max(int(x), 0), max(width - 1, 0))
        clamped_y = min(max(int(y), 0), max(height - 1, 0))
        return clamped_x, clamped_y

    def _client_to_screen(self, x: int, y: int) -> tuple[int, int]:
        left, top, _, _ = self.target.get_client_rect_screen()
        return left + int(x), top + int(y)

    def _ensure_focus(self) -> None:
        if not self.focus_before_input:
            return
        if not self.target.focus():
            raise TargetRuntimeError(
                "window_focus_required",
                "focus_before_input=true but the target window could not be focused.",
                self.target.to_summary(),
            )

    def look_delta(self, dx: int, dy: int) -> None:
        raise TargetRuntimeError(
            "input_capability_unsupported",
            f"Backend '{self.backend_name}' does not support relative look input.",
            {"backend": self.backend_name, "feature": "relative_look"},
        )

    def look_hold(
        self,
        vx: float,
        vy: float,
        *,
        duration_ms: int,
        tick_ms: int | None = None,
    ) -> None:
        raise TargetRuntimeError(
            "input_capability_unsupported",
            f"Backend '{self.backend_name}' does not support sustained relative look input.",
            {"backend": self.backend_name, "feature": "relative_look"},
        )

    def _resolve_look_tick_ms(self, tick_ms: int | None) -> int:
        resolved = self.look_tick_ms if tick_ms is None else int(tick_ms)
        if resolved <= 0:
            raise TargetRuntimeError(
                "look_tick_invalid",
                "look tick must be greater than 0.",
                {"tick_ms": resolved},
            )
        return resolved

    def _validate_look_duration_ms(self, duration_ms: int) -> int:
        resolved = int(duration_ms)
        if resolved <= 0:
            raise TargetRuntimeError(
                "look_duration_invalid",
                "look duration must be greater than 0.",
                {"duration_ms": resolved},
            )
        return resolved

    def _resolve_look_strength(self, value: float, axis: str) -> float:
        resolved = float(value)
        if math.isnan(resolved) or resolved < -1.0 or resolved > 1.0:
            raise TargetRuntimeError(
                "look_strength_invalid",
                f"look strength for axis '{axis}' must be in [-1.0, 1.0].",
                {"axis": axis, "value": value},
            )
        return resolved

    def _resolve_look_delta_units(self, dx: float, dy: float) -> tuple[int, int]:
        def quantize(raw: float, *, scale: float) -> int:
            if raw == 0:
                return 0
            scaled = raw * scale
            quantized = int(round(scaled))
            if quantized == 0:
                quantized = 1 if scaled > 0 else -1
            limit = self.look_max_delta_per_tick
            return max(min(quantized, limit), -limit)

        resolved_dx = quantize(float(dx), scale=self.look_scale_x)
        effective_dy = -float(dy) if self.look_invert_y else float(dy)
        resolved_dy = quantize(effective_dy, scale=self.look_scale_y)
        return resolved_dx, resolved_dy


class WindowsSendInputBackend(BaseWindowsInputBackend):
    backend_name = "sendinput"

    def capabilities(self) -> dict[str, Any]:
        capabilities = super().capabilities()
        capabilities["relative_look"] = True
        return capabilities

    def click(
        self,
        x: int | None = None,
        y: int | None = None,
        *,
        button: str = "left",
        clicks: int = 1,
        interval: float | None = None,
    ) -> None:
        self._ensure_integrity_compatible()
        self._ensure_focus()
        client_x, client_y = self._resolve_point(x, y)
        self.move_to(client_x, client_y, duration=0.0)
        down_flag, up_flag = _resolve_sendinput_button_flags(button)
        wait_interval = self._resolve_interval(interval)
        for index in range(max(int(clicks), 1)):
            _send_mouse_input(flags=down_flag)
            _send_mouse_input(flags=up_flag)
            self._apply_click_post_delay()
            if index < max(int(clicks), 1) - 1 and wait_interval > 0:
                time.sleep(wait_interval)

    def move_to(self, x: int, y: int, *, duration: float | None = None) -> None:
        self._ensure_integrity_compatible()
        self._ensure_focus()
        client_x, client_y = self._resolve_point(x, y)
        destination = self._client_to_screen(client_x, client_y)
        current = win32api.GetCursorPos()
        total_duration = self._resolve_duration(duration)
        if total_duration <= 0:
            win32api.SetCursorPos(destination)
            self._cursor_position = (client_x, client_y)
            return

        steps = max(int(total_duration / 0.02), 1)
        start_x, start_y = current
        end_x, end_y = destination
        for step in range(1, steps + 1):
            progress = step / steps
            next_pos = (
                int(round(start_x + (end_x - start_x) * progress)),
                int(round(start_y + (end_y - start_y) * progress)),
            )
            win32api.SetCursorPos(next_pos)
            time.sleep(total_duration / steps)
        self._cursor_position = (client_x, client_y)

    def move_relative(self, dx: int, dy: int, *, duration: float | None = None) -> None:
        start_x, start_y = self._resolve_point(None, None)
        self.move_to(start_x + int(dx), start_y + int(dy), duration=duration)

    def mouse_down(self, *, button: str = "left") -> None:
        self._ensure_integrity_compatible()
        self._ensure_focus()
        down_flag, _ = _resolve_sendinput_button_flags(button)
        _send_mouse_input(flags=down_flag)
        self._held_mouse_buttons.add(str(button).lower())

    def mouse_up(self, *, button: str = "left") -> None:
        self._ensure_integrity_compatible()
        self._ensure_focus()
        _, up_flag = _resolve_sendinput_button_flags(button)
        _send_mouse_input(flags=up_flag)
        self._held_mouse_buttons.discard(str(button).lower())

    def drag_to(self, x: int, y: int, *, button: str = "left", duration: float | None = None) -> None:
        self._ensure_integrity_compatible()
        self._ensure_focus()
        self.mouse_down(button=button)
        try:
            self.move_to(int(x), int(y), duration=duration)
        finally:
            self.mouse_up(button=button)

    def scroll(self, amount: int, direction: str = "down") -> None:
        self._ensure_integrity_compatible()
        self._ensure_focus()
        steps = max(abs(int(amount)), 1)
        delta = 120 * steps
        if str(direction or "down").lower() == "down":
            delta = -delta
        _send_mouse_input(flags=win32con.MOUSEEVENTF_WHEEL, mouse_data=delta)

    def press_key(self, key: str, presses: int = 1, interval: float | None = None) -> None:
        self._ensure_integrity_compatible()
        self._ensure_focus()
        vk_code = _resolve_vk_code(key)
        wait_interval = self._resolve_interval(interval)
        for index in range(max(int(presses), 1)):
            _send_key_input(vk_code, key_up=False)
            _send_key_input(vk_code, key_up=True)
            if index < max(int(presses), 1) - 1 and wait_interval > 0:
                time.sleep(wait_interval)

    def key_down(self, key: str) -> None:
        self._ensure_integrity_compatible()
        self._ensure_focus()
        vk_code = _resolve_vk_code(key)
        _send_key_input(vk_code, key_up=False)
        self._held_keys.add(vk_code)

    def key_up(self, key: str) -> None:
        self._ensure_integrity_compatible()
        self._ensure_focus()
        vk_code = _resolve_vk_code(key)
        _send_key_input(vk_code, key_up=True)
        self._held_keys.discard(vk_code)

    def type_text(self, text: str, interval: float | None = None) -> None:
        self._ensure_integrity_compatible()
        self._ensure_focus()
        wait_interval = self._resolve_interval(interval)
        for char in str(text or ""):
            if char == "\n":
                self.press_key("enter", presses=1, interval=0.0)
            elif char == "\t":
                self.press_key("tab", presses=1, interval=0.0)
            else:
                _send_unicode_char(char)
            if wait_interval > 0:
                time.sleep(wait_interval)

    def look_delta(self, dx: int, dy: int) -> None:
        self._ensure_integrity_compatible()
        self._ensure_focus()
        resolved_dx, resolved_dy = self._resolve_look_delta_units(dx, dy)
        if resolved_dx == 0 and resolved_dy == 0:
            return
        _send_mouse_input(
            flags=win32con.MOUSEEVENTF_MOVE,
            dx=resolved_dx,
            dy=resolved_dy,
        )

    def look_hold(
        self,
        vx: float,
        vy: float,
        *,
        duration_ms: int,
        tick_ms: int | None = None,
    ) -> None:
        self._ensure_integrity_compatible()
        self._ensure_focus()
        resolved_duration_ms = self._validate_look_duration_ms(duration_ms)
        resolved_tick_ms = self._resolve_look_tick_ms(tick_ms)
        strength_x = self._resolve_look_strength(vx, axis="x")
        strength_y = self._resolve_look_strength(vy, axis="y")
        per_tick_dx, per_tick_dy = self._resolve_look_delta_units(
            self.look_base_delta * strength_x,
            self.look_base_delta * strength_y,
        )
        if per_tick_dx == 0 and per_tick_dy == 0:
            return

        steps = max(int(math.ceil(resolved_duration_ms / resolved_tick_ms)), 1)
        sleep_sec = max(float(resolved_tick_ms) / 1000.0, 0.0)
        for step in range(steps):
            _send_mouse_input(
                flags=win32con.MOUSEEVENTF_MOVE,
                dx=per_tick_dx,
                dy=per_tick_dy,
            )
            if sleep_sec > 0 and step < steps - 1:
                time.sleep(sleep_sec)

    def release_all(self) -> None:
        for button in list(self._held_mouse_buttons):
            try:
                _, up_flag = _resolve_sendinput_button_flags(button)
                _send_mouse_input(flags=up_flag)
            except Exception:
                pass
        for vk_code in list(self._held_keys):
            try:
                _send_key_input(vk_code, key_up=True)
            except Exception:
                pass
        self._held_mouse_buttons.clear()
        self._held_keys.clear()


class WindowsWindowMessageInputBackend(BaseWindowsInputBackend):
    backend_name = "window_message"
    _UNSUPPORTED_WINDOW_CLASSES = {
        "FLUTTER_RUNNER_WIN32_WINDOW",
    }

    def __init__(self, target: WindowTarget, config: dict[str, Any] | None = None):
        super().__init__(target, config)
        self._validate_window_target()
        self._ensure_integrity_compatible()

    def capabilities(self) -> dict[str, Any]:
        capabilities = super().capabilities()
        capabilities["background_input"] = True
        return capabilities

    def click(
        self,
        x: int | None = None,
        y: int | None = None,
        *,
        button: str = "left",
        clicks: int = 1,
        interval: float | None = None,
    ) -> None:
        client_x, client_y = self._resolve_point(x, y)
        self.move_to(client_x, client_y, duration=0.0)
        down_msg, up_msg, key_flag = _resolve_window_message_button(button)
        lparam = win32api.MAKELONG(client_x, client_y)
        wait_interval = self._resolve_interval(interval)
        for index in range(max(int(clicks), 1)):
            win32gui.SendMessage(self.target.hwnd, down_msg, key_flag, lparam)
            win32gui.SendMessage(self.target.hwnd, up_msg, 0, lparam)
            self._apply_click_post_delay()
            if index < max(int(clicks), 1) - 1 and wait_interval > 0:
                time.sleep(wait_interval)

    def move_to(self, x: int, y: int, *, duration: float | None = None) -> None:
        client_x, client_y = self._resolve_point(x, y)
        duration_sec = self._resolve_duration(duration)
        start_x, start_y = self._resolve_point(None, None)
        steps = max(int(duration_sec / 0.02), 1) if duration_sec > 0 else 1
        for step in range(1, steps + 1):
            progress = step / steps
            next_x = int(round(start_x + (client_x - start_x) * progress))
            next_y = int(round(start_y + (client_y - start_y) * progress))
            win32gui.SendMessage(self.target.hwnd, win32con.WM_MOUSEMOVE, 0, win32api.MAKELONG(next_x, next_y))
            if duration_sec > 0 and step < steps:
                time.sleep(duration_sec / steps)
        self._cursor_position = (client_x, client_y)

    def move_relative(self, dx: int, dy: int, *, duration: float | None = None) -> None:
        start_x, start_y = self._resolve_point(None, None)
        self.move_to(start_x + int(dx), start_y + int(dy), duration=duration)

    def mouse_down(self, *, button: str = "left") -> None:
        client_x, client_y = self._resolve_point(None, None)
        down_msg, _, key_flag = _resolve_window_message_button(button)
        win32gui.SendMessage(self.target.hwnd, down_msg, key_flag, win32api.MAKELONG(client_x, client_y))
        self._held_mouse_buttons.add(str(button).lower())

    def mouse_up(self, *, button: str = "left") -> None:
        client_x, client_y = self._resolve_point(None, None)
        _, up_msg, _ = _resolve_window_message_button(button)
        win32gui.SendMessage(self.target.hwnd, up_msg, 0, win32api.MAKELONG(client_x, client_y))
        self._held_mouse_buttons.discard(str(button).lower())

    def drag_to(self, x: int, y: int, *, button: str = "left", duration: float | None = None) -> None:
        if str(button or "left").lower() not in _BUTTON_MESSAGES:
            raise TargetRuntimeError(
                "unsupported_mouse_button",
                f"Unsupported mouse button '{button}' for window_message backend.",
                {"button": button},
            )
        if str(button).lower() not in self._held_mouse_buttons:
            self.mouse_down(button=button)
        try:
            self.move_to(int(x), int(y), duration=duration)
        finally:
            self.mouse_up(button=button)

    def scroll(self, amount: int, direction: str = "down") -> None:
        client_x, client_y = self._resolve_point(None, None)
        steps = max(abs(int(amount)), 1)
        delta = 120 * steps
        if str(direction or "down").lower() == "down":
            delta = -delta
        wparam = win32api.MAKELONG(0, ctypes.c_ushort(delta & 0xFFFF).value)
        win32gui.SendMessage(self.target.hwnd, win32con.WM_MOUSEWHEEL, wparam, win32api.MAKELONG(client_x, client_y))

    def press_key(self, key: str, presses: int = 1, interval: float | None = None) -> None:
        vk_code = _resolve_vk_code(key)
        wait_interval = self._resolve_interval(interval)
        for index in range(max(int(presses), 1)):
            win32gui.SendMessage(self.target.hwnd, win32con.WM_KEYDOWN, vk_code, 0)
            win32gui.SendMessage(self.target.hwnd, win32con.WM_KEYUP, vk_code, 0)
            if index < max(int(presses), 1) - 1 and wait_interval > 0:
                time.sleep(wait_interval)

    def key_down(self, key: str) -> None:
        vk_code = _resolve_vk_code(key)
        win32gui.SendMessage(self.target.hwnd, win32con.WM_KEYDOWN, vk_code, 0)
        self._held_keys.add(vk_code)

    def key_up(self, key: str) -> None:
        vk_code = _resolve_vk_code(key)
        win32gui.SendMessage(self.target.hwnd, win32con.WM_KEYUP, vk_code, 0)
        self._held_keys.discard(vk_code)

    def type_text(self, text: str, interval: float | None = None) -> None:
        wait_interval = self._resolve_interval(interval)
        for char in str(text or ""):
            if char == "\n":
                self.press_key("enter", presses=1, interval=0.0)
            elif char == "\t":
                self.press_key("tab", presses=1, interval=0.0)
            else:
                code_point = ord(char)
                if code_point > 0xFFFF:
                    raise TargetRuntimeError(
                        "text_input_unsupported",
                        f"window_message backend cannot type non-BMP character '{char}'.",
                        {"character": char},
                    )
                win32gui.SendMessage(self.target.hwnd, win32con.WM_CHAR, code_point, 0)
            if wait_interval > 0:
                time.sleep(wait_interval)

    def release_all(self) -> None:
        for button in list(self._held_mouse_buttons):
            try:
                self.mouse_up(button=button)
            except Exception:
                pass
        for vk_code in list(self._held_keys):
            try:
                win32gui.SendMessage(self.target.hwnd, win32con.WM_KEYUP, vk_code, 0)
            except Exception:
                pass
        self._held_mouse_buttons.clear()
        self._held_keys.clear()

    def _validate_window_target(self) -> None:
        class_name = str(self.target.binding.class_name or "").strip()
        if bool(self.config.get("window_message_allow_unsupported", False)):
            return
        if class_name.upper() in self._UNSUPPORTED_WINDOW_CLASSES:
            raise TargetRuntimeError(
                "input_backend_unsupported_for_window",
                "window_message backend is unsupported for the current target window class.",
                {
                    "backend": self.backend_name,
                    "class_name": class_name,
                    "process_name": self.target.binding.process_name,
                    "title": self.target.binding.title,
                },
            )


def build_input_backend(backend: str, target: WindowTarget, config: dict[str, Any]) -> BaseWindowsInputBackend:
    normalized = str(backend or "").strip().lower()
    if normalized == "sendinput":
        return WindowsSendInputBackend(target, config)
    if normalized == "window_message":
        return WindowsWindowMessageInputBackend(target, config)
    raise TargetRuntimeError(
        "input_backend_invalid_for_provider",
        f"Unsupported Windows input backend '{backend}'.",
        {"backend": backend},
    )


def _resolve_sendinput_button_flags(button: str) -> tuple[int, int]:
    normalized = str(button or "left").strip().lower()
    if normalized not in _BUTTON_SENDINPUT_FLAGS:
        raise TargetRuntimeError(
            "unsupported_mouse_button",
            f"Unsupported mouse button '{button}' for sendinput backend.",
            {"button": button},
        )
    return _BUTTON_SENDINPUT_FLAGS[normalized]


def _resolve_window_message_button(button: str) -> tuple[int, int, int]:
    normalized = str(button or "left").strip().lower()
    if normalized not in _BUTTON_MESSAGES:
        raise TargetRuntimeError(
            "unsupported_mouse_button",
            f"Unsupported mouse button '{button}' for window_message backend.",
            {"button": button},
        )
    return _BUTTON_MESSAGES[normalized]


def _resolve_vk_code(key: str) -> int:
    normalized = str(key or "").strip().lower()
    if not normalized:
        raise TargetRuntimeError("keycode_invalid", "Key name is empty.")
    if normalized in _SPECIAL_KEYS:
        return _SPECIAL_KEYS[normalized]
    if len(normalized) == 1 and normalized.isalpha():
        return ord(normalized.upper())
    if len(normalized) == 1 and normalized.isdigit():
        return ord(normalized)
    if normalized.startswith("f") and normalized[1:].isdigit():
        index = int(normalized[1:])
        if 1 <= index <= 24:
            return win32con.VK_F1 + (index - 1)
    raise TargetRuntimeError(
        "keycode_unsupported",
        f"Unsupported Windows key '{key}'.",
        {"key": key},
    )


def _send_mouse_input(*, flags: int, mouse_data: int = 0, dx: int = 0, dy: int = 0) -> None:
    command = _INPUT(
        type=0,
        union=_INPUTUNION(
            mi=_MOUSEINPUT(
                dx=int(dx),
                dy=int(dy),
                mouseData=int(mouse_data),
                dwFlags=int(flags),
                time=0,
                dwExtraInfo=0,
            )
        ),
    )
    sent = ctypes.windll.user32.SendInput(1, ctypes.byref(command), ctypes.sizeof(command))
    if sent != 1:
        raise TargetRuntimeError(
            "windows_input_failed",
            "SendInput failed to send a mouse event.",
            {"flags": int(flags), "mouse_data": int(mouse_data), "dx": int(dx), "dy": int(dy)},
        )


def _send_key_input(vk_code: int, *, key_up: bool) -> None:
    flags = win32con.KEYEVENTF_KEYUP if key_up else 0
    command = _INPUT(
        type=1,
        union=_INPUTUNION(
            ki=_KEYBDINPUT(
                wVk=int(vk_code),
                wScan=0,
                dwFlags=int(flags),
                time=0,
                dwExtraInfo=0,
            )
        ),
    )
    sent = ctypes.windll.user32.SendInput(1, ctypes.byref(command), ctypes.sizeof(command))
    if sent != 1:
        raise TargetRuntimeError(
            "windows_input_failed",
            "SendInput failed to send a keyboard event.",
            {"vk_code": int(vk_code), "key_up": bool(key_up)},
        )


def _send_unicode_char(char: str) -> None:
    code_point = ord(char)
    if code_point > 0xFFFF:
        raise TargetRuntimeError(
            "text_input_unsupported",
            f"sendinput backend cannot type non-BMP character '{char}'.",
            {"character": char},
        )
    down = _INPUT(
        type=1,
        union=_INPUTUNION(
            ki=_KEYBDINPUT(
                wVk=0,
                wScan=code_point,
                dwFlags=win32con.KEYEVENTF_UNICODE,
                time=0,
                dwExtraInfo=0,
            )
        ),
    )
    up = _INPUT(
        type=1,
        union=_INPUTUNION(
            ki=_KEYBDINPUT(
                wVk=0,
                wScan=code_point,
                dwFlags=win32con.KEYEVENTF_UNICODE | win32con.KEYEVENTF_KEYUP,
                time=0,
                dwExtraInfo=0,
            )
        ),
    )
    commands = (_INPUT * 2)(down, up)
    sent = ctypes.windll.user32.SendInput(2, commands, ctypes.sizeof(_INPUT))
    if sent != 2:
        raise TargetRuntimeError(
            "windows_input_failed",
            "SendInput failed to send a unicode character.",
            {"character": char},
        )
