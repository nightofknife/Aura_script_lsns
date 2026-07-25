# -*- coding: utf-8 -*-
from __future__ import annotations

import ctypes
import importlib
import sys
import threading
from typing import Any

import cv2
import numpy as np
from PIL import ImageGrab
import win32con
import win32gui
import win32ui

from packages.aura_core.observability.logging.core_logger import logger

from ..contracts import CaptureResult, TargetRuntimeError
from .window_target import WindowTarget
from .wgc_session import PersistentWgcSession


WGC_CAPTURE_PROFILES = ("performance", "compatible")
WGC_PERFORMANCE_MIN_WINDOWS_BUILD = 26100


def _windows_build_number() -> int | None:
    try:
        return int(sys.getwindowsversion().build)
    except (AttributeError, TypeError, ValueError):
        return None


def _normalize_wgc_profile(value: Any, *, field: str, default: str) -> str:
    profile = str(value or default).strip().lower()
    if profile not in WGC_CAPTURE_PROFILES:
        raise TargetRuntimeError(
            "windows_capture_profile_invalid",
            f"Unsupported WGC capture profile '{profile}'.",
            {
                "field": field,
                "profile": profile,
                "supported": list(WGC_CAPTURE_PROFILES),
            },
        )
    return profile


def _is_optional_wgc_feature_unsupported(exc: BaseException) -> bool:
    messages: list[str] = []
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        messages.append(str(current).lower())
        if isinstance(current, TargetRuntimeError):
            messages.append(str(current.detail.get("error") or "").lower())
        current = current.__cause__ or current.__context__
    combined = " ".join(messages)
    return (
        "not supported by the graphics capture api on this platform" in combined
        or (
            "graphics capture" in combined
            and "not supported" in combined
            and any(
                feature in combined
                for feature in (
                    "capture border",
                    "secondary window",
                    "dirty region",
                    "minimum update interval",
                )
            )
        )
    )


class BaseWindowsCaptureBackend:
    backend_name = ""

    def __init__(self, target: WindowTarget, config: dict[str, Any] | None = None):
        self.target = target
        self.config = dict(config or {})

    def close(self) -> None:
        return None

    def reset_after_target_recovery(self) -> None:
        return None

    def focus(self) -> bool:
        return self.target.focus()

    def get_client_rect(self) -> tuple[int, int, int, int]:
        return self.target.get_client_rect()

    def get_pixel_color_at(self, x: int, y: int) -> tuple[int, int, int]:
        capture = self.capture((int(x), int(y), 1, 1))
        if not capture.success or capture.image is None:
            raise TargetRuntimeError(
                "windows_pixel_failed",
                f"Failed to read a pixel through backend '{self.backend_name}'.",
                {"backend": self.backend_name},
            )
        red, green, blue = capture.image[0, 0].tolist()
        return int(red), int(green), int(blue)

    def capture(self, rect: tuple[int, int, int, int] | None = None) -> CaptureResult:
        self.target.ensure_valid()
        roi = _normalize_client_roi(self.target, rect)
        client_rect = self.target.get_client_rect()
        image = self._capture_roi(roi)
        return CaptureResult(
            success=True,
            image=image,
            window_rect=client_rect,
            relative_rect=roi,
            backend=self.backend_name,
        )

    def self_check(self) -> dict[str, Any]:
        return {
            "ok": True,
            "backend": self.backend_name,
            "client_rect": list(self.target.get_client_rect()),
        }

    def _capture_roi(self, roi: tuple[int, int, int, int]) -> np.ndarray:
        raise NotImplementedError


class WindowsGdiCaptureBackend(BaseWindowsCaptureBackend):
    backend_name = "gdi"

    def _capture_roi(self, roi: tuple[int, int, int, int]) -> np.ndarray:
        left, top, _, _ = self.target.get_client_rect_screen()
        x, y, width, height = roi
        bbox = (left + x, top + y, left + x + width, top + y + height)
        try:
            image = ImageGrab.grab(bbox=bbox, all_screens=True).convert("RGB")
        except Exception as exc:
            raise TargetRuntimeError(
                "windows_capture_init_failed",
                f"GDI capture failed for the configured window: {exc}",
                {"backend": self.backend_name, "bbox": list(bbox)},
            ) from exc
        return np.asarray(image)


