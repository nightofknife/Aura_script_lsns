"""Bounded evidence and cancellation diagnostics without a live target."""
import asyncio
from collections import OrderedDict, deque
import json
import os
import queue
import threading
from types import SimpleNamespace

import pytest

from packages.aura_core.observability import runtime_trace as trace
from packages.aura_core.observability.logging.core_logger import set_cid, reset_cid


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    for name, value in {"_events": deque(maxlen=2048), "_active": {}, "_runtime": OrderedDict(),
                        "_stops": {}, "_providers": {}, "_provider_busy": set(),
                        "_throttles": OrderedDict(), "_dump_times": OrderedDict()}.items():
        monkeypatch.setattr(trace, name, value)
    monkeypatch.setenv("AURA_LOG_ROOT", str(tmp_path))
    monkeypatch.setattr(trace, "_monitor_thread", SimpleNamespace(is_alive=lambda: True))
    logs = []
    monkeypatch.setattr(trace.logger, "info", lambda *args, **kw: logs.append(args))
    token = set_cid("trace-test")
    try:
        yield logs
    finally:
        reset_cid(token)


def test_operation_pairing_details_redaction_and_bound(isolated):
    with trace.activity("input", "type_text", backend="sendinput", parameters={"text": "private"}) as op:
        trace.log_event("input.edge", edge="down", key="left")
    rows = list(trace._events)
    queued = rows[0]
    assert queued["fields"]["parameters"] == {"text_length": 7}
    assert rows[1]["activity_id"] == op == rows[2]["activity_id"]
    assert rows[-1]["operation_id"] == op and rows[-1]["status"] == "completed"
    assert rows[-1]["elapsed_ms"] >= 0
    assert trace.pending_activities("trace-test") == []
    for i in range(2100):
        trace.log_event("capture.poll", detail=True, token="secret", index=i)
    assert len(trace._events) == 2048
    assert trace._events[-1]["token"] == "<redacted>"
    assert len(isolated) == 4


def test_child_work_remains_visible_to_root(isolated):
    with trace.activity("node", "parent", cid="root"):
        token = set_cid("child")
        try:
            with trace.activity("worker", "child"):
                assert trace.current_root_cid() == "root"
                trace.update_runtime({"plan": "resonance_pc", "target_pid": 5})
                assert len(trace.pending_activities("root")) == 2
                assert len(trace.pending_activities("child")) == 1
                assert trace._runtime["root"]["target_pid"] == 5
        finally:
            reset_cid(token)
    child_finish = next(row for row in trace._events
                        if row["event"] == "activity.finished" and row["name"] == "child")
    assert child_finish["root_cid"] == "root"


def test_cancelled_awaiter_does_not_hide_running_thread(isolated):
    started, release = threading.Event(), threading.Event()
    def work():
        started.set()
        assert release.wait(3)
    async def scenario():
        task = asyncio.create_task(trace.tracked_to_thread(work))
        try:
            for _ in range(200):
                if started.is_set():
                    break
                await asyncio.sleep(.005)
            assert started.is_set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            pending = trace.pending_activities("trace-test")
            assert len(pending) == 1 and pending[0]["thread_id"] != threading.get_ident()
        finally:
            release.set()
        for _ in range(200):
            if not trace.pending_activities("trace-test"):
                break
            await asyncio.sleep(.005)
        assert not trace.pending_activities("trace-test")
    asyncio.run(scenario())


