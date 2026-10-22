"""Offline execution-lifecycle tests; no target runtime or input is constructed."""
import asyncio
from collections import deque
from enum import Enum
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.aura_core.context.execution import ExecutionContext
from packages.aura_core.engine.action_injector import ActionInjector
from packages.aura_core.engine.dag_scheduler import DAGScheduler
from packages.aura_core.engine.execution_engine import ExecutionEngine
from packages.aura_core.observability.logging.core_logger import current_cid, set_cid, reset_cid
from packages.aura_core.context.plan import current_plan_name
from packages.aura_core.observability import runtime_trace as trace
from packages.aura_core.scheduler import cancellation
from packages.aura_game.runner import EmbeddedGameRunner


def test_cancelled_root_waits_for_async_node_cleanup_before_finishing():
    async def scenario():
        cid = 'test-node-' + uuid4().hex
        token = set_cid(cid)
        started, release, cancelling = asyncio.Event(), asyncio.Event(), asyncio.Event()
        engine = ExecutionEngine(SimpleNamespace(services={}, debug_mode=False), None)
        engine.nodes = {'trade': {}}
        engine.dependencies = {}
        engine.step_states = {'trade': engine.StepState.PENDING}
        engine.graph_builder = SimpleNamespace(build_graph=lambda steps: None,
                                               get_all_deps_from_struct=lambda deps: [])

        async def execute(node_id, context):
            engine.step_states[node_id] = engine.StepState.RUNNING
            started.set()
            try:
                await release.wait()
                engine.step_states[node_id] = engine.StepState.SUCCESS
            except asyncio.CancelledError:
                cancelling.set()
                await release.wait()
                engine.step_states[node_id] = engine.StepState.CANCELLED
                raise

        engine.node_executor = SimpleNamespace(execute_dag_node=execute)
        try:
            parent = asyncio.create_task(engine.run({'steps': {'trade': {}}}, 'test',
                                                     ExecutionContext(inputs={}, cid=cid)))
            await asyncio.wait_for(started.wait(), 1)
            parent.cancel()
            await asyncio.wait_for(cancelling.wait(), 1)
            assert not parent.done()
            assert trace.pending_activities(cid)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(parent, 1)
            trace.mark_task_terminal(cid, status='cancelled')
            for _ in range(20):
                await asyncio.sleep(0)
            assert not trace.pending_activities(cid)
        finally:
            release.set()
            reset_cid(token)

    asyncio.run(scenario())


def test_node_cancelled_before_first_instruction_retires_queued_activity():
    async def scenario():
        class State(Enum):
            PENDING = 1

        cid = 'test-queued-node-' + uuid4().hex
        token = set_cid(cid)
        executed = []

        async def execute(node_id, context):
            executed.append(node_id)

        engine = SimpleNamespace(
            ready_queue=deque(['node']), _ready_set={'node'}, StepState=State,
            _node_cancelled=False,
            step_states={'node': State.PENDING}, node_contexts={}, running_tasks=set(),
            _prepare_node_context=lambda node_id: SimpleNamespace(data={'cid': cid}),
            node_executor=SimpleNamespace(execute_dag_node=execute),
            _on_task_completed=lambda task, node_id: None,
        )
        try:
            await DAGScheduler(engine).drain_ready_queue()
            assert trace.pending_activities(cid)
            task = next(iter(engine.running_tasks))
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)
            assert not executed
            assert not trace.pending_activities(cid)
        finally:
            reset_cid(token)

    asyncio.run(scenario())


