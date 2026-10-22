from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from packages.aura_core.scheduler.lifecycle import LifecycleManager
from packages.aura_core.scheduler.runtime_lifecycle import RuntimeLifecycleService
from packages.aura_core.scheduler.state_manager import StateManager


class _Config:
    def __init__(self, values):
        self.values = values

    def get(self, path, default=None):
        value = self.values
        for part in path.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value


def _preload_manager(services=None, *, registered=("ocr",), resolve=None):
    scheduler = SimpleNamespace(
        config_service=_Config({"services": services}) if services is not None else None,
        _get_service_definitions=Mock(
            return_value=[SimpleNamespace(alias=name) for name in registered]
        ),
        _resolve_service=resolve or Mock(),
    )
    return LifecycleManager(scheduler)


@pytest.mark.parametrize("services", [None, {}])
def test_registered_ocr_preloads_and_warms_up_without_new_configuration(services):
    ocr = SimpleNamespace(preload_engine=Mock(return_value="cpu"))
    manager = _preload_manager(services, resolve=Mock(return_value=ocr))

    asyncio.run(manager.preload_configured_services())

    manager.scheduler._resolve_service.assert_called_once_with("ocr")
    ocr.preload_engine.assert_called_once_with(warmup=True)


def test_generic_lazy_services_are_not_instantiated_by_ocr_default():
    manager = _preload_manager({}, registered=("vision", "state"))

    asyncio.run(manager.preload_configured_services())

    manager.scheduler._resolve_service.assert_not_called()


def test_explicit_and_default_ocr_targets_are_deduplicated():
    ocr = SimpleNamespace(preload_engine=Mock(return_value="cpu"))
    manager = _preload_manager(
        {"preload": {"enabled": True, "targets": ["ocr", "ocr"]}},
        resolve=Mock(return_value=ocr),
    )

    asyncio.run(manager.preload_configured_services())

    ocr.preload_engine.assert_called_once_with(warmup=True)


def test_missing_required_ocr_aborts_startup_with_original_cause():
    failure = LookupError("missing OCR registration dependency")
    manager = _preload_manager(resolve=Mock(side_effect=failure))

    with pytest.raises(RuntimeError, match="Required startup service 'ocr' is unavailable") as caught:
        asyncio.run(manager.preload_configured_services())

    assert caught.value.__cause__ is failure


@pytest.mark.parametrize("result", [False, RuntimeError("invalid OCR model")])
def test_ocr_preload_failure_is_required_even_without_config(result):
    preload = Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
    manager = _preload_manager(resolve=Mock(return_value=SimpleNamespace(preload_engine=preload)))

    with pytest.raises(RuntimeError, match="Required startup service 'ocr' failed"):
        asyncio.run(manager.preload_configured_services())

    preload.assert_called_once_with(warmup=True)


def test_required_service_without_initialization_method_fails():
    manager = _preload_manager(resolve=Mock(return_value=object()))

    with pytest.raises(RuntimeError, match="has no preload method"):
        asyncio.run(manager.preload_configured_services())


def test_optional_service_failure_warns_and_continues(monkeypatch):
    warning = Mock()
    monkeypatch.setattr("packages.aura_core.scheduler.lifecycle.logger.warning", warning)
    service = SimpleNamespace(preload_engine=Mock(side_effect=RuntimeError("optional unavailable")))
    manager = _preload_manager(
        {"optional": {"preload_on_start": True}},
        registered=(),
        resolve=Mock(return_value=service),
    )

    asyncio.run(manager.preload_configured_services())

    service.preload_engine.assert_called_once_with(warmup=False)
    warning.assert_called_once()
    assert "optional unavailable" in str(warning.call_args)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_internal_type_error_is_not_retried_without_warmup(asynchronous):
    calls = []

    def load(*, warmup=False):
        calls.append(warmup)
        raise TypeError("model construction failed internally")

    async def async_load(*, warmup=False):
        return load(warmup=warmup)

    manager = _preload_manager()

    with pytest.raises(TypeError, match="failed internally"):
        asyncio.run(manager._run_preload_method(async_load if asynchronous else load, {"warmup": True}))

    assert calls == [True]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_legacy_no_argument_preload_is_called_once(asynchronous):
    calls = []

    def load():
        calls.append("loaded")
        return "cpu"

    async def async_load():
        return load()

    manager = _preload_manager()

    result = asyncio.run(manager._run_preload_method(async_load if asynchronous else load, {"warmup": True}))

    assert result == "cpu"
    assert calls == ["loaded"]


