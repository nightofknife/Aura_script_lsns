# -*- coding: utf-8 -*-
"""
提供一个线程安全的、异步的事件总线。

此模块是 Aura 框架内部组件间通信的核心。它实现了一个发布/订阅（Pub/Sub）模式，
允许系统不同部分在不直接相互引用的情况下，通过发布和订阅事件来进行解耦通信。

主要组件:
- `Event`: 一个标准化的事件数据结构。
- `EventBus`: 事件总线的实现，管理订阅和发布逻辑。

功能特性:
- **主题过滤**: 支持使用 `*` 和 `?` 等通配符进行事件名称匹配。
- **频道隔离**: 支持按频道发布和订阅事件。
- **跨线程安全**: 可以在不同的 asyncio 事件循环之间安全地发布事件。
- **持久化订阅**: 支持在清理时保留某些关键的订阅。
- **✅ 内存管理**: 支持取消订阅和自动清理过期订阅。
"""
import asyncio
import fnmatch
import uuid
import weakref
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, Any, List, Optional, Awaitable
import threading

# logger will be imported on first use


def _get_logger():
    """Lazy import logger to avoid circular dependency."""
    from packages.aura_core.observability.logging.core_logger import logger
    return logger





def get_utc_timestamp_ms() -> int:
    """Return current UTC timestamp in milliseconds."""
    return int(datetime.now(timezone.utc).timestamp() * 1000)

@dataclass
class Event:
    """Represents one event on the event bus."""

    name: str
    payload: Dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1
    timestamp_ms: int = field(default_factory=get_utc_timestamp_ms)
    channel: str = '*'

    @property
    def timestamp(self) -> int:
        """Backward-compatible alias for existing call sites."""
        return self.timestamp_ms

    def to_dict(self) -> Dict[str, Any]:
        """Serialize event as typed public envelope."""
        return {
            "name": self.name,
            "schema_version": self.schema_version,
            "timestamp_ms": self.timestamp_ms,
            "payload": self.payload,
        }

@dataclass
class Subscription:
    """代表一个对特定事件模式的订阅。

    Attributes:
        callback (Callable[[Event], Awaitable[None]]): 当匹配的事件发生时
            要执行的异步回调函数。
        loop (Optional[asyncio.AbstractEventLoop]): 此回调函数所属的事件循环。
            如果为 None，则假定在当前事件循环中执行。
        persistent (bool): 如果为 True，此订阅在调用 `clear_subscriptions`
            时不会被移除。
        subscription_id (str): ✅ 订阅的唯一ID，用于取消订阅。
        created_at (float): ✅ 订阅创建时间戳，用于清理过期订阅。
    """
    callback: Callable[[Event], Awaitable[None]]
    loop: Optional[asyncio.AbstractEventLoop] = None
    persistent: bool = False
    subscription_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: float = field(default_factory=lambda: datetime.now().timestamp())