class WindowsWgcCaptureBackend(BaseWindowsCaptureBackend):
    backend_name = "wgc"

    def __init__(self, target: WindowTarget, config: dict[str, Any] | None = None):
        super().__init__(target, config)
        self.module_name = str(self.config.get("module_name") or "windows_capture").strip() or "windows_capture"
        self.capture_cursor = bool(self.config.get("capture_cursor", False))
        self.frame_timeout_ms = max(int(self.config.get("frame_timeout_ms") or 1000), 100)
        self.minimum_update_interval_ms = max(int(self.config.get("minimum_update_interval_ms") or 16), 0)
        self.dirty_region = bool(self.config.get("dirty_region", True))
        self.draw_border = bool(self.config.get("draw_border", False))
        self.secondary_window = bool(self.config.get("secondary_window", False))
        self.max_stale_ms = max(int(self.config.get("max_stale_ms") or 100), 0)
        self.requested_profile = _normalize_wgc_profile(
            self.config.get("profile"),
            field="runtime.capture.windows.profile",
            default="performance",
        )
        self.fallback_profile = _normalize_wgc_profile(
            self.config.get("fallback_profile"),
            field="runtime.capture.windows.fallback_profile",
            default="compatible",
        )
        if self.fallback_profile != "compatible":
            raise TargetRuntimeError(
                "windows_capture_profile_invalid",
                "The WGC fallback profile must be 'compatible'.",
                {
                    "field": "runtime.capture.windows.fallback_profile",
                    "profile": self.fallback_profile,
                    "supported": ["compatible"],
                },
            )
        self.windows_build = _windows_build_number()
        self.effective_profile = self.requested_profile
        self.compatibility_fallback_used = False
        self.compatibility_fallback_reason: str | None = None
        if (
            self.requested_profile == "performance"
            and self.windows_build is not None
            and self.windows_build < WGC_PERFORMANCE_MIN_WINDOWS_BUILD
        ):
            self.effective_profile = "compatible"
            self.compatibility_fallback_used = True
            self.compatibility_fallback_reason = (
                f"windows_build_{self.windows_build}_below_"
                f"{WGC_PERFORMANCE_MIN_WINDOWS_BUILD}"
            )
            logger.warning(
                "WGC performance profile is unsupported on Windows build %s; "
                "using the compatible profile with the system-default capture border.",
                self.windows_build,
            )

        self._session_lock = threading.RLock()
        self._session = self._create_session(int(self.target.hwnd))

    def close(self) -> None:
        with self._session_lock:
            if self._session is not None:
                try:
                    self._session.close()
                finally:
                    self._session = None

    def reset_after_target_recovery(self) -> None:
        self._rebuild_session()

    def capture(self, rect: tuple[int, int, int, int] | None = None) -> CaptureResult:
        self.target.ensure_valid()
        roi = _normalize_client_roi(self.target, rect)
        client_rect = self.target.get_client_rect()
        image = self._capture_roi_with_single_rebuild(roi)
        return CaptureResult(
            success=True,
            image=image,
            window_rect=client_rect,
            relative_rect=roi,
            backend=self.backend_name,
        )

    def _capture_roi(self, roi: tuple[int, int, int, int]) -> np.ndarray:
        frame = self._snapshot_client_frame()
        x, y, width, height = roi
        return frame[y : y + height, x : x + width].copy()

    def _capture_roi_with_single_rebuild(self, roi: tuple[int, int, int, int]) -> np.ndarray:
        rebuild_attempted = False
        while True:
            try:
                return self._capture_roi(roi)
            except TargetRuntimeError as exc:
                if rebuild_attempted or exc.code not in self._rebuildable_error_codes():
                    raise
                rebuild_attempted = True
                self._rebuild_session()

    def _snapshot_client_frame(self) -> np.ndarray:
        session = self._ensure_session_matches_target()
        try:
            session.start_if_needed()
        except TargetRuntimeError as exc:
            if (
                self.effective_profile != "performance"
                or self.fallback_profile != "compatible"
                or not _is_optional_wgc_feature_unsupported(exc)
            ):
                raise
            self._activate_compatible_profile(reason=str(exc))
            session = self._ensure_session_matches_target()
            session.start_if_needed()
        session.wait_for_fresh_frame(self.max_stale_ms, self.frame_timeout_ms)
        frame = session.snapshot_full_frame()
        rgb = _coerce_rgb_frame(frame, backend=self.backend_name)
        return self._crop_wgc_frame_to_client(rgb)

    def _crop_wgc_frame_to_client(self, frame: np.ndarray) -> np.ndarray:
        _, _, client_width, client_height = self.target.get_client_rect()
        frame_height, frame_width = frame.shape[:2]
        if frame_width == client_width and frame_height == client_height:
            return frame.copy()

        client_left, client_top, _, _ = self.target.get_client_rect_screen()
        frame_bounds = _get_dwm_extended_frame_bounds(self.target.hwnd) or self.target.get_window_rect_screen()
        frame_left, frame_top, _, _ = frame_bounds
        offset_x = int(client_left) - int(frame_left)
        offset_y = int(client_top) - int(frame_top)

        if (
            offset_x >= 0
            and offset_y >= 0
            and offset_x + client_width <= frame_width
            and offset_y + client_height <= frame_height
        ):
            return frame[offset_y : offset_y + client_height, offset_x : offset_x + client_width].copy()

        raise TargetRuntimeError(
            "windows_capture_failed",
            "WGC captured frame could not be mapped back to the target client area.",
            {
                "backend": self.backend_name,
                "frame_shape": list(frame.shape),
                "client_rect_screen": [int(client_left), int(client_top), int(client_width), int(client_height)],
                "frame_bounds_screen": list(frame_bounds),
                "client_offset": [int(offset_x), int(offset_y)],
            },
        )

    def self_check(self) -> dict[str, Any]:
        payload = super().self_check()
        session = self._ensure_session_matches_target()
        payload.update(session.health())
        payload["capture_cursor"] = self.capture_cursor
        payload["minimum_update_interval_ms"] = int(self.minimum_update_interval_ms)
        payload["dirty_region"] = self.dirty_region
        payload["draw_border"] = self.draw_border
        payload["secondary_window"] = self.secondary_window
        payload["frame_timeout_ms"] = int(self.frame_timeout_ms)
        payload["max_stale_ms"] = int(self.max_stale_ms)
        payload["capture_profile_requested"] = self.requested_profile
        payload["capture_profile_effective"] = self.effective_profile
        payload["compatibility_fallback_used"] = self.compatibility_fallback_used
        payload["compatibility_fallback_reason"] = self.compatibility_fallback_reason
        payload["windows_build"] = self.windows_build
        return payload

    def _ensure_session_matches_target(self) -> PersistentWgcSession:
        with self._session_lock:
            current_hwnd = int(self.target.hwnd)
            if self._session is None or int(self._session.hwnd) != current_hwnd:
                if self._session is not None:
                    self._session.close()
                self._session = self._create_session(current_hwnd)
            return self._session

    def _rebuild_session(self) -> None:
        with self._session_lock:
            current_hwnd = int(self.target.hwnd)
            if self._session is not None:
                self._session.close()
            self._session = self._create_session(current_hwnd)

    def _activate_compatible_profile(self, *, reason: str) -> None:
        with self._session_lock:
            if self.effective_profile == "compatible":
                return
            self.effective_profile = "compatible"
            self.compatibility_fallback_used = True
            self.compatibility_fallback_reason = str(reason)
            current_hwnd = int(self.target.hwnd)
            if self._session is not None:
                self._session.close()
            self._session = self._create_session(current_hwnd)
        logger.warning(
            "WGC performance profile could not start; retrying once with the "
            "compatible profile and system-default capture border. reason=%s",
            reason,
        )

    def _create_session(self, hwnd: int) -> PersistentWgcSession:
        compatible = self.effective_profile == "compatible"
        return PersistentWgcSession(
            hwnd=int(hwnd),
            module_name=self.module_name,
            capture_cursor=self.capture_cursor,
            draw_border=None if compatible else self.draw_border,
            secondary_window=None if compatible else self.secondary_window,
            minimum_update_interval_ms=None if compatible else self.minimum_update_interval_ms,
            dirty_region=None if compatible else self.dirty_region,
        )

    @staticmethod
    def _rebuildable_error_codes() -> set[str]:
        return {
            "windows_capture_failed",
            "windows_capture_frame_timeout",
            "windows_capture_frame_unavailable",
            "windows_capture_session_closed",
            "windows_capture_session_error",
            "windows_capture_session_stopped",
        }


