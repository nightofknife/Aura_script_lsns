# -*- coding: utf-8 -*-
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from packages.aura_core.observability.logging.core_logger import logger

from ..contracts import CaptureResult, RuntimeAdapter, TargetRuntimeError
from ..runtime_config import (
    RuntimeCaptureConfig,
    RuntimeInputConfig,
    RuntimeTargetConfig,
    RuntimeWindowSpecConfig,
    supported_capture_backends,
)
from .activation import execute_activation
from .capture_backends import BaseWindowsCaptureBackend, build_capture_backend
from .dpi import ensure_process_dpi_awareness, get_window_dpi, get_window_scale_factor
from .input_backends import BaseWindowsInputBackend, build_input_backend
from .window_target import WindowTarget
from .window_spec import ensure_window_spec


class WindowsDesktopAdapter(RuntimeAdapter):
    """Single-window Windows runtime adapter with explicit capture/input backends."""

    def __init__(
        self,
        *,
        target_config: RuntimeTargetConfig,
        capture_config: RuntimeCaptureConfig,
        input_config: RuntimeInputConfig,
        window_spec_config: RuntimeWindowSpecConfig | None = None,
    ):
        self.target_config = target_config
        self.capture_config = capture_config
        self.input_config = input_config
        self.window_spec_config = window_spec_config or RuntimeWindowSpecConfig()

        self.target = WindowTarget.create(target_config)
        self.capture_backend: BaseWindowsCaptureBackend = build_capture_backend(
            capture_config.backend,
            self.target,
            capture_config.provider_options("windows"),
        )
        self.input_backend: BaseWindowsInputBackend = build_input_backend(
            input_config.backend,
            self.target,
            input_config.provider_options("windows"),
        )
        self._operation_lock = threading.RLock()
        self._visibility_recovery_stats: dict[str, Any] = {
            "enabled": bool(target_config.visibility_recovery.enabled and target_config.require_visible),
            "attempts": 0,
            "successes": 0,
            "failures": 0,
            "last_operation": None,
            "last_method": None,
            "last_elapsed_ms": None,
            "last_timestamp": None,
        }
        initial_recovery = getattr(self.target, "last_recovery_result", None)
        if initial_recovery is not None and bool(getattr(initial_recovery, "attempted", False)):
            self._record_recovery_success("runtime_start", initial_recovery, capture_rebuilt=False)

    def ensure_ready(self, operation: str = "ensure_ready") -> None:
        with self._operation_lock:
            self._ensure_ready_locked(operation)

    def _ensure_ready_locked(self, operation: str) -> None:
        ensure_process_dpi_awareness()
        try:
            ensure_available = getattr(self.target, "ensure_available", None)
            if callable(ensure_available):
                recovery = ensure_available(operation)
            else:
                self.target.ensure_valid()
                recovery = None
        except TargetRuntimeError as exc:
            self._record_recovery_failure(operation, exc)
            raise
        if recovery is not None and bool(getattr(recovery, "recovered", False)):
            reset_capture = getattr(self.capture_backend, "reset_after_target_recovery", None)
            if callable(reset_capture):
                reset_capture()
            self._record_recovery_success(operation, recovery, capture_rebuilt=True)
        self._window_spec_status = ensure_window_spec(self.target, self.window_spec_config)

    def _run_read_operation(self, operation: str, callback):
        with self._operation_lock:
            self._ensure_ready_locked(operation)
            try:
                return callback()
            except TargetRuntimeError as exc:
                if exc.code != "window_not_visible" or not self._visibility_recovery_stats["enabled"]:
                    raise
                self._ensure_ready_locked(f"{operation}:retry")
                return callback()

    def _run_input_operation(self, operation: str, callback):
        with self._operation_lock:
            self._ensure_ready_locked(operation)
            return callback()

    def _record_recovery_success(self, operation: str, recovery, *, capture_rebuilt: bool) -> None:
        stats = self._visibility_recovery_stats
        stats["attempts"] += 1
        stats["successes"] += 1
        stats["last_operation"] = operation
        stats["last_method"] = recovery.method
        stats["last_elapsed_ms"] = int(recovery.elapsed_ms)
        stats["last_timestamp"] = datetime.now(timezone.utc).isoformat()
        logger.info(
            "Window recovery applied to runtime operation: operation=%s method=%s elapsed_ms=%s wgc_rebuilt=%s",
            operation,
            recovery.method,
            recovery.elapsed_ms,
            bool(capture_rebuilt and self.capture_config.backend == "wgc"),
        )

    def _record_recovery_failure(self, operation: str, exc: TargetRuntimeError) -> None:
        if exc.code != "window_not_visible" or not self._visibility_recovery_stats["enabled"]:
            return
        stats = self._visibility_recovery_stats
        recovery = dict(exc.detail.get("visibility_recovery") or {})
        stats["attempts"] += 1
        stats["failures"] += 1
        stats["last_operation"] = operation
        stats["last_method"] = recovery.get("method")
        stats["last_elapsed_ms"] = recovery.get("elapsed_ms")
        stats["last_timestamp"] = datetime.now(timezone.utc).isoformat()

    def close(self) -> None:
        with self._operation_lock:
            try:
                self.input_backend.close()
            finally:
                self.capture_backend.close()

    def list_capture_backends(self) -> dict[str, Any]:
        supported = supported_capture_backends("windows")
        return {
            "available": supported,
            "enabled": [self.capture_config.backend],
            "default": self.capture_config.backend,
            "configured": True,
        }

    def set_capture_backend(self, backend: str) -> None:
        normalized = str(backend or "").strip().lower()
        if normalized != self.capture_config.backend:
            raise TargetRuntimeError(
                "backend_runtime_switch_unsupported",
                "Runtime capture backend switching is unsupported; update config.yaml and rebuild the runtime.",
                {"requested": normalized or None, "configured": self.capture_config.backend},
            )

    def capture(self, rect: tuple[int, int, int, int] | None = None) -> CaptureResult:
        return self._run_read_operation("capture", lambda: self.capture_backend.capture(rect=rect))

    def get_client_rect(self) -> tuple[int, int, int, int]:
        return self._run_read_operation("get_client_rect", self.target.get_client_rect)

    def get_pixel_color_at(self, x: int, y: int) -> tuple[int, int, int]:
        return self._run_read_operation(
            "get_pixel_color_at",
            lambda: self.capture_backend.get_pixel_color_at(x, y),
        )

    def focus(self) -> bool:
        return bool(self._run_input_operation("focus", self.target.focus))

    def focus_with_input(self, click_delay: float = 0.3) -> bool:
        result = self._run_input_operation(
            "focus_with_input",
            lambda: execute_activation(
                target=self.target,
                input_backend=self.input_backend,
                activation=self.input_config.activation,
                sleep_ms_override=max(int(float(click_delay) * 1000.0), 0),
            ),
        )
        return bool(result.get("ok"))

    def test_focus_activation(
        self,
        *,
        mode: str | None = None,
        sleep_ms: int | None = None,
        click_point: tuple[int, int] | None = None,
        click_button: str | None = None,
    ) -> dict[str, Any]:
        result = self._run_input_operation(
            "test_focus_activation",
            lambda: execute_activation(
                target=self.target,
                input_backend=self.input_backend,
                activation=self.input_config.activation,
                sleep_ms_override=sleep_ms,
                mode_override=mode,
                click_point_override=click_point,
                click_button_override=click_button,
            ),
        )
        result["target"] = self.target.to_summary()
        return result

    def click(
        self,
        x: int | None = None,
        y: int | None = None,
        *,
        button: str = "left",
        clicks: int = 1,
        interval: float | None = None,
    ) -> None:
        self._run_input_operation(
            "click",
            lambda: self.input_backend.click(x=x, y=y, button=button, clicks=clicks, interval=interval),
        )

    def move_to(self, x: int, y: int, *, duration: float | None = None) -> None:
        self._run_input_operation("move_to", lambda: self.input_backend.move_to(x, y, duration=duration))

    def move_relative(self, dx: int, dy: int, *, duration: float | None = None) -> None:
        self._run_input_operation(
            "move_relative",
            lambda: self.input_backend.move_relative(dx, dy, duration=duration),
        )

    def mouse_down(self, *, button: str = "left") -> None:
        self._run_input_operation("mouse_down", lambda: self.input_backend.mouse_down(button=button))

    def mouse_up(self, *, button: str = "left") -> None:
        self._run_input_operation("mouse_up", lambda: self.input_backend.mouse_up(button=button))

    def drag_to(self, x: int, y: int, *, button: str = "left", duration: float | None = None) -> None:
        self._run_input_operation(
            "drag_to",
            lambda: self.input_backend.drag_to(x, y, button=button, duration=duration),
        )

    def look_delta(self, dx: int, dy: int) -> None:
        self._run_input_operation("look_delta", lambda: self.input_backend.look_delta(dx, dy))

    def look_hold(
        self,
        vx: float,
        vy: float,
        *,
        duration_ms: int,
        tick_ms: int | None = None,
    ) -> None:
        self._run_input_operation(
            "look_hold",
            lambda: self.input_backend.look_hold(vx, vy, duration_ms=duration_ms, tick_ms=tick_ms),
        )

    def scroll(self, amount: int, direction: str = "down") -> None:
        self._run_input_operation("scroll", lambda: self.input_backend.scroll(amount, direction))

    def press_key(self, key: str, presses: int = 1, interval: float | None = None) -> None:
        self._run_input_operation(
            "press_key",
            lambda: self.input_backend.press_key(key, presses, interval),
        )

    def key_down(self, key: str) -> None:
        self._run_input_operation("key_down", lambda: self.input_backend.key_down(key))

    def key_up(self, key: str) -> None:
        self._run_input_operation("key_up", lambda: self.input_backend.key_up(key))

    def type_text(self, text: str, interval: float | None = None) -> None:
        self._run_input_operation("type_text", lambda: self.input_backend.type_text(text, interval))

    def release_all(self) -> None:
        self.input_backend.release_all()

    def capabilities(self) -> dict[str, Any]:
        return self.input_backend.capabilities()

    def self_check(self) -> dict[str, Any]:
        with self._operation_lock:
            self._ensure_ready_locked("self_check")
            return {
                "ok": True,
                "provider": "windows",
                "family": "windows_desktop",
                "target": self.target.to_summary(),
                "window_spec": getattr(
                    self,
                    "_window_spec_status",
                    ensure_window_spec(self.target, self.window_spec_config),
                ).to_dict(),
                "dpi": {
                    "process": ensure_process_dpi_awareness(),
                    "window_dpi": get_window_dpi(self.target.hwnd),
                    "scale_factor": get_window_scale_factor(self.target.hwnd),
                },
                "capture": {
                    "backend": self.capture_config.backend,
                    "options": self.capture_config.provider_options("windows"),
                    "health": self.capture_backend.self_check(),
                },
                "input": {
                    "backend": self.input_config.backend,
                    "options": self.input_config.provider_options("windows"),
                    "health": self.input_backend.self_check(),
                    "capabilities": self.capabilities(),
                },
                "visibility_recovery": dict(self._visibility_recovery_stats),
            }
