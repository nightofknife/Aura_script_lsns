"""Queue timing must never recreate a finished or missing active run."""

import asyncio
import time

import pytest

from packages.aura_core.observability import service
from packages.aura_core.observability.events import Event


@pytest.fixture
def observability(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "get_config_value", lambda _key, default=None: default)
    observer = service.ObservabilityService(None, tmp_path)
    try:
        yield observer
    finally:
        observer.run_store.close()


@pytest.mark.parametrize("queue_first", [False, True])
@pytest.mark.parametrize("status", ["success", "failed", "cancelled"])
def test_queue_completion_keeps_terminal_run_out_of_active_snapshot(observability, queue_first, status):
    cid = "queue-order"
    now = time.time()
    queue_completed = Event(name="queue.completed", payload={"cid": cid, "exec_ms": 10, "queue_wait_ms": 2})
    task_finished = Event(name="task.finished", payload={"cid": cid, "end_time": now, "final_status": status})

    async def ingest():
        await observability.ingest_event(Event(name="task.started", payload={"cid": cid, "start_time": now}))
        events = (queue_completed, task_finished) if queue_first else (task_finished, queue_completed)
        for event in events:
            await observability.ingest_event(event)

    asyncio.run(ingest())

    assert observability.get_active_runs_snapshot() == []
    assert observability.run_store.get_run(cid)["status"] == status
    assert observability._obs_completed[cid]["exec_ms"] == 10
    assert observability._obs_completed[cid]["queue_wait_ms"] == 2


def test_queue_completion_without_a_run_does_not_create_active_snapshot(observability):
    asyncio.run(observability.ingest_event(Event(name="queue.completed", payload={"cid": "unknown", "exec_ms": 10})))
    assert observability.get_active_runs_snapshot() == []
    assert observability.run_store.get_run("unknown") == {}