class WindowsDxgiCaptureBackend(BaseWindowsCaptureBackend):
    backend_name = "dxgi"

    def __init__(self, target: WindowTarget, config: dict[str, Any] | None = None):
        super().__init__(target, config)
        module_name = str(self.config.get("module_name") or "dxcam")
        try:
            dxcam = importlib.import_module(module_name)
        except Exception as exc:
            raise TargetRuntimeError(
                "windows_capture_init_failed",
                f"Unable to import DXGI capture module '{module_name}'.",
                {"backend": self.backend_name, "error": str(exc)},
            ) from exc

        device_idx = self.config.get("device_idx")
        output_idx = self.config.get("output_idx")
        create_kwargs = {"output_color": "RGB"}
        if device_idx is not None:
            create_kwargs["device_idx"] = int(device_idx)
        if output_idx is not None:
            create_kwargs["output_idx"] = int(output_idx)

        try:
            self._camera = dxcam.create(**create_kwargs)
        except Exception as exc:
            raise TargetRuntimeError(
                "windows_capture_init_failed",
                "Failed to initialize the configured DXGI capture backend.",
                {"backend": self.backend_name, "error": str(exc), "options": create_kwargs},
            ) from exc

    def close(self) -> None:
        stop_fn = getattr(self._camera, "stop", None)
        if callable(stop_fn):
            try:
                stop_fn()
            except Exception:
                pass

    def _capture_roi(self, roi: tuple[int, int, int, int]) -> np.ndarray:
        left, top, _, _ = self.target.get_client_rect_screen()
        x, y, width, height = roi
        region = (left + x, top + y, left + x + width, top + y + height)
        try:
            frame = self._camera.grab(region=region)
        except Exception as exc:
            raise TargetRuntimeError(
                "windows_capture_failed",
                f"DXGI capture failed for the configured window: {exc}",
                {"backend": self.backend_name, "region": list(region)},
            ) from exc
        if frame is None:
            raise TargetRuntimeError(
                "windows_capture_failed",
                "DXGI capture returned an empty frame.",
                {"backend": self.backend_name, "region": list(region)},
            )
        return np.asarray(frame).copy()


