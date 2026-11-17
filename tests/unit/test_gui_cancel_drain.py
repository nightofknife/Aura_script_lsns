"""GUI cancellation must wait for the actual synchronous worker to exit."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from packages.aura_core.api import service_registry
from packages.aura_core.engine import action_injector
from packages.aura_core.context.execution import ExecutionContext
from packages.aura_core.observability import runtime_trace
from packages.aura_core.scheduler import operation_scope
from packages.aura_core.scheduler.cancellation import (
    begin_sync_action, end_sync_action, has_pending_sync_actions, clear_task_cancel,
)
from packages.aura_game.runner import EmbeddedGameRunner
from packages.resonance_gui.bridge import RunnerBridge
from packages.resonance_gui.logic import PC_CONSCIOUSNESS_DEEP_DIVE_LOOP_TASK_REF, PC_TRADE_TASK_REF
from plans.resonance_pc.src.services.input_bridge_service import ResonancePcInputBridgeService
from plans.aura_base.src.platform.contracts import TargetRuntimeError


def test_cancelled_worker_remains_pending_until_thread_exits(monkeypatch):
    cid = 'test-cancel-drain-worker'
    started, release, exited = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(operation_scope, 'get_config_value', lambda *args: 0)
    injector = action_injector.ActionInjector(ExecutionContext(inputs={}, cid=cid), None, None, {})
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


@pytest.mark.parametrize('task_ref,status', [
    (PC_CONSCIOUSNESS_DEEP_DIVE_LOOP_TASK_REF, 'cancelled'),
    (PC_CONSCIOUSNESS_DEEP_DIVE_LOOP_TASK_REF, 'failed'),
    (PC_CONSCIOUSNESS_DEEP_DIVE_LOOP_TASK_REF, 'success'),
    (PC_TRADE_TASK_REF, 'cancelled'),
    (PC_TRADE_TASK_REF, 'success'),
])
def test_gui_closes_owned_input_after_root_and_child_drain(monkeypatch, task_ref, status):
    cid = 'bridge-root'
    closes = []
    native = ResonancePcInputBridgeService(SimpleNamespace(target_runtime=SimpleNamespace(config={})))
    native._client = SimpleNamespace(pid=123, request=lambda op, **kw: closes.append(op))
    native._session_root_cid = cid
    monkeypatch.setattr(service_registry, 'get_service_instance', lambda fqid: native)
    runtime = SimpleNamespace(get_run_detail=lambda requested: {
        'cid': requested, 'plan_name': 'resonance_pc', 'status': status,
        'parent_cid': cid if requested == 'child' else None,
    })
    runner = EmbeddedGameRunner()
    monkeypatch.setattr(runner, '_ensure_runtime', lambda: runtime)
    monkeypatch.setattr(runner, 'poll_events', lambda **kw: [])
    gui = RunnerBridge(lambda: runner)
    gui.set_input_bridge_enabled(True)
    gui._current_cid = cid
    gui._current_item = {'game_name': 'resonance_pc', 'task_ref': task_ref, 'label': 'test'}
    gui._busy = True
    gui.taskFinished.connect(lambda run: closes.append('taskFinished'))
    monkeypatch.setattr(gui, 'refresh_history', lambda: None)
    monkeypatch.setattr(gui, 'refresh_target', lambda: None)

    child = None
    try:
        with runtime_trace.activity('task', 'root', cid=cid):
            child = runtime_trace.start_activity('worker', 'child', cid='child')
            gui.poll_current()
            assert not closes and gui.busy
            assert runner.close_task_input(cid) is False
            with pytest.raises(ValueError, match='root run'):
                runner.close_task_input('child')
        gui.poll_current()
        assert not closes and gui.busy
    finally:
        if child is not None:
            runtime_trace.end_activity(child)

    begin_sync_action(cid)
    try:
        gui.poll_current()
        assert not closes and gui.busy
    finally:
        end_sync_action(cid)
    gui.poll_current()
    assert closes == ['close', 'taskFinished'] and not gui.busy
    assert native._client is None


def test_gui_retries_close_and_never_closes_a_later_session(monkeypatch):
    def lose_close_response(op, **kwargs):
        assert op == 'close'
        raise TargetRuntimeError('input_result_unknown', 'close acknowledgement lost')

    native = ResonancePcInputBridgeService(SimpleNamespace(target_runtime=SimpleNamespace(config={})))
    client = SimpleNamespace(pid=123, request=lose_close_response)
    native._client = client
    native._session_root_cid = 'new'
    native.close_for_task('old')
    assert native._client is client
    monkeypatch.setattr('plans.resonance_pc.src.services.input_bridge_service.psutil.pid_exists', lambda _: True)

    def close_task_input(cid):
        native.close_for_task(cid)
        return True

    gui = RunnerBridge()
    gui._runner = SimpleNamespace(poll_events=lambda **kw: [], close_task_input=close_task_input,
                                  get_run=lambda cid: {'cid': cid, 'status': 'success', 'execution_pending': False})
    gui._runner_input_bridge = True
    gui._busy = True
    gui._current_cid = 'new'
    gui._current_item = {'game_name': 'resonance_pc'}
    monkeypatch.setattr(gui, 'refresh_history', lambda: None)
    monkeypatch.setattr(gui, 'refresh_target', lambda: None)
    gui.poll_current()
    assert gui.busy and native._client is client

    def already_closed(op, **kwargs):
        assert op == 'close'
        raise TargetRuntimeError('session_invalid', 'Hello required')

    client.request = already_closed
    gui.poll_current()
    assert not gui.busy and native._client is None


@pytest.mark.parametrize('failure_source', ['poll_events', 'get_run'])
def test_poll_failures_request_cancel_without_losing_cid_or_unlocking(monkeypatch, failure_source):
    state = {'error': True, 'pending': True}
    cancelled, finished, failures, busy_changes, queue_advances = [], [], [], [], []

    def poll_events(**kwargs):
        if state['error'] and failure_source == 'poll_events':
            raise RuntimeError('temporary event IPC failure')
        return []

    def get_run(cid):
        if state['error'] and failure_source == 'get_run':
            raise RuntimeError('temporary status IPC failure')
        return {'cid': cid, 'status': 'cancelled', 'execution_pending': state['pending']}

    bridge = RunnerBridge()
    bridge._runner = SimpleNamespace(
        poll_events=poll_events, get_run=get_run,
        cancel_task=lambda cid: cancelled.append(cid) or {'status': 'cancel_requested'},
    )
    bridge._current_cid = 'poll-error-task'
    bridge._current_item = {'label': 'test', 'timeout_sec': 0}
    bridge._busy = True
    bridge.taskFinished.connect(finished.append)
    bridge.taskFailed.connect(failures.append)
    bridge.busyChanged.connect(busy_changes.append)
    monkeypatch.setattr(bridge, 'refresh_history', lambda: None)
    monkeypatch.setattr(bridge, 'refresh_target', lambda: None)
    monkeypatch.setattr(bridge, "_run_item", lambda item: queue_advances.append(item))
    for _ in range(2):
        bridge.poll_current()
    assert not cancelled
    for _ in range(4):
        bridge.poll_current()
    assert cancelled == ['poll-error-task']
    assert bridge._cancel_sent
    assert bridge.busy and bridge.current_cid == 'poll-error-task'
    assert bridge._current_item == {'label': 'test', 'timeout_sec': 0}
    assert not finished and not busy_changes and not queue_advances
    assert all(failure['recoverable'] for failure in failures)
    assert not bridge._timeout_cancel

    state['error'] = False
    bridge.poll_current()
    assert bridge.busy and bridge.current_cid == 'poll-error-task'
    assert not finished and not busy_changes and not queue_advances
    state['pending'] = False
    bridge.poll_current()
    assert not bridge.busy and not bridge.current_cid
    assert len(finished) == 1
    assert busy_changes == [False]
    assert queue_advances == []


def test_failed_poll_error_cancellation_keeps_lock_and_retries_until_confirmed_exit(monkeypatch):
    state = {'error': True, 'cancel_error': True}
    cancel_attempts, failures, finished = [], [], []

    def get_run(cid):
        if state['error']:
            raise RuntimeError('status IPC failure')
        return {'cid': cid, 'status': 'cancelled', 'execution_pending': False}

    def cancel_task(cid):
        cancel_attempts.append(cid)
        if state['cancel_error']:
            raise RuntimeError('cancel IPC failure')
        return {'status': 'cancel_requested'}

    bridge = RunnerBridge()
    bridge._runner = SimpleNamespace(poll_events=lambda **kwargs: [], get_run=get_run, cancel_task=cancel_task)
    bridge._current_cid = 'poll-error-cancel-retry'
    bridge._current_item = {'label': 'test', 'timeout_sec': 0}
    bridge._busy = True
    bridge.taskFailed.connect(failures.append)
    bridge.taskFinished.connect(finished.append)
    monkeypatch.setattr(bridge, 'refresh_history', lambda: None)
    monkeypatch.setattr(bridge, 'refresh_target', lambda: None)
    for _ in range(3):
        bridge.poll_current()
    assert cancel_attempts == ['poll-error-cancel-retry']
    assert not bridge._cancel_sent
    assert bridge.busy and bridge.current_cid == 'poll-error-cancel-retry'
    assert any(failure['stage'] == 'cancel_task' for failure in failures)
    assert not finished

    state['cancel_error'] = False
    bridge.poll_current()
    assert cancel_attempts == ['poll-error-cancel-retry', 'poll-error-cancel-retry']
    assert bridge._cancel_sent and bridge.busy
    assert bridge.current_cid == 'poll-error-cancel-retry'
    assert not finished
    state['error'] = False
    bridge.poll_current()
    assert not bridge.busy and not bridge.current_cid
    assert len(finished) == 1
