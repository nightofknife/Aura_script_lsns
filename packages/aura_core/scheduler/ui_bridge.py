# -*- coding: utf-8 -*-
"""Scheduler UI通信桥接器

职责: 管理调度器与UI层的通信，包括状态更新推送和事件同步
"""

import queue
from typing import TYPE_CHECKING, Any, Dict
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.observability.events import Event

if TYPE_CHECKING:
    from .core import Scheduler


class UIBridge:
    """UI通信桥接器

    管理调度器与UI层的所有通信，包括:
    - 状态更新推送
    - 事件队列管理
    - 完整状态同步
    """

    def __init__(self, scheduler: 'Scheduler'):
        """初始化UI桥接器

        Args:
            scheduler: 父调度器实例
        """
        self.scheduler = scheduler

    def set_ui_update_queue(self, q: queue.Queue):
        """设置用于向UI发送更新的队列

        实现来自: scheduler.py 行434-438

        Args:
            q: UI更新队列
        """
        self.scheduler.ui_update_queue = q
        if hasattr(self.scheduler, 'execution_manager') and self.scheduler.execution_manager:
            self.scheduler.execution_manager.set_ui_update_queue(q)

    def push_update(self, msg_type: str, data: Any):
        """向UI更新队列中推送一条消息

        实现来自: scheduler.py 行439-449

        Args:
            msg_type: 消息类型
            data: 消息数据
        """
        if hasattr(self.scheduler, 'ui_update_queue') and self.scheduler.ui_update_queue:
            try:
                self.scheduler.ui_update_queue.put_nowait({'type': msg_type, 'data': data})
            except queue.Full:
                # 队列满时丢弃，避免抛到事件循环
                logger.warning(f"UI更新队列已满，丢弃消息: {msg_type}")
            except Exception as e:
                logger.warning(f"推送UI更新失败: {e}")

    def get_event_queue(self) -> queue.Queue:
        """获取UI事件队列

        实现来自: scheduler.py 行1300-1303

        Returns:
            UI事件队列
        """
        if hasattr(self.scheduler, 'observability') and self.scheduler.observability:
            return self.scheduler.observability.get_ui_event_queue()
        return None

    def trigger_full_update(self):
        """手动触发一次向UI的全量状态更新

        实现来自: scheduler.py 行1495-1508

        用于新客户端连接时同步完整状态。
        """
        logger.debug("Scheduler: Triggering a full UI status update for new clients.")

        payload = {}

        # 安全地获取各种状态信息
        try:
            payload['schedule'] = self.scheduler.get_schedule_status()
        except (AttributeError, Exception):
            payload['schedule'] = {}

        try:
            # services 字段暂时设为空，因为 get_all_services_status 方法不存在
            payload['services'] = {}
        except (AttributeError, Exception):
            payload['services'] = {}

        try:
            payload['interrupts'] = self.scheduler.get_all_interrupts_status()
        except (AttributeError, Exception):
            payload['interrupts'] = {}

        try:
            payload['workspace'] = {
                'plans': self.scheduler.get_all_plans(),
                'actions': self.scheduler.actions.get_all_action_definitions() if hasattr(self.scheduler, 'actions') else []
            }
        except (AttributeError, Exception):
            payload['workspace'] = {'plans': [], 'actions': []}

        self.push_update('full_status_update', payload)

    async def mirror_event_to_queue(self, event: Event):
        """将事件镜像到UI队列

        实现来自: scheduler.py 行1263-1266

        Args:
            event: 要镜像的事件
        """
        if hasattr(self.scheduler, 'observability') and self.scheduler.observability:
            await self.scheduler.observability.mirror_event_to_ui_queue(event)
