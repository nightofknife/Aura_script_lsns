# -*- coding: utf-8 -*-
"""Tasklet identifier domain service for Scheduler."""

from datetime import datetime
from typing import TYPE_CHECKING, Dict, List, Optional

from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.queues.task_queue import Tasklet

if TYPE_CHECKING:
    from .core import Scheduler


class TaskletIdentityService:
    """Generates and normalizes tasklet identifiers and resource tags."""

    def __init__(self, scheduler: "Scheduler"):
        self._scheduler = scheduler

    @staticmethod
    def base36_encode(num: int) -> str:
        if num == 0:
            return "0"
        chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        out = []
        n = num
        while n > 0:
            n, r = divmod(n, 36)
            out.append(chars[r])
        return "".join(reversed(out))

    def short_cid_suffix(self, cid: Optional[str]) -> str:
        if not cid:
            return "0000"
        try:
            return self.base36_encode(int(cid))[-4:].rjust(4, "0")
        except Exception:
            return (cid[-4:] if len(cid) >= 4 else cid.rjust(4, "0"))

    def make_trace_id(
        self,
        plan_name: str,
        task_name: str,
        cid: str,
        when: Optional[datetime] = None,
    ) -> str:
        ts = when or datetime.now()
        time_part = ts.strftime("%y%m%d-%H%M%S")
        suffix = self.short_cid_suffix(cid)
        return f"{plan_name}/{task_name}@{time_part}-{suffix}"

    def make_trace_label(self, plan_name: Optional[str], task_name: Optional[str]) -> str:
        full_task_id = f"{plan_name}/{task_name}" if plan_name and task_name else (plan_name or task_name or "")
        task_def = self._scheduler.all_tasks_definitions.get(full_task_id, {}) if full_task_id else {}
        title = task_def.get("meta", {}).get("title") if isinstance(task_def, dict) else None
        return title or full_task_id

    def build_resource_tags(self, plan_name: str, task_name: str) -> List[str]:
        tags: List[str] = []
        full_task_id = f"{plan_name}/{task_name}"
        task_data = self._scheduler.all_tasks_definitions.get(full_task_id)
        if not task_data:
            logger.debug("Task definition missing for %s, fallback to exclusive tags.", full_task_id)
            return ["__global_mutex__:1"]

        meta = task_data.get("meta", {})
        concurrency = meta.get("__normalized_concurrency__", {})
        mode = concurrency.get("mode", "exclusive")
        resources = concurrency.get("resources", [])
        mutex_group = concurrency.get("mutex_group")
        max_instances = concurrency.get("max_instances")

        if mode == "exclusive":
            tags.append("__global_mutex__:1")
        elif mode == "shared":
            tags.extend(resources)
            if mutex_group:
                tags.append(f"__mutex_group__:{mutex_group}:1")

        if max_instances:
            tags.append(f"__max_instances__:{full_task_id}:{max_instances}")

        logger.debug("Task %s concurrency=%s tags=%s", full_task_id, mode, tags)
        return tags

    def ensure_tasklet_identifiers(
        self,
        tasklet: Tasklet,
        plan_name: Optional[str] = None,
        task_name: Optional[str] = None,
        source: Optional[str] = None,
    ) -> Tasklet:
        payload = tasklet.payload if isinstance(tasklet.payload, dict) else {}
        if not plan_name:
            plan_name = payload.get("plan_name")
        if not task_name:
            task_name = payload.get("task_name") or payload.get("task") or payload.get("handler_task")

        if (not plan_name or not task_name) and tasklet.task_name and "/" in tasklet.task_name:
            parts = tasklet.task_name.split("/", 1)
            plan_name = plan_name or parts[0]
            task_name = task_name or parts[1]

        if not tasklet.cid:
            tasklet.cid = str(next(self._scheduler.id_generator))

        if not tasklet.trace_id and plan_name and task_name:
            tasklet.trace_id = self.make_trace_id(plan_name, task_name, tasklet.cid)
        if not tasklet.trace_label and plan_name and task_name:
            tasklet.trace_label = self.make_trace_label(plan_name, task_name)
        if not tasklet.resource_tags and plan_name and task_name:
            tasklet.resource_tags = self.build_resource_tags(plan_name, task_name)

        if source and not tasklet.source:
            tasklet.source = source
        if not tasklet.source and payload.get("source"):
            tasklet.source = payload.get("source")

        if tasklet.cid:
            payload.setdefault("cid", tasklet.cid)
        if tasklet.trace_id:
            payload.setdefault("trace_id", tasklet.trace_id)
        if tasklet.trace_label:
            payload.setdefault("trace_label", tasklet.trace_label)
        if tasklet.source:
            payload.setdefault("source", tasklet.source)
        if plan_name:
            payload.setdefault("plan_name", plan_name)
        if task_name:
            payload.setdefault("task_name", task_name)

        tasklet.payload = payload
        return tasklet

