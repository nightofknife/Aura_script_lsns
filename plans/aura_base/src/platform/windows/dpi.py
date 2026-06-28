# -*- coding: utf-8 -*-
from __future__ import annotations

import ctypes
import threading
from typing import Any

import win32api


_DPI_LOCK = threading.Lock()
_DPI_INITIALIZED = False
_DPI_MODE: str | None = None


def ensure_process_dpi_awareness() -> dict[str, Any]:
    global _DPI_INITIALIZED, _DPI_MODE
    with _DPI_LOCK:
        if _DPI_INITIALIZED:
            return {"ok": True, "mode": _DPI_MODE, "already_initialized": True}

        user32 = getattr(ctypes.windll, "user32", None)
        shcore = getattr(ctypes.windll, "shcore", None)

        # Prefer Per-Monitor V2 on modern Windows.
        awareness_context = ctypes.c_void_p(-4)
        set_context = getattr(user32, "SetProcessDpiAwarenessContext", None) if user32 else None
        if callable(set_context):
            try:
                if bool(set_context(awareness_context)):
                    _DPI_INITIALIZED = True
                    _DPI_MODE = "per_monitor_v2"
                    return {"ok": True, "mode": _DPI_MODE, "already_initialized": False}
            except Exception:
                pass

        set_awareness = getattr(shcore, "SetProcessDpiAwareness", None) if shcore else None
        if callable(set_awareness):
            try:
                result = int(set_awareness(2))
                # S_OK=0, E_ACCESSDENIED=0x80070005 means already set by host.
                if result in {0, -2147024891}:
                    _DPI_INITIALIZED = True
                    _DPI_MODE = "per_monitor"
                    return {"ok": True, "mode": _DPI_MODE, "already_initialized": result != 0}
            except Exception:
                pass

        set_legacy = getattr(user32, "SetProcessDPIAware", None) if user32 else None
        if callable(set_legacy):
            try:
                if bool(set_legacy()):
                    _DPI_INITIALIZED = True
                    _DPI_MODE = "system"
                    return {"ok": True, "mode": _DPI_MODE, "already_initialized": False}
            except Exception:
                pass

        _DPI_INITIALIZED = True
        _DPI_MODE = "unknown"
        return {"ok": False, "mode": _DPI_MODE, "already_initialized": False}


def get_window_dpi(hwnd: int) -> int:
    user32 = getattr(ctypes.windll, "user32", None)
    get_dpi = getattr(user32, "GetDpiForWindow", None) if user32 else None
    if callable(get_dpi):
        try:
            value = int(get_dpi(int(hwnd)))
            if value > 0:
                return value
        except Exception:
            pass
    return 96


def get_window_scale_factor(hwnd: int) -> float:
    return float(get_window_dpi(hwnd)) / 96.0


def get_monitor_scale_factor(monitor_index: int | None = None) -> float:
    monitors = win32api.EnumDisplayMonitors()
    if not monitors:
        return 1.0
    index = 0 if monitor_index is None else max(int(monitor_index), 0)
    if index >= len(monitors):
        index = 0
    monitor_handle = monitors[index][0]
    shcore = getattr(ctypes.windll, "shcore", None)
    get_scale = getattr(shcore, "GetScaleFactorForMonitor", None) if shcore else None
    if callable(get_scale):
        scale = ctypes.c_int()
        try:
            result = int(get_scale(monitor_handle, ctypes.byref(scale)))
            if result == 0 and int(scale.value) > 0:
                return float(scale.value) / 100.0
        except Exception:
            pass
    return 1.0