def test_sync_action_retains_activity_after_cancel_grace_until_worker_exit(monkeypatch):
    from packages.aura_core.scheduler import operation_scope as module
    monkeypatch.setattr(module, 'get_config_value', lambda *a, **kw: .001)

    async def scenario():
        cid = 'test-sync-worker-' + uuid4().hex
        token = set_cid(cid)
        started, release = threading.Event(), threading.Event()
        injector = ActionInjector(ExecutionContext(inputs={}, cid=cid), None, None, {})
        injector._prepare_action_arguments = lambda *a: {}

        def worker():
            started.set()
            assert release.wait(3)

        action = SimpleNamespace(is_async=False, name='blocked_worker', func=worker)
        try:
            task = asyncio.create_task(injector._invoke_action(action, None, {}))
            for _ in range(1000):
                if started.is_set():
                    break
                await asyncio.sleep(.001)
            assert started.is_set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert cancellation.has_pending_sync_actions(cid)
            assert trace.pending_activities(cid)
            release.set()
            for _ in range(1000):
                if not trace.pending_activities(cid) and not cancellation.has_pending_sync_actions(cid):
                    break
                await asyncio.sleep(.001)
            assert not trace.pending_activities(cid)
            assert not cancellation.has_pending_sync_actions(cid)
        finally:
            release.set()
            trace.mark_task_terminal(cid, status='cancelled')
            cancellation.clear_task_cancel(cid)
            reset_cid(token)

    asyncio.run(scenario())


@pytest.mark.parametrize('fails', [False, True])
def test_release_logs_api_outcome_without_claiming_os_state(monkeypatch, fails):
    from packages.aura_core.scheduler import task_dispatcher as module
    cid = 'test-release-' + uuid4().hex
    events = []

    def release():
        if fails:
            raise RuntimeError('injected release failure')

    monkeypatch.setattr(module.service_registry, 'get_service_instance',
                        lambda name: SimpleNamespace(release_all=release))
    monkeypatch.setattr(module, 'log_event', lambda event, **kw: events.append((event, kw)))
    scheduler = SimpleNamespace(_running_task_meta={cid: {'plan_name': 'resonance_pc'}})
    module.TaskDispatcher(scheduler)._release_inputs_for_cancelled_task(cid)
    assert events[0][0] == 'input.release.requested'
    if fails:
        assert events[1][0] == 'input.release.failed'
        assert events[1][1]['error_type'] == 'RuntimeError'
    else:
        assert events[1][0] == 'input.release.returned'
        assert events[1][1]['os_state_verified'] is False


@pytest.mark.parametrize('caller_cid', ['-', 'caller-context'])
@pytest.mark.parametrize('fails', [False, True])
def test_release_propagates_cid_to_real_trace_and_restores_context(monkeypatch, caller_cid, fails):
    from packages.aura_core.scheduler import task_dispatcher as module
    cid = 'test-release-context-' + uuid4().hex
    snapshots, edges = [], []

    def release():
        assert current_cid() == cid
        assert current_plan_name.get() == 'resonance_pc'
        with trace.activity('input_release', 'release_all'):
            snapshots.extend(trace.pending_activities(cid))
            edges.append(trace.log_event('input.edge', detail=True, edge='up', backend='fake'))
            if fails:
                raise RuntimeError('injected release failure')

    monkeypatch.setattr(module.service_registry, 'get_service_instance',
                        lambda name: SimpleNamespace(release_all=release))
    scheduler = SimpleNamespace(_running_task_meta={cid: {'plan_name': 'resonance_pc'}})
    cid_token = set_cid(caller_cid)
    plan_token = current_plan_name.set('caller-plan')
    try:
        module.TaskDispatcher(scheduler)._release_inputs_for_cancelled_task(cid)
        assert current_cid() == caller_cid
        assert current_plan_name.get() == 'caller-plan'
        assert len(snapshots) == 1
        assert snapshots[0]['cid'] == cid
        assert snapshots[0]['root_cid'] == cid
        assert len(edges) == 1
        assert edges[0]['cid'] == cid
        assert edges[0]['root_cid'] == cid
        assert not trace.pending_activities(cid)
    finally:
        current_plan_name.reset(plan_token)
        reset_cid(cid_token)


