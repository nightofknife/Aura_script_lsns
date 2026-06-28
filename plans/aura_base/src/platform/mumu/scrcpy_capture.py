# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib
import threading
import time
from typing import Any, Dict, Optional, Tuple

import cv2
import numpy as np

from ..contracts import CaptureResult, TargetRuntimeError


class MuMuScrcpyCaptureBackend:
    BACKEND_NAME = "scrcpy_stream"
    DEFAULT_MODULE_NAME = "plans.aura_base.src.platform.mumu.scrcpy_compat"

    def __init__(self, serial: str, config: Dict[str, Any] | None = None):
        self.serial = serial
        self.config = dict(config or {})
        self.module_name = str(self.config.get("module_name") or self.DEFAULT_MODULE_NAME)
        self.max_stale_ms = int(self.config.get("max_stale_ms") or 250)
        self.max_fps = int(self.config.get("max_fps") or 30)
        self.max_width = int(self.config.get("max_width") or 0)
        self.bitrate = int(self.config.get("bitrate") or 8000000)
        self.connection_timeout_ms = int(self.config.get("connection_timeout_ms") or 5000)
        self.adb_executable = str(self.config.get("adb_executable") or "adb")
        self.server_version = str(self.config.get("server_version") or "1.24")
        self.server_jar_path = str(self.config.get("server_jar_path") or "")
        self.reconnect_backoff_ms = [int(item) for item in (self.config.get("reconnect_backoff_ms") or [500, 1000, 2000])]

        self._client = None
        self._frame: Optional[np.ndarray] = None
        self._frame_ts = 0.0
        self._lock = threading.RLock()

    def ensure_ready(self):
        should_start = False
        should_restart = False
        with self._lock:
            if self._client is None:
                should_start = True
            elif self.frame_age_ms() > self.max_stale_ms:
                should_restart = True
        if should_start:
            self._start()
        elif should_restart:
            self._restart()
        with self._lock:
            if self._frame is None:
                raise TargetRuntimeError(
                    "capture_stream_unavailable",
                    "scrcpy stream has not produced a frame yet.",
                    {"serial": self.serial},
                )

    def frame_age_ms(self) -> float:
        if self._frame_ts <= 0:
            return float("inf")
        return max((time.monotonic() - self._frame_ts) * 1000.0, 0.0)

    def is_healthy(self) -> bool:
        return self._client is not None and self._frame is not None and self.frame_age_ms() <= self.max_stale_ms

    def focus(self) -> bool:
        self.ensure_ready()
        return True

    def capture(self, rect: Optional[Tuple[int, int, int, int]] = None) -> CaptureResult:
        self.ensure_ready()
        with self._lock:
            frame = None if self._frame is None else self._frame.copy()
        if frame is None:
            raise TargetRuntimeError(
                "capture_stream_unavailable",
                "scrcpy frame buffer is empty.",
                {"serial": self.serial},
            )
        full_h, full_w = frame.shape[:2]
        relative_rect = (0, 0, full_w, full_h)
        if rect is not None:
            x, y, w, h = [int(item) for item in rect]
            if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > full_w or y + h > full_h:
                raise TargetRuntimeError(
                    "capture_rect_invalid",
                    "Capture rect is outside the current MuMu viewport.",
                    {"rect": [x, y, w, h], "viewport": [0, 0, full_w, full_h]},
                )
            frame = frame[y : y + h, x : x + w].copy()
            relative_rect = (x, y, w, h)
        return CaptureResult(
            success=True,
            image=frame,
            window_rect=(0, 0, full_w, full_h),
            relative_rect=relative_rect,
            backend=self.BACKEND_NAME,
        )

    def get_client_rect(self) -> Tuple[int, int, int, int] | None:
        with self._lock:
            frame = self._frame
            if frame is not None:
                h, w = frame.shape[:2]
                return 0, 0, int(w), int(h)
        return None

    def get_pixel_color_at(self, x: int, y: int) -> Tuple[int, int, int]:
        self.ensure_ready()
        with self._lock:
            frame = None if self._frame is None else self._frame.copy()
        if frame is None:
            raise TargetRuntimeError("capture_stream_unavailable", "No scrcpy frame available.")
        px = int(x)
        py = int(y)
        h, w = frame.shape[:2]
        if px < 0 or py < 0 or px >= w or py >= h:
            raise TargetRuntimeError(
                "capture_point_out_of_bounds",
                "Pixel coordinate is outside the current MuMu viewport.",
                {"point": [px, py], "viewport": [0, 0, w, h]},
            )
        pixel = frame[py, px]
        return int(pixel[0]), int(pixel[1]), int(pixel[2])

    def send_keycode(self, keycode: int, action: str) -> bool:
        self.ensure_ready()
        control = getattr(self._client, "control", None)
        keycode_fn = getattr(control, "keycode", None)
        if not callable(keycode_fn):
            raise TargetRuntimeError(
                "scrcpy_keycode_unsupported",
                "scrcpy client does not expose keycode control.",
                {"serial": self.serial},
            )
        action_value = 0 if str(action).lower() == "down" else 1
        keycode_fn(int(keycode), action=action_value)
        return True

    def close(self):
        with self._lock:
            client = self._client
            self._client = None
            self._frame = None
            self._frame_ts = 0.0
        if client is not None:
            stop_fn = getattr(client, "stop", None)
            if callable(stop_fn):
                try:
                    stop_fn()
                except Exception:
                    pass

    def self_check(self) -> Dict[str, Any]:
        return {
            "ok": self.is_healthy(),
            "provider": self.BACKEND_NAME,
            "serial": self.serial,
            "frame_age_ms": round(self.frame_age_ms(), 3),
            "viewport": list(self.get_client_rect()) if self.get_client_rect() else None,
            "module_name": self.module_name,
        }

    def _start(self):
        try:
            scrcpy_mod = importlib.import_module(self.module_name)
        except Exception as exc:
            raise TargetRuntimeError(
                "scrcpy_module_missing",
                f"Unable to import scrcpy module '{self.module_name}'.",
                {"serial": self.serial, "error": str(exc)},
            ) from exc

        client_kwargs: Dict[str, Any] = {
            "device": self.serial,
            "max_fps": self.max_fps,
            "bitrate": self.bitrate,
            "connection_timeout": self.connection_timeout_ms,
            "block_frame": True,
            "adb_executable": self.adb_executable,
            "server_version": self.server_version,
        }
        if self.max_width > 0:
            client_kwargs["max_width"] = self.max_width
        if self.server_jar_path:
            client_kwargs["server_jar_path"] = self.server_jar_path
        try:
            client = scrcpy_mod.Client(**client_kwargs)
        except Exception as exc:
            raise TargetRuntimeError(
                "scrcpy_client_init_failed",
                "Failed to initialize scrcpy capture client.",
                {"serial": self.serial, "error": str(exc)},
            ) from exc

        try:
            client.add_listener("frame", self._on_frame)
        except Exception as exc:
            raise TargetRuntimeError(
                "scrcpy_listener_failed",
                "Failed to subscribe to scrcpy frame events.",
                {"serial": self.serial, "error": str(exc)},
            ) from exc

        try:
            client.start(threaded=True, daemon_threaded=True)
        except TypeError:
            client.start(threaded=True)
        except Exception as exc:
            raise TargetRuntimeError(
                "scrcpy_stream_unavailable",
                "Failed to start scrcpy streaming.",
                {"serial": self.serial, "error": str(exc)},
            ) from exc

        with self._lock:
            self._client = client
        if not self._wait_for_first_frame():
            self.close()
            raise TargetRuntimeError(
                "capture_stream_unavailable",
                "scrcpy started but did not deliver a frame in time.",
                {"serial": self.serial, "timeout_ms": self.connection_timeout_ms},
            )

    def _restart(self):
        self.close()
        last_error: Optional[TargetRuntimeError] = None
        for delay_ms in self.reconnect_backoff_ms:
            try:
                self._start()
                return
            except TargetRuntimeError as exc:
                last_error = exc
                time.sleep(max(delay_ms, 0) / 1000.0)
        if last_error is not None:
            raise last_error

    def _wait_for_first_frame(self) -> bool:
        deadline = time.monotonic() + max(self.connection_timeout_ms, 1000) / 1000.0
        while time.monotonic() < deadline:
            with self._lock:
                if self._frame is not None:
                    return True
            time.sleep(0.05)
        return False

    def _on_frame(self, frame: Any):
        if frame is None:
            return
        array = np.asarray(frame)
        if array.size == 0 or array.dtype == object or array.ndim < 2:
            return
        if array.ndim == 2:
            rgb = cv2.cvtColor(array, cv2.COLOR_GRAY2RGB)
        elif array.ndim == 3 and array.shape[2] == 4:
            rgb = cv2.cvtColor(array, cv2.COLOR_BGRA2RGB)
        else:
            rgb = cv2.cvtColor(array, cv2.COLOR_BGR2RGB)
        with self._lock:
            self._frame = rgb
            self._frame_ts = time.monotonic()
