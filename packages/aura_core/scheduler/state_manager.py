# -*- coding: utf-8 -*-
"""Scheduler state manager (single control path via fallback_lock)."""

from __future__ import annotations

import queue
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Dict, List

from packages.aura_core.observability.logging.core_logger import logger

if TYPE_CHECKING:
    from .core import Scheduler


_ALLOWED_RUN_STATUS_TRANSITIONS = {
    None: {"queued", "running", "idle", "error", "disabled"},
    "queued": {"running", "idle", "error"},
    "running": {"idle", "error"},
    "idle": {"queued", "running", "disabled"},
    "error": {"queued", "idle"},
    "disabled": {"queued", "idle"},
}


class StateManager:
    """Manages scheduler states under a single lock."""

    def __init__(self, scheduler: "Scheduler"):
        self.scheduler = scheduler

    def update_run_status(self, item_id: str, status_update: Dict[str, Any]):
        self._sync_update_run_status(item_id, status_update)

    async def _async_update_run_status(self, item_id: str, status_update: Dict[str, Any]):
        self._sync_update_run_status(item_id, status_update)

    def _sync_update_run_status(self, item_id: str, status_update: Dict[str, Any]):
        if not item_id:
            return
        with self.scheduler.fallback_lock:
            status_update = dict(status_update or {})
            if "status" in status_update:
                current_status = (self.scheduler.run_statuses.get(item_id, {}) or {}).get("status")
                next_status = str(status_update.get("status") or "").lower() or None
                if not self._is_valid_run_status_transition(current_status, next_status):
                    logger.warning(
                        "Reject illegal run status transition: %s -> %s (item_id=%s)",
                        current_status,
                        next_status,
                        item_id,
                    )
                    return

            self.scheduler.run_statuses.setdefault(item_id, {}).update(status_update)
            if hasattr(self.scheduler, "ui_update_queue") and self.scheduler.ui_update_queue:
                try:
                    self.scheduler.ui_update_queue.put_nowait(
                        {
                            "type": "run_status_single_update",
                            "data": {"id": item_id, **self.scheduler.run_statuses[item_id]},
                        }
                    )
                except queue.Full:
                    logger.warning("UI update queue is full, drop: run_status_single_update")

    @staticmethod
    def _is_valid_run_status_transition(current: Any, nxt: Any) -> bool:
        cur = str(current).lower() if current is not None else None
        nst = str(nxt).lower() if nxt is not None else None
        allowed = _ALLOWED_RUN_STATUS_TRANSITIONS.get(cur)
        if allowed is None:
            return True
        return nst in allowed

    def get_running_tasks_count(self) -> int:
        with self.scheduler.fallback_lock:
            return len(self.scheduler.running_tasks)

    async def _async_get_running_tasks_count(self) -> int:
        return self.get_running_tasks_count()

    def get_running_tasks_snapshot(self) -> Dict[str, Any]:
        with self.scheduler.fallback_lock:
            return {
                cid: {
                    "task_name": meta.get("task_name"),
                    "start_time": meta.get("start_time").isoformat() if meta.get("start_time") else None,
                    "duration_sec": (datetime.now() - meta.get("start_time")).total_seconds()
                    if meta.get("start_time")
                    else 0,
                }
                for cid, meta in self.scheduler._running_task_meta.items()
            }

    async def _async_get_running_tasks_snapshot(self) -> Dict[str, Any]:
        return self.get_running_tasks_snapshot()

    def get_master_status(self) -> dict:
        is_running = (
            hasattr(self.scheduler.lifecycle, "_scheduler_thread")
            and self.scheduler.lifecycle._scheduler_thread is not None
            and self.scheduler.lifecycle._scheduler_thread.is_alive()
        )
        return {"is_running": is_running}

    def get_schedule_status(self):
        with self.scheduler.fallback_lock:
            schedule_items_copy = list(self.scheduler.schedule_items)
            run_statuses_copy = dict(self.scheduler.run_statuses)

        status_list = []
        for item in schedule_items_copy:
            full_status = item.copy()
            full_status.update(run_statuses_copy.get(item.get("id"), {}))
            status_list.append(full_status)
        return status_list

    async def _async_get_schedule_status(self):
        return self.get_schedule_status()

    async def _async_update_shared_state(self, update_func: Callable[[], None], read_only: bool = False):
        with self.scheduler.fallback_lock:
            update_func()

    def get_all_services_status(self) -> List[Dict[str, Any]]:
        with self.scheduler.fallback_lock:
            service_defs = self.scheduler._get_service_definitions()
            return [s.__dict__ for s in service_defs]

    async def _async_get_all_services_status(self) -> List[Dict[str, Any]]:
        return self.get_all_services_status()

    def get_all_interrupts_status(self) -> List[Dict[str, Any]]:
        with self.scheduler.fallback_lock:
            return self._fallback_get_all_interrupts_status()

    async def _async_get_all_interrupts_status(self) -> List[Dict[str, Any]]:
        return self.get_all_interrupts_status()

    def _fallback_get_all_interrupts_status(self) -> List[Dict[str, Any]]:
        status_list = []
        for name, definition in self.scheduler.interrupt_definitions.items():
            status_item = definition.copy()
            status_item["is_global_enabled"] = name in self.scheduler.user_enabled_globals
            status_list.append(status_item)
        return status_list
