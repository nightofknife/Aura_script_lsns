# -*- coding: utf-8 -*-
"""提供一个基于 Jinja2 的异步模板渲染器。

`TemplateRenderer` 负责解析和渲染在任务定义（YAML 文件）中使用的
Jinja2 模板字符串。它能够递归地处理字符串、字典和列表中的模板，
并将它们替换为从多层上下文中获取的实际数据。
"""
from typing import Any, Dict, Optional, TYPE_CHECKING

try:
    from jinja2 import BaseLoader, UndefinedError
    from jinja2.nativetypes import NativeEnvironment
    JINJA2_AVAILABLE = True
except ImportError:
    JINJA2_AVAILABLE = False
    BaseLoader = None  # type: ignore
    UndefinedError = Exception  # type: ignore
    NativeEnvironment = None  # type: ignore

from ..context import ExecutionContext
from packages.aura_core.observability.logging.core_logger import logger

if TYPE_CHECKING:
    from packages.aura_core.context.persistence.store_service import StateStoreService


class TemplateRenderer:
    """负责使用多层上下文模型异步渲染 Jinja2 模板。

    此类会构建一个包含多个数据来源的渲染作用域（scope），使得模板
    可以访问到各种上下文信息。
    """

    def __init__(self, execution_context: ExecutionContext, state_store: "StateStoreService"):
        """初始化模板渲染器。

        Args:
            execution_context: 当前任务的执行上下文。
            state_store: 持久化状态存储服务。
        """
        self.execution_context = execution_context
        self.state_store = state_store
        self.jinja_env = NativeEnvironment(loader=BaseLoader(), enable_async=True)

    async def get_render_scope(self) -> Dict[str, Any]:
        """构建并返回用于 Jinja2 渲染的完整数据作用域。

        作用域按以下层次结构组织，模板中可以通过 `state.`, `initial.` 等
        方式访问：
        - `state`: 来自 `StateStoreService` 的持久化数据。
        - `initial`: 任务启动时的初始数据。
        - `inputs`: 传递给当前任务的输入参数。
        - `loop`: 当前循环迭代的变量（如 `item`, `index`）。
        - `nodes`: 任务中所有已执行节点的输出结果。

        Returns:
            一个包含了所有可用上下文数据的字典。
        """
        state_data = {}
        if self.state_store:
            if not getattr(self.state_store, '_initialized', False):
                await self.state_store.initialize()
            state_data = await self.state_store.get_all_data()

        exec_data = self.execution_context.data

        return {
            "state": state_data,
            "initial": exec_data.get("initial", {}),
            "inputs": exec_data.get("inputs", {}),
            "loop": exec_data.get("loop", {}),
            "nodes": exec_data.get("nodes", {})
        }

    async def render(self, value: Any, scope: Optional[Dict[str, Any]] = None) -> Any:
        """递归地渲染一个值（可以是字符串、字典、列表等）。

        如果 `value` 是一个包含 Jinja2 模板的字符串，它将被渲染。
        如果 `value` 是字典或列表，此方法会递归地对其所有子项进行渲染。
        其他类型的值将原样返回。

        Args:
            value (Any): 要渲染的值。
            scope (Optional[Dict[str, Any]]): （可选）预先构建的渲染作用域。
                如果未提供，将动态构建一次。

        Returns:
            渲染后的值。
        """
        if scope is None:
            try:
                scope = await self.get_render_scope()
            except Exception as e:
                logger.error(f"构建渲染作用域时失败: {e}", exc_info=True)
                scope = {}

        return await self._render_recursive(value, scope)

    async def _render_recursive(self, value: Any, scope: Dict[str, Any]) -> Any:
        """(私有) 递归渲染的核心实现。"""
        if isinstance(value, str):
            if "{{" not in value and "{%" not in value:
                return value
            try:
                template = self.jinja_env.from_string(value)
                return await template.render_async(scope)
            except UndefinedError as e:
                logger.warning(f"渲染模板 '{value}' 时出错: 变量或属性未定义 - {e.message}。返回 None。")
                return None
            except Exception as e:
                logger.error(f"渲染模板'{value}'时发生未知错误: {e}", exc_info=True)
                return value

        if isinstance(value, dict):
            return {k: await self._render_recursive(v, scope) for k, v in value.items()}

        if isinstance(value, list):
            return [await self._render_recursive(item, scope) for item in value]

        return value
