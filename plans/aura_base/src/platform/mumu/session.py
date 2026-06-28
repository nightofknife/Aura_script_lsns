# -*- coding: utf-8 -*-
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional, Tuple

from ..contracts import CaptureResult, TargetRuntimeError
from .adb_discovery import AdbController
from .android_touch_input import MuMuAndroidTouchInputBackend
from .scrcpy_capture import MuMuScrcpyCaptureBackend


ANDROID_KEYEVENT_NAME_MAP: Dict[str, str] = {
    "esc": "KEYCODE_ESCAPE",
    "escape": "KEYCODE_ESCAPE",
    "enter": "KEYCODE_ENTER",
    "space": "KEYCODE_SPACE",
    "tab": "KEYCODE_TAB",
    "backspace": "KEYCODE_DEL",
    "delete": "KEYCODE_FORWARD_DEL",
    "up": "KEYCODE_DPAD_UP",
    "down": "KEYCODE_DPAD_DOWN",
    "left": "KEYCODE_DPAD_LEFT",
    "right": "KEYCODE_DPAD_RIGHT",
    "shift": "KEYCODE_SHIFT_LEFT",
    "lshift": "KEYCODE_SHIFT_LEFT",
    "rshift": "KEYCODE_SHIFT_RIGHT",
    "ctrl": "KEYCODE_CTRL_LEFT",
    "lctrl": "KEYCODE_CTRL_LEFT",
    "rctrl": "KEYCODE_CTRL_RIGHT",
    "alt": "KEYCODE_ALT_LEFT",
    "lalt": "KEYCODE_ALT_LEFT",
    "ralt": "KEYCODE_ALT_RIGHT",
    "home": "KEYCODE_HOME",
    "back": "KEYCODE_BACK",
}

ANDROID_KEYCODE_INT_MAP: Dict[str, int] = {
    "esc": 111,
    "escape": 111,
    "enter": 66,
    "space": 62,
    "tab": 61,
    "backspace": 67,
    "delete": 112,
    "up": 19,
    "down": 20,
    "left": 21,
    "right": 22,
    "shift": 59,
    "lshift": 59,
    "rshift": 60,
    "ctrl": 113,
    "lctrl": 113,
    "rctrl": 114,
    "alt": 57,
    "lalt": 57,
    "ralt": 58,
    "home": 3,
    "back": 4,
}