def test_manager_marks_terminal_after_cancel_without_ignoring_pending_node(monkeypatch):
    from packages.aura_core.scheduler.execution import manager as module
    cid = 'test-manager-' + uuid4().hex
    terminal = []
    monkeypatch.setattr(module, 'mark_task_terminal', lambda observed_cid, **kw:
                        terminal.append((observed_cid, kw, trace.pending_activities(observed_cid))))

    async def hook(*args, **kwargs):
        return None

    monkeypatch.setattr(module.hook_manager, 'trigger', hook)

    async def scenario():
        scheduler = SimpleNamespace(fallback_lock=threading.RLock(), running_tasks={}, _running_task_meta={})
        manager = module.ExecutionManager(scheduler)
        manager._io_pool = object()
        manager._cpu_pool = object()
        started = asyncio.Event()

        async def planned(tasklet):
            return True

        async def body(tasklet):
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(manager, '_handle_state_planning', planned)
        monkeypatch.setattr(manager, '_run_execution_chain', body)
        tasklet = SimpleNamespace(payload=None, task_name='resonance_pc/test', cid=cid,
                                  resource_tags=[], timeout=30, execution_mode='dag')
        remaining = trace.start_activity('node', 'still_running', cid=cid)
        try:
            task = asyncio.create_task(manager.submit(tasklet))
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            await task
            assert len(terminal) == 1
            assert terminal[0][0] == cid
            assert terminal[0][1]['status'] == 'cancelled'
            assert terminal[0][2]
        finally:
            trace.end_activity(remaining)
            trace.mark_task_terminal(cid, status='cancelled')
            cancellation.clear_task_cancel(cid)

    asyncio.run(scenario())


def test_runner_exposes_async_pending_after_terminal_status(monkeypatch):
    cid = 'test-runner-pending-' + uuid4().hex
    runner = EmbeddedGameRunner()
    monkeypatch.setattr(runner, '_ensure_runtime', lambda: SimpleNamespace(
        get_run_detail=lambda requested: {'cid': cid, 'status': 'cancelled'},
    ))
    activity_id = trace.start_activity('async_action', 'remaining', cid=cid)
    try:
        row = runner.get_run(cid)
        assert row['execution_pending'] is True
        assert row['pending_activities']
    finally:
        trace.end_activity(activity_id)
    assert runner.get_run(cid)['execution_pending'] is False


def test_runner_keeps_child_activity_pending_after_root_activity_exits(monkeypatch):
    root_cid = 'test-root-pending-' + uuid4().hex
    child_cid = 'test-child-pending-' + uuid4().hex
    runner = EmbeddedGameRunner()
    monkeypatch.setattr(runner, '_ensure_runtime', lambda: SimpleNamespace(
        get_run_detail=lambda requested: {'cid': requested, 'status': 'cancelled'},
    ))
    token = set_cid(root_cid)
    child_activity = None
    try:
        with trace.activity('node', 'root'):
            child_token = set_cid(child_cid)
            try:
                child_activity = trace.start_activity('worker', 'child_remaining')
            finally:
                reset_cid(child_token)
        # Only the child remains; its execution must still keep the root busy.
        trace.mark_task_terminal(root_cid, status='cancelled')
        row = runner.get_run(root_cid)
        assert row['execution_pending'] is True
        assert len(row['pending_activities']) == 1
        assert row['pending_activities'][0]['cid'] == child_cid
        assert row['pending_activities'][0]['root_cid'] == root_cid
    finally:
        if child_activity is not None:
            trace.end_activity(child_activity)
        reset_cid(token)
    assert runner.get_run(root_cid)['execution_pending'] is False


def test_stop_request_is_logged_only_for_first_cancellation(monkeypatch):
    cid = 'test-stop-once-' + uuid4().hex
    requests = []
    monkeypatch.setattr(trace, 'request_stop', lambda cid, **kw: requests.append(cid))
    try:
        assert cancellation.request_task_cancel(cid)
        assert cancellation.request_task_cancel(cid)
        assert requests == [cid]
    finally:
        cancellation.clear_task_cancel(cid)