class WindowsPrintWindowCaptureBackend(BaseWindowsCaptureBackend):
    backend_name = "printwindow"

    def _capture_roi(self, roi: tuple[int, int, int, int]) -> np.ndarray:
        _, _, client_width, client_height = self.target.get_client_rect()
        hwnd = self.target.hwnd

        hwnd_dc = win32gui.GetWindowDC(hwnd)
        if not hwnd_dc:
            raise TargetRuntimeError(
                "windows_capture_failed",
                "PrintWindow could not acquire a device context.",
                {"backend": self.backend_name, "hwnd": hwnd},
            )

        src_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        mem_dc = src_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        try:
            bitmap.CreateCompatibleBitmap(src_dc, client_width, client_height)
            mem_dc.SelectObject(bitmap)
            result = ctypes.windll.user32.PrintWindow(hwnd, mem_dc.GetSafeHdc(), 0x00000001)
            if result != 1:
                raise TargetRuntimeError(
                    "windows_capture_failed",
                    "PrintWindow failed to capture the configured client area.",
                    {"backend": self.backend_name, "hwnd": hwnd},
                )
            buffer = bitmap.GetBitmapBits(True)
            info = bitmap.GetInfo()
            image = np.frombuffer(buffer, dtype=np.uint8)
            image = image.reshape((info["bmHeight"], info["bmWidth"], 4))
            rgb = cv2.cvtColor(image, cv2.COLOR_BGRA2RGB)
            x, y, width, height = roi
            return rgb[y : y + height, x : x + width].copy()
        finally:
            mem_dc.DeleteDC()
            src_dc.DeleteDC()
            win32gui.ReleaseDC(hwnd, hwnd_dc)
            win32gui.DeleteObject(bitmap.GetHandle())


