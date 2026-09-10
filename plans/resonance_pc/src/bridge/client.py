"""Bounded local RPC transport. No request is replayed after an uncertain result."""
from __future__ import annotations

import json
import os
import threading
import time
import uuid

from ....aura_base.src.platform.contracts import TargetRuntimeError


class NamedPipeTransport:
    MAX_MESSAGE = 64 * 1024

    def exchange(self, pipe: str, request: dict, timeout: float) -> dict:
        if os.name != "nt":
            raise TargetRuntimeError("input_bridge_platform_unsupported", "Bridge requires Windows.")
        import pywintypes
        import win32con
        import win32event
        import win32file
        import win32pipe

        deadline = time.monotonic() + timeout
        handle = None
        sent = False

        def transfer(data_or_size, write=False):
            event = win32event.CreateEvent(None, True, False, None)
            overlapped = pywintypes.OVERLAPPED()
            overlapped.hEvent = event
            try:
                if write:
                    _, buffer = win32file.WriteFile(handle, data_or_size, overlapped)
                else:
                    _, buffer = win32file.ReadFile(handle, data_or_size, overlapped)
                remaining = max(int((deadline - time.monotonic()) * 1000), 0)
                if win32event.WaitForSingleObject(event, remaining) != win32con.WAIT_OBJECT_0:
                    win32file.CancelIoEx(handle, overlapped)
                    # The OVERLAPPED and buffer must remain alive until cancellation completes.
                    win32event.WaitForSingleObject(event, win32event.INFINITE)
                    raise TimeoutError("Bridge response deadline expired")
                count = win32file.GetOverlappedResult(handle, overlapped, False)
                return count if write else bytes(buffer[:count])
            finally:
                event.Close()

        try:
            win32pipe.WaitNamedPipe(pipe, max(int(timeout * 1000), 1))
            handle = win32file.CreateFile(pipe, win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                                          0, None, win32con.OPEN_EXISTING,
                                          win32con.FILE_FLAG_OVERLAPPED, None)
            payload = json.dumps(request, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode() + b"\n"
            if len(payload) > self.MAX_MESSAGE:
                raise ValueError("Bridge request too large")
            sent = True
            offset = 0
            while offset < len(payload):
                count = transfer(payload[offset:], True)
                if count <= 0:
                    raise OSError("Bridge pipe closed during write")
                offset += count
            chunks = bytearray()
            while b"\n" not in chunks:
                chunk = transfer(4096)
                if not chunk:
                    raise OSError("Bridge pipe closed before completion")
                chunks.extend(chunk)
                if len(chunks) > self.MAX_MESSAGE:
                    raise ValueError("Bridge response too large")
            result = json.loads(chunks.split(b"\n", 1)[0])
            if not isinstance(result, dict):
                raise ValueError("Bridge response is not an object")
            return result
        except TargetRuntimeError:
            raise
        except Exception as exc:
            code = "input_result_unknown" if sent else "input_bridge_unavailable"
            raise TargetRuntimeError(code, str(exc), {"sequence": request["sequence"]}) from exc
        finally:
            if handle is not None:
                handle.Close()


class BridgeClient:
    def __init__(self, pid: int, transport=None):
        self.pid = int(pid)
        self.pipe = rf"\\.\pipe\resonance-input-bridge-{self.pid}"
        self.session = uuid.uuid4().hex
        self.transport = transport or NamedPipeTransport()
        self._sequence = 0
        self._lock = threading.Lock()
        self.uncertain = False
        self.process_created = None

    def request(self, op: str, args=None, viewport=None, timeout=10.0, cancel_token=None):
        # Only sequence allocation is locked: cancel must run alongside active input RPC.
        with self._lock:
            if cancel_token is not None and cancel_token.is_set():
                raise TargetRuntimeError("input_cancelled", "Queued input was cancelled.")
            self._sequence += 1
            sequence = self._sequence
        request = {"version": 1, "session": self.session, "sequence": sequence,
                   "op": op, "args": args or {}, "viewport": viewport or {},
                   "timeout_ms": max(int(timeout * 1000), 1)}
        if self.process_created is not None:
            request["process_created"] = self.process_created
        try:
            reply = self.transport.exchange(self.pipe, request, timeout)
            if reply.get("version") != 1 or reply.get("sequence") != sequence or reply.get("session") != self.session:
                raise TargetRuntimeError("input_result_unknown", "Mismatched bridge response.")
            created = reply.get("process_created")
            if reply.get("pid") != self.pid or not isinstance(created, str) or not created.isdecimal():
                raise TargetRuntimeError("input_result_unknown", "Missing or invalid bridge process identity.")
            if self.process_created is not None and created != self.process_created:
                raise TargetRuntimeError("input_result_unknown", "Target process was replaced; old session cannot continue.")
            self.process_created = created
            if reply.get("status") != "completed":
                if reply.get("status") == "unknown":
                    self.uncertain = True
                error = reply.get("error") or {}
                if not isinstance(error, dict):
                    error = {"message": str(error)}
                code = error.get("code") or reply.get("code") or (
                    "input_result_unknown" if reply.get("status") == "unknown" else "input_bridge_rejected")
                raise TargetRuntimeError(code, error.get("message") or reply.get("message") or code, reply)
            return reply
        except TargetRuntimeError as exc:
            if exc.code == "input_result_unknown":
                self.uncertain = True
            raise
