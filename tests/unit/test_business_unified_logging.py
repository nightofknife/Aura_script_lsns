"""Diagnostic producers share framework logs and leased PNG attachments."""
import json
from types import SimpleNamespace

import numpy as np
import pytest

from packages.aura_core.observability import failure_evidence, runtime_trace
from packages.aura_core.observability.logging import coordinator
from plans.resonance_pc.src.actions import _deep_dive_four_view_diagnostics as four_view
from plans.resonance_pc.src.actions import consciousness_deep_dive_planned_run_pc_actions as planned


@pytest.fixture
def owned_session(monkeypatch, tmp_path):
    session = SimpleNamespace(root=tmp_path, session_id="diagnostic-session", available=True)
    monkeypatch.setattr(failure_evidence, "get_log_session", lambda: session)
    monkeypatch.setattr(runtime_trace, "get_log_session", lambda: session)
    monkeypatch.setattr(coordinator, "get_current_log_path", lambda: str(tmp_path / "aura_session_test.log"))
    return session


def test_png_is_confined_and_never_overwrites_existing_evidence(owned_session):
    first = failure_evidence.save_evidence_png(b"first", evidence_id="same")
    second = failure_evidence.save_evidence_png(b"second", evidence_id="same")
    escaped = failure_evidence.save_evidence_png(b"escape", evidence_id="../../outside")
    assert first["status"] == "saved"
    assert second["status"] == escaped["status"] == "failed"
    assert (owned_session.root / first["screenshot"]).read_bytes() == b"first"
    assert list(owned_session.root.rglob("*.png")) == [owned_session.root / first["screenshot"]]


def test_png_does_not_create_unowned_session(monkeypatch, tmp_path):
    monkeypatch.setattr(failure_evidence, "get_log_session", lambda: None)
    result = failure_evidence.save_evidence_png(b"pixels")
    assert result["status"] == "skipped" and result["reason"] == "log_session_unavailable"


def test_diagnostic_replays_every_buffered_event_with_shared_identity(owned_session, monkeypatch):
    records = []
    monkeypatch.setattr(runtime_trace.logger, "info", lambda fmt, *args: records.append((fmt, args)))
    monkeypatch.setattr(runtime_trace, "_providers", {})
    events = [{"event": "capture", "index": index} for index in range(256)]
    ref = runtime_trace._write_diagnostic({"cid": "task-cid", "reason": "test", "events": events})
    rows = [json.loads(args[0]) for fmt, args in records if fmt == "[RuntimeDiagnostic] %s"]
    replayed = [row for row in rows if row["kind"] == "recent_event"]
    assert [row["observation"] for row in replayed] == events
    assert all(row["session_id"] == owned_session.session_id and row["cid"] == "task-cid" for row in rows)
    assert all(row["evidence_id"] == ref["evidence_id"] for row in rows)
    assert all(row["process_instance_id"] for row in rows)
    assert not list(owned_session.root.rglob("*.json"))


def test_four_view_events_and_metadata_use_only_framework_log(owned_session, monkeypatch):
    records = []
    monkeypatch.setattr(four_view, "get_current_log_path", coordinator.get_current_log_path)
    monkeypatch.setattr(four_view.logger, "info", lambda fmt, *args: records.append((fmt, args)))
    recorder = four_view.FourViewDiagnostics(owned_session.root, run_id="run", scan_epoch="epoch",
                                            started=0, settings={})
    recorder.event("sample", values=np.array([1, 2]))
    recorder.finish(status="blocked", reason="test")
    events = [json.loads(args[0]) for fmt, args in records if fmt == "[DeepDiveFourViewsEvent] %s"]
    assert [row["event"] for row in events] == ["scan_start", "sample", "scan_stop"]
    assert events[1]["values"] == [1, 2]
    assert recorder.metadata["trace_path"] == coordinator.get_current_log_path()
    assert not list(owned_session.root.iterdir())


def test_planned_run_events_carry_cid_without_separate_file(owned_session, monkeypatch):
    records = []
    monkeypatch.setattr(planned.logger, "info", lambda fmt, *args: records.append((fmt, args)))
    state = {"cid": "task", "session_key": "plan-session", "turns_completed": 3}
    planned._log(state, "turn_done", position=np.int64(2))
    assert records[0][0] == "[DeepDivePlannedRun] %s"
    row = json.loads(records[0][1][0])
    assert row["cid"] == "task" and row["session_key"] == "plan-session"
    assert row["position"] == 2 and row["turn"] == 3
    assert not list(owned_session.root.iterdir())
