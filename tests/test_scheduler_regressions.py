# -*- coding: utf-8 -*-

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import yaml
from unittest.mock import patch

from packages.aura_core.api.definitions import ActionDefinition, ServiceDefinition
from packages.aura_core.context.execution import ExecutionContext
from packages.aura_core.engine import action_injector as action_injector_module
from packages.aura_core.engine import action_resolver as action_resolver_module
from packages.aura_core.engine.action_injector import ActionInjector
from packages.aura_core.engine.action_resolver import ActionResolver
from packages.aura_core.packaging.core.package_manager import PackageManager
from packages.aura_core.packaging.core.task_validator import TaskDefinitionValidator, TaskValidationError
from packages.aura_core.packaging.manifest.schema import PackageInfo, PluginManifest
from packages.aura_core.api import ACTION_REGISTRY, service_registry
from packages.aura_core.observability.events import Event
from packages.aura_core.observability.logging.core_logger import QueueLogHandler
from packages.aura_core.observability.service import ObservabilityService
from packages.aura_core.scheduler import orchestrator as orchestrator_module
from packages.aura_core.scheduler.cancellation import clear_task_cancel, is_task_cancel_requested
from packages.aura_core.scheduler.execution.dispatcher import DispatchService
from packages.aura_core.scheduler.execution.manager import ExecutionManager
from packages.aura_core.scheduler.queues.task_queue import Tasklet
from packages.aura_core.scheduler.runtime_lifecycle import RuntimeLifecycleService
from packages.aura_core.scheduler.run_query import RunQueryService
from packages.aura_core.scheduler import scheduling_service as scheduling_module
from packages.aura_core.utils.middleware import Middleware, middleware_manager
from plans.aura_base.src import actions as actions_package
from plans.aura_base.src.actions import _shared as action_shared_module
from plans.aura_base.src.actions import atomic_actions as atomic_actions_module
from plans.aura_base.src.actions import ocr_actions as ocr_actions_module
from plans.aura_base.src.actions import wait_actions as wait_actions_module
from plans.aura_base.src.actions import yolo_actions as yolo_actions_module


class _DummyRenderer:
    async def get_render_scope(self):
        return {}

    async def render(self, raw, scope=None):
        return raw


class _DummyEngine:
    orchestrator = SimpleNamespace(plan_name="demo")


class _DummySchedulerForDispatch:
    def __init__(self, *, resolve_ok=True, queue_raises=False):
        self.fallback_lock = threading.RLock()
        self.run_statuses = {}
        self._resolve_ok = resolve_ok
        self._queue_raises = queue_raises
        self.task_queue = self

    def _resolve_task_inputs_for_dispatch(self, **_kwargs):
        if not self._resolve_ok:
            return False, "invalid inputs"

        return (
            True,
            {
                "resolved": SimpleNamespace(task_ref="tasks:ok.yaml"),
                "full_task_id": "demo/tasks:ok.yaml",
                "task_def": {},
                "validated_inputs": {"k": "v"},
            },
        )

    def _ensure_tasklet_identifiers(self, _tasklet, **_kwargs):
        return None

    async def put(self, _tasklet):
        if self._queue_raises:
            raise RuntimeError("queue put failed")


class _DummySchedulerForRunQuery:
    def __init__(self, service_defs):
        self.fallback_lock = threading.RLock()
        self._service_defs = service_defs

    def _get_service_definitions(self):
        return list(self._service_defs)


class _TraceMiddleware(Middleware):
    def __init__(self, sink):
        self.sink = sink

    async def handle(self, action_def, context, params, next_handler):
        self.sink.append("before")
        next_params = dict(params)
        next_params["value"] = next_params["value"] + 1
        result = await next_handler(action_def, context, next_params)
        self.sink.append("after")
        return result * 2


class _DummyEventBus:
    def __init__(self):
        self.published: list[Event] = []

    async def publish(self, event: Event):
        self.published.append(event)


class _RecordingEventBus:
    def __init__(self):
        self.subscriptions: list[dict] = []
        self.cleared_keep_persistent: list[bool] = []

    async def subscribe(self, **kwargs):
        self.subscriptions.append(dict(kwargs))

    async def clear_subscriptions(self, keep_persistent=True):
        self.cleared_keep_persistent.append(bool(keep_persistent))

    def get_stats(self):
        return {
            "total_subscriptions": len(self.subscriptions),
            "persistent_subscriptions": len(self.subscriptions),
            "transient_subscriptions": 0,
            "active_loops": 0,
            "unique_patterns": len({item["event_pattern"] for item in self.subscriptions}),
        }


