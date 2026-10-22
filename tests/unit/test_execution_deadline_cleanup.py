"""Local deadlines clean up their children without becoming user cancellation."""
import asyncio
import threading
from uuid import uuid4

import pytest

from packages.aura_core.engine.execution_facade import _await_scoped
from packages.aura_core.scheduler import cancellation
from packages.aura_core.scheduler import operation_scope as operations
from packages.aura_core.scheduler.operation_scope import OperationScope, OperationTimeout, TargetOperationBusy


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.001)


@pytest.mark.parametrize("user_cancel", [False, True])
def test_nested_deadline_waits_for_cleanup_and_real_user_cancel_still_wins(user_cancel):
    async def scenario():
        parent = OperationScope(cid=uuid4().hex)
        child = parent.child(.03)
        cleanup_started, release = asyncio.Event(), asyncio.Event()
        events = []

        async def leaf():
            try:
                events.append("started")
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await release.wait()
                events.append("cleaned")

        async def nested():
            with child.activate():
                return await _await_scoped(leaf(), child)

        task = asyncio.create_task(_await_scoped(nested(), child))
        try:
            await asyncio.wait_for(cleanup_started.wait(), 2)
            assert not parent.cancelled
            assert not cancellation.is_task_cancel_requested(parent.cid)
            assert not task.done()
            if user_cancel:
                task.cancel("user stop during timeout cleanup")
                await eventually(lambda: parent.cancelled)
                task.cancel("repeated user stop")
                await asyncio.sleep(0)
                assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError if user_cancel else OperationTimeout):
                await task
            assert events == ["started", "cleaned"]
            assert parent.cancelled is user_cancel
            assert cancellation.is_task_cancel_requested(parent.cid) is user_cancel
            if not user_cancel:
                # A sibling/follow-up operation still has a usable parent budget.
                assert await _await_scoped(asyncio.sleep(0, result="next"), parent) == "next"
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            cancellation.clear_task_cancel(parent.cid)

    asyncio.run(scenario())


def test_node_deadline_keeps_sync_worker_pending_and_target_quarantined_until_exit(monkeypatch):
    monkeypatch.setattr(operations, "get_config_value", lambda *args: .001)

    class Target:
        pass

    async def scenario():
        parent = OperationScope(cid=uuid4().hex, target=Target())
        child = parent.child(.03)
        started, release = threading.Event(), threading.Event()

        def worker():
            started.set()
            assert release.wait(2)
            return "late result"

        task = asyncio.create_task(_await_scoped(child.run_async(worker), child))
        try:
            await eventually(started.is_set)
            with pytest.raises(OperationTimeout):
                await task
            assert not parent.cancelled
            assert not cancellation.is_task_cancel_requested(parent.cid)
            assert cancellation.has_pending_sync_actions(parent.cid)
            with pytest.raises(TargetOperationBusy) as caught:
                with parent.activate():
                    pass
            assert caught.value.quarantined
            release.set()
            await eventually(lambda: not cancellation.has_pending_sync_actions(parent.cid))
            with parent.activate():
                parent.checkpoint()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            cancellation.clear_task_cancel(parent.cid)

    asyncio.run(scenario())
