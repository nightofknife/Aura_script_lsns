# -*- coding: utf-8 -*-
"""工具与基础设施模块。

核心组件:
- exceptions: 自定义异常类
- id_generator: Snowflake ID生成器
- inheritance_proxy: 服务继承代理
- middleware: 执行中间件
- hot_reload: 热重载策略
- file_watcher: 文件监控服务（需直接导入避免循环依赖）
- updater: 自动更新工具（需直接导入避免循环依赖）
"""

from .exceptions import (
    ActionExecutionError,
    AuraException,
    ConfigurationError,
    DependencyError,
    ExecutionError,
    JumpSignal,
    ResourceAcquisitionError,
    ResourceError,
    StatePlanningError,
    StopTaskException,
    TaskControlException,
    TaskExecutionError,
    TaskNotFoundError,
    TimeoutError,
    action_failed,
    create_jump_signal,
    create_plugin_error,
    create_stop_task,
    create_task_error,
    resource_unavailable,
    step_failed,
)
from .id_generator import SnowflakeGenerator
from .inheritance_proxy import InheritanceProxy
from .middleware import Middleware
from .hot_reload import HotReloadPolicy
# FileWatcherService 需要直接导入以避免循环依赖:
# from packages.aura_core.utils.file_watcher import FileWatcherService
# Updater 需要直接导入以避免循环依赖:
# from packages.aura_core.utils.updater import Updater

__all__ = [
    'ActionExecutionError',
    'AuraException',
    'ConfigurationError',
    'DependencyError',
    'ExecutionError',
    'JumpSignal',
    'ResourceAcquisitionError',
    'ResourceError',
    'SnowflakeGenerator',
    'InheritanceProxy',
    'Middleware',
    'HotReloadPolicy',
    'StatePlanningError',
    'StopTaskException',
    'TaskControlException',
    'TaskExecutionError',
    'TaskNotFoundError',
    'TimeoutError',
    'action_failed',
    'create_jump_signal',
    'create_plugin_error',
    'create_stop_task',
    'create_task_error',
    'resource_unavailable',
    'step_failed',
]
