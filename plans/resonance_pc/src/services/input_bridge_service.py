"""Plan-scoped bridge session, normalization and serialized gesture ownership."""
from __future__ import annotations

import asyncio
import contextvars
from contextlib import contextmanager
import math
import os
import subprocess
import threading
import time

import psutil
import win32gui
import win32process

from packages.aura_core.api import service_info
from packages.aura_core.context.plan import current_plan_name
from packages.aura_core.observability.logging.core_logger import current_cid
from packages.aura_core.observability.runtime_trace import (
    activity, current_root_cid, log_event, update_runtime, register_diagnostic_provider, tracked_to_thread,
)
from packages.aura_core.scheduler.operation_scope import target_operation_guard
from ....aura_base.src.platform.contracts import TargetRuntimeError
from ....aura_base.src.platform.runtime_config import resolve_runtime_config
from ....aura_base.src.platform.windows.window_selector import resolve_window_candidate
from ..bridge.client import BridgeClient
from ..bridge.frame_stream import BridgeFrameStream
from ..bridge.native_bundle import ASSETS, stage_bootstrap, stage_host

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
              deps={"config": "core/config"})
class ResonancePcInputBridgeService:
    def __init__(self, screen=None, *, config=None):
        self.target_runtime = screen.target_runtime if screen is not None else None
        self.config = config if config is not None else self.target_runtime.config
        self._client = None
        self._last_host = None
        self._assets = ASSETS
        self._frame_stream = None
        self._session_root_cid = None
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
        self._trace_mode = None
        self._trace_cids = set()
        self._trace_plan = current_plan_name.get()
        register_diagnostic_provider("input_bridge", self._diagnostic_snapshot)

    @property
    def enabled(self):
        mode = str(self.config.get("resonance_pc.input.mode", "system")).strip().lower()
        configured_mode = mode
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
                self.close(unload=True)
                with self._state_lock:
                    self._mode = mode
            finally:
                with self._state_lock:
                    self._switching = False
        owners = {cid for cid in (current_cid(), current_root_cid()) if cid and cid != "-"}
        if len(self._trace_cids | owners) > 64:
            self._trace_cids.clear()
        self._trace_cids.update(owners)
        self._trace_plan = current_plan_name.get()
        settings = {"input_mode": mode, "configured_input_mode": configured_mode,
                    "gui_bridge_override": gui_override, "bridge_enabled": mode == "bridge"}
        if settings != self._trace_mode:
            self._trace_mode = settings
            log_event("input.mode", **settings)
        update_runtime(settings)
        return mode == "bridge"

    def _diagnostic_snapshot(self, request):
        cid = request.get("cid")
        plan = request.get("plan") or request.get("plan_name") or request.get("runtime", {}).get("plan")
        if not cid or cid not in self._trace_cids or (plan and plan != self._trace_plan):
            return {"status": "unavailable", "reason": "request_does_not_own_bridge"}
        if not self._state_lock.acquire(blocking=False):
            return {"status": "busy"}
        try:
            result = {"status": "ok", "mode": self._mode, "active": self._active,
                      "system_active": self._system_active, "cancel_epoch": self._cancel_epoch}
        finally:
            self._state_lock.release()
        if not self._lock.acquire(blocking=False):
            result["gesture_state"] = "busy"
            return result
        try:
            result.update(held_mouse_buttons=sorted(self._held.copy()), identity=self._identity,
                          client_connected=self._client is not None,
                          result_uncertain=bool(getattr(self._client, "uncertain", False)))
            return result
        finally:
            self._lock.release()

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
        loader = self._assets / "resonance_bridge_loader.exe"
        if not loader.is_file():
            raise TargetRuntimeError("input_bridge_not_built", "Native bridge loader is missing.",
                                     {"directory": str(self._assets)})
        library = stage_bootstrap(self._assets)
        result = subprocess.run([str(loader), "--pid", str(pid), "--dll", str(library)],
                                capture_output=True, text=True, timeout=30,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise TargetRuntimeError("input_bridge_load_failed", result.stderr or result.stdout,
                                     {"returncode": result.returncode})

    def _bridge_target(self):
        # The desktop adapter must never bind or recover a window in bridge mode.
        if self._identity is not None:
            pid, hwnd = self._identity[:2]
            if win32gui.IsWindow(hwnd) and win32process.GetWindowThreadProcessId(hwnd)[1] == pid:
                return pid, hwnd
            raise TargetRuntimeError("viewport_changed", "Bridge target window or process changed")
        candidate = resolve_window_candidate(resolve_runtime_config(self.config).target)
        if not candidate.pid:
            raise TargetRuntimeError("input_viewport_invalid", "Bridge needs a live game process")
        return candidate.pid, candidate.hwnd

    def _ready(self):
        root_cid = current_root_cid()
        if self._client is not None and self._session_root_cid != root_cid:
            raise TargetRuntimeError("input_session_busy", "Another task owns the bridge session")
        pid, hwnd = self._bridge_target()
        if self._identity is None:
            self._needs_hello = True
            self._viewport = {"width": 0, "height": 0, "generation": 1}
            if win32gui.IsIconic(hwnd):
                self._viewport["minimized"] = True
            self._identity = (pid, hwnd, 0, 0)
            log_event("input.bridge_bound", target_pid=pid, target_hwnd=hwnd, viewport=dict(self._viewport))
            update_runtime({"bridge_pid": pid, "bridge_hwnd": hwnd})
        def hello(client, *, wait_for_pipe=False):
            deadline = time.monotonic() + 10.0
            host_hash = None
            prepared = False
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TargetRuntimeError("input_bridge_unavailable", "Bridge startup readiness deadline expired.")
                try:
                    status = client.request("status", timeout=min(2.0, remaining))["result"]
                    if status.get("bootstrap_abi") != 1:
                        raise TargetRuntimeError("bridge_restart_required", "请重启雷索纳斯以载入支持热更新的桥接器。")
                    if status["state"] == "failed" and status.get("host_state") == "unloaded":
                        raise TargetRuntimeError("bridge_failed", status["error"])
                    if host_hash is None:
                        host_hash = stage_host(self._assets, status["cache_root"])
                    if (not prepared or status.get("host_instance") != client.host_instance
                            or status.get("host_state") != "loaded"):
                        prepared_host = client.request(
                            "prepare_host", {"host_hash": host_hash}, timeout=min(2.0, remaining)
                        )["result"]
                        client.host_instance = prepared_host["host_instance"]
                        self._last_host = client
                        prepared = True
                        continue
                    if status["state"] == "failed":
                        raise TargetRuntimeError("bridge_failed", status["error"])
                    if status["state"] != "ready":
                        raise TargetRuntimeError("bootstrap_pending", "Unity bridge is starting up")
                    caps = status["capabilities"]
                    if not caps.get("frame_stream"):
                        raise TargetRuntimeError("bridge_capture_unsupported", "当前 Unity 配置不支持桥接截图。")
                    args = {"hwnd": hwnd}
                    if self._position is not None:
                        args["position"] = self._position
                    return client.request("hello", args, viewport=self._viewport, timeout=min(2.0, remaining))
                except TargetRuntimeError as exc:
                    # Bootstrap is rejected before acquisition; unavailable means no
                    # payload was written. Only these outcomes may be retried.
                    retryable = exc.code in {"bootstrap_pending", "bridge_transition"} or (
                        wait_for_pipe and exc.code == "input_bridge_unavailable")
                    if not retryable or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)

        if self._client is None:
            self._session_root_cid = root_cid
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
        elif self._needs_hello:
            raise TargetRuntimeError("viewport_changed", "Bridge viewport changed during the task")

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
        parameters = {key: value for key, value in args.items()
                      if key in {"x", "y", "to_x", "to_y", "button", "clicks", "interval", "post_delay",
                                 "duration", "hold", "stop_inertia", "signed_detents"}}
        with target_operation_guard(self.target_runtime, cleanup=op == "button_up"), activity(
                "input", op, backend="bridge", parameters=parameters, target=self._identity):
            return self._execute_request(op, args, capability, endpoint, timeout, complete)

    def _execute_request(self, op, args, capability, endpoint, timeout, complete):
        with self._lock:
            if op == "button_up":
                # Release only the existing bridge session. Cancellation must
                # not trigger runtime rebinding, HELLO or block this request.
                if self._client is not None:
                    self._accept(self._client.request(op, args, self._viewport, timeout))
                return
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
            except TargetRuntimeError as exc:
                if exc.code == "viewport_changed":
                    self._needs_hello = True
                raise
            finally:
                with self._state_lock:
                    self._active = False

    def ensure_ready(self):
        with self._lock:
            self._ready()
            return dict(self._capabilities)

    def viewport(self):
        with self._lock:
            self._ready()
            return (0, 0, self._viewport["width"], self._viewport["height"])

    def read_deep_dive_board(self) -> dict:
        return self._read_deep_dive("board")

    def read_deep_dive_turn(self) -> dict:
        return self._read_deep_dive("turn")

    def read_deep_dive_controls(self) -> dict:
        return self._read_deep_dive("controls")

    def _read_deep_dive(self, kind):
        if not self.enabled:
            raise TargetRuntimeError("input_bridge_required", "请启用桥接输入器后读取识海深潜棋盘。")
        capability = f"deep_dive_{kind}"
        with target_operation_guard(self.target_runtime), activity("data", f"bridge.{capability}"), self._lock:
            self._ready()
            if not self._capabilities.get(capability):
                raise TargetRuntimeError("deep_dive_board_unsupported", "当前桥接宿主未提供识海深潜棋盘读取。")
            result = self._client.request("deep_dive_board", {"turn_only": kind == "turn",
                                          "controls_only": kind == "controls"},
                                          viewport=self._viewport, timeout=3.0)["result"]
        board = result.get("board")
        schema = f"resonance_pc.{capability}.v1"
        if not isinstance(board, dict) or board.get("schema") != schema:
            raise TargetRuntimeError("deep_dive_board_invalid", "桥接器返回了无效的棋盘快照。")
        return dict(board, frame=result["frame"])

    def open_frame_stream(self):
        stream = self.active_frame_stream(current_root_cid())
        if stream is not None:
            return stream
        with self._lock:
            self._ready()
            if self._frame_stream is None:
                stale_ms = resolve_runtime_config(self.config).capture.max_stale_ms
                stream = BridgeFrameStream(self._client.session, max_stale_ms=stale_ms)
                try:
                    self._client.request("stream_start", viewport=self._viewport, timeout=2)
                except BaseException:
                    stream.close()
                    raise
                self._frame_stream = stream
                log_event("capture.bridge_started", stream_id=stream.session_id)
            return self._frame_stream

    def active_frame_stream(self, root_cid):
        stream = self._frame_stream
        return stream if root_cid == self._session_root_cid else None

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
        with activity("input", "release_all", backend="bridge"), self._lock:
            if self._client:
                self._accept(self._client.request("release_all", viewport=self._viewport))
                self._held.clear()
            log_event("input.release_result", backend="bridge", success=not self._held,
                      remaining_mouse_buttons=sorted(self._held))

    def cancel_input(self):
        with activity("input", "cancel_input", backend="bridge"):
            return self._cancel_input()

    def _cancel_input(self):
        with self._state_lock:
            self._cancel_epoch += 1
            client = self._client
        if client:
            self._accept(client.request("cancel", viewport=self._viewport))
            client.uncertain = False
        self._held.clear()
        log_event("input.release_result", backend="bridge", success=True, remaining_mouse_buttons=[])

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

        task = asyncio.create_task(tracked_to_thread(invoke))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            token.set()
            cleanup = asyncio.create_task(tracked_to_thread(self.cancel_input))
            try:
                await asyncio.shield(cleanup)
            finally:
                try:
                    await asyncio.shield(task)
                except (TargetRuntimeError, asyncio.CancelledError):
                    pass
            raise

    def close_for_task(self, cid):
        with self._lock:
            if self._session_root_cid == cid:
                self.close()

    def close(self, *, unload=False):
        with activity("input", "close", backend="bridge"), self._lock:
            client = self._client
            stream = self._frame_stream
            if stream is not None:
                # Reject new reads before stopping native production. Keep the
                # client/session on a lost close acknowledgement so it can retry.
                stream.stop_reading()
            if client:
                try:
                    if stream is not None:
                        client.request("stream_stop", viewport=self._viewport, timeout=2.0)
                except TargetRuntimeError as exc:
                    log_event("capture.bridge_stop_unconfirmed", code=exc.code)
                try:
                    client.request("close", viewport=self._viewport, timeout=2.0)
                except TargetRuntimeError as exc:
                    # A lost close acknowledgement is followed by session_invalid.
                    # A process that has exited cannot still own virtual input.
                    if exc.code != "session_invalid" and psutil.pid_exists(client.pid):
                        raise
            if stream is not None:
                stream.close()
                self._frame_stream = None
                log_event("capture.bridge_closed", stream_id=stream.session_id)
            self._client = None
            self._session_root_cid = None
            self._held.clear()
            self._position = None
            self._identity = None
            self._needs_hello = True
            self._capabilities.clear()
            if unload and self._last_host is not None:
                host = self._last_host
                self._last_host = None
                try:
                    host.request("unload_host", {"host_instance": host.host_instance}, timeout=2.0)
                except TargetRuntimeError as exc:
                    if exc.code not in {"session_invalid", "input_bridge_unavailable", "input_session_busy"}:
                        log_event("input.bridge_unload_unconfirmed", code=exc.code)

    def shutdown(self):
        """Framework lifecycle hook: cancel first, so shutdown does not wait for a long drag."""
        try:
            self.cancel_input()
        finally:
            self.close(unload=True)
