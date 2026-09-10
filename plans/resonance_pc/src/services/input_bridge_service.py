"""Plan-scoped bridge session, normalization and serialized gesture ownership."""
from __future__ import annotations

import asyncio
import contextvars
from contextlib import contextmanager
import math
import os
from pathlib import Path
import subprocess
import threading
import time

from packages.aura_core.api import service_info
from plans.aura_base.src.platform.contracts import TargetRuntimeError
from plans.aura_base.src.platform.runtime_config import resolve_runtime_config
from ..bridge.client import BridgeClient

_CANCEL_TOKEN = contextvars.ContextVar("resonance_bridge_cancel_token", default=None)


def finite(value):
    number = float(value)
    if not math.isfinite(number):
        raise TargetRuntimeError("input_parameter_invalid", "Input values must be finite.")
    return number


def seconds(value):
    return max(finite(value), 0.0)


def mouse_button(value):
    name = str(value or "left").strip().lower()
    if name not in ("left", "right", "middle"):
        raise TargetRuntimeError("unsupported_mouse_button", f"Unsupported mouse button: {name}")
    return name, ("left", "right", "middle").index(name)


@service_info(alias="resonance_pc_input_bridge", public=True,
              deps={"screen": "plans/aura_base/screen"})
class ResonancePcInputBridgeService:
    def __init__(self, screen):
        self.target_runtime = screen.target_runtime
        self.config = self.target_runtime.config
        self._client = None
        self._lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._mode = None
        self._switching = False
        self._active = False
        self._system_active = 0
        self._system_held = lambda: False
        self._cancel_epoch = 0
        self._held = set()
        self._position = None
        self._viewport = {}
        self._identity = None
        self._needs_hello = True
        self._capabilities = {}

    @property
    def enabled(self):
        mode = str(self.config.get("resonance_pc.input.mode", "system")).strip().lower()
        # GUI workers explicitly opt in; standalone runners retain plan config.
        gui_override = os.environ.get("AURA_GUI_INPUT_BRIDGE")
        if gui_override is not None:
            mode = "bridge" if gui_override == "1" else "system"
        if mode not in ("system", "bridge"):
            raise TargetRuntimeError("input_parameter_invalid", f"Unknown input mode: {mode}")
        with self._state_lock:
            if self._switching:
                raise TargetRuntimeError("input_gesture_busy", "Input mode is being switched.")
            if self._mode is not None and mode != self._mode and (
                    self._active or self._held or self._system_active or self._system_held()):
                raise TargetRuntimeError("input_gesture_busy", "Input mode cannot change during a gesture.")
            previous = self._mode
            if previous == "bridge" and mode == "system":
                self._switching = True
            else:
                self._mode = mode
        if previous == "bridge" and mode == "system":
            try:
                self.close()
                with self._state_lock:
                    self._mode = mode
            finally:
                with self._state_lock:
                    self._switching = False
        return mode == "bridge"

    def system_call(self, function, *args, **kwargs):
        with self.system_scope():
            return function(*args, **kwargs)

    @contextmanager
    def system_scope(self):
        with self._state_lock:
            self._system_active += 1
        try:
            yield
        finally:
            with self._state_lock:
                self._system_active -= 1

    @property
    def cleanup_in_bridge(self):
        # A rejected config switch must never prevent releasing the old backend's keys.
        with self._state_lock:
            mode = self._mode
        return mode == "bridge" if mode is not None else self.enabled

    def _timing(self, key, default):
        resolved = resolve_runtime_config(self.config)
        options = resolved.input.provider_options(resolved.provider)
        return seconds(float(options.get(key, default)) / 1000.0)

    def _duration(self, duration):
        return self._timing("mouse_move_duration_ms", 120) if duration is None else seconds(duration)

    def _load(self, pid):
        directory = Path(__file__).resolve().parents[2] / "assets" / "input_bridge"
        loader = directory / "resonance_bridge_loader.exe"
        library = directory / "resonance_bridge_host.dll"
        if not loader.is_file() or not library.is_file():
            raise TargetRuntimeError("input_bridge_not_built", "Native bridge binaries are missing.",
                                     {"directory": str(directory)})
        result = subprocess.run([str(loader), "--pid", str(pid), "--dll", str(library)],
                                capture_output=True, text=True, timeout=15,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise TargetRuntimeError("input_bridge_load_failed", result.stderr or result.stdout,
                                     {"returncode": result.returncode})

    def _ready(self):
        summary = self.target_runtime.target_summary()
        rect = self.target_runtime.get_client_rect()
        pid, hwnd = int(summary.get("pid") or 0), int(summary.get("hwnd") or 0)
        if not pid or not hwnd or rect is None or int(rect[2]) <= 0 or int(rect[3]) <= 0:
            raise TargetRuntimeError("input_viewport_invalid", "Bridge needs a live, non-minimized client area.")
        identity = (pid, hwnd, int(rect[2]), int(rect[3]))
        changed = self._identity != identity
        if changed:
            self._needs_hello = True
            if self._held:
                self.cancel_input()
                raise TargetRuntimeError("input_viewport_changed", "Viewport changed during held input.")
            if self._client and self._identity and self._identity[:2] != identity[:2]:
                self.close()
            generation = int(self._viewport.get("generation", 0)) + 1
            self._viewport = {"width": identity[2], "height": identity[3], "generation": generation}
            self._identity = identity
            self._position = self._clip(*(self._position or (identity[2] // 2, identity[3] // 2)))
        def hello(client, *, wait_for_pipe=False):
            deadline = time.monotonic() + 10.0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TargetRuntimeError("input_bridge_unavailable", "Bridge startup readiness deadline expired.")
                try:
                    return client.request("hello", {"hwnd": hwnd, "position": self._position},
                                          self._viewport, min(2.0, remaining))
                except TargetRuntimeError as exc:
                    # Bootstrap is rejected before acquisition; unavailable means no
                    # payload was written. Only these outcomes may be retried.
                    retryable = exc.code == "bootstrap_pending" or (
                        wait_for_pipe and exc.code == "input_bridge_unavailable")
                    if not retryable or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)

        if self._client is None:
            client = BridgeClient(pid)
            try:
                reply = hello(client)
            except TargetRuntimeError as exc:
                if client.uncertain:
                    self._client = client
                if exc.code != "input_bridge_unavailable":
                    raise
                self._load(pid)
                try:
                    reply = hello(client, wait_for_pipe=True)
                except TargetRuntimeError:
                    if client.uncertain:
                        self._client = client
                    raise
            self._client = client
            self._accept(reply)
            self._needs_hello = False
        elif self._client.uncertain:
            raise TargetRuntimeError("input_result_unknown", "Previous result is uncertain; cancel input before continuing.")
        elif self._needs_hello or not self._capabilities:
            self._accept(hello(self._client))
            self._needs_hello = False

    def _accept(self, reply):
        result = reply.get("result") or {}
        if "capabilities" in result:
            self._capabilities = dict(result["capabilities"])
        if "viewport" in result:
            self._viewport = dict(result["viewport"])
        position = result.get("position")
        if isinstance(position, (list, tuple)) and len(position) == 2:
            self._position = tuple(map(int, position))

    def _clip(self, x, y):
        return (min(max(int(x), 0), int(self._viewport["width"]) - 1),
                min(max(int(y), 0), int(self._viewport["height"]) - 1))

    def _point(self, x, y):
        if x is None or y is None:
            return self._clip(*(self._position or (self._viewport["width"] // 2, self._viewport["height"] // 2)))
        return self._clip(x, y)

    def _execute(self, op, args, capability="absolute_pointer", endpoint=None, timeout=10.0, complete=False):
        with self._lock:
            self._ready()
            token = _CANCEL_TOKEN.get()
            if token is not None and token.is_set():
                raise TargetRuntimeError("input_cancelled", "Queued input was cancelled.")
            if not self._capabilities.get(capability, False):
                raise TargetRuntimeError("input_capability_unsupported", f"Bridge capability unavailable: {capability}")
            if complete and self._held:
                raise TargetRuntimeError("input_gesture_busy", "A button is already held.")
            with self._state_lock:
                epoch = self._cancel_epoch
                self._active = True
            try:
                reply = self._client.request(op, args, self._viewport, timeout, cancel_token=token)
                if epoch != self._cancel_epoch:
                    raise TargetRuntimeError("input_cancelled", "Input was cancelled.")
                self._accept(reply)
                if endpoint is not None:
                    self._position = endpoint
            finally:
                with self._state_lock:
                    self._active = False

    def ensure_ready(self):
        with self._lock:
            self._ready()
            return dict(self._capabilities)

    def click(self, x=None, y=None, button="left", clicks=1, interval=None):
        _, button_id = mouse_button(button)
        n = max(int(clicks), 1)
        spacing = self._timing("key_interval_ms", 40) if interval is None else seconds(interval)
        post = self._timing("click_post_delay_ms", 30)
        if x is not None and y is not None:
            finite(x); finite(y)
        with self._lock:
            self._ready()
            point = self._point(x, y)
            self._execute("click", {"x": point[0], "y": point[1], "button": button_id,
                                    "clicks": n, "interval": spacing, "post_delay": post},
                          endpoint=point, timeout=10 + n * (post + spacing + 1), complete=True)

    def move_to(self, x, y, duration=None):
        finite(x); finite(y)
        duration = self._duration(duration)
        with self._lock:
            self._ready()
            point = self._point(x, y)
            self._execute("move", {"x": point[0], "y": point[1], "duration": duration},
                          endpoint=point, timeout=duration + 10)

    def move_relative(self, dx, dy, duration=None):
        finite(dx); finite(dy)
        with self._lock:
            self._ready()
            x, y = self._point(None, None)
            self.move_to(x + int(dx), y + int(dy), duration)

    def button(self, button, down):
        name, button_id = mouse_button(button)
        with self._lock:
            self._execute("button_down" if down else "button_up", {"button": button_id})
            (self._held.add if down else self._held.discard)(name)

    def drag(self, start_x, start_y, end_x, end_y, button="left", duration=None,
             hold_before_release_sec=0.0, *, stop_inertia=None):
        _, button_id = mouse_button(button)
        duration, hold = self._duration(duration), seconds(hold_before_release_sec)
        finite(end_x); finite(end_y)
        for value in (start_x, start_y):
            if value is not None:
                finite(value)
        stop = self.config.get("resonance_pc.input.stop_inertia", True) if stop_inertia is None else stop_inertia
        if not isinstance(stop, bool):
            raise TargetRuntimeError("input_parameter_invalid", "stop_inertia must be boolean.")
        with self._lock:
            self._ready()
            start, end = self._point(start_x, start_y), self._point(end_x, end_y)
            self._execute("drag", {"x": start[0], "y": start[1], "to_x": end[0], "to_y": end[1],
                                   "duration": duration, "hold": hold, "button": button_id,
                                   "stop_inertia": stop}, "direct_drag", endpoint=end,
                          timeout=duration + hold + 10, complete=True)

    def scroll(self, amount, direction="down"):
        ticks = max(abs(int(amount)), 1)
        signed = -ticks if str(direction or "down").lower() == "down" else ticks
        self._execute("scroll", {"signed_detents": signed}, "scroll")

    def release_all(self):
        with self._lock:
            if self._client:
                try:
                    self._accept(self._client.request("release_all", viewport=self._viewport))
                finally:
                    self._held.clear()

    def cancel_input(self):
        with self._state_lock:
            self._cancel_epoch += 1
            client = self._client
        if client:
            self._accept(client.request("cancel", viewport=self._viewport))
            client.uncertain = False
        self._held.clear()

    async def run_async(self, function, *args, **kwargs):
        # Shield keeps the worker alive until native cancellation and cleanup complete.
        token = threading.Event()

        def invoke():
            marker = _CANCEL_TOKEN.set(token)
            try:
                if token.is_set():
                    raise TargetRuntimeError("input_cancelled", "Queued input was cancelled.")
                return function(*args, **kwargs)
            finally:
                _CANCEL_TOKEN.reset(marker)

        task = asyncio.create_task(asyncio.to_thread(invoke))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            token.set()
            cleanup = asyncio.create_task(asyncio.to_thread(self.cancel_input))
            try:
                await asyncio.shield(cleanup)
            finally:
                try:
                    await asyncio.shield(task)
                except (TargetRuntimeError, asyncio.CancelledError):
                    pass
            raise

    def close(self):
        with self._lock:
            if self._client:
                self._client.request("close", viewport=self._viewport)
            self._client = None
            self._held.clear()
            self._position = None
            self._identity = None
            self._needs_hello = True
            self._capabilities.clear()

    def shutdown(self):
        """Framework lifecycle hook: cancel first, so shutdown does not wait for a long drag."""
        try:
            self.cancel_input()
        finally:
            self.close()
