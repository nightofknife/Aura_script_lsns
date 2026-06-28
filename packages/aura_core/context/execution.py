# -*- coding: utf-8 -*-
"""定义了 Aura 框架的核心数据结构：执行上下文（ExecutionContext）。

`ExecutionContext` 负责在单次任务（Task）的整个执行生命周期中，
安全、可预测地管理和传递数据。它被设计成一个只增不减的数据结构，
以支持复杂的执行图，包括并行分支和合并。
"""

from __future__ import annotations
import copy
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from packages.aura_core.context.plan import PlanContext


class ExecutionContext:
    """管理单次任务执行期间的数据流。

    这个类维护着一个任务在执行过程中所有的数据。它被组织成不同的作用域，
    以区分不同来源和用途的数据。它支持深度拷贝（`fork`）以用于并行执行，
    以及将多个分支的结果合并（`merge`）回主线。

    Attributes:
        data (Dict[str, Any]): 存储所有上下文数据的字典，包含以下几个顶级键：
            - `initial`: 任务启动时由触发器等传入的初始数据。
            - `inputs`: 当任务作为一个可调用实体时，由调用方传入的参数。
            - `nodes`: 存储任务图中每个已执行节点（Node）的输出结果。
            - `loop`: 用于存放当前循环迭代的变量（如 `item` 和 `index`）。
    """

    def __init__(self, initial_data: Optional[Dict[str, Any]] = None,
                 inputs: Optional[Dict[str, Any]] = None,
                 cid: Optional[str] = None,
                 plan_context: Optional['PlanContext'] = None,
                 task_services: Optional[Dict[str, Any]] = None):  # ✅ NEW
        """初始化一个执行上下文。

        Args:
            initial_data (Optional[Dict[str, Any]]): 任务启动时由触发器等
                传入的初始数据。
            inputs (Optional[Dict[str, Any]]): 当任务作为可调用实体时，由
                调用方传入的参数。
            cid (Optional[str]): 关联ID，用于追踪子任务调用链。
            plan_context (Optional[PlanContext]): 父级PlanContext引用，
                用于访问plan级别的配置、状态和缓存。
        """
        self.data: Dict[str, Any] = {
            "initial": initial_data or {},
            "inputs": inputs or {},
            "nodes": {},
            "loop": {},  # 用于存放循环变量 (item, index)
            "cid": cid or None,  # ????? cid
            "task_services": task_services or {},
        }

        # ✅ NEW: Store reference to plan-level context
        self.plan_context = plan_context

    def fork(self) -> 'ExecutionContext':
        """为并行分支创建一个当前上下文的深度拷贝副本。

        当任务执行图遇到并行分支时，每个分支都需要一个独立的上下文副本，
        以避免分支间的状态污染。`fork` 方法通过深度拷贝 `self.data` 来
        实现这一点。

        Returns:
            一个新的 `ExecutionContext` 实例，其内容与当前实例完全相同但相互独立。
        """
        forked_context = ExecutionContext()
        forked_context.data = copy.deepcopy(self.data)
        forked_context.plan_context = self.plan_context  # ✅ NEW: Preserve reference
        return forked_context

    def merge(self, other_contexts: List['ExecutionContext']):
        """将来自多个父分支的上下文合并到当前上下文中。

        当并行分支执行完毕并汇合到一个节点时，需要将这些分支的上下文数据
        合并起来。由于节点ID在任务中是唯一的，此方法只需简单地将其他上下文的
        `nodes` 字典更新到当前上下文中即可。

        `initial`, `inputs`, 和 `loop` 数据在分支中是继承或隔离的，不需要合并。

        Args:
            other_contexts (List['ExecutionContext']): 一个包含其他要合并的
                `ExecutionContext` 实例的列表。
        """
        for other in other_contexts:
            self.data["nodes"].update(other.data["nodes"])

    def add_node_result(self, node_id: str, result: Dict[str, Any]):
        """将一个节点的完整输出结果添加到上下文中。

        每个节点执行完毕后，其完整的输出（包括运行状态 `run_state` 和
        具名输出 `output`）会被添加到 `nodes` 作用域下，以节点ID为键。

        Args:
            node_id (str): 节点的唯一ID。
            result (Dict[str, Any]): 该节点的完整输出结果字典。
        """
        self.data["nodes"][node_id] = result

    def set_loop_variables(self, loop_vars: Dict[str, Any]):
        """为单次循环迭代的上下文设置特殊的循环变量。

        在执行 `loop` 节点时，此方法用于将当前迭代的 `item` 和 `index`
        等变量放入 `loop` 作用域，以便循环体内的节点可以访问它们。

        Args:
            loop_vars (Dict[str, Any]): 包含循环变量的字典，
                例如 `{'item': ..., 'index': ...}`。
        """
        self.data['loop'] = loop_vars

    def __repr__(self) -> str:
        """返回一个简洁的、可读的上下文表示形式。"""
        return (f"ExecutionContext(initial_keys={list(self.data['initial'].keys())}, "
                f"inputs_keys={list(self.data['inputs'].keys())}, "
                f"node_keys={list(self.data['nodes'].keys())})")