def _build_manifest() -> PluginManifest:
    return PluginManifest(
        package=PackageInfo(
            name="@demo/pkg",
            version="1.0.0",
            description="demo",
            license="MIT",
        )
    )


def test_enqueue_schedule_item_rolls_back_status_when_input_validation_fails():
    scheduler = _DummySchedulerForDispatch(resolve_ok=False)
    scheduler.run_statuses["item-1"] = {
        "status": "idle",
        "last_run": datetime.now() - timedelta(minutes=10),
    }
    service = DispatchService(scheduler)

    ok = asyncio.run(
        service.enqueue_schedule_item(
            {
                "id": "item-1",
                "plan_name": "demo",
                "task": "tasks:broken.yaml",
                "enabled": True,
                "run_options": {},
            },
            source="schedule",
        )
    )

    assert ok is False
    assert scheduler.run_statuses["item-1"]["status"] == "idle"
    assert "queued_at" not in scheduler.run_statuses["item-1"]


def test_enqueue_schedule_item_rolls_back_status_when_queue_put_fails():
    scheduler = _DummySchedulerForDispatch(resolve_ok=True, queue_raises=True)
    service = DispatchService(scheduler)

    ok = asyncio.run(
        service.enqueue_schedule_item(
            {
                "id": "item-2",
                "plan_name": "demo",
                "task": "tasks:ok.yaml",
                "enabled": True,
                "run_options": {},
            },
            source="schedule",
        )
    )

    assert ok is False
    assert "item-2" not in scheduler.run_statuses


def test_cron_trigger_check_skips_gracefully_when_croniter_missing(monkeypatch):
    scheduler = SimpleNamespace(fallback_lock=threading.RLock(), schedule_items=[], run_statuses={})
    service = scheduling_module.SchedulingService(scheduler)

    monkeypatch.setattr(scheduling_module, "CRONITER_AVAILABLE", False)
    monkeypatch.setattr(scheduling_module, "croniter", None)

    matched = service._has_cron_trigger_match(
        {
            "id": "item-cron",
            "triggers": [{"type": "cron", "expression": "*/5 * * * *"}],
        },
        datetime.now(),
        {},
    )

    assert matched is False
    assert service._croniter_missing_logged is True


def test_resource_tag_parser_accepts_colon_rich_tags():
    manager = ExecutionManager(scheduler=SimpleNamespace())
    tasklet = Tasklet(
        task_name="demo/task",
        resource_tags=[
            "__mutex_group__:alpha:1",
            "__max_instances__:demo/task:2",
        ],
    )

    semaphores = asyncio.run(manager._get_semaphores_for(tasklet))

    assert len(semaphores) == 3
    assert "__mutex_group__:alpha" in manager._resource_sems
    assert "__max_instances__:demo/task" in manager._resource_sems


def test_tasklet_default_timeout_is_12_hours():
    assert Tasklet(task_name="demo/task").timeout == 12 * 60 * 60


def test_execution_manager_requests_cooperative_cancel_on_timeout(monkeypatch):
    cid = "timeout-cid"
    clear_task_cancel(cid)

    scheduler = SimpleNamespace(
        fallback_lock=threading.RLock(),
        running_tasks={},
        _running_task_meta={},
        all_tasks_definitions={"demo/task": {"meta": {}}},
        update_run_status=lambda *_args, **_kwargs: None,
    )
    manager = ExecutionManager(scheduler=scheduler)
    manager._io_pool = object()
    manager._cpu_pool = object()

    async def _timeout_run(_tasklet):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(manager, "_run_execution_chain", _timeout_run)

    asyncio.run(
        manager.submit(
            Tasklet(
                task_name="demo/task",
                cid=cid,
                payload={"id": f"adhoc:{cid}"},
                timeout=1.0,
            )
        )
    )

    assert is_task_cancel_requested(cid) is True
    clear_task_cancel(cid)