def build_capture_backend(backend: str, target: WindowTarget, config: dict[str, Any]) -> BaseWindowsCaptureBackend:
    normalized = str(backend or "").strip().lower()
    if normalized == "wgc":
        return WindowsWgcCaptureBackend(target, config)
    if normalized == "dxgi":
        return WindowsDxgiCaptureBackend(target, config)
    if normalized == "gdi":
        return WindowsGdiCaptureBackend(target, config)
    if normalized == "printwindow":
        return WindowsPrintWindowCaptureBackend(target, config)
    raise TargetRuntimeError(
        "capture_backend_invalid_for_provider",
        f"Unsupported Windows capture backend '{backend}'.",
        {"backend": backend},
    )


def _normalize_client_roi(target: WindowTarget, rect: tuple[int, int, int, int] | None) -> tuple[int, int, int, int]:
    _, _, client_width, client_height = target.get_client_rect()
    if rect is None:
        return 0, 0, client_width, client_height

    x, y, width, height = [int(value) for value in rect]
    if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > client_width or y + height > client_height:
        raise TargetRuntimeError(
            "capture_rect_invalid",
            "Capture rect is outside the current window client area.",
            {"rect": [x, y, width, height], "viewport": [0, 0, client_width, client_height]},
        )
    return x, y, width, height


def _coerce_rgb_frame(frame: Any, *, backend: str) -> np.ndarray:
    if isinstance(frame, np.ndarray):
        array = np.asarray(frame)
    elif hasattr(frame, "__array__"):
        array = np.asarray(frame)
    elif hasattr(frame, "to_ndarray"):
        array = np.asarray(frame.to_ndarray())
    elif hasattr(frame, "to_numpy"):
        array = np.asarray(frame.to_numpy())
    elif hasattr(frame, "copy") and callable(frame.copy):
        try:
            array = np.asarray(frame.copy())
        except Exception as exc:
            raise TargetRuntimeError(
                "windows_capture_failed",
                "Capture backend returned an unsupported frame object.",
                {"backend": backend, "error": str(exc), "frame_type": type(frame).__name__},
            ) from exc
    else:
        raise TargetRuntimeError(
            "windows_capture_failed",
            "Capture backend returned an unsupported frame object.",
            {"backend": backend, "frame_type": type(frame).__name__},
        )

    if array.ndim != 3 or array.shape[2] not in {3, 4}:
        raise TargetRuntimeError(
            "windows_capture_failed",
            "Capture backend returned an array with unsupported shape.",
            {"backend": backend, "shape": list(array.shape)},
        )
    if array.shape[2] == 4:
        return cv2.cvtColor(array, cv2.COLOR_BGRA2RGB)
    return np.asarray(array).copy()


class _RECT(ctypes.Structure):
    _fields_ = (
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    )


def _get_dwm_extended_frame_bounds(hwnd: int) -> tuple[int, int, int, int] | None:
    dwmapi = getattr(ctypes.windll, "dwmapi", None)
    get_attribute = getattr(dwmapi, "DwmGetWindowAttribute", None) if dwmapi else None
    if not callable(get_attribute):
        return None
    rect = _RECT()
    try:
        result = int(get_attribute(int(hwnd), 9, ctypes.byref(rect), ctypes.sizeof(rect)))
    except Exception:
        return None
    if result != 0:
        return None
    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    if width <= 0 or height <= 0:
        return None
    return int(rect.left), int(rect.top), width, height
