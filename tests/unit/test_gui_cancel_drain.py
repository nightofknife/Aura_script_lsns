"""GUI cancellation must wait for the actual synchronous worker to exit."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from packages.aura_core.engine import action_injector
from packages.aura_core.scheduler.cancellation import (
    begin_sync_action, end_sync_action, has_pending_sync_actions, clear_task_cancel,
)
from packages.aura_game.runner import EmbeddedGameRunner
from packages.resonance_gui.bridge import RunnerBridge


def test_cancelled_worker_remains_pending_until_thread_exits(monkeypatch):
    cid = 'test-cancel-drain-worker'
    started, release, exited = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(action_injector, 'current_cid', lambda: cid)
    monkeypatch.setattr(action_injector, 'get_config_value', lambda *args: 0)
    injector = object.__new__(action_injector.ActionInjector)
    injector._prepare_action_arguments = lambda *args: {}

    def worker():
        started.set()
        try:
            assert release.wait(5)
        finally:
            exited.set()

    async def exercise():
        task = asyncio.create_task(injector._invoke_action(
            SimpleNamespace(is_async=False, func=worker, name='blocked'), None, {}))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert has_pending_sync_actions(cid)
        finally:
            release.set()
            assert await asyncio.to_thread(exited.wait, 2)

    try:
        asyncio.run(exercise())
        assert not has_pending_sync_actions(cid)
    finally:
        release.set()
        clear_task_cancel(cid)


def test_runner_reports_live_worker_count_after_terminal_status(monkeypatch):
    cid = 'test-cancel-drain-query'
    runner = object.__new__(EmbeddedGameRunner)
    monkeypatch.setattr(runner, '_ensure_runtime', lambda: SimpleNamespace(
        get_run_detail=lambda cid: {'cid': cid, 'status': 'cancelled'}))
    begin_sync_action(cid)
    begin_sync_action(cid)
    try:
        assert runner.get_run(cid)['execution_pending'] is True
        end_sync_action(cid)
        assert runner.get_run(cid)['execution_pending'] is True
    finally:
        end_sync_action(cid)
    assert runner.get_run(cid)['execution_pending'] is False


def test_gui_stays_busy_until_exit_including_poll_errors(monkeypatch):
    state = {'pending': True, 'error': False}

    def get_run(cid):
        if state['error']:
            raise RuntimeError('temporary IPC failure')
        return {'cid': cid, 'status': 'cancelled', 'execution_pending': state['pending']}

    bridge = RunnerBridge()
    bridge._runner = SimpleNamespace(poll_events=lambda **kw: [], get_run=get_run)
    bridge._current_cid = 'cancelled-task'
    bridge._current_item = {'label': 'test', 'timeout_sec': 0}
    bridge._busy = True
    bridge._cancel_sent = True
    finished = []
    bridge.taskFinished.connect(finished.append)
    monkeypatch.setattr(bridge, 'refresh_history', lambda: None)
    monkeypatch.setattr(bridge, 'refresh_target', lambda: None)
    bridge.poll_current()
    assert bridge.busy and not finished
    state['error'] = True
    for _ in range(4):
        bridge.poll_current()
    assert bridge.busy and bridge.current_cid == 'cancelled-task' and not finished
    state.update(error=False, pending=False)
    bridge.poll_current()
    assert not bridge.busy and not bridge.current_cid and len(finished) == 1
