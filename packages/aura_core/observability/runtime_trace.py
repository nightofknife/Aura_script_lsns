"""Bounded, process-local execution evidence; diagnostics never drive the target."""
from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from contextlib import contextmanager
import contextvars
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time
from uuid import uuid4

from .logging.core_logger import current_cid, logger

_lock = threading.RLock()
_events = deque(maxlen=2048)
_active: dict[str, dict] = {}
_runtime: OrderedDict[str, dict] = OrderedDict()
_stops: dict[str, dict] = {}
_providers: dict[str, object] = {}
_provider_busy: set[str] = set()
_throttles: OrderedDict[tuple, tuple] = OrderedDict()
_dump_times: OrderedDict[tuple, float] = OrderedDict()
_activity_id = contextvars.ContextVar("runtime_trace_activity", default=None)
_root_cid = contextvars.ContextVar("runtime_trace_root_cid", default=None)
_session_id = f"{os.getpid()}-{uuid4().hex[:12]}"
_diagnostics: queue.Queue = queue.Queue(maxsize=8)
_diagnostic_thread = None
_monitor_thread = None


def _cid(value=None) -> str:
    return str(current_cid() if value is None else value)


def current_root_cid() -> str:
    return _root_cid.get() or _cid()


def _safe(value, depth=0):
    if depth > 5:
        return "<depth-limit>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:1000]
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:64]:
            key = str(key)
            if any(word in key.lower() for word in ("password", "secret", "token", "authorization")):
                result[key] = "<redacted>"
            elif key.lower() in {"text", "typed_text"}:
                result[key + "_length"] = len(str(item))
            else:
                result[key] = _safe(item, depth + 1)
        return result
    if isinstance(value, (list, tuple, set)):
        return [_safe(item, depth + 1) for item in list(value)[:128]]
    return f"<{type(value).__name__}>"


def log_event(event: str, cid=None, detail=False, root_cid=None, **fields) -> dict:
    """Detail goes to the bounded ring; ordinary events also go to the log."""
    row = {**_safe(fields), "event": event, "cid": _cid(cid), "session_id": _session_id,
           "pid": os.getpid(), "thread_id": threading.get_ident(),
           "thread_name": threading.current_thread().name,
           "timestamp": datetime.now(timezone.utc).isoformat(),
           "monotonic_ns": time.monotonic_ns(), "activity_id": _activity_id.get(),
           "root_cid": root_cid or _root_cid.get() or _cid(cid)}
    with _lock:
        _events.append(row)
    if not detail:
        try:
            logger.info("[RuntimeTrace] %s", json.dumps(row, ensure_ascii=False, default=str))
        except Exception:
            # Telemetry failure cannot suppress mouse-up or break cancellation.
            pass
    return row


def log_poll(event: str, state, cid=None, interval_sec=10.0, **fields):
    """Keep every observation in memory, log transitions and periodic summaries."""
    cid = _cid(cid)
    key = (cid, event)
    now = time.monotonic()
    fingerprint = json.dumps(_safe(state), sort_keys=True)
    with _lock:
        last = _throttles.get(key)
        publish = last is None or fingerprint != last[0] or now - last[1] >= interval_sec
        count = (last[2] if last else 0) + 1
        _throttles[key] = (fingerprint, now if publish else last[1], 0 if publish else count)
        _throttles.move_to_end(key)
        while len(_throttles) > 256:
            _throttles.popitem(last=False)
    return log_event(event, cid=cid, detail=not publish, state=state,
                     observations_since_summary=count, **fields)


def start_activity(kind: str, name: str, cid=None, **fields) -> str:
    ident = uuid4().hex[:16]
    row = {**_safe(fields), "id": ident, "kind": kind, "name": name, "cid": _cid(cid),
           "parent_activity_id": _activity_id.get(), "root_cid": _root_cid.get() or _cid(cid),
           "started_ns": time.monotonic_ns(),
           "thread_id": None, "state": "queued"}
    with _lock:
        _active[ident] = row
    log_event("activity.queued", cid=row["cid"], operation_id=ident, kind=kind, name=name,
              root_cid=row["root_cid"], parent_activity_id=row["parent_activity_id"], fields=fields)
    return ident


