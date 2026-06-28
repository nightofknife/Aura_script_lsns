# -*- coding: utf-8 -*-
"""Execution service for submitting tasks."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from packages.aura_core.observability.events import Event, EventBus
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.queues.task_queue import Tasklet
from packages.aura_core.types import TaskRefResolver


class ExecutionService:
    def __init__(self, scheduler: Any):
        self._scheduler = scheduler

    def _run_on_control_loop(self, coro, *, timeout: float):
        return self._scheduler.run_on_control_loop(coro, timeout=timeout)

    @staticmethod
    def _extract_task_ref_fields(plan_name: str, resolved: Any) -> tuple[str, Optional[str], Optional[str]]:
        task_ref = getattr(resolved, "task_ref", None)
        if not task_ref:
            raise ValueError("Resolved task reference is missing 'task_ref'.")

        task_file_path = getattr(resolved, "task_file_path", None)
        task_key = getattr(resolved, "task_key", None)
        if task_file_path is not None:
            return task_ref, task_file_path, task_key

        try:
            parsed = TaskRefResolver.resolve(task_ref, default_package=plan_name, enforce_package=plan_name)
            return task_ref, parsed.task_file_path, parsed.task_key
        except Exception:
            return task_ref, None, task_key

    def _build_tasklet_payload(
        self,
        *,
        item_id: str,
        plan_name: str,
        resolved: Any,
        validated_inputs: Dict[str, Any],
    ) -> Dict[str, Any]:
        task_ref, task_file_path, task_key = self._extract_task_ref_fields(plan_name, resolved)
        payload = {
            "id": item_id,
            "plan_name": plan_name,
            "task": task_ref,
            "task_name": task_ref,
            "inputs": validated_inputs,
        }
        if task_file_path is not None:
            payload["task_file_path"] = task_file_path
        if task_key is not None:
            payload["task_key"] = task_key
        return payload

    def run_manual_task(self, task_id: str) -> Dict[str, Any]:
        if not self._scheduler.is_running or self._scheduler._loop is None or not self._scheduler._loop.is_running():
            return {"status": "error", "message": "Scheduler is not running."}

        schedule_item = None
        for it in self._scheduler.schedule_items:
            if it.get("id") == task_id:
                schedule_item = it
                break
        if not schedule_item:
            return {"status": "error", "message": f"Task id '{task_id}' not found in schedule."}

        plan_name = schedule_item.get("plan_name")
        task_name = schedule_item.get("task")
        if not plan_name or not task_name:
            return {"status": "error", "message": "Schedule item missing plan_name/task."}

        ok, resolved_payload = self._scheduler._resolve_task_inputs_for_dispatch(
            plan_name=plan_name,
            task_ref=task_name,
            provided_inputs=(schedule_item.get("inputs") or {}),
            enforce_package=plan_name,
        )
        if not ok:
            return {"status": "error", "message": str(resolved_payload)}

        resolved = resolved_payload["resolved"]
        full_task_id = resolved_payload["full_task_id"]
        task_def = resolved_payload["task_def"]
        validated_inputs = resolved_payload["validated_inputs"]

        try:
            self._scheduler.update_run_status(task_id, {"status": "queued", "queued_at": datetime.now()})
        except Exception:
            pass

        try:
            tasklet = Tasklet(
                task_name=full_task_id,
                payload=self._build_tasklet_payload(
                    item_id=task_id,
                    plan_name=plan_name,
                    resolved=resolved,
                    validated_inputs=validated_inputs,
                ),
                execution_mode=task_def.get("execution_mode", "sync"),
                initial_context=validated_inputs,
            )
            self._scheduler._ensure_tasklet_identifiers(tasklet, plan_name=plan_name, task_name=resolved.task_ref, source="manual")
        except Exception as exc:
            logger.error(f"Create Tasklet failed: {exc}", exc_info=True)
            return {"status": "error", "message": f"Create Tasklet failed: {exc}"}

        async def _enqueue():
            try:
                await self._scheduler.event_bus.publish(
                    Event(
                        name="queue.enqueued",
                        payload={
                            "cid": tasklet.cid,
                            "trace_id": tasklet.trace_id,
                            "trace_label": tasklet.trace_label,
                            "source": tasklet.source,
                            "plan_name": plan_name,
                            "task_name": resolved.task_ref,
                            "priority": None,
                            "enqueued_at": time.time(),
                            "delay_until": None,
                        },
                    )
                )
            except Exception:
                pass
            await self._scheduler.task_queue.put(tasklet)

        try:
            self._run_on_control_loop(_enqueue(), timeout=5.0)
        except Exception as exc:
            logger.error(f"Enqueue task failed: {exc}", exc_info=True)
            return {"status": "error", "message": f"Enqueue failed: {exc}"}

        return {
            "status": "success",
            "message": "Task enqueued.",
            "cid": tasklet.cid,
            "trace_id": tasklet.trace_id,
            "trace_label": tasklet.trace_label,
        }

    def run_ad_hoc_task(
        self,
        plan_name: str,
        task_name: str,
        params: Optional[Dict[str, Any]] = None,
        temp_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        params = params or {}
        canonical_id = str(next(self._scheduler.id_generator))
        status_id = f"adhoc:{canonical_id}"

        async def async_run():
            with self._scheduler.fallback_lock:
                orchestrator = self._scheduler.plan_manager.get_plan(plan_name)
                if not orchestrator:
                    return {"status": "error", "message": f"Plan '{plan_name}' not found or not loaded."}

                ok, resolved_payload = self._scheduler._resolve_task_inputs_for_dispatch(
                    plan_name=plan_name,
                    task_ref=task_name,
                    provided_inputs=params,
                    enforce_package=plan_name,
                )
                if not ok:
                    return {"status": "error", "message": str(resolved_payload)}

                resolved = resolved_payload["resolved"]
                full_task_id = resolved_payload["full_task_id"]
                task_def = resolved_payload["task_def"]
                validated_inputs = resolved_payload["validated_inputs"]

                tasklet = Tasklet(
                    task_name=full_task_id,
                    cid=canonical_id,
                    is_ad_hoc=True,
                    payload=self._build_tasklet_payload(
                        item_id=status_id,
                        plan_name=plan_name,
                        resolved=resolved,
                        validated_inputs=validated_inputs,
                    ),
                    execution_mode=task_def.get("execution_mode", "sync"),
                    initial_context=validated_inputs,
                )
                self._scheduler._ensure_tasklet_identifiers(tasklet, plan_name=plan_name, task_name=resolved.task_ref, source="manual")

            if self._scheduler.task_queue:
                await self._scheduler.task_queue.put(tasklet)
                await self._scheduler._async_update_run_status(
                    status_id,
                    {"status": "queued", "queued_at": datetime.now()},
                )

                try:
                    await self._scheduler.event_bus.publish(
                        Event(
                            name="queue.enqueued",
                            payload={
                                "cid": tasklet.cid,
                                "trace_id": tasklet.trace_id,
                                "trace_label": tasklet.trace_label,
                                "source": tasklet.source,
                                "plan_name": plan_name,
                                "task_name": resolved.task_ref,
                                "priority": (self._scheduler.all_tasks_definitions.get(full_task_id) or {}).get("priority"),
                                "enqueued_at": datetime.now().timestamp(),
                                "delay_until": None,
                            },
                        )
                    )
                except Exception:
                    pass

            return {
                "status": "success",
                "message": f"Task '{full_task_id}' queued for execution.",
                "temp_id": temp_id,
                "cid": tasklet.cid,
                "trace_id": tasklet.trace_id,
                "trace_label": tasklet.trace_label,
                "run_status_id": status_id,
            }

        if self._scheduler._loop and self._scheduler._loop.is_running():
            try:
                return self._run_on_control_loop(async_run(), timeout=5.0)
            except Exception as exc:
                full_id = f"{plan_name}/{task_name}"
                logger.warning(f"Ad-hoc task failed for '{full_id}': {exc}")
                return {"status": "error", "message": str(exc), "temp_id": temp_id}
        else:
            with self._scheduler.fallback_lock:
                logger.info(f"Scheduler not running, buffering ad-hoc task '{plan_name}/{task_name}'")
                ok, resolved_payload = self._scheduler._resolve_task_inputs_for_dispatch(
                    plan_name=plan_name,
                    task_ref=task_name,
                    provided_inputs=(params or {}),
                    enforce_package=plan_name,
                )
                if not ok:
                    return {"status": "error", "message": str(resolved_payload)}

                resolved = resolved_payload["resolved"]
                full_task_id = resolved_payload["full_task_id"]
                task_def = resolved_payload["task_def"]
                validated_inputs = resolved_payload["validated_inputs"]
                tasklet = Tasklet(
                    task_name=full_task_id,
                    cid=canonical_id,
                    is_ad_hoc=True,
                    payload=self._build_tasklet_payload(
                        item_id=status_id,
                        plan_name=plan_name,
                        resolved=resolved,
                        validated_inputs=validated_inputs,
                    ),
                    execution_mode=task_def.get("execution_mode", "sync"),
                    initial_context=validated_inputs,
                )
                self._scheduler._ensure_tasklet_identifiers(tasklet, plan_name=plan_name, task_name=resolved.task_ref, source="manual")
                self._scheduler._pre_start_task_buffer.append(tasklet)
                self._scheduler.run_statuses.setdefault(status_id, {}).update(
                    {"status": "queued", "queued_at": datetime.now()}
                )
                return {
                    "status": "success",
                    "message": f"Task '{full_task_id}' queued for execution.",
                    "temp_id": temp_id,
                    "cid": tasklet.cid,
                    "trace_id": tasklet.trace_id,
                    "trace_label": tasklet.trace_label,
                    "run_status_id": status_id,
                }

    def run_batch_ad_hoc_tasks(self, tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
        results = []
        success_count = 0
        failed_count = 0

        for task in tasks:
            try:
                result = self.run_ad_hoc_task(
                    plan_name=task.get("plan_name"),
                    task_name=task.get("task_name"),
                    params=task.get("inputs", {}),
                )

                if result.get("status") == "success":
                    success_count += 1
                else:
                    failed_count += 1

                results.append(
                    {
                        "plan_name": task.get("plan_name"),
                        "task_name": task.get("task_name"),
                        "status": result.get("status"),
                        "message": result.get("message"),
                        "cid": result.get("cid"),
                        "trace_id": result.get("trace_id"),
                        "trace_label": result.get("trace_label"),
                    }
                )
            except Exception as exc:
                failed_count += 1
                results.append(
                    {
                        "plan_name": task.get("plan_name"),
                        "task_name": task.get("task_name"),
                        "status": "error",
                        "message": str(exc),
                        "cid": None,
                        "trace_id": None,
                        "trace_label": None,
                    }
                )

        return {"results": results, "success_count": success_count, "failed_count": failed_count}

    def run_batch(self, tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self.run_batch_ad_hoc_tasks(tasks)

    def run_ad_hoc(self, plan_name: str, task_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        return self.run_ad_hoc_task(plan_name, task_name, params)

    def run_manual_schedule(self, item_id: str) -> Dict[str, Any]:
        return self.run_manual_task(item_id)