class MuMuSession:
    def __init__(
        self,
        serial: str,
        adb: AdbController,
        capture_backend: MuMuScrcpyCaptureBackend,
        input_backend: MuMuAndroidTouchInputBackend,
        *,
        key_input_provider: str = "adb",
        text_input_provider: str = "adb",
        capture_backend_name: str = "scrcpy_stream",
        input_backend_name: str = "android_touch",
        timing_config: Dict[str, Any] | None = None,
    ):
        self.serial = serial
        self.adb = adb
        self.capture_backend = capture_backend
        self.input_backend = input_backend
        self.key_input_provider = str(key_input_provider or "adb").lower()
        self.text_input_provider = str(text_input_provider or "adb").lower()
        self.capture_backend_name = str(capture_backend_name or "scrcpy_stream").lower()
        self.input_backend_name = str(input_backend_name or "android_touch").lower()
        self.timing_config = dict(timing_config or {})
        self.default_key_interval_sec = max(float(self.timing_config.get("key_interval_ms", 40)) / 1000.0, 0.0)
        self.device_info = None
        self._lock = threading.RLock()
        self._best_effort_keys: set[str] = set()

    def ensure_ready(self):
        with self._lock:
            if self.device_info is None:
                self.device_info = self.adb.get_device_info(self.serial)
            self.capture_backend.ensure_ready()
            self.input_backend.ensure_ready()

    def is_healthy(self) -> bool:
        return self.capture_backend.is_healthy() and self.input_backend.is_healthy()

    def close(self):
        self.input_backend.close()
        self.capture_backend.close()

    def capture(self, rect: Optional[Tuple[int, int, int, int]] = None) -> CaptureResult:
        self.ensure_ready()
        return self.capture_backend.capture(rect=rect)

    def get_client_rect(self) -> Tuple[int, int, int, int] | None:
        self.ensure_ready()
        return self.capture_backend.get_client_rect()

    def get_pixel_color_at(self, x: int, y: int) -> Tuple[int, int, int]:
        self.ensure_ready()
        return self.capture_backend.get_pixel_color_at(x, y)

    def focus(self) -> bool:
        self.ensure_ready()
        return self.capture_backend.focus()

    def focus_with_input(self, _click_delay: float = 0.3) -> bool:
        self.ensure_ready()
        return True

    def click(
        self,
        x: Optional[int] = None,
        y: Optional[int] = None,
        *,
        button: str = "left",
        clicks: int = 1,
        interval: float | None = None,
    ):
        self.ensure_ready()
        self.input_backend.click(x=x, y=y, button=button, clicks=clicks, interval=interval)

    def move_to(self, x: int, y: int, *, duration: float | None = None):
        self.ensure_ready()
        self.input_backend.move_to(x, y, duration=duration)

    def move_relative(self, dx: int, dy: int, *, duration: float | None = None):
        self.ensure_ready()
        self.input_backend.move_relative(dx, dy, duration=duration)

    def mouse_down(self, *, button: str = "left"):
        self.ensure_ready()
        self.input_backend.mouse_down(button=button)

    def mouse_up(self, *, button: str = "left"):
        self.ensure_ready()
        self.input_backend.mouse_up(button=button)

    def drag_to(self, x: int, y: int, *, button: str = "left", duration: float | None = None):
        self.ensure_ready()
        self.input_backend.drag_to(x, y, button=button, duration=duration)

    def look_delta(self, dx: int, dy: int):
        self.ensure_ready()
        self.input_backend.look_delta(dx, dy)

    def look_hold(
        self,
        vx: float,
        vy: float,
        *,
        duration_ms: int,
        tick_ms: int | None = None,
    ):
        self.ensure_ready()
        self.input_backend.look_hold(vx, vy, duration_ms=duration_ms, tick_ms=tick_ms)

    def scroll(self, amount: int, direction: str = "down"):
        self.ensure_ready()
        semantic_direction = str(direction or "down").lower()
        if amount < 0:
            semantic_direction = "down"
        elif amount > 0 and direction == "down":
            semantic_direction = "up"
        self.input_backend.scroll(semantic_direction, abs(int(amount)))

    def release_all(self):
        self.input_backend.release_all()
        self._best_effort_keys.clear()

    def capabilities(self) -> Dict[str, Any]:
        return self.input_backend.capabilities()

    def press_key(self, key: str, presses: int = 1, interval: float | None = None):
        wait_interval = self.default_key_interval_sec if interval is None else max(float(interval), 0.0)
        if self.key_input_provider == "scrcpy":
            keycode = _resolve_android_keycode_int(key)
            for index in range(max(int(presses), 1)):
                self.capture_backend.send_keycode(keycode, "down")
                self.capture_backend.send_keycode(keycode, "up")
                if index < max(int(presses), 1) - 1:
                    time.sleep(wait_interval)
            return

        keyevent = _resolve_android_keyevent_name(key)
        for index in range(max(int(presses), 1)):
            self.adb.input_keyevent(self.serial, keyevent)
            if index < max(int(presses), 1) - 1:
                time.sleep(wait_interval)

    def key_down(self, key: str):
        if self.key_input_provider == "scrcpy":
            keycode = _resolve_android_keycode_int(key)
            self.capture_backend.send_keycode(keycode, "down")
            self._best_effort_keys.add(str(key).lower())
            return
        self.press_key(key, presses=1, interval=0.0)
        self._best_effort_keys.add(str(key).lower())

    def key_up(self, key: str):
        normalized = str(key).lower()
        if self.key_input_provider == "scrcpy":
            keycode = _resolve_android_keycode_int(key)
            self.capture_backend.send_keycode(keycode, "up")
        self._best_effort_keys.discard(normalized)

    def type_text(self, text: str, interval: float | None = None):
        content = str(text or "")
        if not content:
            return
        wait_interval = self.default_key_interval_sec if interval is None else max(float(interval), 0.0)
        if self.text_input_provider == "adb":
            if wait_interval <= 0:
                self.adb.input_text(self.serial, content)
                return
            for char in content:
                self.adb.input_text(self.serial, char)
                time.sleep(wait_interval)
            return
        raise TargetRuntimeError(
            "text_input_provider_unsupported",
            f"Unsupported text_input.provider '{self.text_input_provider}'.",
        )

    def self_check(self) -> Dict[str, Any]:
        device_info = self.device_info or self.adb.get_device_info(self.serial)
        return {
            "ok": self.is_healthy(),
            "provider": "mumu",
            "serial": self.serial,
            "device": {
                "manufacturer": device_info.manufacturer,
                "model": device_info.model,
                "abi": device_info.abi,
            },
            "capture": self.capture_backend.self_check(),
            "input": self.input_backend.self_check(),
            "key_input_provider": self.key_input_provider,
            "text_input_provider": self.text_input_provider,
            "capture_backend": self.capture_backend_name,
            "input_backend": self.input_backend_name,
            "capabilities": self.capabilities(),
        }

    def list_capture_backends(self) -> Dict[str, Any]:
        return {
            "available": ["scrcpy_stream"],
            "enabled": [self.capture_backend_name],
            "default": self.capture_backend_name,
        }

    def set_capture_backend(self, backend: str):
        normalized = str(backend or "").lower()
        if normalized != self.capture_backend_name:
            raise TargetRuntimeError(
                "backend_runtime_switch_unsupported",
                "Runtime capture backend switching is unsupported; update config.yaml and rebuild the runtime.",
                {"requested": backend, "configured": self.capture_backend_name},
            )


def _resolve_android_keyevent_name(key: str) -> str:
    normalized = str(key or "").strip().lower()
    if not normalized:
        raise TargetRuntimeError("keyevent_invalid", "Key name is empty.")
    if normalized in ANDROID_KEYEVENT_NAME_MAP:
        return ANDROID_KEYEVENT_NAME_MAP[normalized]
    if len(normalized) == 1 and normalized.isalpha():
        return f"KEYCODE_{normalized.upper()}"
    if len(normalized) == 1 and normalized.isdigit():
        return f"KEYCODE_{normalized}"
    if normalized.startswith("f") and normalized[1:].isdigit():
        return f"KEYCODE_{normalized.upper()}"
    raise TargetRuntimeError(
        "keyevent_unsupported",
        f"Unsupported Android key '{key}'.",
        {"key": key},
    )


def _resolve_android_keycode_int(key: str) -> int:
    normalized = str(key or "").strip().lower()
    if normalized in ANDROID_KEYCODE_INT_MAP:
        return ANDROID_KEYCODE_INT_MAP[normalized]
    if len(normalized) == 1 and normalized.isalpha():
        return 29 + (ord(normalized) - ord("a"))
    if len(normalized) == 1 and normalized.isdigit():
        return 7 + int(normalized)
    if normalized.startswith("f") and normalized[1:].isdigit():
        index = int(normalized[1:])
        if 1 <= index <= 12:
            return 131 + (index - 1)
    raise TargetRuntimeError(
        "keycode_unsupported",
        f"Unsupported Android keycode mapping for '{key}'.",
        {"key": key},
    )
