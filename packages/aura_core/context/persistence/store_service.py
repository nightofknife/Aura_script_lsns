# -*- coding: utf-8 -*-
"""提供用于管理持久化状态存储的服务。

该模块定义了 `StateStoreService`，这是一个核心服务，负责处理需要
在 Aura 框架重启后依然保持的数据。它将数据以 JSON 格式存储在
文件中，并提供了一套异步、线程安全的接口来访问这些数据。
"""
import asyncio
import json
import os
from typing import Any, Dict

from ...api import requires_services, service_info
from ...config.service import ConfigService
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.observability.events import Event, EventBus


@service_info(alias="state_store", public=True)
@requires_services(config="config")
class StateStoreService:
    """管理长期、持久化的上下文状态。

    此服务从配置文件中读取状态文件的路径，并提供 `get`, `set`, `delete`
    等方法来操作持久化数据。所有文件 I/O 操作都是异步的，并通过一个
    内部锁来确保线程安全。服务采用延迟初始化模式，只在首次被访问时
    才会真正加载文件。
    """

    def __init__(self, config: ConfigService):
        """初始化状态存储服务。

        Args:
            config (ConfigService): 自动注入的配置服务实例。
        """
        self._config = config
        self._filepath: str = ""
        self._data: Dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._initialized = False
        self._event_bus = None

    def set_event_bus(self, event_bus):
        """手动注入事件总线实例。"""
        self._event_bus = event_bus

    async def initialize(self):
        """异步初始化服务。

        此方法会读取配置来确定状态文件的路径，然后从该文件加载初始状态。
        该方法是幂等的，且只在服务首次被需要时调用一次。
        """
        if self._initialized:
            return
        async with self._lock:
            if self._initialized:
                return

            store_config = self._config.get('state_store', {})
            if store_config.get('type') != 'file':
                logger.warning(f"StateStore只支持'file'类型，当前配置为'{store_config.get('type')}'。将禁用。")
                self._filepath = ""
                self._initialized = True
                return

            path = store_config.get('path', './project_state.json')
            self._filepath = os.path.abspath(path)

            await self._load()
            self._initialized = True
            logger.info(f"StateStoreService已初始化，状态文件: {self._filepath}")

    async def _load(self):
        """(私有) 内部加载方法，必须在锁内调用。"""
        if not self._filepath:
            return

        loop = asyncio.get_running_loop()
        try:
            if os.path.exists(self._filepath):
                def _read():
                    with open(self._filepath, 'r', encoding='utf-8') as f:
                        return json.load(f)

                self._data = await loop.run_in_executor(None, _read)
            else:
                self._data = {}
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"读取状态文件'{self._filepath}'失败: {e}。将使用空状态。")
            self._data = {}

    async def _save(self):
        """(私有) 内部保存方法，必须在锁内调用。"""
        if not self._filepath:
            return

        loop = asyncio.get_running_loop()
        try:
            data_to_save = self._data.copy()

            def _write():
                os.makedirs(os.path.dirname(self._filepath), exist_ok=True)
                with open(self._filepath, 'w', encoding='utf-8') as f:
                    json.dump(data_to_save, f, indent=4, ensure_ascii=False)

            await loop.run_in_executor(None, _write)
        except Exception as e:
            logger.error(f"保存状态文件'{self._filepath}'失败: {e}", exc_info=True)

    async def get(self, key: str, default: Any = None) -> Any:
        """从状态存储中异步获取一个值。

        Args:
            key (str): 要获取的键。
            default (Any, optional): 如果键不存在时返回的默认值。

        Returns:
            Any: 查找到的值或默认值。
        """
        if not self._initialized: await self.initialize()
        return self._data.get(key, default)

    async def set(self, key: str, value: Any):
        """在状态存储中异步设置一个值，并立即触发保存操作。

        Args:
            key (str): 要设置的键。
            value (Any): 要设置的值。
        """
        if not self._initialized: await self.initialize()
        async with self._lock:
            old_value = self._data.get(key)
            self._data[key] = value
            await self._save()

        if self._event_bus:
            await self._event_bus.publish(Event(
                name="state.changed",
                payload={"key": key, "old_value": old_value, "new_value": value}
            ))

    async def delete(self, key: str):
        """从状态存储中异步删除一个键，并立即触发保存操作。

        Args:
            key (str): 要删除的键。
        """
        if not self._initialized: await self.initialize()
        async with self._lock:
            if key in self._data:
                old_value = self._data[key]
                del self._data[key]
                await self._save()

                if self._event_bus:
                    await self._event_bus.publish(Event(
                        name="state.changed",
                        payload={"key": key, "old_value": old_value, "new_value": None, "deleted": True}
                    ))

    async def get_all_data(self) -> Dict[str, Any]:
        """异步获取所有状态数据的副本。

        Returns:
            一个包含所有持久化状态数据的字典。
        """
        if not self._initialized: await self.initialize()
        return self._data.copy()