@contextmanager
def bind_activity(ident: str):
    token = _activity_id.set(ident)
    root_token = None
    with _lock:
        row = _active.get(ident)
        if row:
            row.update(thread_id=threading.get_ident(), state="running")
            row = dict(row)
    if row:
        root_token = _root_cid.set(row["root_cid"])
        log_event("activity.started", cid=row["cid"], operation_id=ident,
                  kind=row["kind"], name=row["name"])
    try:
        yield ident
    finally:
        if root_token is not None:
            _root_cid.reset(root_token)
        _activity_id.reset(token)


def end_activity(ident: str, status="completed", **fields):
    with _lock:
        row = _active.pop(ident, None)
    if row is None:
        return
    log_event("activity.finished", cid=row["cid"], operation_id=ident, kind=row["kind"],
              name=row["name"], status=status, root_cid=row["root_cid"],
              elapsed_ms=round((time.monotonic_ns() - row["started_ns"]) / 1e6, 3), **fields)


@contextmanager
def activity(kind: str, name: str, cid=None, **fields):
    ident = start_activity(kind, name, cid=cid, **fields)
    status, error = "completed", None
    try:
        with bind_activity(ident):
            yield ident
    except BaseException as exc:
        status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
        error = {"type": type(exc).__name__, "message": str(exc)[:500]}
        if kind == "input" and status == "failed":
            request_diagnostic("input_failed", cid=cid, operation_id=ident, error=error)
        raise
    finally:
        end_activity(ident, status=status, error=error)


async def tracked_to_thread(function, /, *args, **kwargs):
    """Register queued work before dispatch, retain it until the thread exits."""
    ident = start_activity("worker", getattr(function, "__qualname__", type(function).__name__))
    context = contextvars.copy_context()

    def invoke():
        status, error = "completed", None
        try:
            with bind_activity(ident):
                return function(*args, **kwargs)
        except BaseException as exc:
            status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
            error = type(exc).__name__
            raise
        finally:
            end_activity(ident, status=status, error=error)

    try:
        future = asyncio.get_running_loop().run_in_executor(None, context.run, invoke)
    except BaseException:
        end_activity(ident, status="dispatch_failed")
        raise
    # Retrieve exceptions even when the awaiting task was cancelled first.
    future.add_done_callback(lambda f: None if f.cancelled() else f.exception())
    return await asyncio.shield(future)


def pending_activities(cid) -> list[dict]:
    now = time.monotonic_ns()
    with _lock:
        return [{**row, "elapsed_ms": round((now - row["started_ns"]) / 1e6, 3)}
                for row in _active.values() if _cid(cid) in (row["cid"], row["root_cid"])]


def update_runtime(fields: dict, cid=None):
    cid = _cid(cid)
    clean = _safe(fields)
    with _lock:
        previous = _runtime.get(cid, {})
        merged = {**previous, **clean}
        changed = merged != previous
        _runtime[cid] = merged
        _runtime.move_to_end(cid)
        while len(_runtime) > 64:
            _runtime.popitem(last=False)
        root = _root_cid.get()
        if root and root != cid:
            _runtime[root] = {**_runtime.get(root, {}), **clean}
            _runtime.move_to_end(root)
            while len(_runtime) > 64:
                _runtime.popitem(last=False)
    if changed:
        try:
            from packages.resonance_gui import __version__
        except ImportError:
            __version__ = "unknown"
        log_event("runtime.config", cid=cid, version=__version__, runtime=merged)


