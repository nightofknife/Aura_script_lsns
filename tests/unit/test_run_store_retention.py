"""Durable retention protects whole task families and concurrent event ingestion."""
import asyncio
import json
import sqlite3
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.aura_core.observability.run_store import RunStore


NOW = 2_000_000_000_000
HOUR = 3_600_000
OLD = NOW - 100 * HOUR


@pytest.fixture
def store(tmp_path):
    value = RunStore(tmp_path / "runs.sqlite3")
    yield value
    value.close()


def add(store, cid, parent=None, status="success", stamp=OLD, node=True):
    store.apply_event("task.started", {"cid": cid, "parent_cid": parent, "start_time": stamp}, stamp)
    if node:
        store.apply_event("node.finished", {"cid": cid, "node_id": "one"}, stamp)
    if status != "running":
        store.apply_event("task.finished", {"cid": cid, "parent_cid": parent, "status": status,
                                           "end_time": stamp}, stamp)


def prune(store, **kwargs):
    return store.prune_expired(now_ms=NOW, transaction_ms=1000, **kwargs)


def test_expired_family_nodes_and_owned_snapshot_removed(store, tmp_path):
    add(store, "root")
    add(store, "child", "root")
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    for name in ("root.json", "child.json", "manual.json"):
        (snapshots / name).write_text("{}")
    result = prune(store, snapshot_dir=snapshots)
    assert result["deleted_runs"] == result["deleted_nodes"] == result["deleted_snapshots"] == 2
    assert store.list_runs() == []
    assert (snapshots / "manual.json").exists()
    assert store._conn.execute("SELECT COUNT(*) FROM node_terminal_events").fetchone()[0] == 0


@pytest.mark.parametrize("status,stamp", [("running", OLD), ("success", NOW-HOUR)])
def test_active_or_recent_relative_protects_entire_family(store, status, stamp):
    add(store, "root")
    add(store, "old-sibling", "root")
    add(store, "protected-child", "root", status=status, stamp=stamp)
    assert prune(store)["deleted_runs"] == 0
    assert len(store.list_runs()) == 3


def test_recent_node_and_missing_parent_sibling_protect_family(store):
    add(store, "child-a", "absent-parent")
    add(store, "child-b", "absent-parent")
    store.apply_event("node.finished", {"cid": "child-b", "node_id": "late"}, NOW-HOUR)
    assert prune(store)["deleted_runs"] == 0


def test_started_event_without_parent_preserves_queued_family_link(store):
    add(store, "root", status="running")
    store.apply_event("queue.enqueued", {"cid": "child", "parent_cid": "root"}, OLD)
    store.apply_event("task.started", {"cid": "child", "start_time": OLD}, OLD)
    store.apply_event("task.finished", {"cid": "child", "status": "success", "end_time": OLD}, OLD)
    assert store.get_run("child")["parent_cid"] == "root"
    assert prune(store)["deleted_runs"] == 0


def test_large_family_progresses_leaf_first_without_orphans(store):
    add(store, "root")
    for i in range(505):
        add(store, f"child-{i:04}", "root", node=False)
    result = prune(store)
    assert result["deleted_runs"] == 500
    assert store.get_run("root")
    assert store._conn.execute("SELECT COUNT(*) FROM runs r WHERE parent_cid IS NOT NULL "
                               "AND NOT EXISTS(SELECT 1 FROM runs p WHERE p.cid=r.parent_cid)").fetchone()[0] == 0
    # The scan cursor can spend one bounded call wrapping to the beginning.
    for _ in range(4):
        prune(store)
    assert store.list_runs() == []


def test_other_connection_write_lease_skips_promptly(store):
    add(store, "old")
    other = sqlite3.connect(str(store._db_path))
    try:
        other.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        assert prune(store)["status"] == "busy"
        assert time.monotonic() - started < .5
        assert store.get_run("old")
    finally:
        other.rollback()
        other.close()
    assert prune(store)["deleted_runs"] == 1


