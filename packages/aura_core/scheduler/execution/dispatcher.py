# -*- coding: utf-8 -*-
"""Dispatch service for queue handling and consumers."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from packages.aura_core.config.loader import get_config_value
from packages.aura_core.observability.events import Event, EventBus
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.queues.task_queue import Tasklet


class DispatchService:
    def __init__(self, scheduler: Any):
        self._scheduler = scheduler

    def get_queue_overview(self) -> dict:
        return self._scheduler.get_queue_overview()

    def list_queue(self, state: str, limit: int = 200) -> dict:
        return self._scheduler.list_queue(state, limit)

    async def consume_main_task_queue(self):
        max_cc = int(getattr(self._scheduler.execution_manager, "max_concurrent_tasks", 1) or 1)
        queue_full_sleep = float(get_config_value("scheduler.loop_sleep_sec.queue_full", 0.2))
        consumer_error_sleep = float(get_config_value("scheduler.loop_sleep_sec.consumer_error", 0.5))

        while True:
            try:
                with self._scheduler.fallback_lock:
                    current_running_count = len(self._scheduler.running_tasks)
                    running_keys = list(self._scheduler.running_tasks.keys())
                queue_size = self._scheduler.task_queue.qsize()
                if current_running_count > 0 or queue_size > 0:
                    logger.debug(  # 改为DEBUG级别，避免过多INFO日志
                        f"[Queue Consumer] current status running={current_running_count}/{max_cc}, "
                        f"queue_size={queue_size}, keys={running_keys}"
                    )

                if current_running_count >= max_cc:
                    logger.debug("[Queue Consumer] concurrency limit reached, waiting...")  # 改为DEBUG级别
                    await asyncio.sleep(queue_full_sleep)
                    continue

                tasklet = await self._scheduler.task_queue.get()
                self._scheduler._ensure_tasklet_identifiers(tasklet)
                dequeued_at = time.time()
                queued_at = getattr(tasklet, "enqueued_at", None) or dequeued_at
                queue_wait_ms = max(0.0, (dequeued_at - queued_at) * 1000)

                try:
                    payload = {}
                    tname = getattr(tasklet, "task_name", None) or getattr(tasklet, "name", None)
                    if tname and isinstance(tname, str):
                        if "/" in tname:
                            plan_name, task_name = tname.split("/", 1)
                        else:
                            plan_name, task_name = None, tname
                    else:
                        plan_name = getattr(tasklet, "plan_name", None)
                        task_name = getattr(tasklet, "task_name", None)

                    payload.update(
                        {
                            "cid": tasklet.cid,
                            "trace_id": tasklet.trace_id,
                            "trace_label": tasklet.trace_label,
                            "source": tasklet.source,
                            "plan_name": plan_name,
                            "task_name": task_name,
                            "start_time": dequeued_at,
                            "queue_wait_ms": queue_wait_ms,
                        }
                    )
                    await self._scheduler.event_bus.publish(Event(name="queue.dequeued", payload=payload))
                except Exception:
                    pass

                submit_task = asyncio.create_task(self._scheduler.execution_manager.submit(tasklet))

                key = tasklet.cid

                logger.info(f"[Queue Consumer] task enqueued: key={key}")
                with self._scheduler.fallback_lock:
                    self._scheduler.running_tasks[key] = submit_task
                    self._scheduler._running_task_meta[key] = {
                        "plan_name": plan_name,
                        "task_name": task_name,
                        "source": tasklet.source,
                        "trace_id": tasklet.trace_id,
                        "trace_label": tasklet.trace_label,
                        "dequeued_at": dequeued_at,
                        "queued_at": queued_at,
                    }

                def _cleanup(_fut: asyncio.Task):
                    meta = {}
                    try:
                        with self._scheduler.fallback_lock:
                            removed = self._scheduler.running_tasks.pop(key, None)
                            meta = self._scheduler._running_task_meta.pop(key, {})
                        if removed:
                            logger.debug(f"[consume_main_task_queue] removed running task key={key}")
                        else:
                            # ✅ 降低日志级别：这可能是正常的双重清理
                            logger.debug(f"[consume_main_task_queue] running task key already removed={key}")
                    finally:
                        try:
                            self._scheduler.task_queue.task_done()
                        except Exception:
                            pass
                        try:
                            end_ts = time.time()
                            start_ts = meta.get("dequeued_at") or end_ts
                            q_at = meta.get("queued_at") or start_ts
                            exec_ms = max(0.0, (end_ts - start_ts) * 1000)
                            q_wait = max(0.0, (start_ts - q_at) * 1000)
                            evt_payload = {
                                "cid": key,
                                "trace_id": meta.get("trace_id"),
                                "trace_label": meta.get("trace_label"),
                                "plan_name": meta.get("plan_name"),
                                "task_name": meta.get("task_name"),
                                "source": meta.get("source"),
                                "dequeued_at": start_ts,
                                "completed_at": end_ts,
                                "queue_wait_ms": q_wait,
                                "exec_ms": exec_ms,
                            }
                            try:
                                asyncio.create_task(
                                    self._scheduler.event_bus.publish(Event(name="queue.completed", payload=evt_payload))
                                )
                            except Exception:
                                pass
                        except Exception:
                            logger.debug("queue.completed emit failed")

                submit_task.add_done_callback(_cleanup)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.error("Error consuming main task queue", exc_info=True)
                await asyncio.sleep(consumer_error_sleep)

    async def consume_interrupt_queue(self):
        while self._scheduler.is_running.is_set():
            try:
                handler_rule = await asyncio.wait_for(self._scheduler.interrupt_queue.get(), timeout=1.0)
                rule_name = handler_rule.get("name", "unknown_interrupt")
                logger.info(f"Commander: start handling interrupt '{rule_name}'...")
                scope = handler_rule.get("scope") or "plan"
                target_plan = handler_rule.get("plan_name")
                tasks_to_cancel = []
                with self._scheduler.fallback_lock:
                    for cid, task in self._scheduler.running_tasks.items():
                        meta = self._scheduler._running_task_meta.get(cid, {})
                        if meta.get("source") == "interrupt":
                            continue
                        if scope != "global" and target_plan and meta.get("plan_name") != target_plan:
                            continue
                        tasks_to_cancel.append(task)
                for task in tasks_to_cancel:
                    task.cancel()
                handler_task_id = f"{handler_rule['plan_name']}/{handler_rule['handler_task']}"
                handler_item = {
                    "plan_name": handler_rule["plan_name"],
                    "task_name": handler_rule["handler_task"],
                    "handler_task": handler_rule["handler_task"],
                }
                tasklet = Tasklet(task_name=handler_task_id, payload=handler_item, is_ad_hoc=True, execution_mode="sync")
                self._scheduler._ensure_tasklet_identifiers(tasklet, source="interrupt")
                await asyncio.create_task(self._scheduler.execution_manager.submit(tasklet, is_interrupt_handler=True))
                self._scheduler.interrupt_queue.task_done()
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                logger.info("Interrupt queue consumer cancelled.")
                break

    async def event_worker_loop(self, worker_id: int):
        while self._scheduler.is_running.is_set():
            try:
                tasklet = await asyncio.wait_for(self._scheduler.event_task_queue.get(), timeout=1.0)
                self._scheduler._ensure_tasklet_identifiers(tasklet)
                await self._scheduler.execution_manager.submit(tasklet)
                self._scheduler.event_task_queue.task_done()
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                logger.info(f"Event worker #{worker_id} cancelled.")
                break

    async def enqueue_schedule_item(
        self,
        item: Dict[str, Any],
        *,
        source: str,
        triggering_event: Optional[Event] = None,
    ) -> bool:
        previous_status: Optional[Dict[str, Any]] = None
        status_existed = False

        def _rollback_status() -> None:
            with self._scheduler.fallback_lock:
                if status_existed:
                    self._scheduler.run_statuses[item_id] = previous_status or {}
                else:
                    self._scheduler.run_statuses.pop(item_id, None)

        plan_name = item.get("plan_name")
        task_name = item.get("task")
        item_id = item.get("id")
        if not plan_name or not task_name or not item_id:
            return False
        if not item.get("enabled", False):
            return False

        now = datetime.now()
        with self._scheduler.fallback_lock:
            status_existed = item_id in self._scheduler.run_statuses
            status = self._scheduler.run_statuses.get(item_id, {})
            previous_status = dict(status)
            if status.get("status") in ("queued", "running"):
                return False
            cooldown = item.get("run_options", {}).get("cooldown", 0)
            last_run = status.get("last_run")
            if last_run and (now - last_run).total_seconds() < cooldown:
                return False
            self._scheduler.run_statuses.setdefault(item_id, {}).update({"status": "queued", "queued_at": now})

        provided_inputs = item.get("inputs") if isinstance(item, dict) else None
        ok, resolved_payload = self._scheduler._resolve_task_inputs_for_dispatch(
            plan_name=plan_name,
            task_ref=task_name,
            provided_inputs=(provided_inputs if isinstance(provided_inputs, dict) else {}),
            enforce_package=plan_name,
        )
        if not ok:
            logger.error("Schedule '%s' inputs invalid: %s", item_id, resolved_payload)
            _rollback_status()
            return False

        resolved = resolved_payload["resolved"]
        full_task_id = resolved_payload["full_task_id"]
        task_def = resolved_payload["task_def"]
        initial_context = resolved_payload["validated_inputs"]

        payload = dict(item)
        if isinstance(initial_context, dict):
            payload["inputs"] = initial_context
        payload["task"] = resolved.task_ref
        payload["task_name"] = resolved.task_ref
        tasklet = Tasklet(
            task_name=full_task_id,
            payload=payload,
            triggering_event=triggering_event,
            initial_context=initial_context,
            execution_mode=task_def.get("execution_mode", "sync"),
        )
        self._scheduler._ensure_tasklet_identifiers(tasklet, plan_name=plan_name, task_name=resolved.task_ref, source=source)
        try:
            await self._scheduler.task_queue.put(tasklet)
            return True
        except Exception as exc:
            logger.error("Schedule '%s' enqueue failed: %s", item_id, exc, exc_info=True)
            _rollback_status()
            return False

    async def queue_insert_at(
        self,
        index: int,
        plan_name: str,
        task_name: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        provided_inputs = params if isinstance(params, dict) else {}
        ok, resolved_payload = self._scheduler._resolve_task_inputs_for_dispatch(
            plan_name=plan_name,
            task_ref=task_name,
            provided_inputs=provided_inputs,
            enforce_package=plan_name,
        )
        if not ok:
            return {"status": "error", "message": str(resolved_payload)}

        resolved = resolved_payload["resolved"]
        full_task_id = resolved_payload["full_task_id"]
        task_def = resolved_payload["task_def"]
        validated_inputs = resolved_payload["validated_inputs"]

        canonical_id = str(next(self._scheduler.id_generator))
        status_id = f"adhoc:{canonical_id}"

        tasklet = Tasklet(
            task_name=full_task_id,
            cid=canonical_id,
            is_ad_hoc=True,
            payload={
                "id": status_id,
                "plan_name": plan_name,
                "task": resolved.task_ref,
                "task_name": resolved.task_ref,
                "inputs": validated_inputs,
            },
            execution_mode=task_def.get("execution_mode", "sync"),
            initial_context=validated_inputs,
        )
        self._scheduler._ensure_tasklet_identifiers(tasklet, plan_name=plan_name, task_name=resolved.task_ref, source="manual")

        success = await self._scheduler.task_queue.insert_at(index, tasklet)

        if success:
            self._scheduler.update_run_status(status_id, {"status": "queued", "queued_at": datetime.now()})
            return {
                "status": "success",
                "cid": canonical_id,
                "run_status_id": status_id,
                "message": f"Task inserted at position {index}",
            }
        return {"status": "error", "message": "Failed to insert task"}

    async def queue_remove(self, cid: str) -> Dict[str, Any]:
        success = await self._scheduler.task_queue.remove_by_cid(cid)

        if success:
            try:
                await self._scheduler.event_bus.publish(Event(name="queue.dropped", payload={"cid": cid}))
            except Exception:
                logger.debug("queue.dropped emit failed for %s", cid)
            return {"status": "success", "message": f"Task {cid} removed from queue"}
        return {"status": "error", "message": f"Task {cid} not found in queue"}

    async def queue_move_to_front(self, cid: str) -> Dict[str, Any]:
        success = await self._scheduler.task_queue.move_to_front(cid)

        if success:
            return {"status": "success", "message": f"Task {cid} moved to front"}
        return {"status": "error", "message": f"Task {cid} not found in queue"}

    async def queue_move_to_position(self, cid: str, new_index: int) -> Dict[str, Any]:
        success = await self._scheduler.task_queue.move_to_position(cid, new_index)

        if success:
            return {"status": "success", "message": f"Task {cid} moved to position {new_index}"}
        return {"status": "error", "message": f"Task {cid} not found in queue"}

    async def queue_list_all(self) -> List[Dict[str, Any]]:
        return await self._scheduler.task_queue.list_all()

    async def queue_clear(self) -> Dict[str, Any]:
        existing_items = await self._scheduler.task_queue.list_all()
        count = await self._scheduler.task_queue.clear()
        for item in existing_items:
            cid = item.get("cid")
            if not cid:
                continue
            try:
                await self._scheduler.event_bus.publish(Event(name="queue.dropped", payload={"cid": cid}))
            except Exception:
                logger.debug("queue.dropped emit failed for %s", cid)
        return {"status": "success", "message": f"Cleared {count} tasks from queue"}

    async def queue_reorder(self, cid_order: List[str]) -> Dict[str, Any]:
        success = await self._scheduler.task_queue.reorder(cid_order)

        if success:
            return {"status": "success", "message": "Queue reordered successfully"}
        return {"status": "error", "message": "Failed to reorder queue"}
