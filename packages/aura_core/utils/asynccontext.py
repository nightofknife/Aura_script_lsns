# -*- coding: utf-8 -*-
"""提供用于管理异步上下文的工具。

此模块主要包含用于在异步代码中安全设置和重置上下文变量（ContextVar）的
上下文管理器。这对于在并发环境中跟踪特定于任务的状态（如当前正在执行的
Plan 名称）至关重要。
"""

from contextlib import asynccontextmanager
from packages.aura_core.context.plan import current_plan_name

@asynccontextmanager
async def plan_context(plan_name: str):
    """一个异步上下文管理器，用于临时设置当前正在执行的 Plan 名称。

    此上下文管理器确保 `current_plan_name` 这个上下文变量在进入 `async with`
    块时被设置为指定的 `plan_name`，并在退出时（无论正常退出还是发生异常）
    被安全地重置回其先前的值。

    这对于框架中需要知道当前操作属于哪个 Plan 的部分（例如，日志记录、
    资源隔离等）非常有用。

    用法:
        async with plan_context("my_awesome_plan"):
            # 在此代码块内，current_plan_name.get() 将返回 "my_awesome_plan"
            ...
        # 在此代码块外，current_plan_name 的值已恢复

    Args:
        plan_name (str): 要在上下文期间设置的 Plan 的名称。
    """
    current = current_plan_name.get()
    if current != plan_name:
        token = current_plan_name.set(plan_name)
        try:
            yield
        finally:
            current_plan_name.reset(token)
            from packages.aura_core.observability.logging.core_logger import logger
            logger.debug(f"Plan 上下文已从 '{plan_name}' 重置回 '{current}'")
    else:
        # 如果当前上下文中的 Plan 名称已经正确，则无需任何操作，直接进入代码块
        yield
