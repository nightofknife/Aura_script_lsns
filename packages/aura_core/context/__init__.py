# -*- coding: utf-8 -*-
"""上下文管理系统。

此模块提供任务执行上下文、Plan上下文、持久化和状态管理功能。

模块组织:
- execution.py: 任务级上下文（ExecutionContext）
- plan.py: Plan级上下文（PlanContext）
- persistence/: 持久化策略和状态存储
- state/: 状态规划和状态动作

常用导入:
    from packages.aura_core.context.execution import ExecutionContext, PlanContext
    from packages.aura_core.context.persistence import StateStoreService
    from packages.aura_core.context.state import StatePlanner
"""

from .execution import ExecutionContext
from .plan import PlanContext

# 子系统可按需导入
# from .persistence import StateStoreService, PersistenceStrategy
# from .state import StatePlanner, StateMap

__all__ = [
    'ExecutionContext',
    'PlanContext',
]
