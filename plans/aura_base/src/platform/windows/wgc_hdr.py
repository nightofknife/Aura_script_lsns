"""Native FP16 WGC capture and demand-driven Windows display color state."""
from __future__ import annotations

import ctypes as ct
import threading
import time
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from ..contracts import TargetRuntimeError


_DLL = Path(__file__).resolve().parents[3] / "assets" / "windows" / "aura_wgc_hdr.dll"
_FALLBACK_QUERY_INTERVAL = 5.0


def _failed(operation, result):
    return RuntimeError(f"{operation} failed (HRESULT 0x{result & 0xffffffff:08x})")


@lru_cache(maxsize=1)
def _native():
    api = ct.WinDLL(str(_DLL))
    for name, args, result in (
        ("display_query", [ct.c_size_t, ct.POINTER(ct.c_uint32)], ct.c_int32),
        ("watch_open", [ct.c_size_t], ct.c_void_p),
        ("watch_epoch", [ct.c_void_p], ct.c_uint64),
        ("watch_close", [ct.c_void_p], None),
        ("capture_open", [ct.c_size_t, ct.c_int, ct.POINTER(ct.c_void_p)], ct.c_int32),
        ("capture_read", [ct.c_void_p, ct.c_uint32, ct.c_void_p, ct.c_size_t,
                          ct.POINTER(ct.c_int), ct.POINTER(ct.c_int)], ct.c_int32),
        ("capture_close", [ct.c_void_p], None),
    ):
        fn = getattr(api, f"aura_hdr_{name}")
        fn.argtypes, fn.restype = args, result
    return api


class DisplayColorState:
    """Zero display queries on a stable watched output; no idle timer."""

    def __init__(self):
        self.hwnd = None
        self.watch = None
        self.epoch = None
        self.poll_after = 0.0
        self.white = None  # 0 is confirmed SDR; positive is HDR SDR-white in milli units.
        self.error = None
        self.query_count = 0
        self.notifications = False

    def invalidate(self):
        self.epoch = None

    def close(self):
        if self.watch:
            _native().aura_hdr_watch_close(self.watch)
            self.watch = None
        self.hwnd = None
        self.notifications = False
        self.invalidate()

    def current(self, hwnd: int) -> int:
        try:
            api = _native()
        except (OSError, AttributeError) as exc:
            raise TargetRuntimeError("windows_capture_hdr_module_unavailable",
                                     "WGC HDR support could not load its native component.",
                                     {"path": str(_DLL), "error": str(exc)}) from exc
        if hwnd != self.hwnd:
            self.close()
            self.hwnd = hwnd
            self.watch = api.aura_hdr_watch_open(hwnd)
        snapshot = api.aura_hdr_watch_epoch(self.watch) if self.watch else 0
        epoch, reliable = snapshot >> 1, bool(snapshot & 1)
        self.notifications = reliable
        now = time.monotonic()
        if self.epoch != epoch or ((not reliable or self.error is not None) and now >= self.poll_after):
            white = ct.c_uint32()
            hr = api.aura_hdr_display_query(hwnd, ct.byref(white))
            self.query_count += 1
            self.epoch = epoch
            self.poll_after = now + _FALLBACK_QUERY_INTERVAL
            self.error = _failed("Display color query", hr) if hr != 0 else None
            self.white = int(white.value) if hr == 0 else None
        if self.error is not None or self.white is None:
            raise TargetRuntimeError("windows_capture_hdr_display_unknown",
                                     "Could not determine the game monitor's HDR settings.",
                                     {"hwnd": hwnd, "error": str(self.error)})
        return self.white


class _Control:
    def __init__(self, hwnd, cursor, white, on_frame, on_closed):
        self.white = white
        self.error = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(hwnd, cursor, on_frame, on_closed),
                                        name="AuraWgcHdr", daemon=True)
        self._thread.start()
        if not self._ready.wait(5.0):
            self.stop()
            raise RuntimeError("Timed out starting native FP16 WGC")
        if self.error is not None:
            raise self.error

    def set_white(self, white):
        self.white = white

    def stop(self):
        self._stop.set()

    def wait(self):
        self._thread.join(timeout=3.0)

    def is_finished(self):
        return not self._thread.is_alive()

    def _run(self, hwnd, cursor, on_frame, on_closed):
        handle = ct.c_void_p()
        try:
            api = _native()
            hr = api.aura_hdr_capture_open(hwnd, cursor, ct.byref(handle))
            if hr != 0:
                raise _failed("FP16 WGC start", hr)
            self._ready.set()
            width, height = ct.c_int(), ct.c_int()
            buffer = np.empty(0, dtype=np.uint8)
            while not self._stop.is_set():
                white = self.white
                result = api.aura_hdr_capture_read(handle, white, buffer.ctypes.data, buffer.size,
                                                   ct.byref(width), ct.byref(height))
                if result == 0:
                    continue
                if result == 2:
                    buffer = np.empty(width.value * height.value * 4, dtype=np.uint8)
                    continue
                if result != 1:
                    raise _failed("FP16 WGC frame read", result)
                if not self._stop.is_set():
                    array = buffer[:width.value * height.value * 4].reshape(height.value, width.value, 4)
                    on_frame(SimpleNamespace(frame_buffer=array, hdr_white=white), self)
        except Exception as exc:
            self.error = exc
        finally:
            if handle:
                api.aura_hdr_capture_close(handle)
            self._ready.set()
            # Session close already clears state on a requested stop.
            if not self._stop.is_set() and self.error is None:
                on_closed()


class HdrWindowsCapture:
    """Expose the same callback/control shape as the SDR WGC session."""

    def __init__(self, *, hwnd: int, cursor: bool, white: int):
        self.hwnd = hwnd
        self.cursor = cursor
        self.white = white

    def event(self, handler):
        setattr(self, handler.__name__, handler)
        return handler

    def start_free_threaded(self):
        return _Control(self.hwnd, self.cursor, self.white, self.on_frame_arrived, self.on_closed)
