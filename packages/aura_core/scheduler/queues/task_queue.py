# -*- coding: utf-8 -*-
"""提供了 Aura 框架内部使用的异步任务队列和任务单元定义。

此模块定义了两个核心类：
- `Tasklet`: 一个轻量级的数据类，代表一个待执行的任务单元。
- `TaskQueue`: 一个支持高级操作（插入、删除、重排序）的异步任务队列。
"""
import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Literal, Callable

from ...observability.events import Event


@dataclass
class Tasklet:
    """一个轻量级的待办任务单元，将被放入任务队列。

    它封装了执行一个任务所需的所有信息。

    Attributes:
        task_name (str): 要执行的任务的完全限定ID (e.g., "my_plan/my_task")。
        cid (Optional[str]): 权威追踪ID。
        payload (Optional[Dict[str, Any]]): 与任务相关的任意数据字典。
        is_ad_hoc (bool): 标记此任务是否为临时（ad-hoc）任务。
        triggering_event (Optional[Event]): 触发此任务的事件对象（如有）。
        initial_context (Optional[Dict[str, Any]]): 传递给任务的初始输入参数。
        execution_mode (Literal['sync', 'async']): 任务的执行模式。
        resource_tags (List[str]): 用于并发控制的资源标签列表。
        timeout (Optional[float]): 任务的执行超时时间（秒）。
        cpu_bound (bool): 标记此任务是否为CPU密集型。
        planning_depth (int): ✅ 状态规划深度，用于防止无限递归。
    """
    task_name: str
    cid: Optional[str] = None
    trace_id: Optional[str] = None
    trace_label: Optional[str] = None
    source: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None
    is_ad_hoc: bool = False
    triggering_event: Optional[Event] = None
    initial_context: Optional[Dict[str, Any]] = field(default_factory=dict)
    execution_mode: Literal['sync', 'async'] = 'sync'
    resource_tags: List[str] = field(default_factory=list)
    timeout: Optional[float] = 43200.0
    cpu_bound: bool = False
    enqueued_at: float = field(default_factory=time.time)
    planning_depth: int = 0  # ✅ 新增：状态规划递归深度


