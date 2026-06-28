# -*- coding: utf-8 -*-
"""Runtime lifecycle domain service for Scheduler."""

import asyncio
from asyncio import TaskGroup
from typing import TYPE_CHECKING

from packages.aura_core.config.loader import get_config_value
from packages.aura_core.observability.logging.core_logger import logger

if TYPE_CHECKING:
    from .core import Scheduler


class RuntimeLifecycleService:
    """Encapsulates scheduler runtime loop and event subscription lifecycle."""

    def __init__(self, scheduler: "Scheduler"):
        self._scheduler = scheduler

    async def run(self):
        scheduler = self._scheduler
        profile = scheduler.runtime_profile
        scheduler.lifecycle.initialize_async_components()
        scheduler.is_running.set()
        scheduler._loop = asyncio.get_running_loop()
        scheduler._main_task = asyncio.current_task()

        scheduler.lifecycle._loop = scheduler._loop
        scheduler.lifecycle._main_task = scheduler._main_task

        buffered_tasklets = []
        with scheduler.fallback_lock:
            if scheduler._pre_start_task_buffer:
                buffered_tasklets = list(scheduler._pre_start_task_buffer)
                scheduler._pre_start_task_buffer.clear()

        if buffered_tasklets:
            logger.info("Flushing %d buffered tasks into queue...", len(buffered_tasklets))
            for tasklet in buffered_tasklets:
                await scheduler.task_queue.put(tasklet)

        logger.info("Scheduler runtime loop started.")
        try:
            await self.async_reload_subscriptions()
            await scheduler.lifecycle.preload_configured_services()
            async with TaskGroup() as tg:
                tg.create_task(self.consume_interrupt_queue())
                tg.create_task(self.consume_main_task_queue())
                for i in range(scheduler.num_event_workers):
                    tg.create_task(self.event_worker_loop(i + 1))
                if profile.enable_schedule_loop:
                    tg.create_task(scheduler.scheduling_service.run())
                else:
                    logger.info("[RuntimeProfile] Scheduling loop is disabled.")
                if profile.enable_interrupt_loop:
                    tg.create_task(scheduler.interrupt_service.run())
                else:
                    logger.info("[RuntimeProfile] Interrupt loop is disabled.")
                scheduler.observability.start_cleanup_task()
                tg.create_task(self.monitor_event_subscriptions())
                scheduler.file_watcher_service.start()
                scheduler.startup_complete_event.set()
        except asyncio.CancelledError:
            logger.info("Scheduler runtime loop cancelled.")
        finally:
            scheduler.is_running.clear()
            await scheduler.observability.stop_cleanup_task()
            scheduler.file_watcher_service.stop()
            scheduler._loop = None
            scheduler._main_task = None
            scheduler.startup_complete_event.set()
            logger.info("Scheduler runtime loop exited.")

    async def async_reload_subscriptions(self):
        scheduler = self._scheduler
        if not scheduler._core_subscriptions_ready:
            await scheduler.event_bus.subscribe(
                event_pattern="scheduler.started",
                callback=self.mirror_event_to_ui_queue,
                channel="*",
                persistent=True,
            )
            await scheduler.event_bus.subscribe(
                event_pattern="metrics.update",
                callback=self.mirror_event_to_ui_queue,
                channel="*",
                persistent=True,
            )
            await scheduler.event_bus.subscribe(
                event_pattern="queue.*",
                callback=self.mirror_event_to_ui_queue,
                channel="*",
                persistent=True,
            )
            await scheduler.event_bus.subscribe(
                event_pattern="task.*",
                callback=self.mirror_event_to_ui_queue,
                channel="*",
                persistent=True,
            )
            await scheduler.event_bus.subscribe(
                event_pattern="node.*",
                callback=self.mirror_event_to_ui_queue,
                channel="*",
                persistent=True,
            )
            await scheduler.event_bus.subscribe(
                event_pattern="task.*",
                callback=self.obs_ingest_event,
                channel="*",
                persistent=True,
            )
            await scheduler.event_bus.subscribe(
                event_pattern="node.*",
                callback=self.obs_ingest_event,
                channel="*",
                persistent=True,
            )
            await scheduler.event_bus.subscribe(
                event_pattern="queue.*",
                callback=self.obs_ingest_event,
                channel="*",
                persistent=True,
            )
            scheduler._core_subscriptions_ready = True
            logger.info("[EventBus] Core persistent subscriptions are registered.")
        else:
            logger.info("[EventBus] Clearing old transient subscriptions...")
            stats_before = scheduler.event_bus.get_stats()
            await scheduler.event_bus.clear_subscriptions(keep_persistent=True)
            stats_after = scheduler.event_bus.get_stats()
            logger.info(
                "[EventBus] Cleared transient subscriptions: %s -> %s (removed=%s)",
                stats_before["total_subscriptions"],
                stats_after["total_subscriptions"],
                stats_before["total_subscriptions"] - stats_after["total_subscriptions"],
            )

        if scheduler.runtime_profile.enable_event_triggers:
            await scheduler.dispatcher.subscribe_event_triggers()
        else:
            logger.info("[RuntimeProfile] Event-trigger subscriptions are disabled.")

    async def monitor_event_subscriptions(self):
        scheduler = self._scheduler
        logger.info("[EventBusMonitor] subscription monitor started.")
        check_interval_sec = int(get_config_value("scheduler.subscription_monitor.interval_sec", 300))
        max_transient_subs = int(get_config_value("scheduler.subscription_monitor.max_transient", 1000))
        cleanup_age_hours = float(get_config_value("scheduler.subscription_monitor.cleanup_age_hours", 1))
        previous_total = 0
        growth_count = 0

        while scheduler.is_running.is_set():
            try:
                await asyncio.sleep(check_interval_sec)
                if not scheduler.is_running.is_set():
                    break

                stats = scheduler.event_bus.get_stats()
                logger.info(
                    "[EventBusMonitor] total=%s persistent=%s transient=%s loops=%s patterns=%s",
                    stats["total_subscriptions"],
                    stats["persistent_subscriptions"],
                    stats["transient_subscriptions"],
                    stats["active_loops"],
                    stats["unique_patterns"],
                )

                if stats["total_subscriptions"] > previous_total:
                    growth_count += 1
                    if growth_count >= 3:
                        logger.warning(
                            "[EventBusMonitor] subscription count keeps growing for %d checks (total=%d).",
                            growth_count,
                            stats["total_subscriptions"],
                        )
                else:
                    growth_count = 0
                previous_total = stats["total_subscriptions"]

                if stats["transient_subscriptions"] > max_transient_subs:
                    logger.warning(
                        "[EventBusMonitor] transient subscriptions exceed threshold (%d > %d), cleaning...",
                        stats["transient_subscriptions"],
                        max_transient_subs,
                    )
                    removed = await scheduler.event_bus.cleanup_stale_subscriptions(max_age_hours=cleanup_age_hours)
                    logger.info("[EventBusMonitor] removed stale subscriptions: %d", removed)

                fix_result = scheduler.event_bus.verify_and_fix_index_consistency()
                if fix_result["total_fixed"] > 0:
                    logger.warning(
                        "[EventBusMonitor] fixed subscription index inconsistencies: orphaned=%d missing=%d",
                        fix_result["orphaned_count"],
                        fix_result["missing_count"],
                    )

            except asyncio.CancelledError:
                logger.info("[EventBusMonitor] monitor cancelled.")
                break
            except Exception as exc:
                logger.error("[EventBusMonitor] monitor error: %s", exc, exc_info=True)

        logger.info("[EventBusMonitor] subscription monitor stopped.")

    async def consume_main_task_queue(self):
        await self._scheduler.dispatch.consume_main_task_queue()

    async def consume_interrupt_queue(self):
        await self._scheduler.dispatch.consume_interrupt_queue()

    async def event_worker_loop(self, worker_id: int):
        await self._scheduler.dispatch.event_worker_loop(worker_id)

    async def mirror_event_to_ui_queue(self, event):
        await self._scheduler.observability.mirror_event_to_ui_queue(event)

    async def obs_ingest_event(self, event):
        await self._scheduler.observability.ingest_event(event)
