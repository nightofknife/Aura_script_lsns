"""Observation drain owns the real target, including bridge input and cleanup."""
import asyncio
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.aura_core.observability import runtime_trace
from packages.aura_core.scheduler import operation_scope
from packages.aura_core.scheduler.cancellation import (
    clear_task_cancel, has_pending_sync_actions, request_task_cancel,
)
from plans.aura_base.src.services.target_runtime_service import TargetRuntimeService
from plans.resonance_pc.src.services import input_bridge_service


@pytest.fixture
def target(monkeypatch):
    runtime = object.__new__(TargetRuntimeService)
    runtime._lock = threading.RLock()
    releases = []
    runtime._session = SimpleNamespace(mouse_up=lambda *args: releases.append(args))
    monkeypatch.setattr(runtime, "_get_or_create_session", lambda **kwargs: pytest.fail("unexpected target rebinding"))
    monkeypatch.setattr(runtime_trace, "_monitor_thread", SimpleNamespace(is_alive=lambda: True))
    return runtime, releases


def bridge_for(runtime):
    bridge = object.__new__(input_bridge_service.ResonancePcInputBridgeService)
    requests = []

    def request(*args, **kwargs):
        requests.append((args, kwargs))
        return {"result": {}}

    bridge.target_runtime = runtime
    bridge._lock = threading.RLock()
    bridge._client = SimpleNamespace(request=request)
    bridge._viewport = {"width": 1280, "height": 720}
    bridge._identity = (10, 20, 1280, 720)
    bridge._held = {"left"}
    bridge._ready = lambda: pytest.fail("cleanup must not send HELLO or rebind")
    return bridge, requests


def test_cancelled_operation_rejects_input_but_releases_existing_sessions(target):
    runtime, releases = target
    bridge, requests = bridge_for(runtime)
    cid = f"guard-cancel-{uuid4().hex}"
    scope = operation_scope.OperationScope(cid=cid, target=runtime)
    cancelled = threading.Event()
    cancelled.set()
    token = input_bridge_service._CANCEL_TOKEN.set(cancelled)
    try:
        with scope.activate():
            request_task_cancel(cid)
            with pytest.raises(asyncio.CancelledError):
                runtime._call_session("click", 5, 5)
            with pytest.raises(asyncio.CancelledError):
                bridge._execute("click", {})
            runtime._call_session("mouse_up", "left")
            bridge.button("left", False)
        assert releases == [("left",)]
        assert len(requests) == 1
        args, kwargs = requests[0]
        assert args[0] == "button_up" and "cancel_token" not in kwargs
        assert bridge._held == set()
    finally:
        input_bridge_service._CANCEL_TOKEN.reset(token)
        clear_task_cancel(cid)


def test_quarantined_worker_blocks_runtime_and_bridge_until_actual_exit(target, monkeypatch):
    runtime, releases = target
    bridge, requests = bridge_for(runtime)
    cid = f"guard-drain-{uuid4().hex}"
    monkeypatch.setattr(operation_scope, "get_config_value", lambda *args: 0)
    started, release = threading.Event(), threading.Event()
    scope = operation_scope.OperationScope(cid=cid, target=runtime)

    def worker():
        started.set()
        assert release.wait(3), "test must always release its worker"

    async def exercise():
        task = asyncio.create_task(scope.run_async(worker))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert has_pending_sync_actions(cid)
            for call in (lambda: runtime._call_session("capture", None),
                         lambda: bridge._execute("click", {})):
                with pytest.raises(operation_scope.TargetOperationBusy) as caught:
                    call()
                assert caught.value.quarantined
            runtime._call_session("mouse_up", "left")
            bridge.button("left", False)
            assert releases == [("left",)] and len(requests) == 1
        finally:
            release.set()
            for _ in range(200):
                if not has_pending_sync_actions(cid):
                    break
                await asyncio.sleep(.005)
            assert not has_pending_sync_actions(cid)
        with operation_scope.target_operation_guard(runtime):
            pass

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        clear_task_cancel(cid)


def test_cleanup_without_session_does_not_discover_or_create_one(target):
    runtime, releases = target
    runtime._session = None
    bridge, requests = bridge_for(runtime)
    bridge._client = None
    runtime._call_session("mouse_up", "left")
    bridge.button("left", False)
    assert releases == [] and requests == []
    assert bridge._held == set()