def request_stop(cid, **metadata):
    global _monitor_thread
    cid = _cid(cid)
    with _lock:
        if cid in _stops:
            return
        _stops[cid] = {"started": time.monotonic(), "root_terminal": False,
                       "observed": False, "dumped": False, "quiet_since": None}
        log_event("stop.requested", cid=cid, pending=pending_activities(cid), **metadata)
        if _monitor_thread is None or not _monitor_thread.is_alive():
            _monitor_thread = threading.Thread(target=_monitor_stops, name="aura-stop-monitor", daemon=True)
            _monitor_thread.start()


def note_cancel_observed(cid):
    with _lock:
        state = _stops.get(_cid(cid))
        if state is None or state["observed"]:
            return
        state["observed"] = True
    log_event("stop.observed", cid=cid)


def clear_stop(cid):
    """A reset withdraws monitoring; it is not evidence that workers exited."""
    with _lock:
        removed = _stops.pop(_cid(cid), None)
    if removed is not None:
        log_event("stop.reset", cid=cid, pending=pending_activities(cid))


def mark_task_terminal(cid, **metadata):
    with _lock:
        state = _stops.get(_cid(cid))
        if state is not None:
            state["root_terminal"] = True
    log_event("task.terminal", cid=cid, pending=pending_activities(cid), **metadata)


def _monitor_stops():
    global _monitor_thread
    while True:
        with _lock:
            if not _stops:
                _monitor_thread = None
                return
            items = list(_stops.items())
        _poll_stops(items)
        # Keep the monitor independent of task-specific sleep adapters/fake clocks.
        threading.Event().wait(0.05)


def _poll_stops(items=None):
    if items is None:
        with _lock:
            items = list(_stops.items())
    for cid, state in items:
        pending = pending_activities(cid)
        now = time.monotonic()
        elapsed = round((now - state["started"]) * 1000, 3)
        if state["root_terminal"] and not pending:
            # Allow completion callbacks to register their downstream work.
            if state["quiet_since"] is None:
                state["quiet_since"] = now
            elif now - state["quiet_since"] >= 0.1:
                log_event("stop.drained", cid=cid, elapsed_ms=elapsed, pending_count=0,
                          meaning="registered_execution_exited; see input.release for release results")
                with _lock:
                    _stops.pop(cid, None)
                continue
        else:
            state["quiet_since"] = None
        if elapsed >= 2000 and not state["dumped"]:
            state["dumped"] = True
            log_event("stop.slow", cid=cid, elapsed_ms=elapsed,
                      root_terminal=state["root_terminal"], pending=pending)
            request_diagnostic("stop_slow", cid=cid)
        if elapsed >= 2000:
            log_poll("stop.pending", [row["id"] for row in pending], cid=cid,
                     elapsed_ms=elapsed, root_terminal=state["root_terminal"], pending=pending)


def register_diagnostic_provider(name: str, callback):
    with _lock:
        _providers[name] = callback


def _log_root() -> Path:
    configured = os.environ.get("AURA_LOG_ROOT")
    if configured:
        return Path(configured)
    for handler in logger.logger.handlers:
        filename = getattr(handler, "baseFilename", None)
        if filename:
            return Path(filename).parent
    return Path(os.environ.get("AURA_BASE_PATH") or Path.cwd()) / "logs"


def request_diagnostic(reason: str, cid=None, **metadata):
    """Bound queue and per-CID rate; never capture synchronously on input path."""
    global _diagnostic_thread
    cid = _cid(cid)
    now = time.monotonic()
    key = (cid, reason)
    with _lock:
        if now - _dump_times.get(key, -1e9) < 10:
            return
        _dump_times[key] = now
        _dump_times.move_to_end(key)
        while len(_dump_times) > 128:
            _dump_times.popitem(last=False)
        request = {"cid": cid, "reason": reason, "metadata": _safe(metadata),
                   "runtime": dict(_runtime.get(cid, {})), "session_id": _session_id,
                   "events": [dict(row) for row in _events if cid in (row["cid"], row["root_cid"])][-256:],
                   "pending": pending_activities(cid)}
        try:
            _diagnostics.put_nowait(request)
        except queue.Full:
            log_event("diagnostic.skipped", cid=cid, reason="queue_full")
            return
        if _diagnostic_thread is None or not _diagnostic_thread.is_alive():
            _diagnostic_thread = threading.Thread(target=_diagnostic_worker,
                                                  name="aura-diagnostic-writer", daemon=True)
            _diagnostic_thread.start()