def test_failure_rolls_back_runs_nodes_and_tombstones(store):
    add(store, "old")
    store._conn.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON runs BEGIN SELECT RAISE(ABORT,'test rollback'); END")
    store._conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="test rollback"):
        prune(store)
    assert store.get_run("old")["nodes"]
    assert store._conn.execute("SELECT COUNT(*) FROM expired_runs").fetchone()[0] == 0
    assert store._conn.execute("SELECT watermark_ms FROM retention_state").fetchone()[0] == 0


def test_transaction_budget_interrupt_does_not_partially_delete(store, monkeypatch):
    from packages.aura_core.observability import run_store
    for i in range(30):
        add(store, f"old-{i}")
    ticks = iter([0.0] + [100.0] * 100)
    monkeypatch.setattr(run_store.time, "monotonic", lambda: next(ticks, 100.0))
    result = store.prune_expired(now_ms=NOW, transaction_ms=1)
    assert result["status"] == "budget"
    assert len(store.list_runs(limit=100)) == 30
    assert store._conn.execute("SELECT COUNT(*) FROM node_terminal_events").fetchone()[0] == 30


def test_delayed_events_cannot_recreate_expired_tasks_or_nodes(store):
    add(store, "old")
    assert prune(store)["deleted_runs"] == 1
    second = RunStore(store._db_path)
    try:
        for name in ("queue.enqueued", "task.started", "task.finished", "node.finished"):
            assert second.apply_event(name, {"cid": "old"}, NOW) is False
        assert second.apply_event("task.started", {"cid": "late-child", "parent_cid": "old"}, NOW) is False
        assert second.apply_event("node.finished", {"cid": "unknown", "node_id": "late"}, NOW) is False
    finally:
        second.close()
    assert store.list_runs() == []
    assert store._conn.execute("SELECT COUNT(*) FROM node_terminal_events").fetchone()[0] == 0


def test_tombstones_expire_but_watermark_rejects_old_event_replay(store):
    add(store, "old")
    prune(store)
    store.prune_expired(now_ms=NOW+100*HOUR, transaction_ms=1000)
    assert store._conn.execute("SELECT COUNT(*) FROM expired_runs").fetchone()[0] == 0
    assert store.apply_event("queue.enqueued", {"cid": "old"}, OLD) is False
    assert store.apply_event("task.finished", {"cid": "old"}, NOW+100*HOUR) is False


def test_legacy_orphan_nodes_pruned_and_snapshot_cannot_return(store, tmp_path):
    store._conn.execute("INSERT INTO node_terminal_events(cid,node_id,loop_index,source_event,updated_at_ms) VALUES('orphan','n',0,'node.finished',?)", (OLD,))
    store._conn.commit()
    assert prune(store)["deleted_nodes"] == 1
    assert not store.persist_snapshot(tmp_path, "orphan", {})
    assert not (tmp_path / "orphan.json").exists()
    assert RunStore._snapshot_path(tmp_path, "../outside") is None


def test_snapshot_links_are_not_followed(store, tmp_path, monkeypatch):
    add(store, "old")
    target = tmp_path / "preserved.json"
    target.write_text("preserve")
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    try:
        (snapshots / "old.json").symlink_to(target)
    except OSError:
        # Windows may not grant symlink creation; simulate the junction/reparse
        # metadata returned by lstat while retaining an actual protected file.
        (snapshots / "old.json").write_text("linked-file")
        original = Path.lstat
        def lstat(path, *args, **kwargs):
            if path == snapshots / "old.json":
                return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "lstat", lstat)
    assert not store.persist_snapshot(snapshots, "old", {})
    assert prune(store, snapshot_dir=snapshots)["deleted_snapshots"] == 0
    assert target.read_text() == "preserve"


def test_scan_cursor_prevents_old_protected_family_starving_other_families(store):
    add(store, "a-root", status="running")
    for i in range(6):
        add(store, f"a-child-{i}", "a-root")
    add(store, "z-expired")
    for _ in range(5):
        prune(store, batch_size=2)
    assert not store.get_run("z-expired")
    assert len(store.list_runs()) == 7