def test_services_api_serialization_handles_manifest_plugins_and_unknown_objects():
    manifest_plugin = _build_manifest()
    weird_plugin = object()

    service_defs = [
        ServiceDefinition(
            alias="svc1",
            fqid="demo/svc1",
            service_class=dict,
            plugin=manifest_plugin,
            public=True,
        ),
        ServiceDefinition(
            alias="svc2",
            fqid="demo/svc2",
            service_class=list,
            plugin=weird_plugin,
            public=True,
        ),
    ]
    query_service = RunQueryService(_DummySchedulerForRunQuery(service_defs))

    rows = query_service.get_all_services_for_api()

    assert rows[0]["plugin"]["name"] == "@demo/pkg"
    assert rows[0]["plugin"]["canonical_id"] == "demo/pkg"
    assert rows[0]["plugin"]["version"] == "1.0.0"
    assert rows[1]["plugin"]["name"] is None
    assert rows[1]["plugin"]["canonical_id"] is None


def test_action_injector_executes_through_middleware(monkeypatch):
    trace = []

    async def sample_action(value: int):
        trace.append("action")
        return value + 1

    action_def = ActionDefinition(
        func=sample_action,
        name="sample_action",
        read_only=False,
        public=True,
        service_deps={},
        plugin=_build_manifest(),
        is_async=True,
    )

    monkeypatch.setattr(action_injector_module.ACTION_REGISTRY, "get", lambda _name: action_def)

    context = ExecutionContext()
    injector = ActionInjector(
        context=context,
        engine=_DummyEngine(),
        renderer=_DummyRenderer(),
        services={},
    )
    injector.action_resolver = SimpleNamespace(resolve=lambda name: name)

    existing_middlewares = list(middleware_manager._middlewares)
    middleware_manager._middlewares.clear()
    middleware_manager.add(_TraceMiddleware(trace))
    try:
        result = asyncio.run(injector.execute("pkg/sample_action", {"value": 1}))
    finally:
        middleware_manager._middlewares.clear()
        middleware_manager._middlewares.extend(existing_middlewares)

    assert result == 6
    assert trace == ["before", "action", "after"]


def test_action_resolver_keeps_local_bare_action_resolution(monkeypatch):
    current_package = SimpleNamespace(
        package=SimpleNamespace(canonical_id="demo/pkg"),
        dependencies={},
        extends=[],
    )
    resolver = ActionResolver(current_package=current_package)

    monkeypatch.setattr(
        action_resolver_module.ACTION_REGISTRY,
        "get",
        lambda fqid: object() if fqid == "demo/pkg/click" else None,
    )

    assert resolver.resolve("click") == "demo/pkg/click"


def test_action_resolver_does_not_fallback_to_external_package_for_bare_name(monkeypatch):
    current_package = SimpleNamespace(
        package=SimpleNamespace(canonical_id="demo/pkg"),
        dependencies={},
        extends=[],
    )
    resolver = ActionResolver(current_package=current_package)

    external_def = SimpleNamespace(
        fqid="other/pkg/click",
        plugin=SimpleNamespace(package=SimpleNamespace(canonical_id="@other/pkg")),
    )

    def _get(fqid):
        if fqid == "click":
            return external_def
        return None

    monkeypatch.setattr(action_resolver_module.ACTION_REGISTRY, "get", _get)

    assert resolver.resolve("click") == "demo/pkg/click"


def test_action_resolver_requires_declared_dependency_for_explicit_external_action():
    current_package = SimpleNamespace(
        package=SimpleNamespace(canonical_id="demo/pkg"),
        dependencies={},
        extends=[],
    )
    resolver = ActionResolver(current_package=current_package)

    try:
        resolver.resolve("other/pkg/click")
        assert False, "expected undeclared external action to fail"
    except ValueError as exc:
        assert "undeclared external package" in str(exc)


def test_action_resolver_accepts_declared_dependency_for_explicit_external_action():
    current_package = SimpleNamespace(
        package=SimpleNamespace(canonical_id="demo/pkg"),
        dependencies={"@other/pkg": SimpleNamespace()},
        extends=[],
    )
    resolver = ActionResolver(current_package=current_package)

    assert resolver.resolve("other/pkg/click") == "other/pkg/click"


def test_task_validator_accepts_list_payload_under_logical_dep_operator():
    validator = TaskDefinitionValidator(
        plan_name="demo",
        enable_schema_validation=False,
        strict_validation=True,
    )

    validator._validate_depends_on_syntax(
        {
            "all": [
                "prepare",
                {"fetch": "success|skipped"},
            ]
        },
        file_path=Path("demo.yaml"),
        task_name="demo_task",
        step_id="finish",
        field_path="depends_on",
    )


