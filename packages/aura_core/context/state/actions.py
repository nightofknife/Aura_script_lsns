# -*- coding: utf-8 -*-
"""提供了一组用于与持久化状态存储交互的核心 Action。

这些 Action (`state.set`, `state.get`, `state.delete`) 为自动化任务（Tasks）
提供了一种标准化的方式来读取、写入和删除需要跨任务、跨会话持久化的数据。
它们底层都依赖于 `StateStoreService` 服务。
"""
import asyncio
from typing import Any

try:
    from pydantic import BaseModel, Field
    PYDANTIC_AVAILABLE = True
except ImportError:
    PYDANTIC_AVAILABLE = False
    BaseModel = object  # type: ignore
    Field = lambda *args, **kwargs: None  # type: ignore

from ...api import action_info, requires_services
from packages.aura_core.context.persistence.store_service import StateStoreService


class StateSetParams(BaseModel):
    """`state.set` Action 的参数模型。"""
    key: str = Field(..., description="要设置的键名。")
    value: Any = Field(..., description="要设置的值。")

@action_info(name="state.set")
@requires_services(state_store="state_store")
async def state_set(params: StateSetParams, state_store: StateStoreService):
    """在持久化状态存储中设置一个键值对。

    此 Action 会将指定的键值对写入由 `StateStoreService` 管理的
    持久化上下文中。

    Args:
        params (StateSetParams): 包含 `key` 和 `value` 的参数对象。
        state_store (StateStoreService): 自动注入的状态存储服务实例。

    Returns:
        bool: 操作成功时返回 True。
    """
    await state_store.set(params.key, params.value)
    return True


class StateGetParams(BaseModel):
    """`state.get` Action 的参数模型。"""
    key: str = Field(..., description="要获取的键名。")
    default: Any = Field(default=None, description="如果键不存在时返回的默认值。")

@action_info(name="state.get", read_only=True)
@requires_services(state_store="state_store")
async def state_get(params: StateGetParams, state_store: StateStoreService) -> Any:
    """从持久化状态存储中获取一个值。

    如果指定的键不存在，将返回 `default` 参数所指定的值。

    Args:
        params (StateGetParams): 包含 `key` 和 `default` 的参数对象。
        state_store (StateStoreService): 自动注入的状态存储服务实例。

    Returns:
        Any: 查找到的值或默认值。
    """
    return await state_store.get(params.key, params.default)


class StateDeleteParams(BaseModel):
    """`state.delete` Action 的参数模型。"""
    key: str = Field(..., description="要删除的键名。")

@action_info(name="state.delete")
@requires_services(state_store="state_store")
async def state_delete(params: StateDeleteParams, state_store: StateStoreService):
    """从持久化状态存储中删除一个键。

    Args:
        params (StateDeleteParams): 包含要删除的 `key` 的参数对象。
        state_store (StateStoreService): 自动注入的状态存储服务实例。

    Returns:
        bool: 操作成功时返回 True。
    """
    await state_store.delete(params.key)
    return True



# ---- ?? demo action ----
class EchoParams(BaseModel):
    """???? action ??"""
    message: Any = Field(..., description="??????")


@action_info(name="sample.echo")
async def sample_echo(params: EchoParams):
    """???????????? demo?"""
    return params.message


class SleepParams(BaseModel):
    """???? action ??"""
    seconds: float = Field(..., description="?????????")


@action_info(name="sample.sleep")
async def sample_sleep(params: SleepParams):
    """????????????"""
    await asyncio.sleep(max(0.0, float(params.seconds)))
    return {"slept": float(params.seconds)}