def test_idle_service_constructor_expires_history_without_starting_runtime(tmp_path, monkeypatch):
    from packages.aura_core.observability import service
    monkeypatch.setattr(service, "get_config_value", lambda key, default=None: default)
    seed = RunStore(tmp_path / "logs" / "runs" / "run_store.sqlite3")
    add(seed, "expired", stamp=int(time.time()*1000)-100*HOUR)
    seed.close()
    value = service.ObservabilityService(None, tmp_path)
    worker = value._history_retention_thread
    try:
        deadline = time.monotonic()+3
        while value.run_store.get_run("expired") and time.monotonic() < deadline:
            time.sleep(.01)
        assert not value.run_store.get_run("expired")
        assert value._cleanup_task is None  # No asyncio/scheduler startup needed.
        assert worker.is_alive()
    finally:
        value.run_store.close()
    assert not worker.is_alive()
    value.run_store.close()
    value.start_history_retention()
    assert not worker.is_alive() and value.run_store.closed


def test_idle_service_periodic_retention_and_store_close_without_asyncio(tmp_path, monkeypatch):
    from packages.aura_core.observability import service
    calls = []
    second = threading.Event()
    def prune(self):
        calls.append(time.monotonic())
        if len(calls) >= 2:
            second.set()
    monkeypatch.setattr(service.ObservabilityService, "_prune_run_history", prune)
    monkeypatch.setattr(service, "get_config_value", lambda key, default=None:
                        1 if key == "observability.runs.cleanup_interval_seconds" else default)
    value = service.ObservabilityService(None, tmp_path)
    worker = value._history_retention_thread
    try:
        value.start_history_retention()
        assert value._history_retention_thread is worker
        assert second.wait(3)
        assert value._cleanup_task is None
    finally:
        value.run_store.close()
    assert not worker.is_alive()


def test_async_memory_cleanup_reuses_and_stops_history_worker(tmp_path, monkeypatch):
    from packages.aura_core.observability import service
    monkeypatch.setattr(service, "get_config_value", lambda key, default=None: default)
    value = service.ObservabilityService(None, tmp_path)
    worker = value._history_retention_thread
    async def scenario():
        value.start_cleanup_task()
        try:
            assert value._history_retention_thread is worker
            assert value._cleanup_task is not None
        finally:
            await value.stop_cleanup_task()
        assert not worker.is_alive()
    try:
        asyncio.run(scenario())
    finally:
        value.run_store.close()


def test_store_close_signals_and_joins_in_progress_history_sweep(tmp_path, monkeypatch):
    from packages.aura_core.observability import service
    started = threading.Event()
    def prune(self):
        started.set()
        assert self._history_retention_stop.wait(3)
    monkeypatch.setattr(service, "get_config_value", lambda key, default=None: default)
    monkeypatch.setattr(service.ObservabilityService, "_prune_run_history", prune)
    value = service.ObservabilityService(None, tmp_path)
    worker = value._history_retention_thread
    try:
        assert started.wait(3)
    finally:
        value.run_store.close()
    assert not worker.is_alive()
    assert value.run_store.closed


def test_runner_reset_stops_runtime_before_history_and_database(monkeypatch):
    from packages.aura_game import runner
    calls = []
    fake = SimpleNamespace(observability=SimpleNamespace(
        close_history_retention=lambda: calls.append("history"),
        run_store=SimpleNamespace(close=lambda: calls.append("store")),
    ))
    monkeypatch.setattr(runner, "peek_runtime", lambda: fake)
    monkeypatch.setattr(runner, "stop_runtime", lambda: calls.append("runtime"))
    for name in ("ACTION_REGISTRY", "service_registry", "hook_manager"):
        monkeypatch.setattr(runner, name, SimpleNamespace(clear=lambda name=name: calls.append(name)))
    runner._reset_global_runtime_state()
    assert calls == ["runtime", "history", "store", "ACTION_REGISTRY", "service_registry", "hook_manager"]
    calls.clear()
    monkeypatch.setattr(runner, "peek_runtime", lambda: SimpleNamespace())
    runner._reset_global_runtime_state()  # Test doubles without observability remain supported.
    assert calls[0] == "runtime"