def test_task_validator_rejects_top_level_list_dependency_shorthand():
    validator = TaskDefinitionValidator(
        plan_name="demo",
        enable_schema_validation=False,
        strict_validation=True,
    )

    try:
        validator._validate_depends_on_syntax(
            ["a", "b"],
            file_path=Path("demo.yaml"),
            task_name="demo_task",
            step_id="finish",
            field_path="depends_on",
        )
        assert False, "expected deprecated list shorthand to fail"
    except TaskValidationError as exc:
        assert exc.code == "deprecated_syntax"


def test_orchestrator_return_rendering_failure_includes_original_error(monkeypatch):
    class _BrokenRenderer:
        def __init__(self, *_args, **_kwargs):
            pass

        async def render(self, _raw):
            raise RuntimeError("boom")

    monkeypatch.setattr(orchestrator_module, "TemplateRenderer", _BrokenRenderer)

    orchestrator = orchestrator_module.Orchestrator.__new__(orchestrator_module.Orchestrator)
    orchestrator.state_store = None

    try:
        asyncio.run(
            orchestrator._render_task_returns(
                final_context=SimpleNamespace(data={}),
                returns_template={"value": "{{ broken }}"},
                full_task_id="demo/task",
            )
        )
        assert False, "expected returns rendering to fail"
    except ValueError as exc:
        message = str(exc)
        assert "demo/task" in message
        assert "RuntimeError: boom" in message
        assert "returns=" in message


def test_math_compute_rejects_unsupported_power_operator():
    assert atomic_actions_module.math_compute("2 ** 10") is None


def test_math_compute_keeps_basic_arithmetic_support():
    assert atomic_actions_module.math_compute("(1 + 2) * 3 / 2") == 4.5


def test_wait_for_text_uses_async_polling_path(monkeypatch):
    results = [
        ocr_actions_module.OcrResult(found=False),
        ocr_actions_module.OcrResult(found=True, text="ok"),
    ]
    calls = []

    def _fake_find_text(*_args, **_kwargs):
        calls.append("find_text")
        return results.pop(0)

    async def _fake_to_thread(func, *args, **kwargs):
        calls.append("to_thread")
        return func(*args, **kwargs)

    async def _fake_sleep(delay):
        calls.append(("sleep", delay))

    monkeypatch.setattr(wait_actions_module, "find_text", _fake_find_text)
    monkeypatch.setattr(wait_actions_module, "OcrResult", ocr_actions_module.OcrResult)
    monkeypatch.setattr(action_shared_module.asyncio, "to_thread", _fake_to_thread)
    monkeypatch.setattr(action_shared_module.asyncio, "sleep", _fake_sleep)

    result = asyncio.run(
        wait_actions_module.wait_for_text(
            app=None,
            ocr=None,
            engine=None,
            text_to_find="ok",
            timeout=1.0,
            interval=0.25,
        )
    )

    assert result.found is True
    assert calls.count("to_thread") == 2
    assert ("sleep", 0.25) in calls


def test_sleep_action_awaits_asyncio_sleep(monkeypatch):
    observed = []

    async def _fake_sleep(delay):
        observed.append(delay)

    monkeypatch.setattr(wait_actions_module, "asyncio", SimpleNamespace(sleep=_fake_sleep), raising=False)

    assert asyncio.run(wait_actions_module.sleep(0.5)) is True
    assert observed == [0.5]


def test_poll_until_reraises_cancelled_error(monkeypatch):
    calls = []

    async def _fake_to_thread(func, *args, **kwargs):
        calls.append("to_thread")
        return func(*args, **kwargs)

    async def _fake_sleep(delay):
        calls.append(("sleep", delay))
        raise asyncio.CancelledError()

    monkeypatch.setattr(action_shared_module.asyncio, "to_thread", _fake_to_thread)
    monkeypatch.setattr(action_shared_module.asyncio, "sleep", _fake_sleep)

    async def _run():
        return await action_shared_module.poll_until(
            timeout=1.0,
            interval=0.25,
            probe=lambda: False,
            predicate=bool,
        )

    try:
        asyncio.run(_run())
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("poll_until swallowed asyncio.CancelledError")

    assert calls == ["to_thread", ("sleep", 0.25)]


def test_sleep_action_reraises_cancelled_error(monkeypatch):
    observed = []

    async def _fake_sleep(delay):
        observed.append(delay)
        raise asyncio.CancelledError()

    monkeypatch.setattr(wait_actions_module, "asyncio", SimpleNamespace(sleep=_fake_sleep), raising=False)

    try:
        asyncio.run(wait_actions_module.sleep(0.5))
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("sleep action swallowed asyncio.CancelledError")

    assert observed == [0.5]


