# -*- coding: utf-8 -*-
"""可观测性系统。

此模块提供运行跟踪、事件总线和日志功能。

核心组件:
- ObservabilityService: 运行跟踪、指标、持久化 (需要时手动导入)
- EventBus: 发布/订阅、事件通信
- logger: 日志系统

使用方式:
    from packages.aura_core.observability import EventBus, logger
    from packages.aura_core.observability.service import ObservabilityService  # 延迟导入
"""

# 只导出不会引起循环依赖的模块
from .events import EventBus
from .logging import logger

# ObservabilityService 需要时手动导入以避免循环依赖
# from .service import ObservabilityService

__all__ = [
    'EventBus',
    'logger',
    # 'ObservabilityService',  # 请手动导入
]
