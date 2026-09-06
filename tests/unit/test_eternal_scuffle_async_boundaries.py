"""Real asyncio responsiveness and cancellation at Scuffle sync boundaries.

All inputs are fake calls. Blocking gates reproduce a slow native input/capture
or filesystem operation without opening, focusing or interacting with a game.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import logging
from pathlib import Path
import threading
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest

from plans.resonance_pc.src.actions import _eternal_scuffle_runtime as runtime


class Gate:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.timed_out = False
        self.thread_id = None

    def block(self):
        self.thread_id = threading.get_ident()
        self.entered.set()
        self.timed_out = not self.release.wait(2)
        self.finished.set()


class Store:
    def __init__(self):
        self.data = {}

    async def get(self, key):
        return deepcopy(self.data.get(key))

    async def set(self, key, value):
        self.data[key] = deepcopy(value)

    async def delete(self, key):
        self.data.pop(key, None)


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []
        self.gate = None
        self.gate_filter = lambda payload, record: True

    def emit(self, record):
        message = record.getMessage()
        if not message.startswith("[EternalScuffle]"):
            return
        payload = json.loads(message[message.index("{"):])
        if self.gate and self.gate_filter(payload, record):
            self.gate.block()
        self.records.append({"payload": payload, "level": record.levelno})


@pytest.fixture
def log_capture():
    capture = LogCapture()
    logger = runtime.logger.logger
    old_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(capture)
    try:
        yield capture
    finally:
        logger.removeHandler(capture)
        logger.setLevel(old_level)


def checked_round(result):
    return {"nodes": {"finish": {"output": {"success": True, "status": "completed", "round_result": result}}}}


async def wait_for_event(event):
    async with asyncio.timeout(3):
        while not event.is_set():
            await asyncio.sleep(.001)


async def assert_heartbeat_continues(awaitable, gate):
    task = asyncio.create_task(awaitable)
    try:
        await wait_for_event(gate.entered)
        await asyncio.sleep(.01)
        assert gate.thread_id != threading.get_ident(), "Blocking boundary ran on asyncio loop"
        assert not gate.timed_out, "Heartbeat could not release the blocking call"
        assert not task.done(), "Synchronous operation finished before heartbeat could run"
    finally:
        gate.release.set()
        result = await task
    return result


@pytest.fixture
def rig(monkeypatch, tmp_path, log_capture):
    catalog = {"characters": [], "equipment": [], "ranking_version": "async-test"}
    observer = SimpleNamespace(observe=lambda: {"valid": True, "scene": "home", "controls": {}})
    monkeypatch.setattr(runtime, "catalog_for", lambda engine=None: catalog)
    monkeypatch.setattr(runtime, "make_observer", lambda *args: observer)
    monkeypatch.setattr(runtime, "is_current_task_cancel_requested", lambda: False)

    async def sleep(_seconds):
        await asyncio.sleep(0)

    monkeypatch.setattr(runtime, "aura_sleep", sleep)
    state = {
        "schema": runtime.SCHEMA, "cid": "async-boundary", "coins_per_run": 1,
        "run_count": 1, "run_index": 1, "completed_runs": 0,
        "cleared_runs": 0, "abandoned_runs": 0, "stage": "coin",
        "status": "running", "sequence": 0, "log_dir": str(tmp_path / "logs"),
        "round": {"phase": "coin", "team": []},
    }
    machine = runtime.ScuffleRuntime(state, Store(), object(), object())
    machine.log_handler = log_capture
    machine.catalog_stub = catalog
    machine.observer_stub = observer
    machine.test_plan_root = tmp_path / "plans/resonance_pc"
    return machine


def test_send_click_keeps_loop_responsive(rig, monkeypatch):
    gate, clicks = Gate(), []

    def click(*, app, x, y):
        gate.block()
        clicks.append([x, y])

    monkeypatch.setattr(runtime, "aura_click", click)
    asyncio.run(assert_heartbeat_continues(rig.send_click([100, 200], "test"), gate))
    assert clicks == [[100, 200]]
    rows = [record["payload"] for record in rig.log_handler.records]
    assert rows[-1]["type"] == "click"
    assert not Path(rig.state["log_dir"]).exists()


@pytest.mark.parametrize("boundary", ["resolution", "catalog", "observer"])
def test_initialize_native_checks_and_asset_loading_keep_loop_responsive(rig, monkeypatch, boundary):
    gate = Gate()
    monkeypatch.setattr(runtime, "plan_root_for", lambda engine=None: rig.test_plan_root)

    def resolution(**kwargs):
        if boundary == "resolution":
            gate.block()

    def catalog(engine=None):
        if boundary == "catalog":
            gate.block()
        return rig.catalog_stub

    def observer(*args):
        if boundary == "observer":
            gate.block()
        return rig.observer_stub

    monkeypatch.setattr(runtime, "resonance_pc_require_client_resolution", resolution)
    monkeypatch.setattr(runtime, "catalog_for", catalog)
    monkeypatch.setattr(runtime, "make_observer", observer)

    async def run():
        return await assert_heartbeat_continues(runtime.initialize(
            1, 1, rig.app, rig.vision, rig.store,
            context=SimpleNamespace(data={"cid": "async-initialize"})), gate)

    result = asyncio.run(run())
    assert result["success"] is True
    assert rig.store.data[result["session_key"]]["status"] == "running"


def test_diagnostic_existing_log_handler_keeps_loop_responsive_without_image_files(rig, monkeypatch):
    gate = Gate()
    rig.last_observation = {"valid": True, "scene": "home", "_image": np.zeros((720, 1280, 3), dtype=np.uint8)}
    rig.log_handler.gate = gate
    rig.log_handler.gate_filter = lambda payload, record: record.levelno >= logging.ERROR
    monkeypatch.setattr(Image.Image, "save", lambda *a, **kw: pytest.fail("Diagnostics must not create PNGs"))
    asyncio.run(assert_heartbeat_continues(rig.fail(RuntimeError("test diagnostic")), gate))
    records = [r for r in rig.log_handler.records if r["level"] >= logging.ERROR]
    assert records[-1]["payload"]["state"]["status"] == "failed"
    assert "_image" not in records[-1]["payload"]["observation"]
    assert not Path(rig.state["log_dir"]).exists()


def test_record_existing_log_handler_keeps_loop_responsive_without_journal(rig):
    gate = Gate()
    rig.log_handler.gate = gate
    asyncio.run(assert_heartbeat_continues(rig.record("async-test", detail="saved"), gate))
    assert rig.log_handler.records[-1]["payload"]["detail"] == "saved"
    assert not Path(rig.state["log_dir"]).exists()


def test_finish_existing_log_handler_keeps_loop_responsive_without_file_roundtrip(rig, monkeypatch):
    result = {"run_index": 1, "outcome": "cleared", "elapsed_ms": 42}
    rig.state.update(completed_runs=1, cleared_runs=1)
    rig.round["phase"] = "completed"
    asyncio.run(rig.store.set(rig.key, rig.state))
    gate = Gate()
    rig.log_handler.gate = gate
    monkeypatch.setattr(Path, "open", lambda *a, **kw: pytest.fail("finish must not read or write files"))
    summary = asyncio.run(assert_heartbeat_continues(runtime.finish(rig.key, rig.store, round_results=[checked_round(result)]), gate))
    assert summary["success"] is True
    assert summary["rounds"] == [result]
    assert rig.key not in rig.store.data
    assert not Path(rig.state["log_dir"]).exists()


def test_cancelled_click_queued_behind_worker_never_inputs(rig, monkeypatch):
    gate, clicks = Gate(), []
    monkeypatch.setattr(runtime, "aura_click", lambda **kwargs: clicks.append(kwargs))

    async def run():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        blocker = loop.run_in_executor(None, gate.block)
        await wait_for_event(gate.entered)
        task = asyncio.create_task(rig.send_click([100, 100], "queued"))
        await asyncio.sleep(.02)
        task.cancel()
        await asyncio.sleep(.01)
        assert clicks == []
        gate.release.set()
        await blocker
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(.02)
        assert clicks == []

    try:
        asyncio.run(run())
    finally:
        gate.release.set()


def test_cancelling_started_click_waits_for_worker_and_skips_following_input(rig, monkeypatch):
    gate, clicks = Gate(), []

    def click(**kwargs):
        clicks.append((kwargs["x"], kwargs["y"]))
        gate.block()

    monkeypatch.setattr(runtime, "aura_click", click)

    async def two_clicks():
        await rig.send_click([100, 100], "first")
        await rig.send_click([200, 200], "second")

    async def run():
        task = asyncio.create_task(two_clicks())
        try:
            await wait_for_event(gate.entered)
            task.cancel()
            await asyncio.sleep(.02)
            assert not task.done(), "Cancellation returned while native click was still active"
            assert not gate.finished.is_set()
            assert clicks == [(100, 100)]
        finally:
            gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert gate.finished.is_set()
        await asyncio.sleep(.02)
        assert clicks == [(100, 100)]

    asyncio.run(run())


def test_capture_probe_keeps_loop_responsive(rig, monkeypatch):
    gate = Gate()

    def observe():
        if not gate.finished.is_set():
            gate.block()
        return {"valid": True, "scene": "home", "controls": {}}

    monkeypatch.setattr(rig.observer, "observe", observe)
    result = asyncio.run(assert_heartbeat_continues(rig.scene("home"), gate))
    assert result["scene"] == "home"


def test_cancelled_inflight_probe_finishes_before_failure_diagnostics(rig, monkeypatch):
    gate, saved_after_probe = Gate(), []

    def observe():
        gate.block()
        return {"valid": True, "scene": "home", "controls": {}}

    monkeypatch.setattr(rig.observer, "observe", observe)
    original_emit = rig.log_handler.emit

    def emit(record):
        message = record.getMessage()
        if message.startswith("[EternalScuffle]"):
            payload = json.loads(message[message.index("{"):])
            if payload.get("type") == "cancelled":
                saved_after_probe.append(gate.finished.is_set())
        return original_emit(record)

    monkeypatch.setattr(rig.log_handler, "emit", emit)

    async def run():
        await rig.store.set(rig.key, rig.state)
        # scene has no clicks and exercises invoke's cancellation diagnostics.
        task = asyncio.create_task(runtime.invoke(
            rig.key, "begin_round", rig.app, rig.vision, rig.store, run_index=1))
        try:
            await wait_for_event(gate.entered)
            task.cancel()
            await asyncio.sleep(.02)
            assert not task.done()
            assert not saved_after_probe
        finally:
            gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert saved_after_probe == [True]
        assert rig.store.data[rig.key]["status"] == "cancelled"
        assert rig.store.data[rig.key]["completed_runs"] == 0
        assert not Path(rig.state["log_dir"]).exists()

    asyncio.run(run())


def test_probe_result_after_deadline_is_rejected_even_when_it_matches(rig, monkeypatch):
    gate, clock = Gate(), [0.0]
    # Advance the runtime clock explicitly after the worker has started. The
    # shared poller and asyncio retain their real clocks, avoiding timing races.
    monkeypatch.setattr(runtime, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def probe():
        gate.block()
        return {"matches": True}

    async def run():
        task = asyncio.create_task(runtime._poll_until(
            timeout=.01, interval=.001, probe=probe, predicate=lambda row: row["matches"]))
        try:
            await wait_for_event(gate.entered)
            clock[0] = .05
            assert not task.done()
        finally:
            gate.release.set()
        ok, observation = await task
        assert gate.finished.is_set()
        assert observation["matches"] is True
        assert ok is False

    asyncio.run(run())


def test_guard_past_transition_deadline_never_clicks(rig, monkeypatch):
    gate, clock, clicks = Gate(), [0.0], []
    monkeypatch.setattr(runtime, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(runtime, "TRANSITION_TIMEOUT", .01)
    monkeypatch.setattr(runtime, "aura_click", lambda **kwargs: clicks.append(kwargs))
    frame = {"valid": True, "scene": "home", "controls": {"play": {"center": [100, 100]}}}

    async def scene(*args, **kwargs):
        return frame

    monkeypatch.setattr(rig, "scene", scene)

    def guard(observation):
        gate.block()
        return True

    async def run():
        task = asyncio.create_task(rig.click_transition("home", "play", ("coin",), guard=guard))
        try:
            await wait_for_event(gate.entered)
            clock[0] = .05
            assert not task.done()
        finally:
            gate.release.set()
        with pytest.raises(runtime.ScuffleError, match="transition_timeout"):
            await task
        assert clicks == []

    asyncio.run(run())