def test_observability_ui_queue_is_bounded_and_drops_oldest(monkeypatch, tmp_path):
    def _fake_get_config_value(key, default=None):
        if key == "observability.ui_event_queue_maxsize":
            return 2
        return default

    monkeypatch.setattr("packages.aura_core.observability.service.get_config_value", _fake_get_config_value)

    service = ObservabilityService(event_bus=None, base_path=tmp_path)

    asyncio.run(service.mirror_event_to_ui_queue(Event(name="one", payload={"i": 1})))
    asyncio.run(service.mirror_event_to_ui_queue(Event(name="two", payload={"i": 2})))
    asyncio.run(service.mirror_event_to_ui_queue(Event(name="three", payload={"i": 3})))

    queued = []
    while not service.get_ui_event_queue().empty():
        queued.append(service.get_ui_event_queue().get_nowait()["name"])

    assert queued == ["two", "three"]


def test_observability_metrics_update_is_rate_limited(monkeypatch, tmp_path):
    bus = _DummyEventBus()

    def _fake_get_config_value(key, default=None):
        if key == "observability.metrics_emit_interval_ms":
            return 1000
        return default

    monkeypatch.setattr("packages.aura_core.observability.service.get_config_value", _fake_get_config_value)

    service = ObservabilityService(event_bus=bus, base_path=tmp_path)

    asyncio.run(
        service.ingest_event(
            Event(name="queue.enqueued", payload={"cid": "cid-1", "game_name": "resonance", "task_name": "tasks:demo"})
        )
    )
    asyncio.run(
        service.ingest_event(
            Event(name="queue.dequeued", payload={"cid": "cid-1", "game_name": "resonance", "task_name": "tasks:demo"})
        )
    )

    metric_events = [event for event in bus.published if event.name == "metrics.update"]
    assert len(metric_events) == 1


def test_observability_trace_index_is_pruned_with_completed_runs(monkeypatch, tmp_path):
    service = ObservabilityService(event_bus=None, base_path=tmp_path)

    asyncio.run(
        service.ingest_event(
            Event(name="task.started", payload={"cid": "cid-1", "trace_id": "trace-1", "task_name": "tasks:demo"})
        )
    )
    asyncio.run(
        service.ingest_event(
            Event(
                name="task.finished",
                payload={"cid": "cid-1", "trace_id": "trace-1", "task_name": "tasks:demo", "final_status": "success"},
            )
        )
    )

    assert service._obs_runs_by_trace["trace-1"] == "cid-1"

    service.completed_task_ttl = 0
    for run in service._obs_completed.values():
        run["completed_timestamp"] = 0.0

    service._cleanup_completed_tasks()

    assert "trace-1" not in service._obs_runs_by_trace


def test_queue_log_handler_drops_oldest_when_bounded_queue_is_full():
    log_queue = queue.Queue(maxsize=1)
    handler = QueueLogHandler(log_queue)
    handler.setFormatter(logging.Formatter("%(message)s"))

    first = logging.LogRecord("test", logging.INFO, __file__, 1, "first", (), None)
    second = logging.LogRecord("test", logging.INFO, __file__, 2, "second", (), None)

    handler.emit(first)
    handler.emit(second)

    assert log_queue.qsize() == 1
    assert log_queue.get_nowait() == "second"


def test_runtime_lifecycle_registers_explicit_ui_subscriptions_only():
    event_bus = _RecordingEventBus()
    scheduler = SimpleNamespace(
        _core_subscriptions_ready=False,
        event_bus=event_bus,
        runtime_profile=SimpleNamespace(enable_event_triggers=False),
        dispatcher=SimpleNamespace(subscribe_event_triggers=None),
        observability=SimpleNamespace(
            mirror_event_to_ui_queue=lambda *_args, **_kwargs: None,
            ingest_event=lambda *_args, **_kwargs: None,
        ),
    )
    service = RuntimeLifecycleService(scheduler)

    asyncio.run(service.async_reload_subscriptions())

    patterns = [item["event_pattern"] for item in event_bus.subscriptions]
    assert "*" not in patterns
    assert patterns == [
        "scheduler.started",
        "metrics.update",
        "queue.*",
        "task.*",
        "node.*",
        "task.*",
        "node.*",
        "queue.*",
    ]
    assert scheduler._core_subscriptions_ready is True


