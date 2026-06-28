# -*- coding: utf-8 -*-
"""Scheduler任务调度器

职责: 管理任务的调度、入队和分发
"""

import time
import fnmatch
from typing import TYPE_CHECKING, Any, Dict, Optional
from pathlib import Path
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.observability.events import Event
from packages.aura_core.api import service_registry
from packages.aura_core.context.plan import current_plan_name
from packages.aura_core.scheduler.cancellation import request_task_cancel

if TYPE_CHECKING:
    from .core import Scheduler


class TaskDispatcher:
    """任务调度器

    管理任务的调度和分发，包括:
    - 手动任务执行
    - 临时任务执行
    - 调度项入队
    - 事件触发订阅
    - 队列消费
    """

    def __init__(self, scheduler: 'Scheduler'):
        """初始化任务调度器

        Args:
            scheduler: 父调度器实例
        """
        self.scheduler = scheduler

    def _release_inputs_for_cancelled_task(self, cid: str) -> None:
        """Best-effort safety release for tasks stopped from outside their action."""
        meta = {}
        try:
            meta = dict(getattr(self.scheduler, "_running_task_meta", {}).get(cid) or {})
        except Exception:
            meta = {}

        plan_name = str(meta.get("plan_name") or "").strip()
        tasklet = meta.get("tasklet")
        payload = getattr(tasklet, "payload", None)
        if not plan_name and isinstance(payload, dict):
            plan_name = str(payload.get("plan_name") or "").strip()
        if not plan_name:
            task_name = str(meta.get("task_name") or getattr(tasklet, "task_name", "") or "").strip()
            if "/" in task_name:
                plan_name = task_name.split("/", 1)[0]
        if not plan_name:
            return

        token = current_plan_name.set(plan_name)
        try:
            target_runtime = service_registry.get_service_instance("target_runtime")
            if hasattr(target_runtime, "release_all"):
                target_runtime.release_all()
                logger.info("Released all inputs for cancelled task '%s' in plan '%s'.", cid, plan_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to release inputs for cancelled task '%s' in plan '%s': %s",
                cid,
                plan_name,
                exc,
            )
        finally:
            current_plan_name.reset(token)

    def run_manual_task(self, task_id: str):
        """执行手动调度任务

        实现来自: scheduler.py 行1223-1226

        委托给执行服务处理。

        Args:
            task_id: 任务ID

        Returns:
            任务执行结果
        """
        if hasattr(self.scheduler, 'executor') and self.scheduler.executor:
            return self.scheduler.executor.run_manual_task(task_id)
        else:
            logger.error("执行服务(executor)未初始化")
            return None

    def run_ad_hoc_task(self, plan_name: str, task_name: str, params: Optional[Dict[str, Any]] = None, temp_id: Optional[str] = None):
        """执行临时任务

        实现来自: scheduler.py 行1227-1230

        委托给执行服务处理。

        Args:
            plan_name: Plan名称
            task_name: 任务名称
            params: 任务参数
            temp_id: 临时ID

        Returns:
            任务执行结果
        """
        if hasattr(self.scheduler, 'executor') and self.scheduler.executor:
            return self.scheduler.executor.run_ad_hoc_task(plan_name, task_name, params, temp_id)
        else:
            logger.error("执行服务(executor)未初始化")
            return None

    async def enqueue_schedule_item(self, item: Dict[str, Any], *, source: str,
                                     triggering_event: Optional[Event] = None) -> bool:
        """将调度项加入队列

        实现来自: scheduler.py 行857-861

        委托给dispatch服务处理。

        Args:
            item: 调度项
            source: 来源
            triggering_event: 触发事件

        Returns:
            是否成功入队
        """
        if hasattr(self.scheduler, 'dispatch') and self.scheduler.dispatch:
            return await self.scheduler.dispatch.enqueue_schedule_item(item, source=source, triggering_event=triggering_event)
        else:
            logger.error("调度服务(dispatch)未初始化")
            return False

    async def subscribe_event_triggers(self):
        """订阅所有调度规则中的事件类触发器

        实现来自: scheduler.py 行745-856

        订阅cron、variable、task、file和event类型的触发器。
        """
        logger.info("--- 订阅调度触发器 ---")

        with self.scheduler.fallback_lock:
            subscribed_count = 0
            schedule_items = list(self.scheduler.schedule_items)

        for item in schedule_items:
            plan_name = item.get('plan_name')
            triggers = item.get('triggers') or []
            if not plan_name or not isinstance(triggers, list):
                continue

            for idx, trigger in enumerate(triggers):
                if not isinstance(trigger, dict):
                    continue

                trigger_type = trigger.get('type')

                # 跳过cron触发器（由其他机制处理）
                if trigger_type == 'cron':
                    continue

                # 处理variable触发器
                elif trigger_type == 'variable':
                    key = trigger.get('key')
                    target_value = trigger.get('value')
                    operator = trigger.get('operator', 'eq')
                    if key:
                        async def var_handler(event, sched_item=item, k=key, v=target_value, op=operator):
                            if event.payload.get('key') == k:
                                current_val = event.payload.get('new_value')
                                match = False
                                if v is None:
                                    match = True
                                elif op == 'eq' and str(current_val) == str(v):
                                    match = True
                                elif op == 'neq' and str(current_val) != str(v):
                                    match = True
                                if match:
                                    await self.enqueue_schedule_item(
                                        sched_item,
                                        source="schedule_trigger",
                                        triggering_event=event
                                    )

                        await self.scheduler.event_bus.subscribe('state.changed', var_handler)
                        subscribed_count += 1

                # 处理task触发器
                elif trigger_type == 'task':
                    target_task = trigger.get('task')
                    target_status = trigger.get('status', 'completed')
                    if target_task and target_status == 'completed':
                        target_full = f"{plan_name}/{target_task}"

                        async def task_handler(event, sched_item=item, target=target_full):
                            completed_task = f"{event.payload.get('plan_name')}/{event.payload.get('task_name')}"
                            if completed_task == target:
                                await self.enqueue_schedule_item(
                                    sched_item,
                                    source="schedule_trigger",
                                    triggering_event=event
                                )

                        await self.scheduler.event_bus.subscribe('queue.completed', task_handler)
                        subscribed_count += 1

                # 处理file触发器
                elif trigger_type == 'file':
                    path = trigger.get('path')
                    pattern = trigger.get('pattern', '*')
                    events = trigger.get('events')
                    recursive = trigger.get('recursive', False)
                    if path:
                        watch_id = f"watch_{item.get('id', 'schedule')}_{idx}_{int(time.time())}"
                        self.scheduler.file_watcher_service.add_watch(watch_id, path, events, recursive)

                        async def file_handler(event, sched_item=item, p=pattern, w_id=watch_id):
                            if event.payload.get('watch_id') == w_id:
                                file_name = Path(event.payload.get('path')).name
                                if fnmatch.fnmatch(file_name, p):
                                    await self.enqueue_schedule_item(
                                        sched_item,
                                        source="schedule_trigger",
                                        triggering_event=event
                                    )

                        await self.scheduler.event_bus.subscribe('file.changed', file_handler, channel='file_watcher')
                        subscribed_count += 1

                # 处理event触发器
                elif trigger_type == 'event':
                    event_pattern = trigger.get('event')
                    if not event_pattern:
                        continue

                    manifest = next(
                        (m for m in self.scheduler.plan_manager.package_manager.loaded_packages.values()
                         if m.path.name == plan_name),
                        None
                    )
                    if not manifest:
                        continue

                    channel = manifest.package.canonical_id

                    from functools import partial

                    async def handler(event, sched_item):
                        await self.enqueue_schedule_item(
                            sched_item,
                            source="schedule_trigger",
                            triggering_event=event
                        )

                    callback = partial(handler, sched_item=item)
                    callback.__name__ = f"schedule_event_trigger_for_{item.get('id', 'schedule')}"
                    await self.scheduler.event_bus.subscribe(event_pattern, callback, channel=channel)
                    subscribed_count += 1

        if subscribed_count:
            logger.info(f"--- 已订阅 {subscribed_count} 个调度触发器 ---")

    async def consume_main_queue(self):
        """消费主任务队列

        实现来自: scheduler.py 行721-724

        委托给dispatch服务处理。
        """
        if hasattr(self.scheduler, 'dispatch') and self.scheduler.dispatch:
            await self.scheduler.dispatch.consume_main_task_queue()
        else:
            logger.error("调度服务(dispatch)未初始化")

    async def consume_interrupt_queue(self):
        """消费中断队列

        实现来自: scheduler.py 行725-728

        委托给dispatch服务处理。
        """
        if hasattr(self.scheduler, 'dispatch') and self.scheduler.dispatch:
            await self.scheduler.dispatch.consume_interrupt_queue()
        else:
            logger.error("调度服务(dispatch)未初始化")

    async def event_worker_loop(self, worker_id: int):
        """事件工作循环

        实现来自: scheduler.py 行729-732

        委托给dispatch服务处理。

        Args:
            worker_id: 工作线程ID
        """
        if hasattr(self.scheduler, 'dispatch') and self.scheduler.dispatch:
            await self.scheduler.dispatch.event_worker_loop(worker_id)
        else:
            logger.error("调度服务(dispatch)未初始化")

    def run_batch_ad_hoc_tasks(self, tasks: list[Dict[str, Any]]) -> Dict[str, Any]:
        """批量执行临时任务

        实现来自: scheduler.py 行1663-1666

        委托给执行服务处理。

        Args:
            tasks: 任务列表

        Returns:
            批量执行结果
        """
        if hasattr(self.scheduler, 'executor') and self.scheduler.executor:
            return self.scheduler.executor.run_batch_ad_hoc_tasks(tasks)
        else:
            logger.error("执行服务(executor)未初始化")
            return {"status": "error", "message": "Executor not initialized"}

    def cancel_task(self, cid: str) -> Dict[str, Any]:
        """取消指定任务

        实现来自: scheduler.py 行1667-1687

        Args:
            cid: 任务的唯一追踪ID

        Returns:
            包含取消操作结果的字典
        """
        def _cancel_task_now() -> Dict[str, Any]:
            with self.scheduler.fallback_lock:
                if cid not in self.scheduler.running_tasks:
                    return {"status": "error", "message": f"Task with cid '{cid}' is not running or not found."}

                task = self.scheduler.running_tasks.get(cid)
                if task and not task.done():
                    request_task_cancel(cid)
                    self._release_inputs_for_cancelled_task(cid)
                    task.cancel()
                    logger.info(f"Task with cid '{cid}' has been cancelled.")
                    return {"status": "success", "message": f"Task '{cid}' cancellation initiated."}
                return {"status": "error", "message": f"Task '{cid}' is already finished or cannot be cancelled."}

        if not getattr(self.scheduler, "_loop", None) or not self.scheduler._loop.is_running():
            return _cancel_task_now()

        async def _cancel_task_on_control_loop() -> Dict[str, Any]:
            return _cancel_task_now()

        try:
            return self.scheduler.run_on_control_loop(_cancel_task_on_control_loop(), timeout=5.0)
        except RuntimeError as exc:
            if "control loop" in str(exc).lower():
                return _cancel_task_now()
            logger.error("Failed to cancel task '%s': %s", cid, exc, exc_info=True)
            return {"status": "error", "message": str(exc)}
        except Exception as exc:
            logger.error("Failed to cancel task '%s': %s", cid, exc, exc_info=True)
            return {"status": "error", "message": str(exc)}
