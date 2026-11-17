"""Read the native frame-end buffer without entering the bridge input queue."""
from __future__ import annotations

import ctypes
import mmap
import os
import struct
import threading
import time
from uuid import uuid4

import numpy as np
import win32event

from ....aura_base.src.platform.contracts import CaptureResult, TargetRuntimeError

BACKEND = "unity_bridge"
WIDTH, HEIGHT = 1280, 720
_HEADER = struct.Struct("<IIIIQQQ")
_PIXEL_BYTES = WIDTH * HEIGHT * 4


class BridgeFrameStream:
    def __init__(self, session: str, *, max_stale_ms: int):
        self.session_id = uuid4().int
        self._max_age = max_stale_ms / 1000
        self._lock = threading.Lock()
        self._closed = False
        self._accepting_reads = True
        kernel = ctypes.windll.kernel32
        frequency, tick = ctypes.c_int64(), ctypes.c_int64()
        kernel.QueryPerformanceFrequency(ctypes.byref(frequency))
        monotonic = time.monotonic()
        kernel.QueryPerformanceCounter(ctypes.byref(tick))
        self._frequency = frequency.value
        # Python on Windows can use GetTickCount64 for monotonic(), which has
        # a coarser clock than QPC. Bias toward an earlier capture timestamp.
        self._clock_origin = monotonic - tick.value / self._frequency - time.get_clock_info("monotonic").resolution
        name = f"Local\\AuraResonanceFrame-{os.getpid()}-{session}"
        self._mutex = win32event.CreateMutex(None, False, name + "-lock")
        try:
            self._buffer = mmap.mmap(-1, _HEADER.size + _PIXEL_BYTES, tagname=name)
        except BaseException:
            self._mutex.Close()
            raise

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                self._buffer.close()
                self._mutex.Close()

    def stop_reading(self):
        with self._lock:
            self._accepting_reads = False

    def capture_stream_frame(self, after_generation=-1, *, expected_session_id=None):
        if expected_session_id is not None and expected_session_id != self.session_id:
            raise TargetRuntimeError("capture_stream_replaced", "Bridge frame session changed")
        if not self._lock.acquire(blocking=False):
            return None
        try:
            if self._closed or not self._accepting_reads:
                raise TargetRuntimeError("capture_stream_closed", "Bridge frame session is closed")
            acquired = win32event.WaitForSingleObject(self._mutex, 0)
            if acquired == win32event.WAIT_TIMEOUT:
                return None
            if acquired == win32event.WAIT_ABANDONED:
                win32event.ReleaseMutex(self._mutex)
                raise TargetRuntimeError("bridge_capture_failed", "Game abandoned the frame buffer")
            if acquired != win32event.WAIT_OBJECT_0:
                raise TargetRuntimeError("bridge_capture_failed", "Cannot lock the frame buffer")
            try:
                state, width, height, size, generation, tick, unity_frame = _HEADER.unpack_from(self._buffer)
                if state == 2:
                    raise TargetRuntimeError("viewport_changed", "Game window or Unity render size changed")
                if state == 3:
                    raise TargetRuntimeError("bridge_capture_failed", "Unity could not read a rendered frame")
                if not state or generation <= after_generation:
                    return None
                if state != 1 or (width, height, size) != (WIDTH, HEIGHT, _PIXEL_BYTES):
                    raise TargetRuntimeError("bridge_capture_invalid", "Invalid bridge frame header")
                timestamp = self._clock_origin + tick / self._frequency
                if time.monotonic() - timestamp > self._max_age:
                    return None
                pixels = self._buffer[_HEADER.size:]
            finally:
                win32event.ReleaseMutex(self._mutex)
        finally:
            self._lock.release()
        rgb = np.frombuffer(pixels, dtype=np.uint8).reshape(height, width, 4)[::-1, :, :3].copy()
        client_rect = (0, 0, width, height)
        return {"capture": CaptureResult(True, rgb, client_rect, client_rect, BACKEND),
                "generation": generation, "arrived_at_monotonic": timestamp,
                "session_id": self.session_id, "unity_frame": unity_frame}
