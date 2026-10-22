"""Exercise real tracked workers without a game, inputs or model inference."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from collections import deque
import threading
import time
from types import SimpleNamespace
from uuid import uuid4
import weakref

import pytest

from packages.aura_core.observability import runtime_trace
from packages.aura_core.observability.logging.core_logger import current_cid
from packages.aura_core.scheduler import cancellation
from packages.aura_core.scheduler import operation_scope as operations
from packages.aura_core.scheduler.operation_scope import (
    OperationScope, OperationTimeout, TargetOperationBusy, current_operation_scope,
    target_operation_guard,
)


class Clock:
    def __init__(self):
        self.now = 10.0

    def __call__(self):
        return self.now


class Target:
    pass


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch):
    monkeypatch.setattr(cancellation, "_cancelled_cids", set())
    monkeypatch.setattr(cancellation, "_sync_actions", {})
    monkeypatch.setattr(runtime_trace, "_events", deque(maxlen=2048))
    monkeypatch.setattr(runtime_trace, "_active", {})
    monkeypatch.setattr(runtime_trace, "_stops", {})
    monkeypatch.setattr(runtime_trace, "_monitor_thread", SimpleNamespace(is_alive=lambda: True))
    monkeypatch.setattr(runtime_trace.logger, "info", lambda *args, **kwargs: None)
    monkeypatch.setattr(operations, "get_config_value", lambda *args: .1)


async def eventually(predicate):
    until = time.monotonic() + 2
    while not predicate():
        if time.monotonic() >= until:
            pytest.fail("worker did not reach expected lifecycle state")
        await asyncio.sleep(.001)


@pytest.mark.parametrize("timeout", [-1, float("nan"), float("inf"), float("-inf")])
def test_invalid_budget_is_rejected(timeout):
    with pytest.raises(ValueError):
        OperationScope(timeout)


def test_child_budget_never_extends_parent_and_zero_is_expired():
    clock = Clock()
    parent = OperationScope(5, monotonic=clock)
    clock.now += 2
    assert parent.child(100).deadline == parent.deadline == 15
    assert parent.child(None).remaining == 3
    assert parent.child(1).deadline == 13
    assert OperationScope(monotonic=clock).remaining is None
    with pytest.raises(OperationTimeout):
        parent.child(0).checkpoint()


def test_cancellation_wins_over_expiry_and_remains_latched_after_cid_reset():
    clock = Clock()
    scope = OperationScope(0, cid="old", monotonic=clock)
    cancellation.request_task_cancel("old")
    with pytest.raises(asyncio.CancelledError):
        scope.child(None).checkpoint()
    cancellation.clear_task_cancel("old")
    assert scope.cancelled
    assert not OperationScope(cid="old").cancelled
    with pytest.raises(asyncio.CancelledError):
        scope.checkpoint()


def test_scope_binding_is_nested_and_restores_cid_and_target_ownership():
    target = Target()
    scope = OperationScope(cid="scope", target=target)
    previous = current_cid()
    with scope.activate():
        assert current_operation_scope() is scope
        assert current_cid() == "scope"
        with target_operation_guard(target):
            with scope.child().activate():
                pass
        with pytest.raises(ValueError):
            with target_operation_guard(Target()):
                pass
    assert current_operation_scope() is None
    assert current_cid() == previous
    with OperationScope(target=target).activate():
        pass


def test_worker_inherits_scope_cid_and_trace_and_releases_registration():
    async def scenario():
        cid, target = uuid4().hex, Target()
        scope = OperationScope(cid=cid, target=target)
        thread = threading.get_ident()

        def callback():
            assert threading.get_ident() != thread
            assert current_operation_scope() is scope
            assert current_cid() == cid
            assert cancellation.has_pending_sync_actions(cid)
            with target_operation_guard(target):
                return 7

        assert await scope.run_async(callback) == 7
        assert not cancellation.has_pending_sync_actions(cid)
        assert runtime_trace.pending_activities(cid) == []
        with target_operation_guard(target):
            pass

    asyncio.run(scenario())


def test_late_worker_result_cannot_succeed():
    async def scenario():
        clock = Clock()
        scope = OperationScope(1, monotonic=clock)

        def callback():
            clock.now += 2
            return "late result"

        with pytest.raises(OperationTimeout):
            await scope.run_async(callback)
        assert not cancellation.has_pending_sync_actions(scope.cid)

    asyncio.run(scenario())


def test_cancel_during_work_drains_and_repeated_cancel_does_not_abandon_worker():
    async def scenario():
        entered, release = threading.Event(), threading.Event()
        scope = OperationScope(cid=uuid4().hex, target=Target())

        def callback():
            entered.set()
            assert release.wait(2)
            return "must not succeed"

        task = asyncio.create_task(scope.run_async(callback))
        try:
            await eventually(entered.is_set)
            task.cancel()
            await eventually(lambda: scope.cancelled)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not cancellation.has_pending_sync_actions(scope.cid)
            assert runtime_trace.pending_activities(scope.cid) == []
            with target_operation_guard(scope.target):
                pass
        finally:
            release.set()

    asyncio.run(scenario())


def test_cancel_before_queued_worker_starts_quarantines_until_actual_exit(monkeypatch):
    monkeypatch.setattr(operations, "get_config_value", lambda *args: 0.)

    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        occupied, release = threading.Event(), threading.Event()
        called = []

        def occupy():
            occupied.set()
            assert release.wait(2)

        blocker = loop.run_in_executor(None, occupy)
        scope = OperationScope(cid=uuid4().hex, target=Target())
        task = asyncio.create_task(scope.run_async(lambda: called.append(True)))
        try:
            await eventually(occupied.is_set)
            await eventually(lambda: bool(runtime_trace.pending_activities(scope.cid)))
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert cancellation.has_pending_sync_actions(scope.cid)
            with pytest.raises(TargetOperationBusy) as error:
                with target_operation_guard(scope.target):
                    pass
            assert error.value.quarantined
            with target_operation_guard(scope.target, cleanup=True):
                pass
            release.set()
            await blocker
            await eventually(lambda: not cancellation.has_pending_sync_actions(scope.cid))
            assert called == []
            with target_operation_guard(scope.target):
                pass
        finally:
            release.set()

    asyncio.run(scenario())


def test_timeout_drain_quarantine_releases_only_after_worker_and_recovers_exception(monkeypatch):
    monkeypatch.setattr(operations, "get_config_value", lambda *args: .01)

    async def scenario():
        entered, release = threading.Event(), threading.Event()
        scope = OperationScope(.02, cid=uuid4().hex, target=Target())
        loop = asyncio.get_running_loop()
        unhandled = []
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))

        def callback():
            entered.set()
            assert release.wait(2)
            raise ValueError("late background failure")

        task = asyncio.create_task(scope.run_async(callback))
        try:
            await eventually(entered.is_set)
            with pytest.raises(OperationTimeout):
                await task
            assert cancellation.has_pending_sync_actions(scope.cid)
            with pytest.raises(TargetOperationBusy) as error:
                with OperationScope(cid=scope.cid, target=scope.target).activate():
                    pass
            assert error.value.quarantined
            with target_operation_guard(scope.target, cleanup=True):
                pass
            release.set()
            await eventually(lambda: not cancellation.has_pending_sync_actions(scope.cid))
            await eventually(lambda: not runtime_trace.pending_activities(scope.cid))
            for _ in range(3):
                await asyncio.sleep(0)
            assert unhandled == []
            with OperationScope(target=scope.target).activate():
                pass
        finally:
            release.set()

    asyncio.run(scenario())


def test_scheduler_cancel_dominates_worker_error():
    async def scenario():
        scope = OperationScope(cid=uuid4().hex)

        def callback():
            cancellation.request_task_cancel(scope.cid)
            raise ValueError("recognizer failed during cancellation")

        with pytest.raises(asyncio.CancelledError):
            await scope.run_async(callback)
        assert not cancellation.has_pending_sync_actions(scope.cid)

    asyncio.run(scenario())


def test_executor_submission_failure_does_not_leak_target_or_sync_registration(monkeypatch):
    async def scenario():
        loop = asyncio.get_running_loop()
        scope = OperationScope(cid=uuid4().hex, target=Target())

        def reject(*args, **kwargs):
            raise RuntimeError("executor shut down")

        monkeypatch.setattr(loop, "run_in_executor", reject)
        with pytest.raises(RuntimeError, match="executor shut down"):
            await scope.run_async(lambda: None)
        assert not cancellation.has_pending_sync_actions(scope.cid)
        assert runtime_trace.pending_activities(scope.cid) == []
        with target_operation_guard(scope.target):
            pass

    asyncio.run(scenario())


def test_new_cancel_during_timeout_drain_wins_over_timeout(monkeypatch):
    monkeypatch.setattr(operations, "get_config_value", lambda *args: .5)

    async def scenario():
        entered, release = threading.Event(), threading.Event()
        scope = OperationScope(.01, cid=uuid4().hex, target=Target())

        def callback():
            entered.set()
            assert release.wait(2)

        task = asyncio.create_task(scope.run_async(callback))
        try:
            await eventually(entered.is_set)
            await eventually(lambda: scope.expired)
            await asyncio.sleep(.01)
            assert not task.done()
            task.cancel()
            await eventually(lambda: scope.cancelled)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not cancellation.has_pending_sync_actions(scope.cid)
        finally:
            release.set()

    asyncio.run(scenario())


def test_cleanup_remains_legal_in_cancelled_context():
    target = Target()
    scope = OperationScope(cid="cleanup", target=target)
    with scope.activate():
        cancellation.request_task_cancel(scope.cid)
        with pytest.raises(asyncio.CancelledError):
            with target_operation_guard(target):
                pass
        with target_operation_guard(target, cleanup=True):
            pass


def test_registry_does_not_retain_finished_target():
    target = Target()
    reference = weakref.ref(target)
    with target_operation_guard(target):
        pass
    del target
    assert reference() is None


def test_cancel_drain_waits_for_tracked_worker_teardown_not_only_user_callback(monkeypatch):
    monkeypatch.setattr(operations, "get_config_value", lambda *args: 1.)
    real_end = runtime_trace.end_activity
    teardown_entered, teardown_release = threading.Event(), threading.Event()
    worker_entered, worker_release = threading.Event(), threading.Event()

    def delayed_end(ident, *args, **kwargs):
        row = runtime_trace._active.get(ident, {})
        if row.get("kind") == "worker":
            teardown_entered.set()
            assert teardown_release.wait(2)
        return real_end(ident, *args, **kwargs)

    monkeypatch.setattr(runtime_trace, "end_activity", delayed_end)

    async def scenario():
        scope = OperationScope(cid=uuid4().hex, target=Target())

        def callback():
            worker_entered.set()
            assert worker_release.wait(2)

        task = asyncio.create_task(scope.run_async(callback))
        try:
            await eventually(worker_entered.is_set)
            task.cancel()
            await eventually(lambda: scope.cancelled)
            worker_release.set()
            await eventually(teardown_entered.is_set)
            await asyncio.sleep(0)
            assert not task.done(), "drain returned while tracked worker teardown was still running"
            assert runtime_trace.pending_activities(scope.cid)
            teardown_release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert runtime_trace.pending_activities(scope.cid) == []
        finally:
            worker_release.set()
            teardown_release.set()

    asyncio.run(scenario())
