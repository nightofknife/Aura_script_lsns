# -*- coding: utf-8 -*-
"""调度器执行层。

此模块提供任务执行管理、调度和分发功能。

核心组件:
- ExecutionManager: 执行管理器，线程池/进程池、并发控制
- ExecutionService: 执行服务，包装器（ad-hoc、批量、手动）
- DispatchService: 队列调度器，消费者循环、入队/出队
"""

from .manager import ExecutionManager
from .service import ExecutionService
from .dispatcher import DispatchService

__all__ = [
    'ExecutionManager',
    'ExecutionService',
    'DispatchService',
]
