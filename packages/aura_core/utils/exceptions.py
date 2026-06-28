# -*- coding: utf-8 -*-
"""定义了 Aura 框架使用的所有自定义异常。

这个模块建立了一个层次化的异常结构，所有自定义异常都继承自 `AuraException`。
异常被分为几大类：
- **任务控制流异常**: 用于控制任务执行流程，不代表真正的错误，例如 `JumpSignal`。
- **执行时错误**: 在任务或 Action 执行期间发生的实际错误。
- **配置和设置错误**: 与框架或插件配置相关的错误。
- **资源错误**: 与外部资源（如文件、网络）交互时发生的错误。

此外，还提供了一系列工厂函数来方便地创建特定类型的异常。
"""
from typing import Optional, Dict, Any
import traceback


# --- 基础异常 ---

class AuraException(Exception):
    """所有 Aura 核心自定义异常的基类。

    这个基类提供了标准化的方式来处理异常信息、附加细节、原始原因（cause）
    以及严重级别。它还支持 Python 的异常链，可以保留完整的堆栈跟踪。

    Attributes:
        details (Dict[str, Any]): 包含有关异常的附加结构化数据。
        cause (Optional[Exception]): 触发此异常的原始异常对象。
        severity (str): 异常的严重级别 ('error', 'warning', 'critical', 'signal')。
    """
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None, cause: Optional[Exception] = None, severity: str = 'error'):
        """初始化 AuraException。"""
        super().__init__(message)
        self.details = details or {}
        self.cause = cause
        self.severity = severity
        if cause:
            self.__cause__ = cause

    def get_full_traceback(self) -> str:
        """获取完整的堆栈跟踪信息，包括其 cause 的信息。

        Returns:
            一个格式化的、包含完整异常链的字符串。
        """
        if self.__cause__:
            return ''.join(traceback.format_exception(type(self.__cause__), self.__cause__, self.__cause__.__traceback__))
        else:
            return ''.join(traceback.format_exception(type(self), self, self.__traceback__))


# --- 任务控制流异常 ---

class TaskControlException(AuraException):
    """用于控制任务流程的基类异常，通常不应被视为错误。"""
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None, cause: Optional[Exception] = None, severity: str = 'signal'):
        super().__init__(message, details, cause, severity)


class JumpSignal(TaskControlException):
    """用于实现控制流跳转的信号，例如循环中的 break 或 continue。"""
    def __init__(self, type: str, target: str, cause: Optional[Exception] = None):
        message = f"Jump signal: {type} to {target}"
        super().__init__(message, {'type': type, 'target': target}, cause, severity='signal')
        self.type = type
        self.target = target


class StopTaskException(TaskControlException):
    """当需要提前、正常地停止任务执行时抛出。"""
    def __init__(self, message: str, success: bool = True, details: Optional[Dict[str, Any]] = None, cause: Optional[Exception] = None,severity = None):
        self.success = success
        severity = 'success' if success else 'critical'
        super().__init__(message, details or {'success': success}, cause, severity)
        self.severity = severity


# --- 执行时错误 ---

class ExecutionError(AuraException):
    """在执行期间发生的通用错误。"""
    pass


class TaskExecutionError(ExecutionError):
    """在特定任务执行期间发生的错误。"""
    def __init__(self, message: str, task_id: Optional[str] = None, cause: Optional[Exception] = None):
        super().__init__(message, {'task_id': task_id}, cause)
        self.task_id = task_id


class ActionExecutionError(ExecutionError):
    """在特定 Action 执行期间发生的错误。"""
    def __init__(self, message: str, action_name: Optional[str] = None, cause: Optional[Exception] = None):
        super().__init__(message, {'action_name': action_name}, cause)
        self.action_name = action_name


class TaskNotFoundError(TaskExecutionError):
    """当找不到指定的任务定义时抛出。"""
    def __init__(self, task_id: str, cause: Optional[Exception] = None):
        super().__init__(f"Task definition not found for '{task_id}'", task_id=task_id, cause=cause)


# --- 配置和设置错误 ---

class ConfigurationError(AuraException):
    """与配置相关的错误。"""
    pass


class StatePlanningError(ConfigurationError):
    """在状态规划阶段发生的错误。"""
    pass


class DependencyError(ConfigurationError):
    """在解析或注入依赖项时发生的错误。"""
    pass


# --- 资源错误 ---

class ResourceError(AuraException):
    """与资源相关的错误，例如文件访问、网络连接等。"""
    pass


class ResourceAcquisitionError(ResourceError):
    """在获取资源（如锁、信号量）失败时抛出。"""
    def __init__(self, message: str, resource_type: str, operation: str, cause: Optional[Exception] = None):
        details = {'resource_type': resource_type, 'operation': operation}
        super().__init__(message, details, cause)


# --- 其他错误 ---

class TimeoutError(AuraException):
    """当某个操作超时时抛出。"""
    pass


# --- 异常工厂函数 ---

def create_plugin_error(message: str, plugin_name: Optional[str] = None, cause: Optional[Exception] = None) -> ConfigurationError:
    """创建一个插件相关的配置错误。"""
    return ConfigurationError(message, {'plugin_name': plugin_name}, cause)


def create_task_error(message: str, task_id: Optional[str] = None, cause: Optional[Exception] = None) -> TaskExecutionError:
    """创建一个任务执行错误。"""
    return TaskExecutionError(message, task_id=task_id, cause=cause)


def resource_unavailable(resource_type: str, cause: Optional[Exception] = None) -> ResourceError:
    """创建一个资源不可用的错误。"""
    return ResourceError(f"Resource of type '{resource_type}' is unavailable.", cause=cause)


def step_failed(step_id: str, reason: str, cause: Optional[Exception] = None) -> ActionExecutionError:
    """创建一个步骤（节点）失败的错误。"""
    return ActionExecutionError(f"Step '{step_id}' failed: {reason}", action_name=step_id, cause=cause)


def action_failed(action_name: str, reason: str, cause: Optional[Exception] = None) -> ActionExecutionError:
    """创建一个 Action 失败的错误。"""
    return ActionExecutionError(f"Action '{action_name}' failed: {reason}", action_name=action_name, cause=cause)

def create_jump_signal(type: str, target: str, cause: Optional[Exception] = None) -> JumpSignal:
    """创建一个用于控制流跳转的信号异常。"""
    return JumpSignal(type, target, cause)

def create_stop_task(message: str, success: bool = True, cause: Optional[Exception] = None) -> StopTaskException:
    """创建一个用于停止任务的异常。"""
    return StopTaskException(message, success=success, cause=cause)
