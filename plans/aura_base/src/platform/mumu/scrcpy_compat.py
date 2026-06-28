# -*- coding: utf-8 -*-
from __future__ import annotations

import socket
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

import cv2
import numpy as np
from av.codec import CodecContext

from ..contracts import TargetRuntimeError
from .adb_discovery import AdbController, shell_quote
from .runtime_assets import resolve_scrcpy_server_jar_path

EVENT_INIT = "init"
EVENT_FRAME = "frame"

ACTION_DOWN = 0
ACTION_UP = 1
LOCK_SCREEN_ORIENTATION_UNLOCKED = -1

TYPE_INJECT_KEYCODE = 0


class ControlSender:
    def __init__(self, parent: "Client"):
        self.parent = parent

    def keycode(self, keycode: int, action: int = ACTION_DOWN, repeat: int = 0) -> bytes:
        payload = struct.pack(">B", TYPE_INJECT_KEYCODE) + struct.pack(">Biii", action, int(keycode), int(repeat), 0)
        self.parent._send_control_payload(payload)
        return payload


class Client:
    def __init__(
        self,
        device: Optional[str] = None,
        max_width: int = 0,
        bitrate: int = 8000000,
        max_fps: int = 0,
        flip: bool = False,
        block_frame: bool = False,
        stay_awake: bool = False,
        lock_screen_orientation: int = LOCK_SCREEN_ORIENTATION_UNLOCKED,
        connection_timeout: int = 3000,
        adb_executable: str = "adb",
        server_version: str = "1.24",
        server_jar_path: str | None = None,
    ):
        if not device:
            raise TargetRuntimeError("scrcpy_device_required", "scrcpy client requires an explicit adb serial.")

        self.device = str(device)
        self.listeners: dict[str, list[Callable[..., Any]]] = {EVENT_FRAME: [], EVENT_INIT: []}
        self.last_frame: Optional[np.ndarray] = None
        self.resolution: Optional[Tuple[int, int]] = None
        self.device_name: Optional[str] = None
        self.control = ControlSender(self)

        self.flip = bool(flip)
        self.max_width = int(max_width or 0)
        self.bitrate = int(bitrate or 8000000)
        self.max_fps = int(max_fps or 0)
        self.block_frame = bool(block_frame)
        self.stay_awake = bool(stay_awake)
        self.lock_screen_orientation = int(lock_screen_orientation)
        self.connection_timeout = max(int(connection_timeout or 3000), 1000)
        self.adb_executable = str(adb_executable or "adb")
        self.server_version = str(server_version or "1.24").strip().lstrip("v")
        self.server_jar_path = Path(server_jar_path).resolve() if server_jar_path else resolve_scrcpy_server_jar_path(self.server_version)

        self.alive = False
        self.control_socket: Optional[socket.socket] = None
        self.control_socket_lock = threading.Lock()
        self._adb = AdbController(executable=self.adb_executable)
        self._local_port = _pick_free_port()
        self._video_socket: Optional[socket.socket] = None
        self._server_process: Optional[subprocess.Popen[bytes]] = None
        self._stream_thread: Optional[threading.Thread] = None

    def add_listener(self, event_name: str, callback: Callable[..., Any]) -> None:
        self.listeners.setdefault(str(event_name), []).append(callback)

    def start(self, threaded: bool = False, daemon_threaded: bool = True) -> None:
        if self.alive:
            return
        self._push_server()
        self._start_server()
        self._init_server_connection()
        self.alive = True
        self._emit(EVENT_INIT)
        if threaded:
            self._stream_thread = threading.Thread(target=self._stream_loop, daemon=bool(daemon_threaded))
            self._stream_thread.start()
        else:
            self._stream_loop()

    def stop(self) -> None:
        self.alive = False
        _close_socket(self.control_socket)
        _close_socket(self._video_socket)
        self.control_socket = None
        self._video_socket = None
        self._stop_stale_servers()
        if self._server_process is not None:
            try:
                self._server_process.terminate()
                self._server_process.wait(timeout=2.0)
            except Exception:
                try:
                    self._server_process.kill()
                except Exception:
                    pass
            finally:
                self._server_process = None
        try:
            self._adb.remove_forward(self.device, self._local_port)
        except Exception:
            pass

    def _push_server(self) -> None:
        if not self.server_jar_path.is_file():
            raise TargetRuntimeError(
                "scrcpy_server_missing",
                "scrcpy server jar is missing.",
                {"expected_path": str(self.server_jar_path), "serial": self.device},
            )
        remote_path = f"/data/local/tmp/{self.server_jar_path.name}"
        self._adb.push(self.device, str(self.server_jar_path), remote_path)

    def _start_server(self) -> None:
        remote_path = f"/data/local/tmp/{self.server_jar_path.name}"
        self._stop_stale_servers()
        command_parts = [
            f"CLASSPATH={shell_quote(remote_path)}",
            "app_process",
            "/",
            "com.genymobile.scrcpy.Server",
            self.server_version,
            "log_level=info",
            f"bit_rate={self.bitrate}",
            f"max_size={self.max_width}",
            f"max_fps={self.max_fps}",
            f"lock_video_orientation={self.lock_screen_orientation}",
            "tunnel_forward=true",
            "control=true",
            "display_id=0",
            "show_touches=false",
            f"stay_awake={str(self.stay_awake).lower()}",
            "clipboard_autosync=false",
        ]
        self._adb.remove_forward(self.device, self._local_port)
        self._adb.forward_socket(self.device, self._local_port, "localabstract:scrcpy")
        self._server_process = subprocess.Popen(
            [
                self.adb_executable,
                "-s",
                self.device,
                "shell",
                " ".join(command_parts),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.25)

    def _stop_stale_servers(self) -> None:
        self._adb.shell_script(
            self.device,
            """
            if command -v pkill >/dev/null 2>&1; then
              pkill -f com.genymobile.scrcpy.Server >/dev/null 2>&1 || true
            else
              for pid in $(ps -A | grep 'com.genymobile.scrcpy.Server' | awk '{print $2}'); do
                kill "$pid" >/dev/null 2>&1 || true
              done
            fi
            """,
            timeout_sec=5.0,
            check=False,
        )

    def _init_server_connection(self) -> None:
        deadline = time.monotonic() + self.connection_timeout / 1000.0
        self._video_socket = None
        self.control_socket = None
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                self._video_socket = socket.create_connection(("127.0.0.1", self._local_port), timeout=1.0)
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(0.1)
        if self._video_socket is None:
            raise TargetRuntimeError(
                "scrcpy_stream_unavailable",
                "Failed to connect to the local scrcpy video socket.",
                {"serial": self.device, "local_port": self._local_port, "error": str(last_error) if last_error else None},
            )

        try:
            dummy_byte = self._recv_exact(self._video_socket, 1)
            if dummy_byte != b"\x00":
                raise TargetRuntimeError(
                    "scrcpy_protocol_error",
                    "scrcpy video socket did not send the expected dummy byte.",
                    {"serial": self.device, "received": dummy_byte.hex()},
                )
            self.control_socket = socket.create_connection(("127.0.0.1", self._local_port), timeout=1.0)
            self.device_name = self._recv_exact(self._video_socket, 64).decode("utf-8", errors="ignore").rstrip("\x00")
            if not self.device_name:
                raise TargetRuntimeError(
                    "scrcpy_protocol_error",
                    "scrcpy video socket did not provide a device name.",
                    {"serial": self.device},
                )
            resolution = self._recv_exact(self._video_socket, 4)
            self.resolution = struct.unpack(">HH", resolution)
            self._video_socket.setblocking(False)
        except Exception:
            self.stop()
            raise

    def _stream_loop(self) -> None:
        codec = CodecContext.create("h264", "r")
        while self.alive:
            try:
                if self._video_socket is None:
                    return
                raw_h264 = self._video_socket.recv(0x10000)
                if not raw_h264:
                    time.sleep(0.01)
                    continue
                packets = codec.parse(raw_h264)
                for packet in packets:
                    try:
                        frames = codec.decode(packet)
                    except Exception:
                        if not self.alive:
                            return
                        continue
                    for frame in frames:
                        array = frame.to_ndarray(format="bgr24")
                        if self.flip:
                            array = cv2.flip(array, 1)
                        self.last_frame = array
                        self.resolution = (array.shape[1], array.shape[0])
                        self._emit(EVENT_FRAME, array)
            except BlockingIOError:
                time.sleep(0.01)
                if not self.block_frame:
                    self._emit(EVENT_FRAME, None)
            except OSError:
                if not self.alive:
                    return
                time.sleep(0.02)
            except Exception:
                if not self.alive:
                    return
                time.sleep(0.02)

    def _send_control_payload(self, payload: bytes) -> None:
        if self.control_socket is None:
            raise TargetRuntimeError(
                "scrcpy_control_unavailable",
                "scrcpy control socket is unavailable.",
                {"serial": self.device},
            )
        with self.control_socket_lock:
            self.control_socket.sendall(payload)

    def _emit(self, event_name: str, *args: Any) -> None:
        for callback in list(self.listeners.get(event_name, [])):
            callback(*args)

    @staticmethod
    def _recv_exact(sock: socket.socket, length: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < length:
            chunk = sock.recv(length - len(chunks))
            if not chunk:
                raise ConnectionError("Unexpected EOF from scrcpy socket.")
            chunks.extend(chunk)
        return bytes(chunks)


def _pick_free_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    try:
        return int(probe.getsockname()[1])
    finally:
        probe.close()


def _close_socket(sock: Optional[socket.socket]) -> None:
    if sock is None:
        return
    try:
        sock.close()
    except Exception:
        pass
