# -*- coding: utf-8 -*-
"""Engine模块 - 外观模式

对外暴露ExecutionEngine接口，保持向后兼容性。

重构说明：
- 原始 engine.py (910行) 已拆分为4个子模块
- graph_builder.py: DAG图构建
- dag_scheduler.py: DAG调度
- node_executor.py: 节点执行
- execution_engine.py: 核心引擎类（组合所有子组件）

使用方式：
    from packages.aura_core.engine import ExecutionEngine, StepState

    engine = ExecutionEngine(orchestrator, pause_event, event_callback)
    result = await engine.run(task_data, task_name, root_context)
"""

from .execution_engine import ExecutionEngine, StepState

__all__ = ['ExecutionEngine', 'StepState']
