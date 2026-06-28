# -*- coding: utf-8 -*-
"""Aura 框架的核心调度器。

此模块定义了 `Scheduler` 类，它是整个 Aura 框架的"大脑"和主入口点。
`Scheduler` 负责初始化和协调所有核心服务，管理主事件循环，处理任务的
入队和执行，并提供一个统一的外部 API 来与框架交互。

主要职责:
- **生命周期管理**: 启动和停止主事件循环以及所有相关的后台服务。
- **服务协调**: 初始化并持有对所有核心服务（如 `PlanManager`,
  `ExecutionManager`, `EventBus` 等）的引用。
- **资源加载**: 协调 `PlanManager` 加载所有插件、任务、配置、调度项和中断规则。
- **任务队列**: 管理主任务队列、事件驱动任务队列和中断队列。
- **主循环与消费者**: 运行多个异步消费者来处理不同队列中的任务。
- **状态查询 API**: 提供一系列线程安全的方法来查询框架的内部状态，如
  运行状态、计划任务、服务和中断等。
- **热重载**: 实现 `HotReloadPolicy` 来监控文件系统变动，并触发对
  任务或整个插件的实时、动态重载。
"""
import asyncio
import os
import queue
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Any, List, Optional

from packages.aura_core.observability.events import EventBus, Event
from packages.aura_core.context.persistence.store_service import StateStoreService
from packages.aura_core.config.service import ConfigService
from packages.aura_core.scheduler.queues.task_queue import TaskQueue, Tasklet
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.api.registries import service_registry
from packages.aura_core.utils.id_generator import SnowflakeGenerator
from packages.aura_core.config.loader import get_config_value
from packages.aura_core.runtime.profiles import resolve_runtime_profile, RuntimeProfile
from packages.aura_core.runtime.privilege import ensure_admin_startup
from packages.aura_core.observability.service import ObservabilityService
from packages.aura_core.packaging.core.plan_registry import PlanRegistry
from packages.aura_core.packaging.core.workspace_service import PlanWorkspaceService
from packages.aura_core.services import YoloService
from packages.aura_core.scheduler.execution.dispatcher import DispatchService
from packages.aura_core.scheduler.execution.service import ExecutionService
from packages.aura_core.utils.hot_reload import HotReloadPolicy

# 导入核心管理器
from packages.aura_core.scheduler.execution.manager import ExecutionManager
from packages.aura_core.scheduler.queues.interrupt import InterruptService
from packages.aura_core.packaging.core.plan_manager import PlanManager
from packages.aura_core.scheduler.scheduling_service import SchedulingService

# 导入子管理器
from .lifecycle import LifecycleManager
from .task_dispatcher import TaskDispatcher
from .state_manager import StateManager
from .runtime_state import SchedulerRuntimeState
from .validation import InputValidator
from .ui_bridge import UIBridge
from .runtime_lifecycle import RuntimeLifecycleService
from .run_query import RunQueryService
from .hot_reload_control import HotReloadControlService
from .tasklet_identity import TaskletIdentityService

if TYPE_CHECKING:
    from .orchestrator import Orchestrator

