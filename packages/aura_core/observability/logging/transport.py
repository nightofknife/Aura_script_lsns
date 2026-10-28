"""Bounded, dedicated local logging transport. Never uses the task RPC pipe."""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from multiprocessing.connection import Client, Listener
from uuid import uuid4

MAX_PACKET_BYTES = 16 * 1024 * 1024
QUEUE_SIZE = 8192
_emergency_stream = None


def set_emergency_stream(stream):
    global _emergency_stream
    _emergency_stream = stream


def emergency(message: str) -> None:
    """Report transport failure without re-entering logging or captured streams."""
    stream = _emergency_stream or sys.__stderr__
    if stream is not None:
        try:
            stream.write("[AuraLogTransport] " + message + "\n")
            stream.flush()
        except Exception:
            pass


def encode(value: dict) -> bytes:
    data = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
    if len(data) > MAX_PACKET_BYTES:
        raise ValueError("Log record exceeds the 16 MiB transport limit")
    return data


def receive(connection) -> dict:
    value = json.loads(connection.recv_bytes(MAX_PACKET_BYTES).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Expected logging envelope")
    return value


def make_listener():
    family = "AF_PIPE" if os.name == "nt" else "AF_INET"
    address = rf"\\.\pipe\aura-log-{os.getpid()}-{uuid4().hex}" if os.name == "nt" else ("127.0.0.1", 0)
    key = os.urandom(32)
    # Authenticate in each client thread, not inside the sole accept loop.
    listener = Listener(address, family=family, authkey=None)
    return listener, {"family": family, "address": listener.address, "authkey": key.hex()}


def connect(endpoint):
    address = endpoint["address"]
    if endpoint["family"] == "AF_INET":
        address = tuple(address)
    return Client(address, family=endpoint["family"], authkey=bytes.fromhex(endpoint["authkey"]))


class RemoteProducer:
    """Only this daemon thread can block on OS pipe I/O, never an input thread."""

    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.queue = queue.Queue(maxsize=QUEUE_SIZE)
        self.error = None
        self.dropped = 0
        self.closed = False
        self.connection = None
        self.thread = threading.Thread(target=self._run, name="aura-log-sender", daemon=True)
        self.thread.start()

    def submit(self, record):
        if self.closed or self.error:
            emergency("Logging unavailable: " + str(self.error or "producer closed"))
            return False
        try:
            self.queue.put_nowait(("record", record, None))
            return True
        except queue.Full:
            self.dropped += 1
            if self.dropped == 1:
                emergency("Logging queue full; records lost; input cleanup remains unblocked")
            return False

    def _run(self):
        active = None
        try:
            self.connection = connect(self.endpoint)
            while True:
                kind, payload, active = self.queue.get()
                if self.dropped:
                    self.connection.send_bytes(encode({"op": "overflow", "count": self.dropped, "pid": os.getpid()}))
                    self.dropped = 0
                if kind == "record":
                    self.connection.send_bytes(encode({"op": "record", "record": payload}))
                else:
                    self.connection.send_bytes(encode({"op": "flush"}))
                    reply = receive(self.connection)
                    if not reply.get("ok"):
                        raise OSError(reply.get("error", "Writer flush failed"))
                    active.set()
                    if kind == "close":
                        break
                active = None
        except Exception as exc:
            self.error = str(exc)
            emergency("Logging channel failed: " + self.error)
        finally:
            if active is not None:
                active.set()
            while True:
                try:
                    _, _, event = self.queue.get_nowait()
                    if event is not None:
                        event.set()
                except queue.Empty:
                    break
            if self.connection is not None:
                self.connection.close()

    def _barrier(self, kind, timeout):
        if self.error or not self.thread.is_alive():
            return False
        deadline = time.monotonic() + timeout
        event = threading.Event()
        try:
            self.queue.put((kind, None, event), timeout=max(0, timeout))
        except queue.Full:
            emergency("Logging flush timed out while queue was full")
            return False
        ok = event.wait(max(0, deadline - time.monotonic())) and self.error is None
        if not ok:
            emergency("Logging flush failed or timed out")
        return ok

    def flush(self, timeout=5):
        return self._barrier("flush", timeout)

    def close(self, timeout=5):
        if self.closed:
            return
        self.closed = True
        self._barrier("close", timeout)


class RemoteSubscriber:
    """A separate, lossy display stream; never shares a producer/RPC channel."""

    def __init__(self, endpoint, callback):
        self.endpoint, self.callback = endpoint, callback
        self.connection = None
        self.closed = False
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, name="aura-log-subscriber", daemon=True)
        self.thread.start()
        self.ready.wait(2)

    def _run(self):
        try:
            self.connection = connect(self.endpoint)
            self.connection.send_bytes(encode({"op": "subscribe"}))
            while not self.closed:
                packet = receive(self.connection)
                if packet.get("op") == "subscribed":
                    self.ready.set()
                elif packet.get("op") == "written":
                    self.callback(packet["record"])
        except (OSError, EOFError):
            pass
        except Exception as exc:
            emergency(f"Display subscription failed: {exc}")
        finally:
            self.ready.set()
            if self.connection is not None:
                self.connection.close()

    def close(self):
        self.closed = True
        if self.connection is not None:
            try:
                self.connection.send_bytes(encode({"op": "unsubscribe"}))
                self.connection.close()
            except OSError:
                pass