def _thread_stacks() -> list[dict]:
    names = {t.ident: t.name for t in threading.enumerate()}
    result = []
    for ident, frame in sys._current_frames().items():
        frames = []
        while frame is not None and len(frames) < 32:
            frames.append({"file": frame.f_code.co_filename, "line": frame.f_lineno,
                           "function": frame.f_code.co_name})
            frame = frame.f_back
        result.append({"thread_id": ident, "name": names.get(ident), "frames": frames})
    return result


def _provider_snapshot(name, provider, request):
    with _lock:
        if name in _provider_busy:
            return {"status": "busy"}
        _provider_busy.add(name)
    response = queue.Queue(maxsize=1)
    def invoke():
        try:
            response.put(provider(request))
        except Exception as exc:
            response.put({"status": "failed", "error": str(exc)[:500]})
        finally:
            with _lock:
                _provider_busy.discard(name)
    threading.Thread(target=invoke, name="aura-diagnostic-provider", daemon=True).start()
    try:
        return response.get(timeout=0.5)
    except queue.Empty:
        return {"status": "timeout"}


def _write_diagnostic(request):
    directory = _log_root() / "diagnostics"
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"aura-trace-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{uuid4().hex[:8]}"
    payload = {**request, "thread_stacks": _thread_stacks(), "providers": {}}
    with _lock:
        providers = list(_providers.items())
    for index, (name, provider) in enumerate(providers):
        snapshot = _provider_snapshot(name, provider, request)
        if not isinstance(snapshot, dict):
            snapshot = {"status": "invalid_provider_result"}
        else:
            snapshot = dict(snapshot)
        png = snapshot.pop("png", None)
        if isinstance(png, bytes) and len(png) <= 10 * 1024 * 1024:
            path = directory / f"{stem}-{index}.png"
            path.write_bytes(png)
            snapshot["screenshot"] = path.name
        payload["providers"][name] = _safe(snapshot)
    path = directory / f"{stem}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log_event("diagnostic.saved", cid=request["cid"], reason=request["reason"], path=str(path))
    _prune_diagnostics(directory)
    return path


def _prune_diagnostics(directory):
    """Retain only our own named bundles: 72 hours and at most 128 bundles."""
    pattern = re.compile(r"^aura-trace-\d{8}-\d{6}-\d+-[0-9a-f]{8}(?:-\d+)?\.(?:json|png)$")
    files = []
    for path in directory.iterdir():
        if pattern.fullmatch(path.name) and path.is_file():
            try:
                files.append((path, path.stat().st_mtime))
            except OSError:
                pass
    bundles = sorted(((p, stamp) for p, stamp in files if p.suffix == ".json"),
                     key=lambda pair: pair[1], reverse=True)
    retired = {p.stem for p, stamp in bundles[128:]}
    cutoff = time.time() - 72 * 3600
    for path, stamp in files:
        stem = path.stem if path.suffix == ".json" else path.stem.rsplit("-", 1)[0]
        if stamp < cutoff or stem in retired:
            try:
                path.unlink()
            except OSError:
                pass


def _diagnostic_worker():
    while True:
        request = _diagnostics.get()
        try:
            _write_diagnostic(request)
        except Exception as exc:
            log_event("diagnostic.failed", cid=request["cid"], error=str(exc))
        finally:
            _diagnostics.task_done()
