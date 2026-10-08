"""Bounded, exact-RGB RPC to an owned isolated DirectML process.

Importing this adapter never imports ONNX Runtime. The normal CPU environment
and its module bindings are not changed by selecting this optional backend.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time
import uuid

import numpy as np

PROTOCOL_VERSION = 1
_MAGIC = b"DDW1"
_FRAME = struct.Struct("!4sII")
_MAX_HEADER = 16_384
_MAX_PAYLOAD = 3_000_000
_RGB_SHAPE = (720, 1280, 3)
_RGB_BYTES = 720 * 1280 * 3
_MAX_PREDICTIONS = 20_000
_CLASSES = ["player_head", "singularity", "inspiration"]


def _read_exact(stream, size):
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            raise EOFError("Entity worker closed its protocol stream.")
        chunks.extend(chunk)
    return bytes(chunks)


def read_frame(stream):
    magic, header_size, payload_size = _FRAME.unpack(_read_exact(stream, _FRAME.size))
    if magic != _MAGIC or not (0 < header_size <= _MAX_HEADER) or payload_size > _MAX_PAYLOAD:
        raise ValueError("Invalid or oversized entity worker frame.")
    header = json.loads(_read_exact(stream, header_size).decode("utf-8"))
    if not isinstance(header, dict) or header.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("Invalid entity worker protocol version/header.")
    return header, _read_exact(stream, payload_size)


def write_frame(stream, header, payload=b""):
    header = dict(header, protocol_version=PROTOCOL_VERSION)
    encoded = json.dumps(header, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if not (0 < len(encoded) <= _MAX_HEADER) or len(payload) > _MAX_PAYLOAD:
        raise ValueError("Oversized entity worker output frame.")
    stream.write(_FRAME.pack(_MAGIC, len(encoded), len(payload)))
    stream.write(encoded)
    stream.write(payload)
    stream.flush()


class EntityDmlWorkerBackend:
    """One serialized request, no worker cache, and explicit lifecycle failures."""

    def __init__(self, model_path, *, config=None,
                 config_prefix="resonance_pc.deep_dive.entity_detector", repository_root=None):
        self.root = Path(repository_root or Path(__file__).resolve().parents[4]).resolve()
        self.model_path = self._resolve(model_path)
        self._config, self._prefix = config, config_prefix
        runtime = self._get("worker.runtime_site", None)
        if not runtime:
            raise ValueError("dml_worker requires explicit worker.runtime_site; the main ORT is never replaced.")
        self.runtime_site = self._resolve(runtime)
        python = self._get("worker.python_executable", None)
        if not python and getattr(sys, "frozen", False):
            raise ValueError("A packaged application requires worker.python_executable for dml_worker.")
        self.python_executable = self._resolve(python or sys.executable)
        self.ort_version = str(self._get("worker.ort_version", "1.24.4"))
        self.device_id = int(self._get("dml_device_id", 1))
        self.intra_threads = int(self._get("session.intra_op_num_threads", 4))
        if self.device_id < 0 or self.intra_threads < 1:
            raise ValueError("DirectML device ID must be nonnegative and thread count positive.")
        self.startup_timeout = self._timeout("worker.startup_timeout_sec", 15)
        self.request_timeout = self._timeout("worker.request_timeout_sec", 5)
        self.close_timeout = self._timeout("worker.close_timeout_sec", 5)
        self._lock = threading.RLock()
        self._cleanup_lock = threading.Lock()
        self._process = self._executor = self._stderr = None
        self._metadata = {}
        self._request_id = 0
        self._broken = None
        self._log_path = None

    def _resolve(self, path):
        path = Path(str(path)).expanduser()
        return (path if path.is_absolute() else self.root / path).resolve()

    def _get(self, suffix, default):
        return default if self._config is None else self._config.get(f"{self._prefix}.{suffix}", default)

    def _timeout(self, suffix, default):
        value = float(self._get(suffix, default))
        if not math.isfinite(value) or not 0 < value <= 60:
            raise ValueError(f"{suffix} must be finite, positive, and at most 60 seconds.")
        return value

    @property
    def ready(self):
        return bool(self._metadata) and self._broken is None and self._process is not None and self._process.poll() is None

    @property
    def status(self):
        return dict(deepcopy(self._metadata), ready=self.ready, error=self._broken,
                    stderr_path=str(self._log_path) if self._log_path else None)

    def _worker_command(self):
        return [str(self.python_executable), "-u", "-m",
                "plans.resonance_pc.src.services._deep_dive_entity_dml_worker"]

    def initialize(self):
        with self._lock:
            if self.ready:
                return self.status
            if self._broken or self._process is not None:
                raise RuntimeError(f"Entity worker is not reusable after a lifecycle failure: {self._broken}")
            if not self.model_path.is_file() or not self.python_executable.is_file():
                raise FileNotFoundError("Entity model or worker Python executable is missing.")
            if not (self.runtime_site / "onnxruntime" / "__init__.py").is_file():
                raise FileNotFoundError("worker.runtime_site must contain the isolated onnxruntime package.")
            self._model_hash = hashlib.sha256(self.model_path.read_bytes()).hexdigest()
            artifact = self.root / ".pytest_tmp" / "deep_dive_dml_workers" / uuid.uuid4().hex
            artifact.mkdir(parents=True)
            self._log_path = artifact / "stderr.log"
            self._stderr = self._log_path.open("wb")
            env = os.environ.copy()
            env.update(TEMP=str(artifact), TMP=str(artifact), TMPDIR=str(artifact),
                       PYTHONUNBUFFERED="1", PYTHONNOUSERSITE="1", PYTHONIOENCODING="utf-8")
            env.pop("PYTHONPATH", None)
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="deep-dive-dml-rpc")
            try:
                self._process = subprocess.Popen(self._worker_command(), cwd=self.root, env=env,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr,
                    bufsize=0, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                header, payload = self._exchange(dict(op="initialize", request_id=0,
                    model_path=str(self.model_path), model_sha256=self._model_hash,
                    runtime_site=str(self.runtime_site), ort_version=self.ort_version,
                    device_id=self.device_id, intra_threads=self.intra_threads), b"", self.startup_timeout)
                if payload or header.get("op") != "initialized" or header.get("request_id") != 0:
                    raise ValueError("Invalid entity worker initialization acknowledgement.")
                self._validate_metadata(header)
                self._metadata = {k: v for k, v in header.items() if k not in ("op", "request_id", "protocol_version")}
                return self.status
            except Exception as exc:
                self._abort(str(exc))
                raise

    def _validate_metadata(self, header):
        actual_path = Path(str(header.get("ort_path", ""))).resolve()
        if (header.get("provider") != "DmlExecutionProvider" or
            not isinstance(header.get("providers"), list) or not header["providers"] or
            header["providers"][0] != "DmlExecutionProvider" or
            header.get("ort_version") != self.ort_version or
            header.get("distribution") != "onnxruntime-directml" or
            header.get("device_id") != self.device_id or
            header.get("model_sha256") != self._model_hash or
            header.get("classes") != _CLASSES or header.get("input_shape") != [1, 3, 640, 640] or
            Path(str(header.get("runtime_site", ""))).resolve() != self.runtime_site or
            not actual_path.is_relative_to(self.runtime_site)):
            raise ValueError("Entity worker actual runtime/provider/model metadata does not match the requested contract.")

    def _exchange(self, header, payload, timeout):
        def transaction():
            write_frame(self._process.stdin, header, payload)
            response, data = read_frame(self._process.stdout)
            if response.get("op") == "error":
                raise RuntimeError(f"Entity worker failed: {response.get('error', 'unspecified')}")
            return response, data
        future = self._executor.submit(transaction)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout as exc:
            self._abort("Entity worker request timed out; owned child terminated.")
            raise TimeoutError(self._broken) from exc

    def run(self, rgb):
        if not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8 or rgb.shape != _RGB_SHAPE:
            raise ValueError("Entity worker requires uint8 RGB [720,1280,3].")
        with self._lock:
            self.initialize()
            started = time.perf_counter()
            snapshot = np.array(rgb, dtype=np.uint8, order="C", copy=True)
            payload = snapshot.tobytes()
            digest = hashlib.sha256(payload).hexdigest()
            self._request_id += 1
            request_id = self._request_id
            try:
                header, data = self._exchange(dict(op="infer", request_id=request_id,
                    rgb_sha256=digest, rgb_shape=list(_RGB_SHAPE), rgb_dtype="uint8",
                    model_sha256=self._model_hash), payload, self.request_timeout)
                raw, letterbox = self._validate_result(header, data, request_id, digest, snapshot)
                diagnostic = dict(header, ipc_total_sec=time.perf_counter()-started)
                return raw, letterbox, diagnostic
            except Exception as exc:
                self._abort(str(exc))
                raise

    def _validate_result(self, header, data, request_id, digest, rgb):
        if (header.get("op") != "result" or header.get("request_id") != request_id or
            header.get("rgb_sha256") != digest or header.get("model_sha256") != self._model_hash or
            header.get("provider") != "DmlExecutionProvider" or header.get("raw_dtype") != "<f4"):
            raise ValueError("Entity worker response has mismatched frame/model/provider identity.")
        shape = header.get("raw_shape")
        if (not isinstance(shape, list) or len(shape) != 3 or shape[:2] != [1, 7] or
            isinstance(shape[2], bool) or not isinstance(shape[2], int) or not 0 < shape[2] <= _MAX_PREDICTIONS or
            len(data) != 4 * 7 * shape[2] or hashlib.sha256(data).hexdigest() != header.get("raw_sha256")):
            raise ValueError("Entity worker returned malformed raw predictions.")
        raw = np.frombuffer(data, dtype="<f4").reshape(shape).copy()
        if not np.isfinite(raw).all():
            raise ValueError("Entity worker returned nonfinite raw predictions.")
        # Lazy shared helper import: no runtime module is loaded by this helper.
        from ._deep_dive_entity_onnx import prepare_rgb_roi
        _, expected_letterbox = prepare_rgb_roi(rgb)
        if header.get("letterbox") != list(expected_letterbox):
            raise ValueError("Entity worker preprocessing disagrees with the source letterbox function.")
        inference_sec = header.get("inference_sec")
        if isinstance(inference_sec, bool) or not isinstance(inference_sec, (float, int)) or not math.isfinite(inference_sec) or inference_sec < 0:
            raise ValueError("Entity worker inference timing is invalid.")
        return raw, expected_letterbox

    def _abort(self, reason):
        # close() may reach its deadline while run() is unwinding. Serialize
        # cleanup, and preserve the original failure rather than masking it.
        with self._cleanup_lock:
            self._broken = reason
            process = self._process
            cleanup_errors = []
            if process is not None:
                try:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    cleanup_errors.append(str(exc))
                for pipe in (process.stdin, process.stdout):
                    if pipe:
                        try:
                            pipe.close()
                        except OSError as exc:
                            cleanup_errors.append(str(exc))
            if self._executor:
                # Once the owned child exits, its pipe read is EOF and drains.
                self._executor.shutdown(wait=True, cancel_futures=True)
                self._executor = None
            if self._stderr:
                try:
                    self._stderr.close()
                except OSError as exc:
                    cleanup_errors.append(str(exc))
                self._stderr = None
            if cleanup_errors:
                self._broken = f"{reason}; cleanup errors: {'; '.join(cleanup_errors)}"

    def close(self):
        acquired = self._lock.acquire(timeout=self.close_timeout)
        if not acquired:
            self._abort("Entity worker close could not drain an active request within its deadline.")
            return dict(drained=False, error=self._broken)
        try:
            if self._process is None:
                return dict(drained=True, error=self._broken)
            if self._broken or self._process.poll() is not None:
                reason = self._broken or "Entity worker exited before close acknowledgement."
                self._abort(reason)
                self._process = None
                self._metadata = {}
                return dict(drained=False, error=reason)
            try:
                self._request_id += 1
                header, payload = self._exchange(dict(op="close", request_id=self._request_id), b"", self.close_timeout)
                if payload or header.get("op") != "closed" or header.get("request_id") != self._request_id or header.get("drained") is not True:
                    raise ValueError("Entity worker failed to acknowledge drained close.")
                self._process.wait(timeout=self.close_timeout)
                if self._process.returncode != 0:
                    raise RuntimeError("Entity worker exited unsuccessfully after close.")
                self._abort("closed")
                self._broken = None
                self._process = None
                self._metadata = {}
                return dict(drained=True, error=None)
            except Exception as exc:
                self._abort(str(exc))
                self._process = None
                self._metadata = {}
                return dict(drained=False, error=str(exc))
        finally:
            self._lock.release()