def _runtime_scheduler(preload):
    scheduler = SimpleNamespace(
        runtime_profile=SimpleNamespace(enable_schedule_loop=False, enable_interrupt_loop=False),
        fallback_lock=threading.Lock(),
        _pre_start_task_buffer=["buffered task"],
        task_queue=SimpleNamespace(put=AsyncMock()),
        startup_complete_event=threading.Event(),
        num_event_workers=1,
        observability=SimpleNamespace(start_cleanup_task=Mock(), stop_cleanup_task=AsyncMock()),
        file_watcher_service=SimpleNamespace(start=Mock(), stop=Mock()),
        dispatch=SimpleNamespace(
            consume_interrupt_queue=AsyncMock(),
            consume_main_task_queue=AsyncMock(),
            event_worker_loop=AsyncMock(),
        ),
    )

    def initialize():
        scheduler.is_running = asyncio.Event()

    scheduler.lifecycle = SimpleNamespace(
        initialize_async_components=initialize,
        preload_configured_services=preload,
    )
    runtime = RuntimeLifecycleService(scheduler)
    runtime.async_reload_subscriptions = AsyncMock()
    runtime.monitor_event_subscriptions = AsyncMock()
    return scheduler, runtime


def test_runtime_does_not_dispatch_or_report_ready_until_preload_finishes():
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def preload():
            entered.set()
            await release.wait()

        scheduler, runtime = _runtime_scheduler(preload)
        task = asyncio.create_task(runtime.run())
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            assert not scheduler.is_running.is_set()
            assert not scheduler.startup_complete_event.is_set()
            scheduler.dispatch.consume_main_task_queue.assert_not_called()
            scheduler.dispatch.consume_interrupt_queue.assert_not_called()
            scheduler.dispatch.event_worker_loop.assert_not_called()
            scheduler.file_watcher_service.start.assert_not_called()
            scheduler.observability.start_cleanup_task.assert_not_called()
            scheduler.task_queue.put.assert_awaited_once_with("buffered task")

            release.set()
            for _ in range(20):
                if scheduler.startup_complete_event.is_set():
                    break
                await asyncio.sleep(0)
            assert scheduler.startup_complete_event.is_set()
            assert scheduler.is_running.is_set()
            await asyncio.wait_for(task, timeout=1)
            scheduler.dispatch.consume_main_task_queue.assert_awaited_once()
            scheduler.dispatch.consume_interrupt_queue.assert_awaited_once()
            scheduler.dispatch.event_worker_loop.assert_awaited_once_with(1)
        finally:
            if not task.done():
                task.cancel()
                await task

    asyncio.run(scenario())


def test_failed_preload_never_starts_task_consumers_or_signals_ready():
    async def scenario():
        failure = RuntimeError("OCR warmup failed")
        scheduler, runtime = _runtime_scheduler(AsyncMock(side_effect=failure))

        with pytest.raises(RuntimeError, match="OCR warmup failed") as caught:
            await runtime.run()

        assert caught.value is failure
        assert not scheduler.is_running.is_set()
        assert not scheduler.startup_complete_event.is_set()
        scheduler.dispatch.consume_main_task_queue.assert_not_called()
        scheduler.dispatch.consume_interrupt_queue.assert_not_called()
        scheduler.dispatch.event_worker_loop.assert_not_called()
        scheduler.file_watcher_service.start.assert_not_called()
        scheduler.observability.stop_cleanup_task.assert_awaited_once()

    asyncio.run(scenario())


def test_start_propagates_thread_failure_without_publishing_success_or_ui_running():
    failure = RuntimeError("OCR model file missing")

    async def run():
        raise failure

    scheduler = SimpleNamespace(
        startup_timeout_sec=2,
        startup_complete_event=threading.Event(),
        execution_manager=SimpleNamespace(startup=Mock(), shutdown=Mock()),
        run=run,
        is_running=threading.Event(),
        fallback_lock=threading.Lock(),
        state=SimpleNamespace(clear_active_runs=Mock()),
        actions={},
        _clear_service_registry=Mock(),
        event_bus=SimpleNamespace(publish=AsyncMock()),
        ui_bridge=SimpleNamespace(push_update=Mock()),
    )
    manager = LifecycleManager(scheduler)

    with pytest.raises(RuntimeError, match="Scheduler startup failed: OCR model file missing") as caught:
        manager.start()

    manager._scheduler_thread.join(timeout=1)
    assert not manager._scheduler_thread.is_alive()
    assert caught.value.__cause__ is failure
    assert manager._startup_error is failure
    assert scheduler.startup_complete_event.is_set()
    assert not scheduler.is_running.is_set()
    scheduler.execution_manager.shutdown.assert_called_once()
    scheduler.event_bus.publish.assert_not_called()
    scheduler.ui_bridge.push_update.assert_not_called()


@pytest.mark.parametrize(
    "thread_alive,running,ready,expected",
    [(True, False, False, False), (True, True, False, False),
     (True, False, True, False), (False, True, True, False), (True, True, True, True)],
)
def test_master_status_requires_completed_warmup_and_live_runtime(thread_alive, running, ready, expected):
    scheduler = SimpleNamespace(
        lifecycle=SimpleNamespace(_scheduler_thread=SimpleNamespace(is_alive=lambda: thread_alive)),
        is_running=threading.Event(),
        startup_complete_event=threading.Event(),
    )
    if running:
        scheduler.is_running.set()
    if ready:
        scheduler.startup_complete_event.set()

    assert StateManager(scheduler).get_master_status() == {"is_running": expected}
