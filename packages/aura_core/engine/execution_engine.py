# -*- coding: utf-8 -*-
"""执行引擎核心模块

ExecutionEngine类的重构版本，使用组合模式集成所有子组件：
- GraphBuilder: DAG图构建
- DAGScheduler: DAG调度
- NodeExecutor: 节点执行

保持与原始engine.py相同的公共API，确保向后兼容性。
"""
import asyncio
import time
import traceback
import uuid
from collections import deque
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Callable

from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.config.loader import get_config_value
from packages.aura_core.context.execution import ExecutionContext
from packages.aura_core.utils.exceptions import StopTaskException
from packages.aura_core.context.persistence.store_service import StateStoreService


# 导入子组件
from .graph_builder import GraphBuilder
from .dag_scheduler import DAGScheduler
from .node_executor import NodeExecutor


class StepState(Enum):
    """表示任务中一个步骤（节点）的执行状态。"""
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class ExecutionEngine:
    """负责单个任务的图构建、调度和执行的核心引擎（重构版）

    采用组合模式，将职责委托给专门的子组件：
    - GraphBuilder: 构建DAG图
    - DAGScheduler: 调度和执行节点
    - NodeExecutor: 执行单个节点

    每个任务在运行时都会创建一个独立的ExecutionEngine实例。
    """

    # 类变量，供子组件使用
    StepState = StepState

    def __init__(
        self,
        orchestrator: Any,
        pause_event: asyncio.Event,
        event_callback: Optional[Callable] = None
    ):
        """初始化执行引擎

        Args:
            orchestrator: 父级Orchestrator实例，用于访问共享资源
            pause_event: 一个全局的asyncio.Event，用于暂停/恢复任务执行
            event_callback: 一个可选的回调函数，用于在引擎执行过程中发送事件
        """
        self.orchestrator = orchestrator
        self.pause_event = pause_event
        self.engine_id = str(uuid.uuid4())[:8]
        self.event_callback = event_callback
        self.debug_mode = getattr(orchestrator, 'debug_mode', True)
        self.services = getattr(orchestrator, 'services', {})
        self.default_node_timeout = float(
            get_config_value("execution.default_node_timeout_sec", 0) or 0
        )

        # ===== 核心状态 =====
        self.nodes: Dict[str, Dict] = {}
        self.dependencies: Dict[str, Any] = {}
        self.reverse_dependencies: Dict[str, Set[str]] = {}
        self.step_states: Dict[str, StepState] = {}
        self.ready_queue: deque[str] = deque()
        self._ready_set: Set[str] = set()

        # ===== 上下文管理 =====
        self.root_context: Optional[ExecutionContext] = None
        self.node_contexts: Dict[str, ExecutionContext] = {}

        # ===== 异步任务管理 =====
        self.running_tasks: Set[asyncio.Task] = set()
        self.completion_event: Optional[asyncio.Event] = None

        # ===== 依赖的服务 =====
        self.state_store: StateStoreService = self.services.get('state_store')
        self.VALID_DEPENDENCY_STATUSES = {'success', 'failed', 'running', 'skipped'}

        # ===== 控制流异常机制 =====

        # ===== 节点元数据 =====
        self.node_metadata: Dict[str, Dict[str, Any]] = {}  # node_id -> metadata

        # ===== 安全机制配置 =====
        self.total_node_executions = 0
        self.max_total_steps = int(get_config_value("execution.max_total_steps", 1000))

        # ===== 创建子组件（组合模式） =====
        self.graph_builder = GraphBuilder(self)
        self.dag_scheduler = DAGScheduler(self)
        self.node_executor = NodeExecutor(self)

    # ========================================
    # 公共API - 主执行方法
    # ========================================

    async def run(
        self,
        task_data: Dict[str, Any],
        task_name: str,
        root_context: ExecutionContext
    ) -> ExecutionContext:
        """执行一个任务的主入口点

        此方法会构建依赖图，启动DAG调度器，并等待所有节点执行完毕。

        迁移自 engine.py:117-163

        Args:
            task_data: 任务的完整定义字典
            task_name: 任务的名称
            root_context: 本次任务运行的根执行上下文

        Returns:
            执行完毕后，包含了所有节点结果的最终根执行上下文
        """
        task_display_name = task_data.get('meta', {}).get('title', task_name)
        logger.info(f"======= 开始执行任务: {task_display_name} =======")

        self.root_context = root_context
        steps = task_data.get('steps', {})
        if not isinstance(steps, dict) or not steps:
            logger.info('任务中没有可执行的步骤。')
            return self.root_context

        try:
            # 使用GraphBuilder构建图
            self.graph_builder.build_graph(steps)
            # 使用DAGScheduler调度执行
            await self.dag_scheduler.run_dag_scheduler()
        except StopTaskException:
            pass
        except Exception as e:
            logger.error(
                f"!! 任务 '{task_name}' 在图构建或调度时发生严重错误: {e}",
                exc_info=True
            )
            for node_id, state in self.step_states.items():
                if state == StepState.PENDING:
                    self.step_states[node_id] = StepState.FAILED
                    error_info = {"type": type(e).__name__, "message": str(e)}
                    if self.debug_mode:
                        error_info["traceback"] = traceback.format_exc()
                    run_state = self._create_run_state(
                        StepState.FAILED, time.time(), error=error_info
                    )
                    self.root_context.add_node_result(node_id, {"run_state": run_state})

        final_status = 'success'
        for state in self.step_states.values():
            if state == StepState.FAILED:
                final_status = 'failed'
                break

        logger.info(f"======= 任务 '{task_display_name}' 执行结束 (最终状态: {final_status}) =======")
        return self.root_context

    # ========================================
    # 辅助方法（保留在核心类中）
    # ========================================

    async def _check_pause(self):
        """检查暂停事件，如果暂停则等待恢复

        迁移自 engine.py:110-116
        """
        if not self.pause_event:
            return
        if not self.pause_event.is_set():
            await self.pause_event.wait()

    def _prepare_node_context(self, node_id: str) -> ExecutionContext:
        """为即将执行的节点准备其执行上下文（通过分支和合并父上下文）

        迁移自 engine.py:303-320

        Args:
            node_id: 节点ID

        Returns:
            新的ExecutionContext实例
        """
        parent_ids = self.graph_builder.get_all_deps_from_struct(
            self.dependencies.get(node_id, [])
        )

        if not parent_ids:
            return self.root_context.fork()

        parent_contexts = [
            self.node_contexts[pid]
            for pid in parent_ids
            if pid in self.node_contexts
        ]

        if not parent_contexts:
            return self.root_context.fork()

        new_context = parent_contexts[0].fork()
        if len(parent_contexts) > 1:
            new_context.merge(parent_contexts[1:])

        return new_context

    def _on_task_completed(self, task: asyncio.Task, node_id: str):
        """异步任务完成时的回调

        迁移自 engine.py:321-339

        Args:
            task: 完成的asyncio.Task
            node_id: 对应的节点ID
        """
        self.running_tasks.discard(task)
        try:
            task.result()
        except Exception as e:
            logger.critical(f"DAG节点执行任务发生未捕获异常: {e}", exc_info=True)

        async def reschedule_and_maybe_finish():
            try:
                for downstream_id in self.reverse_dependencies.get(node_id, set()):
                    await self.dag_scheduler.enqueue_ready_node(downstream_id)
                await self.dag_scheduler.drain_ready_queue()
            finally:
                if not self.running_tasks and not self.ready_queue and self.completion_event:
                    self.completion_event.set()

        asyncio.create_task(reschedule_and_maybe_finish())

    def _create_run_state(
        self,
        status: StepState,
        start_time: float,
        error: Optional[Dict] = None
    ) -> Dict:
        """创建运行状态字典

        迁移自 engine.py:396-406

        Args:
            status: 步骤状态
            start_time: 开始时间戳
            error: 错误信息（可选）

        Returns:
            包含状态、时间、持续时间和错误的字典
        """
        end_time = time.time()
        return {
            "status": status.name,
            "start_time": time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(start_time)),
            "end_time": time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(end_time)),
            "duration": round(end_time - start_time, 3),
            "error": error
        }