class EventBus:
    """实现发布/订阅模式的事件总线。

    这是一个线程安全的类，用于管理所有事件订阅和处理事件发布。
    """
    def __init__(self):
        from packages.aura_core.observability.logging.core_logger import logger
        """初始化 EventBus。"""
        self._subscriptions: Dict[str, List[Subscription]] = defaultdict(list)
        self._lock = threading.Lock()
        # ✅ 新增：反向索引，用于快速unsubscribe
        self._subscription_index: Dict[str, tuple[str, Subscription]] = {}
        # ✅ 新增：使用弱引用跟踪事件循环
        self._loop_refs: weakref.WeakSet = weakref.WeakSet()

    async def subscribe(
            self,
            event_pattern: str,
            callback: Callable[[Event], Awaitable[None]],
            channel: str = '*',
            *,
            loop: Optional[asyncio.AbstractEventLoop] = None,
            persistent: bool = False
    ) -> str:
        """订阅一个或多个事件。

        Args:
            event_pattern (str): 要订阅的事件名称模式。支持 `*` 和 `?` 通配符。
            callback: 匹配到事件时要执行的异步回调函数。
            channel (str): 要订阅的频道。默认为 `*`，表示所有频道。
            loop (Optional[asyncio.AbstractEventLoop]): 回调函数应在哪个事件循环
                中执行。如果为 None，则在发布者的事件循环中执行。
            persistent (bool): 是否为持久化订阅。

        Returns:
            str: 订阅ID，可用于后续unsubscribe
        """
        key = f"{channel}::{event_pattern}"

        # ✅ 改进：使用函数对象ID进行更可靠的重复检测
        callback_id = id(callback)
        callback_name = getattr(callback, '__name__', repr(callback))

        with self._lock:
            # 检查重复订阅
            for sub in self._subscriptions[key]:
                if (id(sub.callback) == callback_id and
                    sub.loop is loop and
                    sub.persistent == persistent):
                    _get_logger().debug(f"[EventBus] 跳过重复订阅: {key} ({callback_name})")
                    return sub.subscription_id

            # 创建新订阅
            subscription = Subscription(
                callback=callback,
                loop=loop,
                persistent=persistent
            )
            self._subscriptions[key].append(subscription)

            # 建立反向索引
            self._subscription_index[subscription.subscription_id] = (key, subscription)

            # 跟踪事件循环
            if loop:
                self._loop_refs.add(loop)

            _get_logger().debug(
                f"[EventBus] 已注册订阅: key='{key}', "
                f"id={subscription.subscription_id[:8]}, "
                f"persistent={persistent}, "
                f"callback={callback_name}"
            )

            return subscription.subscription_id

    # ✅ 新增：unsubscribe方法
    async def unsubscribe(self, subscription_id: str) -> bool:
        """取消订阅。

        Args:
            subscription_id: subscribe方法返回的订阅ID

        Returns:
            bool: 是否成功取消
        """
        with self._lock:
            if subscription_id not in self._subscription_index:
                _get_logger().warning(f"[EventBus] 未找到订阅ID: {subscription_id}")
                return False

            key, subscription = self._subscription_index.pop(subscription_id)

            try:
                self._subscriptions[key].remove(subscription)
                # 清理空列表
                if not self._subscriptions[key]:
                    del self._subscriptions[key]

                _get_logger().debug(f"[EventBus] 已取消订阅: {subscription_id[:8]} (key={key})")
                return True
            except ValueError:
                _get_logger().error(f"[EventBus] 订阅索引不一致: {subscription_id}")
                return False

    # ✅ 新增：按模式unsubscribe
    async def unsubscribe_pattern(self, channel: str, event_pattern: str) -> int:
        """取消某个模式的所有订阅。

        Returns:
            int: 取消的订阅数量
        """
        key = f"{channel}::{event_pattern}"
        count = 0

        with self._lock:
            if key in self._subscriptions:
                # 从索引中移除
                for sub in self._subscriptions[key]:
                    if sub.subscription_id in self._subscription_index:
                        del self._subscription_index[sub.subscription_id]
                        count += 1

                # 删除订阅列表
                del self._subscriptions[key]

        _get_logger().debug(f"[EventBus] 已取消模式订阅: {key}, 数量={count}")
        return count
    async def publish(self, event: Event):
        """发布一个事件到事件总线。

        此方法会查找所有匹配该事件名称和频道的订阅，并异步地执行它们的回调函数。
        如果订阅者属于不同的事件循环，它会安全地将回调任务提交到对应的循环中。

        Args:
            event (Event): 要发布的事件对象。
        """
        tasks = []
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        with self._lock:
            all_subscriptions = list(self._subscriptions.items())

        for key, subscriptions in all_subscriptions:
            channel, pattern = key.split('::', 1)
            if (channel == '*' or event.channel == channel) and fnmatch.fnmatch(event.name, pattern):
                for sub in subscriptions:
                    # ✅ 检查loop是否仍然有效
                    if sub.loop and sub.loop.is_closed():
                        _get_logger().warning(
                            f"[EventBus] 检测到已关闭的事件循环，跳过订阅: "
                            f"{key}, id={sub.subscription_id[:8]}"
                        )
                        continue

                    if sub.loop and sub.loop is not current_loop:
                        try:
                            sub.loop.call_soon_threadsafe(
                                sub.loop.create_task,
                                sub.callback(event)
                            )
                        except RuntimeError as e:
                            _get_logger().error(f"[EventBus] 跨循环调用失败: {e}")
                    elif current_loop:
                        tasks.append(current_loop.create_task(sub.callback(event)))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            # ✅ 记录回调异常
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    _get_logger().error(f"[EventBus] 事件回调异常: {result}", exc_info=result)

    async def clear_subscriptions(self, keep_persistent: bool = True):
        """清除订阅。

        Args:
            keep_persistent: 是否保留持久化订阅（默认True）
        """
        with self._lock:
            before_count = sum(len(subs) for subs in self._subscriptions.values())

            if keep_persistent:
                # 只清理非持久化订阅
                for key, subs in list(self._subscriptions.items()):
                    persistent_subs = [s for s in subs if s.persistent]
                    removed_subs = [s for s in subs if not s.persistent]

                    # 先清理索引
                    for sub in removed_subs:
                        self._subscription_index.pop(sub.subscription_id, None)

                    # 再更新或删除订阅列表
                    if persistent_subs:
                        self._subscriptions[key] = persistent_subs
                    else:
                        # 只有在没有持久订阅时才删除key
                        del self._subscriptions[key]
            else:
                # 清除所有订阅
                self._subscriptions.clear()
                self._subscription_index.clear()

            # ✅ 修复：验证索引一致性
            index_ids = set(self._subscription_index.keys())
            actual_ids = {
                sub.subscription_id
                for subs in self._subscriptions.values()
                for sub in subs
            }
            orphaned = index_ids - actual_ids
            if orphaned:
                _get_logger().warning(f"[EventBus] 发现 {len(orphaned)} 个孤立的订阅索引，清理中...")
                for orphaned_id in orphaned:
                    self._subscription_index.pop(orphaned_id, None)

            missing = actual_ids - index_ids
            if missing:
                _get_logger().error(f"[EventBus] 发现 {len(missing)} 个缺失索引的订阅，重建索引中...")
                # 重建缺失的索引
                for key, subs in self._subscriptions.items():
                    for sub in subs:
                        if sub.subscription_id in missing:
                            self._subscription_index[sub.subscription_id] = (key, sub)

            after_count = sum(len(subs) for subs in self._subscriptions.values())
            _get_logger().info(
                f"[EventBus] clear_subscriptions 执行完毕: "
                f"清理前 {before_count} 个订阅，清理后 {after_count} 个"
            )

    # ✅ 新增：清理过期订阅
    async def cleanup_stale_subscriptions(self, max_age_hours: float = 24):
        """清理长时间未使用的非持久化订阅。

        Args:
            max_age_hours: 订阅的最大存活时间（小时）
        """
        cutoff_time = datetime.now().timestamp() - (max_age_hours * 3600)
        removed_count = 0

        with self._lock:
            for key, subs in list(self._subscriptions.items()):
                # 过滤掉过期的非持久化订阅
                fresh_subs = [
                    s for s in subs
                    if s.persistent or s.created_at >= cutoff_time
                ]

                stale_subs = [
                    s for s in subs
                    if not s.persistent and s.created_at < cutoff_time
                ]

                if stale_subs:
                    # 清理索引
                    for sub in stale_subs:
                        self._subscription_index.pop(sub.subscription_id, None)
                        removed_count += 1

                    # 更新或删除订阅列表
                    if fresh_subs:
                        self._subscriptions[key] = fresh_subs
                    else:
                        del self._subscriptions[key]

        if removed_count > 0:
            _get_logger().info(f"[EventBus] 清理了 {removed_count} 个过期订阅")

        return removed_count

    # ✅ 新增：获取订阅统计
    def get_stats(self) -> Dict[str, Any]:
        """获取事件总线统计信息"""
        with self._lock:
            total_subscriptions = sum(len(subs) for subs in self._subscriptions.values())
            persistent_count = sum(
                1 for subs in self._subscriptions.values()
                for sub in subs if sub.persistent
            )

            return {
                "total_subscriptions": total_subscriptions,
                "persistent_subscriptions": persistent_count,
                "transient_subscriptions": total_subscriptions - persistent_count,
                "unique_patterns": len(self._subscriptions),
                "active_loops": len(self._loop_refs)
            }

    # ✅ 新增：验证并修复索引一致性
    def verify_and_fix_index_consistency(self) -> Dict[str, int]:
        """验证订阅索引的一致性，并自动修复问题。

        Returns:
            Dict包含orphaned（孤立）和missing（缺失）的数量
        """
        with self._lock:
            index_ids = set(self._subscription_index.keys())
            actual_ids = {
                sub.subscription_id
                for subs in self._subscriptions.values()
                for sub in subs
            }

            # 查找孤立的索引（索引中有但订阅列表中没有）
            orphaned = index_ids - actual_ids
            if orphaned:
                _get_logger().warning(f"[EventBus] 修复 {len(orphaned)} 个孤立的订阅索引")
                for orphaned_id in orphaned:
                    self._subscription_index.pop(orphaned_id, None)

            # 查找缺失的索引（订阅列表中有但索引中没有）
            missing = actual_ids - index_ids
            if missing:
                _get_logger().warning(f"[EventBus] 重建 {len(missing)} 个缺失的订阅索引")
                for key, subs in self._subscriptions.items():
                    for sub in subs:
                        if sub.subscription_id in missing:
                            self._subscription_index[sub.subscription_id] = (key, sub)

            return {
                "orphaned_count": len(orphaned),
                "missing_count": len(missing),
                "total_fixed": len(orphaned) + len(missing)
            }

