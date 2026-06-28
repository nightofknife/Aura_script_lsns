# -*- coding: utf-8 -*-
from __future__ import annotations

import threading
from typing import Any

from packages.aura_core.api import service_info
from packages.aura_core.config.service import ConfigService

from ..platform.contracts import TargetRuntimeError
from ..platform.runtime_config import resolve_runtime_config
from ..platform.windows.debug_artifacts import DebugArtifactsManager
from ..platform.windows.gamepad_backends import build_gamepad_backend


@service_info(alias="gamepad", public=True, singleton=True, deps={"config": "core/config"})
class GamepadService:
    def __init__(self, config: ConfigService):
        self._config = config
        self._lock = threading.RLock()
        self._backend = None
        self._backend_key = None
        self._debug_artifacts = DebugArtifactsManager(config)

    def capabilities(self) -> dict[str, Any]:
        resolved = resolve_runtime_config(self._config)
        return {
            "enabled": bool(resolved.gamepad.enabled),
            "backend": resolved.gamepad.backend,
            "device_type": resolved.gamepad.device_type,
            "auto_connect": bool(resolved.gamepad.auto_connect),
        }

    def self_check(self) -> dict[str, Any]:
        resolved = resolve_runtime_config(self._config)
        payload = self.capabilities()
        if not resolved.gamepad.enabled:
            payload["ok"] = False
            payload["reason"] = "disabled"
            return payload
        try:
            backend = self._get_backend()
        except TargetRuntimeError as exc:
            payload["ok"] = False
            payload["error"] = exc.to_dict()
            return payload
        payload["ok"] = True
        payload["backend_state"] = backend.self_check()
        return payload

    def press_button(self, button: str) -> None:
        self._debug_artifacts.record_input_event("gamepad.press_button", {"button": button})
        self._get_backend().press_button(button)

    def release_button(self, button: str) -> None:
        self._debug_artifacts.record_input_event("gamepad.release_button", {"button": button})
        self._get_backend().release_button(button)

    def tap_button(self, button: str, *, duration_ms: int = 0) -> None:
        self._debug_artifacts.record_input_event("gamepad.tap_button", {"button": button, "duration_ms": duration_ms})
        self._get_backend().tap_button(button, duration_ms=duration_ms)

    def tilt_stick(
        self,
        *,
        stick: str,
        x: float,
        y: float,
        duration_ms: int = 0,
        auto_center: bool = False,
    ) -> None:
        self._debug_artifacts.record_input_event(
            "gamepad.tilt_stick",
            {"stick": stick, "x": x, "y": y, "duration_ms": duration_ms, "auto_center": auto_center},
        )
        self._get_backend().tilt_stick(
            stick=stick,
            x=x,
            y=y,
            duration_ms=duration_ms,
            auto_center=auto_center,
        )

    def center_stick(self, stick: str) -> None:
        self._debug_artifacts.record_input_event("gamepad.center_stick", {"stick": stick})
        self._get_backend().center_stick(stick)

    def set_trigger(
        self,
        *,
        side: str,
        value: float,
        duration_ms: int = 0,
        auto_reset: bool = False,
    ) -> None:
        self._debug_artifacts.record_input_event(
            "gamepad.set_trigger",
            {"side": side, "value": value, "duration_ms": duration_ms, "auto_reset": auto_reset},
        )
        self._get_backend().set_trigger(
            side=side,
            value=value,
            duration_ms=duration_ms,
            auto_reset=auto_reset,
        )

    def reset(self) -> None:
        self._debug_artifacts.record_input_event("gamepad.reset", {})
        backend = self._get_backend()
        backend.reset()

    def close(self) -> None:
        with self._lock:
            if self._backend is not None:
                try:
                    self._backend.close()
                except Exception:
                    pass
            self._backend = None
            self._backend_key = None

    def _get_backend(self):
        resolved = resolve_runtime_config(self._config)
        if not resolved.gamepad.enabled:
            raise TargetRuntimeError(
                "gamepad_disabled",
                "runtime.gamepad.enabled is false.",
                resolved.gamepad.to_dict(),
            )
        backend_key = (resolved.gamepad.backend, resolved.gamepad.device_type)
        with self._lock:
            if self._backend is None or self._backend_key != backend_key:
                self.close()
                self._backend = build_gamepad_backend(resolved.gamepad)
                self._backend_key = backend_key
            return self._backend
