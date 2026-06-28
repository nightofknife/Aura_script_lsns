# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib
import time
from typing import Any

from ..contracts import TargetRuntimeError
from ..runtime_config import RuntimeGamepadConfig


class BaseWindowsGamepadBackend:
    backend_name = ""

    def __init__(self, config: RuntimeGamepadConfig):
        self.config = config

    def capabilities(self) -> dict[str, Any]:
        return {
            "buttons": True,
            "sticks": True,
            "triggers": True,
            "device_type": self.config.device_type,
            "backend": self.backend_name,
        }

    def self_check(self) -> dict[str, Any]:
        return {
            "ok": True,
            "backend": self.backend_name,
            "device_type": self.config.device_type,
            "capabilities": self.capabilities(),
        }

    def press_button(self, button: str) -> None:
        raise NotImplementedError

    def release_button(self, button: str) -> None:
        raise NotImplementedError

    def tap_button(self, button: str, *, duration_ms: int = 0) -> None:
        self.press_button(button)
        if duration_ms > 0:
            time.sleep(float(duration_ms) / 1000.0)
        self.release_button(button)

    def tilt_stick(self, *, stick: str, x: float, y: float, duration_ms: int = 0, auto_center: bool = False) -> None:
        raise NotImplementedError

    def center_stick(self, stick: str) -> None:
        self.tilt_stick(stick=stick, x=0.0, y=0.0, duration_ms=0, auto_center=False)

    def set_trigger(self, *, side: str, value: float, duration_ms: int = 0, auto_reset: bool = False) -> None:
        raise NotImplementedError

    def reset(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        self.reset()


class VGamepadBackend(BaseWindowsGamepadBackend):
    backend_name = "vgamepad"

    def __init__(self, config: RuntimeGamepadConfig):
        super().__init__(config)
        try:
            self.vg = importlib.import_module("vgamepad")
        except Exception as exc:
            raise TargetRuntimeError(
                "gamepad_backend_unavailable",
                "vgamepad is not installed in the current Python environment.",
                {"backend": self.backend_name, "error": str(exc)},
            ) from exc

        device_type = str(config.device_type or "xbox360").lower()
        if device_type == "xbox360":
            self.device = self.vg.VX360Gamepad()
        elif device_type == "ds4":
            self.device = self.vg.VDS4Gamepad()
        else:
            raise TargetRuntimeError(
                "gamepad_device_type_invalid",
                "Unsupported gamepad device type.",
                {"device_type": config.device_type},
            )

        self.device_type = device_type

    def press_button(self, button: str) -> None:
        normalized = str(button or "").strip().lower()
        mapping = self._resolve_button(normalized)
        if mapping["kind"] == "special":
            self.device.press_special_button(special_button=mapping["value"])
        else:
            self.device.press_button(button=mapping["value"])
        self._update()

    def release_button(self, button: str) -> None:
        normalized = str(button or "").strip().lower()
        mapping = self._resolve_button(normalized)
        if mapping["kind"] == "special":
            self.device.release_special_button(special_button=mapping["value"])
        else:
            self.device.release_button(button=mapping["value"])
        self._update()

    def tilt_stick(self, *, stick: str, x: float, y: float, duration_ms: int = 0, auto_center: bool = False) -> None:
        normalized_stick = str(stick or "").strip().lower()
        clamped_x = _clamp_unit_float(x)
        clamped_y = _clamp_unit_float(y)
        if normalized_stick == "left":
            self.device.left_joystick_float(x_value_float=clamped_x, y_value_float=clamped_y)
        elif normalized_stick == "right":
            self.device.right_joystick_float(x_value_float=clamped_x, y_value_float=clamped_y)
        else:
            raise TargetRuntimeError(
                "gamepad_stick_invalid",
                "stick must be one of: left, right.",
                {"stick": stick},
            )
        self._update()
        if duration_ms > 0:
            time.sleep(float(duration_ms) / 1000.0)
        if auto_center:
            self.center_stick(normalized_stick)

    def set_trigger(self, *, side: str, value: float, duration_ms: int = 0, auto_reset: bool = False) -> None:
        normalized_side = str(side or "").strip().lower()
        clamped_value = _clamp_trigger_float(value)
        if normalized_side == "left":
            self.device.left_trigger_float(value_float=clamped_value)
        elif normalized_side == "right":
            self.device.right_trigger_float(value_float=clamped_value)
        else:
            raise TargetRuntimeError(
                "gamepad_trigger_invalid",
                "side must be one of: left, right.",
                {"side": side},
            )
        self._update()
        if duration_ms > 0:
            time.sleep(float(duration_ms) / 1000.0)
        if auto_reset:
            self.set_trigger(side=normalized_side, value=0.0, duration_ms=0, auto_reset=False)

    def reset(self) -> None:
        self.device.reset()
        self._update()

    def self_check(self) -> dict[str, Any]:
        payload = super().self_check()
        payload["module"] = getattr(self.vg, "__name__", "vgamepad")
        return payload

    def _update(self) -> None:
        self.device.update()
        if self.config.update_delay_ms > 0:
            time.sleep(float(self.config.update_delay_ms) / 1000.0)

    def _resolve_button(self, button: str) -> dict[str, Any]:
        if self.device_type == "xbox360":
            enum_cls = self.vg.XUSB_BUTTON
            mapping = {
                "a": enum_cls.XUSB_GAMEPAD_A,
                "b": enum_cls.XUSB_GAMEPAD_B,
                "x": enum_cls.XUSB_GAMEPAD_X,
                "y": enum_cls.XUSB_GAMEPAD_Y,
                "cross": enum_cls.XUSB_GAMEPAD_A,
                "circle": enum_cls.XUSB_GAMEPAD_B,
                "square": enum_cls.XUSB_GAMEPAD_X,
                "triangle": enum_cls.XUSB_GAMEPAD_Y,
                "back": enum_cls.XUSB_GAMEPAD_BACK,
                "share": enum_cls.XUSB_GAMEPAD_BACK,
                "start": enum_cls.XUSB_GAMEPAD_START,
                "options": enum_cls.XUSB_GAMEPAD_START,
                "guide": enum_cls.XUSB_GAMEPAD_GUIDE,
                "ps": enum_cls.XUSB_GAMEPAD_GUIDE,
                "lb": enum_cls.XUSB_GAMEPAD_LEFT_SHOULDER,
                "l1": enum_cls.XUSB_GAMEPAD_LEFT_SHOULDER,
                "rb": enum_cls.XUSB_GAMEPAD_RIGHT_SHOULDER,
                "r1": enum_cls.XUSB_GAMEPAD_RIGHT_SHOULDER,
                "ls": enum_cls.XUSB_GAMEPAD_LEFT_THUMB,
                "l3": enum_cls.XUSB_GAMEPAD_LEFT_THUMB,
                "rs": enum_cls.XUSB_GAMEPAD_RIGHT_THUMB,
                "r3": enum_cls.XUSB_GAMEPAD_RIGHT_THUMB,
                "dpad_up": enum_cls.XUSB_GAMEPAD_DPAD_UP,
                "dpad_down": enum_cls.XUSB_GAMEPAD_DPAD_DOWN,
                "dpad_left": enum_cls.XUSB_GAMEPAD_DPAD_LEFT,
                "dpad_right": enum_cls.XUSB_GAMEPAD_DPAD_RIGHT,
            }
            if button in mapping:
                return {"kind": "button", "value": mapping[button]}
        else:
            buttons = self.vg.DS4_BUTTONS
            mapping = {
                "cross": buttons.DS4_BUTTON_CROSS,
                "a": buttons.DS4_BUTTON_CROSS,
                "circle": buttons.DS4_BUTTON_CIRCLE,
                "b": buttons.DS4_BUTTON_CIRCLE,
                "square": buttons.DS4_BUTTON_SQUARE,
                "x": buttons.DS4_BUTTON_SQUARE,
                "triangle": buttons.DS4_BUTTON_TRIANGLE,
                "y": buttons.DS4_BUTTON_TRIANGLE,
                "share": buttons.DS4_BUTTON_SHARE,
                "back": buttons.DS4_BUTTON_SHARE,
                "options": buttons.DS4_BUTTON_OPTIONS,
                "start": buttons.DS4_BUTTON_OPTIONS,
                "l1": buttons.DS4_BUTTON_SHOULDER_LEFT,
                "lb": buttons.DS4_BUTTON_SHOULDER_LEFT,
                "r1": buttons.DS4_BUTTON_SHOULDER_RIGHT,
                "rb": buttons.DS4_BUTTON_SHOULDER_RIGHT,
                "l3": buttons.DS4_BUTTON_THUMB_LEFT,
                "ls": buttons.DS4_BUTTON_THUMB_LEFT,
                "r3": buttons.DS4_BUTTON_THUMB_RIGHT,
                "rs": buttons.DS4_BUTTON_THUMB_RIGHT,
                "dpad_up": buttons.DS4_BUTTON_DPAD_NORTH,
                "dpad_down": buttons.DS4_BUTTON_DPAD_SOUTH,
                "dpad_left": buttons.DS4_BUTTON_DPAD_WEST,
                "dpad_right": buttons.DS4_BUTTON_DPAD_EAST,
            }
            if button in mapping:
                return {"kind": "button", "value": mapping[button]}

            special_cls = getattr(self.vg, "DS4_SPECIAL_BUTTONS", None)
            if special_cls is not None:
                special_mapping = {
                    "ps": getattr(special_cls, "DS4_SPECIAL_BUTTON_PS", None),
                    "guide": getattr(special_cls, "DS4_SPECIAL_BUTTON_PS", None),
                    "touchpad": getattr(special_cls, "DS4_SPECIAL_BUTTON_TOUCHPAD", None),
                }
                if button in special_mapping and special_mapping[button] is not None:
                    return {"kind": "special", "value": special_mapping[button]}

        raise TargetRuntimeError(
            "gamepad_button_invalid",
            "Unsupported gamepad button for the configured device.",
            {"device_type": self.device_type, "button": button},
        )


def build_gamepad_backend(config: RuntimeGamepadConfig) -> BaseWindowsGamepadBackend:
    backend = str(config.backend or "vgamepad").strip().lower()
    if backend == "vgamepad":
        return VGamepadBackend(config)
    raise TargetRuntimeError(
        "gamepad_backend_invalid",
        "Unsupported gamepad backend.",
        {"backend": config.backend},
    )


def _clamp_unit_float(value: float) -> float:
    resolved = float(value)
    return max(min(resolved, 1.0), -1.0)


def _clamp_trigger_float(value: float) -> float:
    resolved = float(value)
    return max(min(resolved, 1.0), 0.0)