def test_successor_dispatch_is_registered_before_node_retires(monkeypatch):
    from packages.aura_core.engine import dag_scheduler as module
    retired_snapshots = []
    original_end = module.end_activity

    def end(activity_id, **kwargs):
        original_end(activity_id, **kwargs)
        retired_snapshots.append(trace.pending_activities(cid))

    cid = 'test-node-transfer-' + uuid4().hex
    monkeypatch.setattr(module, 'end_activity', end)

    async def scenario():
        token = set_cid(cid)
        engine = ExecutionEngine(SimpleNamespace(services={}, debug_mode=False), None)
        engine.nodes = {'one': {}, 'two': {}}
        engine.dependencies = {'two': 'one'}
        engine.reverse_dependencies = {'one': {'two'}}
        engine.step_states = dict.fromkeys(engine.nodes, engine.StepState.PENDING)
        engine.graph_builder = SimpleNamespace(build_graph=lambda steps: None,
                                               get_all_deps_from_struct=lambda deps: [])

        async def execute(node_id, context):
            engine.step_states[node_id] = engine.StepState.SUCCESS

        engine.node_executor = SimpleNamespace(execute_dag_node=execute)
        try:
            await engine.run({'steps': engine.nodes}, 'test', ExecutionContext(inputs={}, cid=cid))
            for _ in range(10):
                await asyncio.sleep(0)
            assert len(retired_snapshots) == 2
            assert all(retired_snapshots)
            assert not trace.pending_activities(cid)
        finally:
            reset_cid(token)

    asyncio.run(scenario())


def test_timeout_evidence_and_deferred_event_survive_cancel_until_sender_exits(monkeypatch):
    """v1.11.8 timeout classification must coexist with the new pending tracker."""
    from packages.aura_core.scheduler.execution import manager as module
    from packages.aura_core.observability.events import Event

    cid = 'test-timeout-evidence-' + uuid4().hex
    evidence = {'evidence_id': 'timeout-regression', 'status': 'saved'}
    captures = []

    async def capture(error, **kwargs):
        captures.append((error, kwargs))
        return [evidence]

    async def hook(*args, **kwargs):
        return None

    monkeypatch.setattr(module, 'capture_failure', capture)
    monkeypatch.setattr(module.hook_manager, 'trigger', hook)

    async def scenario():
        published, release = asyncio.Event(), asyncio.Event()
        delivered = []

        async def publish(event):
            published.set()
            await release.wait()
            delivered.append(event)

        scheduler = SimpleNamespace(fallback_lock=threading.RLock(), running_tasks={},
                                    _running_task_meta={}, event_bus=SimpleNamespace(publish=publish))
        manager = module.ExecutionManager(scheduler)
        manager._io_pool = object()
        manager._cpu_pool = object()

        async def planned(tasklet):
            return True

        async def body(tasklet):
            completion = module.task_completion_scope.get()
            try:
                await asyncio.Event().wait()
            finally:
                completion['deferred_event'] = Event('task.finished', payload={
                    'cid': cid, 'final_status': 'CANCELLED',
                    'final_result': {'status': 'CANCELLED'},
                })

        monkeypatch.setattr(manager, '_handle_state_planning', planned)
        monkeypatch.setattr(manager, '_run_execution_chain', body)
        tasklet = SimpleNamespace(payload=None, task_name='resonance_pc/test', cid=cid,
                                  resource_tags=[], timeout=.01, execution_mode='dag')
        task = asyncio.create_task(manager.submit(tasklet))
        try:
            await asyncio.wait_for(published.wait(), 2)
            assert len(captures) == 1
            assert isinstance(captures[0][0], TimeoutError)
            assert captures[0][1]['terminal'] is True
            assert manager._global_sem._value == manager.max_concurrent_tasks
            assert cid not in scheduler.running_tasks
            task.cancel()  # A second cancel must not cancel the event subscribers.
            with pytest.raises(asyncio.CancelledError):
                await task
            pending = trace.pending_activities(cid)
            assert any(row['kind'] == 'terminal_event' for row in pending)
            senders = tuple(manager._terminal_publish_tasks)
            assert len(senders) == 1 and not senders[0].done()
            release.set()
            await asyncio.gather(*senders)
            await asyncio.sleep(0)
            assert len(delivered) == 1
            payload = delivered[0].payload
            assert payload['final_status'] == payload['final_result']['status'] == 'TIMEOUT'
            assert payload['final_result']['failure_evidence'] == [evidence]
            assert payload['final_result']['error']['failure_evidence'] == [evidence]
            assert not trace.pending_activities(cid)
            assert module.task_completion_scope.get() is None
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await asyncio.gather(*manager._terminal_publish_tasks, return_exceptions=True)
            cancellation.clear_task_cancel(cid)

    asyncio.run(scenario())