class Scheduler:
    """Aura 框架的核心调度器和总协调器。"""

    def __init__(self, runtime_profile: str = "api_full"):
        """初始化 Scheduler 实例。

        此构造函数会初始化所有核心服务和状态属性，并执行首次的资源加载。
        """
        ensure_admin_startup("Aura Scheduler")
        # --- 核心属性与状态 (非异步部分) ---
        self.base_path = self._resolve_base_path()
        if str(self.base_path) not in sys.path:
            sys.path.insert(0, str(self.base_path))
        self.runtime_profile: RuntimeProfile = resolve_runtime_profile(runtime_profile)

        self._main_task: Optional[asyncio.Task] = None
        self._scheduler_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._hot_reload_observer = None

        self.num_event_workers = int(get_config_value("scheduler.num_event_workers", 1))
        self.startup_complete_event = threading.Event()
        self._pre_start_task_buffer: List = []
        self.fallback_lock = threading.RLock()

        # 添加启动锁，保护启动/停止操作
        self._startup_lock = threading.Lock()
        # 新增：资源清理控制
        self._shutdown_lock = threading.Lock()
        self._shutdown_done = False

        self.pause_event = asyncio.Event()
        self.pause_event.set()

        self.id_generator = SnowflakeGenerator(
            instance=int(get_config_value("id_generator.instance_id", 1)),
            epoch=int(get_config_value("id_generator.epoch_ms", 1609459200000)),
        )

        # --- 异步组件 (运行时初始化) ---
        self.is_running: Optional[asyncio.Event] = None
        self.task_queue: Optional[TaskQueue] = None
        self.event_task_queue: Optional[TaskQueue] = None
        self.interrupt_queue: Optional[asyncio.Queue[Dict]] = None
        self.state = SchedulerRuntimeState()

        # API log queue is bounded to avoid unbounded log growth when no consumer is attached.
        self.api_log_queue: queue.Queue = queue.Queue(
            maxsize=max(int(get_config_value("logging.api_queue_maxsize", 2000)), 1)
        )

        # --- 服务实例 ---
        self.config_service = ConfigService()
        self.event_bus = EventBus()
        self.state_store = StateStoreService(config=self.config_service)
        self.plan_manager = PlanManager(
            str(self.base_path),
            self.pause_event,
            runtime_services_provider=self._get_runtime_services,
            service_resolver=self._resolve_service,
            orchestrator_factory=self._create_orchestrator,
        )
        self.execution_manager = ExecutionManager(
            self,
            max_concurrent_tasks=int(get_config_value("execution.max_concurrent_tasks", 1)),
            io_workers=int(get_config_value("execution.io_workers", 16)),
            cpu_workers=int(get_config_value("execution.cpu_workers", 4)),
        )
        self.scheduling_service = SchedulingService(self)
        self.interrupt_service = InterruptService(self)

        from packages.aura_core.utils.file_watcher import FileWatcherService
        self.file_watcher_service = FileWatcherService(self.event_bus)
        self.yolo_service = YoloService(self.config_service)

        # --- service wrappers ---
        self.plan_registry = PlanRegistry(self)
        self.dispatch = DispatchService(self)
        self.executor = ExecutionService(self)
        self.hot_reload = HotReloadPolicy(self)

        # --- 运行/调度状态 ---

        # --- Observability/UI ---
        self.observability = ObservabilityService(
            event_bus=self.event_bus,
            base_path=self.base_path,
            running_tasks_provider=self.get_running_tasks_count,
        )
        # UI event queue is unbounded to avoid dropping updates under burst.
        self.ui_event_queue = self.observability.get_ui_event_queue()
        self.ui_update_queue: Optional[queue.Queue] = None
        # Core subscriptions should be registered only once.
        self._core_subscriptions_ready = False

        # --- 初始化子管理器 ---
        self.lifecycle = LifecycleManager(self)
        self.dispatcher = TaskDispatcher(self)
        self.state_mgr = StateManager(self)
        self.plan_mgr = PlanWorkspaceService(self)
        self.validator = InputValidator(self)
        self.ui_bridge = UIBridge(self)
        self.runtime_lifecycle = RuntimeLifecycleService(self)
        self.query_service = RunQueryService(self)
        self.hot_reload_control = HotReloadControlService(self)
        self.tasklet_identity = TaskletIdentityService(self)

        # --- 初始化日志和核心服务 ---
        logger.setup(
            log_dir=str(get_config_value("logging.log_dir", "logs")),
            task_name=str(get_config_value("logging.task_name.default", "aura_session")),
            api_log_queue=self.api_log_queue
        )
        self._register_core_services()
        self.reload_plans()

    @staticmethod
    def _resolve_base_path() -> Path:
        """解析项目基础路径

        Returns:
            项目的基础路径
        """
        env_base = os.getenv("AURA_BASE_PATH")
        if env_base:
            return Path(env_base).resolve()
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent
        return Path(__file__).resolve().parents[3]

    def _create_orchestrator(self, **kwargs):
        from .orchestrator import Orchestrator

        return Orchestrator(**kwargs)

    def run_on_control_loop(self, coro, *, timeout: Optional[float] = None):
        """Run a coroutine on scheduler loop and wait for result."""
        if not self._loop or not self._loop.is_running():
            raise RuntimeError("Scheduler event loop is not running.")
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is self._loop:
            if asyncio.iscoroutine(coro):
                coro.close()
            raise RuntimeError(
                "run_on_control_loop cannot be called from the control loop; "
                "await the coroutine directly."
            )
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def _get_runtime_services(self) -> Dict[str, Any]:
        """Return currently available runtime service instances."""
        return service_registry.get_all_services()

    def _get_service_definitions(self):
        """Return a snapshot of all registered service definitions."""
        return service_registry.get_all_service_definitions()

    def _resolve_service(self, service_id: str) -> Any:
        """Resolve one service instance from registry."""
        return service_registry.get_service_instance(service_id)

    def _clear_service_registry(self):
        """Clear all service registrations."""
        service_registry.clear()

    def _remove_services_by_prefix(self, prefix: str):
        """Remove services by fqid prefix from registry."""
        service_registry.remove_services_by_prefix(prefix)

    def _register_core_services(self):
        """(私有) 向服务注册表注册所有框架核心服务。

        实现来自: scheduler.py 行450-472
        """
        # 注意：旧系统的set_project_base_path已随builder模块删除
        # 新系统不需要这个函数，因为不再从源码构建manifest

        service_registry.register_instance('config', self.config_service, public=True, fqid='core/config')
        service_registry.register_instance('state_store', self.state_store, public=True, fqid='core/state_store')
        service_registry.register_instance('event_bus', self.event_bus, public=True, fqid='core/event_bus')

        service_registry.register_instance('scheduler', self, public=True, fqid='core/scheduler')
        service_registry.register_instance('plan_manager', self.plan_manager, public=False, fqid='core/plan_manager')
        service_registry.register_instance('execution_manager', self.execution_manager, public=False,
                                           fqid='core/execution_manager')
        service_registry.register_instance('scheduling_service', self.scheduling_service, public=False,
                                           fqid='core/scheduling_service')
        service_registry.register_instance('interrupt_service', self.interrupt_service, public=False,
                                           fqid='core/interrupt_service')
        service_registry.register_instance('file_watcher_service', self.file_watcher_service, public=False,
                                           fqid='core/file_watcher_service')
        service_registry.register_instance('yolo', self.yolo_service, public=True, fqid='core/yolo')

        # 手动注入 EventBus 到 StateStore
        self.state_store.set_event_bus(self.event_bus)

    def _push_full_status_update(self):
        self.ui_bridge.push_update(
            "full_status_update",
            {
                "schedule": self.get_schedule_status(),
                "services": self.get_all_services_status(),
                "interrupts": self.get_all_interrupts_status(),
                "workspace": {
                    "plans": self.get_all_plans(),
                    "actions": self.actions.get_all_action_definitions(),
                },
            },
        )

    def reload_plans(self):
        """Reload all plans and related runtime resources."""
        if self._loop and self._loop.is_running():
            self.run_on_control_loop(self.reload_plans_async(), timeout=10.0)
            return

        logger.info("======= Scheduler: start reloading framework resources =======")
        with self.fallback_lock:
            try:
                self.config_service.load_environment_configs(self.base_path)
                self.plan_registry.load_all()
                self._push_full_status_update()
            except Exception as e:
                logger.critical(f"Framework reload failed: {e}", exc_info=True)
                raise
        logger.info("======= Framework resources reload completed =======")

    async def reload_plans_async(self):
        """Async authoritative reload path for runtime control loop."""
        logger.info("======= Scheduler: start async reloading framework resources =======")
        with self.fallback_lock:
            self.config_service.load_environment_configs(self.base_path)
            self.plan_registry.load_all()
        if self._loop and self._loop.is_running():
            await self._async_reload_subscriptions()
        with self.fallback_lock:
            self._push_full_status_update()
        logger.info("======= Async framework resources reload completed =======")

    async def run(self):
        """Run the scheduler runtime loop."""
        await self.runtime_lifecycle.run()

    def start_scheduler(self):
        """启动调度器的主事件循环和所有后台服务。

        委托给: LifecycleManager.start()
        """
        self.lifecycle.start()

    def stop_scheduler(self):
        """优雅地停止调度器和所有后台服务。

        委托给: LifecycleManager.stop()
        """
        self.lifecycle.stop()

    # ========== 委托方法 - 状态管理 ==========

    def update_run_status(self, item_id: str, status_update: Dict[str, Any]):
        """线程安全地更新一个计划任务的运行状态。

        委托给: StateManager.update_run_status()
        """
        self.state_mgr.update_run_status(item_id, status_update)

    def get_master_status(self) -> dict:
        """获取调度器的宏观运行状态。

        委托给: StateManager.get_master_status()
        """
        return self.state_mgr.get_master_status()

    def get_schedule_status(self):
        """获取所有预定义计划任务的当前状态列表。

        委托给: StateManager.get_schedule_status()
        """
        return self.state_mgr.get_schedule_status()

    def get_running_tasks_count(self) -> int:
        """线程安全地获取运行中任务数量。

        委托给: StateManager.get_running_tasks_count()
        """
        return self.state_mgr.get_running_tasks_count()

    def get_running_tasks_snapshot(self) -> Dict[str, Any]:
        """获取运行中任务的快照（线程安全）。

        委托给: StateManager.get_running_tasks_snapshot()
        """
        return self.state_mgr.get_running_tasks_snapshot()

    # ========== 委托方法 - 任务调度 ==========

    def run_manual_task(self, task_id: str):
        """手动运行计划任务。

        委托给: TaskDispatcher.run_manual_task()
        """
        return self.dispatcher.run_manual_task(task_id)

    def run_ad_hoc_task(self, plan_name: str, task_name: str, params: Optional[Dict[str, Any]] = None, temp_id: Optional[str] = None):
        """运行临时任务。

        委托给: TaskDispatcher.run_ad_hoc_task()
        """
        return self.dispatcher.run_ad_hoc_task(plan_name, task_name, params, temp_id)

    def run_batch_ad_hoc_tasks(self, tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
        """批量运行临时任务。

        委托给: TaskDispatcher.run_batch_ad_hoc_tasks()
        """
        return self.dispatcher.run_batch_ad_hoc_tasks(tasks)

    def cancel_task(self, cid: str) -> Dict[str, Any]:
        """取消指定任务。

        委托给: TaskDispatcher.cancel_task()
        """
        return self.dispatcher.cancel_task(cid)

    # ========== 委托方法 - Plan文件管理 ==========

    def delete_plan(self, plan_name: str, *, dry_run: bool = False, backup: bool = True, force: bool = False) -> Dict[str, Any]:
        """删除方案。

        委托给: PlanFileManager.delete_plan()
        """
        return self.plan_mgr.delete_plan(plan_name, dry_run=dry_run, backup=backup, force=force)

    def get_all_plans(self) -> List[str]:
        """获取所有已加载 Plan 的名称列表。

        委托给: PlanFileManager.get_all_plans()
        """
        return self.plan_mgr.get_all_plans()

    def get_plan_files(self, plan_name: str) -> Dict[str, Any]:
        """获取指定 Plan 的文件目录树结构。

        委托给: PlanFileManager.get_plan_files()
        """
        return self.plan_mgr.get_plan_files(plan_name)

    def get_tasks_for_plan(self, plan_name: str) -> List[str]:
        """获取指定 Plan 下所有任务的名称列表。

        委托给: PlanFileManager.get_tasks_for_plan()
        """
        return self.plan_mgr.get_tasks_for_plan(plan_name)

    async def get_file_content(self, plan_name: str, relative_path: str) -> str:
        """异步、安全地读取指定 Plan 内的文件内容。

        委托给: PlanFileManager.get_file_content()
        """
        return await self.plan_mgr.get_file_content(plan_name, relative_path)

    async def get_file_content_bytes(self, plan_name: str, relative_path: str) -> bytes:
        """异步、安全地读取指定 Plan 内的文件内容（二进制）。"""
        orchestrator = self.plan_manager.get_plan(plan_name)
        if not orchestrator:
            raise FileNotFoundError(f"Plan '{plan_name}' not found or not loaded.")
        return await orchestrator.get_file_content_bytes(relative_path)

    async def save_file_content(self, plan_name: str, relative_path: str, content: Any):
        """异步、安全地向指定 Plan 内的文件写入内容。"""
        orchestrator = self.plan_manager.get_plan(plan_name)
        if not orchestrator:
            raise FileNotFoundError(f"Plan '{plan_name}' not found or not loaded.")
        await orchestrator.save_file_content(relative_path, content)
        logger.info(f"文件已通过Orchestrator异步保存: {relative_path}")

    async def create_directory(self, plan_name: str, relative_path: str):
        """异步、安全地在指定 Plan 内创建目录。"""
        orchestrator = self.plan_manager.get_plan(plan_name)
        if not orchestrator:
            raise FileNotFoundError(f"Plan '{plan_name}' not found or not loaded.")
        return await orchestrator.create_directory(relative_path)

    async def create_file(self, plan_name: str, relative_path: str, content: str = ""):
        """异步、安全地在指定 Plan 内创建文件。"""
        orchestrator = self.plan_manager.get_plan(plan_name)
        if not orchestrator:
            raise FileNotFoundError(f"Plan '{plan_name}' not found or not loaded.")
        return await orchestrator.create_file(relative_path, content)

    async def rename_path(self, plan_name: str, old_relative_path: str, new_relative_path: str):
        """异步、安全地在指定 Plan 内重命名路径。"""
        orchestrator = self.plan_manager.get_plan(plan_name)
        if not orchestrator:
            raise FileNotFoundError(f"Plan '{plan_name}' not found or not loaded.")
        return await orchestrator.rename_path(old_relative_path, new_relative_path)

    async def delete_path(self, plan_name: str, relative_path: str):
        """异步、安全地删除指定 Plan 内的路径。"""
        orchestrator = self.plan_manager.get_plan(plan_name)
        if not orchestrator:
            raise FileNotFoundError(f"Plan '{plan_name}' not found or not loaded.")
        return await orchestrator.delete_path(relative_path)

    # ========== 委托方法 - UI交互 ==========

    def set_ui_update_queue(self, q: queue.Queue):
        """设置用于向 UI 发送更新的队列。"""
        self.ui_update_queue = q
        self.ui_bridge.set_ui_update_queue(q)

    def _push_ui_update(self, msg_type: str, data: Any):
        """(私有) 向 UI 更新队列中推送一条消息。"""
        if self.ui_update_queue:
            try:
                self.ui_update_queue.put_nowait({'type': msg_type, 'data': data})
            except queue.Full:
                # 队列满时丢弃，避免抛到事件循环
                logger.warning(f"UI更新队列已满，丢弃消息: {msg_type}")
            except Exception as e:
                logger.warning(f"推送UI更新失败: {e}")

    def trigger_full_ui_update(self):
        """手动触发一次向 UI 的全量状态更新。"""
        logger.debug("Scheduler: Triggering a full UI status update for new clients.")
        payload = {
            'schedule': self.state_mgr.get_schedule_status(),
            'services': self.state_mgr.get_all_services_status(),
            'interrupts': self.state_mgr.get_all_interrupts_status(),
            'workspace': {
                'plans': self.plan_mgr.get_all_plans(),
                'actions': self.actions.get_all_action_definitions()
            }
        }
        self._push_ui_update('full_status_update', payload)

    # ========== 核心属性访问器 ==========

    @property
    def run_statuses(self) -> Dict[str, Dict[str, Any]]:
        return self.state.run_statuses

    @property
    def running_tasks(self) -> Dict[str, Any]:
        return self.state.running_tasks

    @property
    def _running_task_meta(self) -> Dict[str, Dict[str, Any]]:
        return self.state.running_task_meta

    @property
    def schedule_items(self) -> List[Dict[str, Any]]:
        return self.state.schedule_items

    @property
    def interrupt_definitions(self) -> Dict[str, Dict[str, Any]]:
        return self.state.interrupt_definitions

    @property
    def user_enabled_globals(self) -> set[str]:
        return self.state.user_enabled_globals

    @property
    def all_tasks_definitions(self) -> Dict[str, Any]:
        return self.state.all_tasks_definitions

    @property
    def task_load_errors(self) -> Dict[str, Dict[str, Any]]:
        return self.state.task_load_errors

    @property
    def plans(self) -> Dict[str, 'Orchestrator']:
        """获取所有已加载 Plan 的 `Orchestrator` 实例字典。"""
        return self.plan_manager.plans

    @property
    def actions(self):
        """获取对 Action 注册表的只读访问。"""
        from packages.aura_core.api import ACTION_REGISTRY
        return ACTION_REGISTRY

    # ========== 内部异步方法 ==========

    async def _async_reload_subscriptions(self):
        """Reload event subscriptions through runtime lifecycle service."""
        await self.runtime_lifecycle.async_reload_subscriptions()

    async def _monitor_event_subscriptions(self):
        """Monitor EventBus subscription health."""
        await self.runtime_lifecycle.monitor_event_subscriptions()

    async def _consume_main_task_queue(self):
        """Delegate main queue consumption to runtime lifecycle service."""
        await self.runtime_lifecycle.consume_main_task_queue()

    async def _consume_interrupt_queue(self):
        """Delegate interrupt queue consumption to runtime lifecycle service."""
        await self.runtime_lifecycle.consume_interrupt_queue()

    async def _event_worker_loop(self, worker_id: int):
        """Delegate event worker loop to runtime lifecycle service."""
        await self.runtime_lifecycle.event_worker_loop(worker_id)

    async def _mirror_event_to_ui_queue(self, event: Event):
        """Mirror EventBus events into UI queue."""
        await self.runtime_lifecycle.mirror_event_to_ui_queue(event)

    async def _obs_ingest_event(self, event: Event):
        """Forward events to observability ingest pipeline."""
        await self.runtime_lifecycle.obs_ingest_event(event)

    def get_all_task_definitions_with_meta(self) -> List[Dict[str, Any]]:
        """Return all task definitions with metadata."""
        return self.query_service.get_all_task_definitions_with_meta()

    def get_all_services_status(self) -> List[Dict[str, Any]]:
        """Return all registered service statuses."""
        return self.query_service.get_all_services_status()

    def get_all_interrupts_status(self) -> List[Dict[str, Any]]:
        """Return all interrupt rule statuses."""
        return self.query_service.get_all_interrupts_status()

    def get_all_services_for_api(self) -> List[Dict[str, Any]]:
        """Return API-safe service definitions."""
        return self.query_service.get_all_services_for_api()

    def get_queue_overview(self) -> Dict[str, Any]:
        """Return a snapshot of queue stats."""
        return self.query_service.get_queue_overview()

    def list_queue(self, state: str, limit: int = 200) -> Dict[str, Any]:
        """List queued items by state."""
        return self.query_service.list_queue(state, limit)

    def get_metrics_snapshot(self) -> Dict[str, Any]:
        """Return metrics snapshot."""
        return self.query_service.get_metrics_snapshot()

    async def _persist_run_snapshot(self, cid: str, run: Dict[str, Any]):
        """Delegate run snapshot persistence."""
        await self.query_service.persist_run_snapshot(cid, run)

    def list_persisted_runs(self, limit: int = 50, plan_name: Optional[str] = None,
                            task_name: Optional[str] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """List persisted runs."""
        return self.query_service.list_persisted_runs(limit=limit, plan_name=plan_name, task_name=task_name, status=status)

    def get_persisted_run(self, cid: str) -> Dict[str, Any]:
        """Get a persisted run by CID."""
        return self.query_service.get_persisted_run(cid)

    def get_run_timeline(self, cid_or_trace: str) -> Dict[str, Any]:
        """Return run timeline data."""
        return self.query_service.get_run_timeline(cid_or_trace)

    def get_ui_event_queue(self) -> queue.Queue:
        """Return UI event queue."""
        return self.query_service.get_ui_event_queue()

    def get_active_runs_snapshot(self) -> List[Dict[str, Any]]:
        """Return active runs snapshot."""
        return self.query_service.get_active_runs_snapshot()

    def get_batch_task_status(self, cids: List[str]) -> List[Dict[str, Any]]:
        """Batch get task status."""
        return self.query_service.get_batch_task_status(cids)

    def list_run_history(self, limit: int = 50, plan_name: Optional[str] = None,
                         task_name: Optional[str] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """List completed run history."""
        return self.query_service.list_run_history(limit=limit, plan_name=plan_name, task_name=task_name, status=status)

    def get_run_detail(self, cid: str) -> Dict[str, Any]:
        """Return one run detail payload."""
        return self.query_service.get_run_detail(cid)

    def get_task_load_errors(self, plan_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return structured task file load errors."""
        return self.query_service.get_task_load_errors(plan_name=plan_name)

    async def reload_all(self):
        """Perform a full destructive reload."""
        return await self.hot_reload_control.reload_all()

    def enable_hot_reload(self):
        """Enable file-system watch to hot reload plan/task files."""
        return self.hot_reload_control.enable_hot_reload()

    def disable_hot_reload(self):
        """Disable file-system watch for hot reload."""
        return self.hot_reload_control.disable_hot_reload()

    def is_hot_reload_enabled(self) -> bool:
        """Return True if the hot reload watcher is running."""
        return self.hot_reload_control.is_hot_reload_enabled()

    async def reload_task_file(self, file_path: Path):
        """Hot-reload a single task file."""
        await self.hot_reload_control.reload_task_file(file_path)

    async def reload_plugin_from_py_file(self, file_path: Path):
        """Hot-reload package from changed Python file."""
        await self.hot_reload_control.reload_plugin_from_py_file(file_path)

    async def queue_insert_at(self, index: int, plan_name: str, task_name: str,
                              params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Insert a task at a specific position in the queue."""
        return await self.dispatch.queue_insert_at(index, plan_name, task_name, params)

    async def queue_remove_task(self, cid: str) -> Dict[str, Any]:
        """Remove a task from the queue."""
        return await self.dispatch.queue_remove(cid)

    async def queue_move_to_front(self, cid: str) -> Dict[str, Any]:
        """Move a task to the front of the queue."""
        return await self.dispatch.queue_move_to_front(cid)

    async def queue_move_to_position(self, cid: str, new_index: int) -> Dict[str, Any]:
        """Move a task to a specific position in the queue."""
        return await self.dispatch.queue_move_to_position(cid, new_index)

    async def queue_list_all(self) -> List[Dict[str, Any]]:
        """List all queued tasks."""
        return await self.dispatch.queue_list_all()

    async def queue_clear(self) -> Dict[str, Any]:
        """Clear the queue."""
        return await self.dispatch.queue_clear()

    async def queue_reorder(self, cid_order: List[str]) -> Dict[str, Any]:
        """Reorder the queue."""
        return await self.dispatch.queue_reorder(cid_order)

    def update_task_priority(self, cid: str, new_priority: int) -> Dict[str, Any]:
        """调整指定任务的优先级。

        注意：此功能需要任务仍在队列中（未开始执行）。

        Args:
            cid: 任务的唯一追踪ID
            new_priority: 新的优先级值（数字越小优先级越高）

        Returns:
            包含操作结果的字典
        """
        # 由于当前的 TaskQueue 实现基于 asyncio.PriorityQueue，
        # 无法直接修改已入队任务的优先级。
        # 这需要重新实现队列或在任务未入队时设置优先级。

        logger.warning(f"Priority update for task '{cid}' is not fully implemented yet.")
        return {
            "status": "error",
            "message": "Priority update is not supported for tasks already in the queue. "
                       "This feature requires queue implementation upgrade."
        }

    async def _enqueue_schedule_item(self, item: Dict[str, Any], *, source: str,
                                     triggering_event: Optional[Event] = None) -> bool:
        """(private) Delegate schedule enqueue to dispatch service."""
        return await self.dispatch.enqueue_schedule_item(item, source=source, triggering_event=triggering_event)

    # ========== Tasklet Identifier Methods ==========

    def _base36_encode(self, num: int) -> str:
        """Encode integer to base36."""
        return self.tasklet_identity.base36_encode(num)

    def _short_cid_suffix(self, cid: Optional[str]) -> str:
        """Generate short CID suffix."""
        return self.tasklet_identity.short_cid_suffix(cid)

    def _make_trace_id(self, plan_name: str, task_name: str, cid: str,
                       when: Optional[Any] = None) -> str:
        """Generate task trace id."""
        return self.tasklet_identity.make_trace_id(plan_name, task_name, cid, when=when)

    def _make_trace_label(self, plan_name: Optional[str], task_name: Optional[str]) -> str:
        """Generate task trace label."""
        return self.tasklet_identity.make_trace_label(plan_name, task_name)

    def _build_resource_tags(self, plan_name: str, task_name: str) -> List[str]:
        """Build resource tags for a task."""
        return self.tasklet_identity.build_resource_tags(plan_name, task_name)

    def _ensure_tasklet_identifiers(self, tasklet: Tasklet,
                                    plan_name: Optional[str] = None,
                                    task_name: Optional[str] = None,
                                    source: Optional[str] = None) -> Tasklet:
        """Ensure tasklet contains stable identifiers."""
        return self.tasklet_identity.ensure_tasklet_identifiers(tasklet, plan_name=plan_name, task_name=task_name, source=source)

    async def _async_update_run_status(self, item_id: str, status_update: Dict[str, Any]):
        """Asynchronously update run status."""
        await self.state_mgr._async_update_run_status(item_id, status_update)

    def _infer_enum_type(self, enum_vals: Any) -> Optional[str]:
        """Infer enum value type."""
        return self.validator.infer_enum_type(enum_vals)

    def _normalize_input_schema(self, schema: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize meta.inputs schema."""
        return self.validator.normalize_input_schema(schema)

    def _build_default_from_schema(self, schema: Dict[str, Any]):
        """Build default value from schema."""
        return self.validator.build_default_from_schema(schema)

    def _validate_input_value(self, schema: Dict[str, Any], value: Any, path: str):
        """Validate single input value."""
        return self.validator.validate_input_value(schema, value, path)

    def _validate_inputs_against_meta(self, inputs_meta: List[Dict[str, Any]], provided_inputs: Dict[str, Any]):
        """Validate and normalize user inputs against meta.inputs."""
        return self.validator.validate_inputs_against_meta(inputs_meta, provided_inputs)

    def _resolve_task_inputs_for_dispatch(
        self,
        *,
        plan_name: str,
        task_ref: str,
        provided_inputs: Optional[Dict[str, Any]] = None,
        enforce_package: Optional[str] = None,
    ):
        """Resolve task reference and validate inputs against task meta.inputs."""
        return self.validator.resolve_and_validate_task_inputs(
            plan_name=plan_name,
            task_ref=task_ref,
            provided_inputs=provided_inputs,
            enforce_package=enforce_package,
        )