class TaskQueue:
    """一个支持高级操作的、异步的、有界任务队列。

    支持的操作：
    - 插入任务到任意位置
    - 删除队列中的任务
    - 调整任务顺序
    - 批量操作
    """

    def __init__(self, maxsize: int = 1000):
        """初始化任务队列。

        Args:
            maxsize (int): 队列的最大容量，用于实现背压（back-pressure）。
        """
        self._maxsize = maxsize
        self._queue: deque[Tasklet] = deque()
        self._lock = asyncio.Lock()
        self._not_empty = asyncio.Condition(self._lock)
        self._not_full = asyncio.Condition(self._lock)
        self._unfinished_tasks = 0

    async def put(self, tasklet: Tasklet, high_priority: bool = False):
        """将一个任务单元异步放入队列。

        如果队列已满，此操作将会阻塞等待，直到有空间可用。

        Args:
            tasklet: 要放入的任务单元。
            high_priority: 如果为 True，任务将被插入到队列头部。
        """
        async with self._not_full:
            while len(self._queue) >= self._maxsize:
                await self._not_full.wait()

            if high_priority:
                self._queue.appendleft(tasklet)
            else:
                self._queue.append(tasklet)

            self._unfinished_tasks += 1
            self._not_empty.notify()

    def put_nowait(self, tasklet: Tasklet, high_priority: bool = False):
        """从同步代码中非阻塞地将任务放入队列。

        Args:
            tasklet: 要放入的任务单元。
            high_priority: 如果为 True，任务将被插入到队列头部。

        Raises:
            asyncio.QueueFull: 如果队列已满。
        """
        if len(self._queue) >= self._maxsize:
            raise asyncio.QueueFull()

        if high_priority:
            self._queue.appendleft(tasklet)
        else:
            self._queue.append(tasklet)

        self._unfinished_tasks += 1

    async def get(self) -> Tasklet:
        """从队列中异步获取一个任务单元。

        如果队列为空，此操作将会阻塞等待，直到有任务可用。
        """
        async with self._not_empty:
            while not self._queue:
                await self._not_empty.wait()

            tasklet = self._queue.popleft()
            self._not_full.notify()
            return tasklet

    def task_done(self):
        """Notify the queue that a task has completed."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            if self._unfinished_tasks <= 0:
                raise ValueError("task_done() called too many times")
            self._unfinished_tasks -= 1
            return
        loop.create_task(self._task_done_async())

    async def _task_done_async(self):
        async with self._not_empty:
            if self._unfinished_tasks <= 0:
                raise ValueError("task_done() called too many times")
            self._unfinished_tasks -= 1
            if self._unfinished_tasks == 0:
                self._not_empty.notify_all()

    async def join(self):
        """阻塞直到队列中的所有任务都被获取并处理完毕。"""
        async with self._not_empty:
            while self._unfinished_tasks:
                await self._not_empty.wait()

    def empty(self) -> bool:
        """检查队列是否为空。"""
        return len(self._queue) == 0

    def qsize(self) -> int:
        """返回队列中的大致项目数量。"""
        return len(self._queue)

    # ========== 高级队列操作 ==========

    async def insert_at(self, index: int, tasklet: Tasklet) -> bool:
        """将任务插入到队列的指定位置。

        Args:
            index: 插入位置（0 为队列头部）
            tasklet: 要插入的任务

        Returns:
            是否成功插入
        """
        async with self._not_full:
            while len(self._queue) >= self._maxsize:
                await self._not_full.wait()

            try:
                self._queue.insert(max(0, min(index, len(self._queue))), tasklet)
                self._unfinished_tasks += 1
                self._not_empty.notify()
                return True
            except Exception:
                return False

    async def remove_by_cid(self, cid: str) -> bool:
        """从队列中删除指定 cid 的任务。

        Args:
            cid: 任务的唯一追踪ID

        Returns:
            是否成功删除
        """
        async with self._lock:
            for i, tasklet in enumerate(self._queue):
                if tasklet.cid == cid:
                    del self._queue[i]
                    if self._unfinished_tasks > 0:
                        self._unfinished_tasks -= 1
                    self._not_full.notify()
                    return True
            return False

    async def remove_by_filter(self, predicate: Callable[[Tasklet], bool]) -> int:
        """根据条件批量删除任务。

        Args:
            predicate: 判断函数，返回 True 的任务将被删除

        Returns:
            删除的任务数量
        """
        async with self._lock:
            to_remove = [i for i, t in enumerate(self._queue) if predicate(t)]
            for i in reversed(to_remove):
                del self._queue[i]
            if to_remove:
                # 将计数与实际队列同步，避免 join() 永久阻塞
                self._unfinished_tasks = max(0, self._unfinished_tasks - len(to_remove))
                self._not_full.notify()
            return len(to_remove)

    async def move_to_front(self, cid: str) -> bool:
        """将指定任务移动到队列头部。

        Args:
            cid: 任务的唯一追踪ID

        Returns:
            是否成功移动
        """
        async with self._lock:
            for i, tasklet in enumerate(self._queue):
                if tasklet.cid == cid:
                    self._queue.rotate(-i)
                    return True
            return False

    async def move_to_position(self, cid: str, new_index: int) -> bool:
        """将指定任务移动到指定位置。

        Args:
            cid: 任务的唯一追踪ID
            new_index: 新的位置索引

        Returns:
            是否成功移动
        """
        async with self._lock:
            for i, tasklet in enumerate(self._queue):
                if tasklet.cid == cid:
                    self._queue.rotate(-i)
                    task = self._queue.popleft()
                    self._queue.rotate(i)

                    target_index = max(0, min(new_index, len(self._queue)))
                    self._queue.insert(target_index, task)
                    return True
            return False

    async def list_all(self) -> List[Dict[str, Any]]:
        """获取队列中所有任务的快照。

        Returns:
            包含所有任务信息的列表
        """
        async with self._lock:
            return [
                {
                    "cid": t.cid,
                    "task_name": t.task_name,
                    "plan_name": t.payload.get("plan_name") if t.payload else None,
                    "is_ad_hoc": t.is_ad_hoc,
                    "execution_mode": t.execution_mode,
                }
                for t in self._queue
            ]

    async def clear(self) -> int:
        """清空队列中的所有任务。

        Returns:
            清除的任务数量
        """
        async with self._lock:
            count = len(self._queue)
            self._queue.clear()
            # 清空时重置未完成计数，防止后续 join() 悬挂
            self._unfinished_tasks = 0
            self._not_full.notify_all()
            return count

    async def reorder(self, cid_order: List[str]) -> bool:
        """根据提供的 cid 列表重新排序队列。

        Args:
            cid_order: 期望的 cid 顺序列表

        Returns:
            是否成功重排序
        """
        async with self._lock:
            cid_to_task = {t.cid: t for t in self._queue if t.cid}

            new_queue = []
            for cid in cid_order:
                if cid in cid_to_task:
                    new_queue.append(cid_to_task.pop(cid))

            # 将未在列表中的任务追加到末尾
            new_queue.extend(cid_to_task.values())

            self._queue = deque(new_queue)
            # 重排不影响未完成计数
            return True
