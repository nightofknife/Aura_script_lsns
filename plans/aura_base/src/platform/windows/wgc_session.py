# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib
import threading
import time
from typing import Any

import numpy as np

from ..contracts import TargetRuntimeError


class PersistentWgcSession:
    """Hold a single long-lived Windows Graphics Capture session."""

    def __init__(
        self,
        *,
        hwnd: int,
        module_name: str = "windows_capture",
        capture_cursor: bool = False,
        draw_border: bool = False,
        secondary_window: bool = False,
        minimum_update_interval_ms: int = 16,
        dirty_region: bool = True,
    ) -> None:
        self.hwnd = int(hwnd)
        self.module_name = str(module_name or "windows_capture").strip() or "windows_capture"
        self.capture_cursor = bool(capture_cursor)
        self.draw_border = bool(draw_border)
        self.secondary_window = bool(secondary_window)
        self.minimum_update_interval_ms = max(int(minimum_update_interval_ms), 0)
        self.dirty_region = bool(dirty_region)

        self._lock = threading.RLock()
        self._frame_event = threading.Event()
        self._closed = False
        self._generation = 0
        self._arrived_at_monotonic: float | None = None
        self._frame_shape: tuple[int, ...] | None = None
        self._latest_frame: np.ndarray | None = None
        self._latest_frame_dtype: np.dtype[Any] | None = None
        self._last_error: dict[str, Any] | None = None

        self._module = self._import_module()
        self._capture_cls = getattr(self._module, "WindowsCapture", None)
        if not callable(self._capture_cls):
            raise TargetRuntimeError(
                "windows_capture_api_missing",
                "The configured WGC module does not expose windows_capture.WindowsCapture.",
                {
                    "module_name": self.module_name,
                    "required_api": "WindowsCapture",
                },
            )

        self._capturer: Any | None = None
        self._control: Any | None = None

    def _import_module(self) -> Any:
        try:
            return importlib.import_module(self.module_name)
        except Exception as exc:
            raise TargetRuntimeError(
                "windows_capture_init_failed",
                f"Unable to import WGC capture module '{self.module_name}'.",
                {"module_name": self.module_name, "error": str(exc)},
            ) from exc

    @property
    def generation(self) -> int:
        with self._lock:
            return int(self._generation)

    def start_if_needed(self) -> None:
        with self._lock:
            if self._closed:
                raise TargetRuntimeError(
                    "windows_capture_session_closed",
                    "The persistent WGC session has already been closed.",
                    {"module_name": self.module_name, "hwnd": self.hwnd},
                )

            if self._control is not None:
                try:
                    if not bool(self._control.is_finished()):
                        return
                except Exception as exc:
                    self._set_last_error("control_status_failed", str(exc))
                    raise TargetRuntimeError(
                        "windows_capture_session_error",
                        "The persistent WGC capture control is in an invalid state.",
                        self.health(),
                    ) from exc

                raise TargetRuntimeError(
                    "windows_capture_session_stopped",
                    "The persistent WGC session has stopped and must be rebuilt.",
                    self.health(),
                )

            self._capturer = self._create_capturer()
            self._register_callbacks(self._capturer)
            try:
                self._control = self._capturer.start_free_threaded()
            except Exception as exc:
                self._set_last_error("start_failed", str(exc))
                self._capturer = None
                self._control = None
                raise TargetRuntimeError(
                    "windows_capture_init_failed",
                    f"Failed to start the persistent WGC session: {exc}",
                    {"module_name": self.module_name, "hwnd": self.hwnd},
                ) from exc

    def wait_for_fresh_frame(self, max_stale_ms: int, timeout_ms: int) -> None:
        stale_limit = max(int(max_stale_ms), 0)
        wait_timeout_ms = max(int(timeout_ms), 1)
        deadline = time.monotonic() + (wait_timeout_ms / 1000.0)

        with self._lock:
            baseline_generation = int(self._generation)
            if self._frame_is_fresh_locked(stale_limit):
                return
            self._raise_if_session_unhealthy_locked()
            self._frame_event.clear()

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TargetRuntimeError(
                    "windows_capture_frame_timeout",
                    "The persistent WGC session did not produce a fresh frame before timeout.",
                    {
                        **self.health(),
                        "max_stale_ms": stale_limit,
                        "timeout_ms": wait_timeout_ms,
                        "baseline_generation": baseline_generation,
                    },
                )

            self._frame_event.wait(remaining)
            with self._lock:
                self._raise_if_session_unhealthy_locked()
                if self._generation > baseline_generation or self._frame_is_fresh_locked(stale_limit):
                    return
                self._frame_event.clear()

    def snapshot_full_frame(self) -> np.ndarray:
        with self._lock:
            self._raise_if_session_unhealthy_locked()
            if self._latest_frame is None:
                raise TargetRuntimeError(
                    "windows_capture_frame_unavailable",
                    "The persistent WGC session has not produced a frame yet.",
                    self.health(),
                )
            return self._latest_frame.copy()

    def close(self) -> None:
        control = None
        with self._lock:
            if self._closed:
                return
            self._closed = True
            control = self._control
            self._control = None
            self._capturer = None
            self._frame_event.set()

        if control is not None:
            try:
                control.stop()
            except Exception:
                pass
            try:
                control.wait()
            except Exception:
                pass

        with self._lock:
            self._latest_frame = None
            self._latest_frame_dtype = None
            self._frame_shape = None
            self._arrived_at_monotonic = None

    def health(self) -> dict[str, Any]:
        with self._lock:
            latest_age_ms: float | None = None
            if self._arrived_at_monotonic is not None:
                latest_age_ms = max((time.monotonic() - self._arrived_at_monotonic) * 1000.0, 0.0)
            control_finished = False
            if self._control is not None:
                try:
                    control_finished = bool(self._control.is_finished())
                except Exception:
                    control_finished = True
            return {
                "session_mode": "persistent",
                "module_name": self.module_name,
                "hwnd": int(self.hwnd),
                "session_started": self._control is not None,
                "control_finished": control_finished,
                "latest_frame_age_ms": None if latest_age_ms is None else round(latest_age_ms, 3),
                "latest_frame_shape": list(self._frame_shape) if self._frame_shape is not None else None,
                "generation": int(self._generation),
                "closed": bool(self._closed),
                "last_error": dict(self._last_error or {}),
            }

    def _create_capturer(self) -> Any:
        try:
            return self._capture_cls(
                cursor_capture=self.capture_cursor,
                draw_border=self.draw_border,
                secondary_window=self.secondary_window,
                minimum_update_interval=self.minimum_update_interval_ms,
                dirty_region=self.dirty_region,
                window_hwnd=int(self.hwnd),
            )
        except TypeError:
            try:
                return self._capture_cls(
                    cursor_capture=self.capture_cursor,
                    draw_border=self.draw_border,
                    window_hwnd=int(self.hwnd),
                )
            except Exception as exc:
                self._set_last_error("create_failed", str(exc))
                raise TargetRuntimeError(
                    "windows_capture_init_failed",
                    f"Failed to initialize the persistent WGC session: {exc}",
                    {"module_name": self.module_name, "hwnd": self.hwnd},
                ) from exc
        except Exception as exc:
            self._set_last_error("create_failed", str(exc))
            raise TargetRuntimeError(
                "windows_capture_init_failed",
                f"Failed to initialize the persistent WGC session: {exc}",
                {"module_name": self.module_name, "hwnd": self.hwnd},
            ) from exc

    def _register_callbacks(self, capturer: Any) -> None:
        @capturer.event
        def on_frame_arrived(frame, control):  # noqa: ANN001
            try:
                frame_buffer = getattr(frame, "frame_buffer", frame)
                array = np.asarray(frame_buffer)
                if array.ndim != 3 or array.shape[2] not in {3, 4}:
                    raise ValueError(f"Unsupported frame shape: {tuple(array.shape)}")

                with self._lock:
                    if self._latest_frame is None or self._frame_shape != tuple(array.shape) or self._latest_frame_dtype != array.dtype:
                        self._latest_frame = np.empty_like(array)
                        self._latest_frame_dtype = array.dtype
                        self._frame_shape = tuple(int(value) for value in array.shape)
                    np.copyto(self._latest_frame, array, casting="no")
                    self._generation += 1
                    self._arrived_at_monotonic = time.monotonic()
                    self._last_error = None
                    self._frame_event.set()
            except Exception as exc:  # noqa: BLE001
                self._set_last_error("frame_callback_failed", str(exc))
                try:
                    control.stop()
                except Exception:
                    pass
                self._frame_event.set()

        @capturer.event
        def on_closed():
            with self._lock:
                self._closed = True
                self._frame_event.set()

    def _frame_is_fresh_locked(self, stale_limit_ms: int) -> bool:
        if self._latest_frame is None or self._arrived_at_monotonic is None:
            return False
        if stale_limit_ms <= 0:
            return True
        age_ms = max((time.monotonic() - self._arrived_at_monotonic) * 1000.0, 0.0)
        return age_ms <= stale_limit_ms

    def _raise_if_session_unhealthy_locked(self) -> None:
        if self._last_error:
            raise TargetRuntimeError(
                "windows_capture_session_error",
                "The persistent WGC session entered an unhealthy state.",
                self.health(),
            )
        if self._closed:
            raise TargetRuntimeError(
                "windows_capture_session_closed",
                "The persistent WGC session is closed.",
                self.health(),
            )
        if self._control is not None:
            try:
                if bool(self._control.is_finished()):
                    raise TargetRuntimeError(
                        "windows_capture_session_stopped",
                        "The persistent WGC session stopped and must be rebuilt.",
                        self.health(),
                    )
            except TargetRuntimeError:
                raise
            except Exception as exc:
                self._set_last_error("control_status_failed", str(exc))
                raise TargetRuntimeError(
                    "windows_capture_session_error",
                    "The persistent WGC capture control is in an invalid state.",
                    self.health(),
                ) from exc

    def _set_last_error(self, code: str, message: str) -> None:
        with self._lock:
            self._last_error = {"code": str(code), "message": str(message)}

