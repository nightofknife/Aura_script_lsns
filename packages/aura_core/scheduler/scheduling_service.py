# -*- coding: utf-8 -*-
"""Time-based scheduling service."""

from __future__ import annotations

import asyncio
from datetime import datetime

try:
    from croniter import croniter

    CRONITER_AVAILABLE = True
except ImportError:
    CRONITER_AVAILABLE = False
    croniter = None  # type: ignore

from packages.aura_core.config.loader import get_config_value
from packages.aura_core.observability.logging.core_logger import logger
from ..utils.asynccontext import plan_context


class SchedulingService:
    """Periodically evaluate schedule items and enqueue ready tasks."""

    def __init__(self, scheduler):
        self.scheduler = scheduler
        self.is_running = None
        self.tick_sec = int(get_config_value("scheduling_service.tick_sec", 60))
        self._stop_requested = None
        self._croniter_missing_logged = False

    async def run(self):
        logger.info("SchedulingService starting...")
        self.is_running = asyncio.Event()
        self._stop_requested = asyncio.Event()
        self.is_running.set()
        try:
            while not self._stop_requested.is_set():
                if self.scheduler.is_running.is_set():
                    await self._check_and_enqueue_tasks(datetime.now())

                try:
                    await asyncio.wait_for(self._stop_requested.wait(), timeout=self.tick_sec)
                    logger.info("SchedulingService stop requested.")
                    break
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            logger.info("SchedulingService cancelled.")
            raise
        finally:
            self.is_running.clear()
            logger.info("SchedulingService stopped.")

    def stop(self):
        logger.info("Requesting SchedulingService stop...")
        if self._stop_requested:
            self._stop_requested.set()

    async def _check_and_enqueue_tasks(self, now: datetime):
        with self.scheduler.fallback_lock:
            schedule_items_copy = list(self.scheduler.schedule_items)

        for item in schedule_items_copy:
            item_id = item.get("id")
            plan_name = item.get("plan_name")

            if not item_id or not plan_name or not item.get("enabled", False):
                continue

            async with plan_context(plan_name):
                try:
                    with self.scheduler.fallback_lock:
                        status = self.scheduler.run_statuses.get(item_id, {})
                        if status.get("status") in ["queued", "running"]:
                            continue

                    if self._is_ready_to_run(item, now, status) and self._has_cron_trigger_match(item, now, status):
                        logger.info(
                            "Scheduled task '%s' (%s) is ready and enqueued.",
                            item.get("name", item_id),
                            plan_name,
                        )
                        await self.scheduler._enqueue_schedule_item(item, source="schedule")
                except Exception as exc:
                    logger.error("Error checking schedule '%s': %s", item_id, exc, exc_info=True)

    def _is_ready_to_run(self, item: dict, now: datetime, status: dict) -> bool:
        cooldown = item.get("run_options", {}).get("cooldown", 0)
        last_run = status.get("last_run")
        if last_run and (now - last_run).total_seconds() < cooldown:
            return False
        return True

    def _has_cron_trigger_match(self, item: dict, now: datetime, status: dict) -> bool:
        triggers = item.get("triggers") or []
        if not isinstance(triggers, list):
            return False

        has_cron = any(isinstance(trigger, dict) and trigger.get("type") == "cron" for trigger in triggers)
        if has_cron and not CRONITER_AVAILABLE:
            if not self._croniter_missing_logged:
                logger.warning(
                    "Cron triggers detected but 'croniter' is not installed. Cron schedules will be skipped."
                )
                self._croniter_missing_logged = True
            return False

        last_run = status.get("last_run")
        for trigger in triggers:
            if not isinstance(trigger, dict):
                continue
            if trigger.get("type") != "cron":
                continue
            expression = trigger.get("expression")
            if not expression:
                continue

            try:
                iterator = croniter(expression, now)
                prev_scheduled_run = iterator.get_prev(datetime)
                effective_last_run = last_run or datetime.min
                if prev_scheduled_run > effective_last_run:
                    return True
            except Exception as exc:
                item_id = item.get("id")
                logger.error("Invalid cron expression for schedule '%s': %s (%s)", item_id, expression, exc)

        return False