def test_yolo_wait_for_target_uses_async_polling_path(monkeypatch):
    calls = []

    async def _fake_poll_until(*, timeout, interval, probe, predicate):
        calls.append(("poll_until", timeout, interval))
        assert predicate({"found": True}) is True
        return True, {"found": True, "target_labels": ["enemy"]}

    monkeypatch.setattr(yolo_actions_module, "poll_until", _fake_poll_until)

    result = asyncio.run(
        yolo_actions_module.yolo_wait_for_target(
            yolo=None,
            app=None,
            target_labels=["enemy"],
            timeout_sec=1.5,
            poll_interval_sec=0.2,
        )
    )

    assert result["found"] is True
    assert result["target_labels"] == ["enemy"]
    assert calls[0] == ("poll_until", 1.5, 0.2)


def test_yolo_find_and_click_target_uses_async_sleep_and_to_thread(monkeypatch):
    calls = []

    class _FakeController:
        def click(self, *, x, y, button, clicks, interval):
            calls.append(("controller_click", x, y, button, clicks, interval))

    def _fake_find_target(**_kwargs):
        calls.append("find_target")
        return {
            "found": True,
            "center_point": [100, 200],
            "detection": {"id": "det-1"},
        }

    async def _fake_to_thread(func, *args, **kwargs):
        calls.append(("to_thread", getattr(func, "__name__", str(func))))
        return func(*args, **kwargs)

    async def _fake_sleep(delay):
        calls.append(("sleep", delay))

    monkeypatch.setattr(yolo_actions_module, "yolo_find_target", _fake_find_target)
    monkeypatch.setattr(yolo_actions_module.asyncio, "to_thread", _fake_to_thread)
    monkeypatch.setattr(yolo_actions_module.asyncio, "sleep", _fake_sleep)

    result = asyncio.run(
        yolo_actions_module.yolo_find_and_click_target(
            yolo=None,
            app=None,
            controller=_FakeController(),
            target_labels=["enemy"],
            click_offset=[5, -3],
            button="left",
            clicks=2,
            interval=0.15,
            post_delay_sec=0.4,
        )
    )

    assert result["clicked"] is True
    assert result["click_point"] == [105, 197]
    assert ("to_thread", "click") in calls
    assert ("sleep", 0.4) in calls


def test_split_aura_base_actions_still_register_from_new_modules():
    root = Path(__file__).resolve().parents[1]
    ACTION_REGISTRY.clear()
    service_registry.clear()
    manager = PackageManager(packages_dir=root / "packages", plans_dir=root / "plans")

    try:
        manager.load_all_packages()
        actions_by_name = {
            action_def.name: action_def.func.__module__
            for action_def in ACTION_REGISTRY.get_all_action_definitions()
        }
    finally:
        manager._unload_loaded_packages()
        ACTION_REGISTRY.clear()
        service_registry.clear()

    assert actions_by_name["wait_for_text"].endswith("wait_actions")
    assert actions_by_name["click"].endswith("input_actions")
    assert actions_by_name["math_compute"].endswith("data_actions")
    assert actions_by_name["find_image"].endswith("vision_actions")
    assert actions_by_name["aura.run_task"].endswith("task_actions")


def test_actions_package_exposes_grouped_module_entrypoints():
    exported = set(actions_package.__all__)

    assert "atomic_actions" in exported
    assert "vision_actions" in exported
    assert "wait_actions" in exported
    assert "process_actions" in exported

    assert actions_package.atomic_actions.__name__.endswith("atomic_actions")
    assert actions_package.input_actions.__name__.endswith("input_actions")


def test_aura_base_manifest_points_to_split_action_modules():
    manifest_path = Path(__file__).resolve().parents[1] / "plans" / "aura_base" / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    actions = manifest["exports"]["actions"]

    atomic_refs = [item for item in actions if item.get("module") == "plans.aura_base.src.actions.atomic_actions"]
    assert atomic_refs == []

    module_by_name = {item["name"]: item["module"] for item in actions}
    assert module_by_name["wait_for_text"] == "plans.aura_base.src.actions.wait_actions"
    assert module_by_name["click"] == "plans.aura_base.src.actions.input_actions"
    assert module_by_name["math_compute"] == "plans.aura_base.src.actions.data_actions"
    assert module_by_name["find_image"] == "plans.aura_base.src.actions.vision_actions"
    assert module_by_name["aura.run_task"] == "plans.aura_base.src.actions.task_actions"