def test_slow_stop_dump_once_and_never_premature_drained(isolated, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(trace, "time", SimpleNamespace(monotonic=lambda: now[0],
                                                     monotonic_ns=lambda: int(now[0]*1e9)))
    dumps = []
    monkeypatch.setattr(trace, "request_diagnostic", lambda *a, **kw: dumps.append((a, kw)))
    op = trace.start_activity("worker", "blocked")
    trace.request_stop("trace-test")
    trace.request_stop("trace-test")
    trace.note_cancel_observed("trace-test")
    trace.note_cancel_observed("trace-test")
    trace.mark_task_terminal("trace-test")
    now[0] = 2.1
    trace._poll_stops()
    trace._poll_stops()
    assert len(dumps) == 1
    assert "stop.drained" not in [r["event"] for r in trace._events]
    trace.end_activity(op)
    trace._poll_stops()
    now[0] += .11
    trace._poll_stops()
    events = [r["event"] for r in trace._events]
    assert events.count("stop.requested") == events.count("stop.observed") == events.count("stop.drained") == 1


def test_poll_transition_and_periodic_summary_with_full_ring(isolated, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(trace, "time", SimpleNamespace(monotonic=lambda: now[0],
                                                     monotonic_ns=lambda: int(now[0]*1e9)))
    for _ in range(10):
        trace.log_poll("arrival", "traveling", confidence=.1)
    assert len(isolated) == 1 and len(trace._events) == 10
    now[0] = 11
    trace.log_poll("arrival", "traveling")
    trace.log_poll("arrival", "arrived")
    assert len(isolated) == 3


def test_diagnostic_uses_provider_and_records_stack_without_locals(isolated, tmp_path):
    trace.register_diagnostic_provider("target", lambda request:
        {"status": "ok", "foreground_hwnd": 17, "png": b"fake-png", "held_mouse_buttons": []})
    path = trace._write_diagnostic({"cid": "trace-test", "reason": "input_failed"})
    payload = json.loads(path.read_text(encoding="utf-8"))
    snapshot = payload["providers"]["target"]
    assert (path.parent / snapshot["screenshot"]).read_bytes() == b"fake-png"
    assert payload["thread_stacks"]
    assert all("locals" not in frame for item in payload["thread_stacks"] for frame in item["frames"])
    assert path.is_relative_to(tmp_path)


def test_hung_provider_is_bounded_and_not_spawned_again(isolated):
    release = threading.Event()
    calls = []
    def provider(request):
        calls.append(1)
        release.wait(3)
        return {}
    try:
        assert trace._provider_snapshot("hung", provider, {})["status"] == "timeout"
        assert trace._provider_snapshot("hung", provider, {})["status"] == "busy"
        assert calls == [1]
    finally:
        release.set()


def test_input_failure_requests_diagnostic_but_keeps_original_exception(isolated, monkeypatch):
    requests = []
    monkeypatch.setattr(trace, "request_diagnostic", lambda *a, **kw: requests.append((a, kw)))
    with pytest.raises(OSError, match="input failed"):
        with trace.activity("input", "click"):
            raise OSError("input failed")
    assert requests[0][0] == ("input_failed",)
    assert trace._events[-1]["status"] == "failed"
    assert not trace.pending_activities("trace-test")


def test_diagnostic_queue_is_bounded_rate_limited_and_cid_scoped(isolated, monkeypatch):
    pending = queue.Queue(maxsize=8)
    monkeypatch.setattr(trace, "_diagnostics", pending)
    monkeypatch.setattr(trace, "_diagnostic_thread", SimpleNamespace(is_alive=lambda: True))
    trace.log_event("input.edge", cid="other-cid")
    trace.log_event("input.edge", cid="trace-test")
    trace.request_diagnostic("input_failed")
    trace.request_diagnostic("input_failed")
    assert pending.qsize() == 1
    assert all(row["cid"] == "trace-test" for row in pending.queue[0]["events"])
    for i in range(10):
        trace.request_diagnostic("input_failed", cid=f"other-{i}")
    assert pending.qsize() == 8
    assert any(row["event"] == "diagnostic.skipped" for row in trace._events)


def test_retention_only_removes_owned_artifacts(isolated, tmp_path):
    old = tmp_path / "aura-trace-20200101-120000-123-abcdef12.json"
    companion = old.with_name(old.stem + "-0.png")
    foreign = tmp_path / "manual-evidence.json"
    for path in (old, companion, foreign):
        path.write_bytes(b"evidence")
        os.utime(path, (1, 1))
    recent = tmp_path / "aura-trace-20260101-120000-123-abcdef34.json"
    recent.write_bytes(b"new")
    trace._prune_diagnostics(tmp_path)
    assert not old.exists() and not companion.exists()
    assert foreign.exists() and recent.exists()


def test_cancel_reset_stops_monitoring_without_hiding_live_work(isolated):
    op = trace.start_activity("worker", "still-running")
    trace.request_stop("trace-test")
    trace.clear_stop("trace-test")
    trace.mark_task_terminal("trace-test")
    trace._poll_stops()
    assert "trace-test" not in trace._stops
    assert trace.pending_activities("trace-test")
    assert "stop.drained" not in [row["event"] for row in trace._events]
    trace.end_activity(op)
