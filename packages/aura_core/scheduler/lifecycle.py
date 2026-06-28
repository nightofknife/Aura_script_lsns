# -*- coding: utf-8 -*-
"""Scheduler lifecycle management."""

from __future__ import annotations

import asyncio
import inspect
import queue
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, Iterable, Optional

from packages.aura_core.config.loader import get_config_value
from packages.aura_core.observability.events import Event
from packages.aura_core.observability.logging.core_logger import logger

if TYPE_CHECKING:
    from .core import Scheduler


class LifecycleManager:
    """Own scheduler thread lifecycle, startup wait, shutdown and preload."""

    def __init__(self, scheduler: "Scheduler"):
        self.scheduler = scheduler
        self._scheduler_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._main_task: Optional[asyncio.Task] = None
        self._startup_lock = threading.Lock()
        self._shutdown_lock = threading.Lock()
        self._shutdown_done = False

    def _run_on_control_loop(self, coro, *, timeout: float):
        return self.scheduler.run_on_control_loop(coro, timeout=timeout)

    def start(self):
        timed_out = False
        startup_timeout = float(
            getattr(
                self.scheduler,
                "startup_timeout_sec",
                get_config_value(
                    "runtime.startup_timeout_sec",
                    get_config_value("backend.scheduler_startup_timeout_sec", 10),
                ),
            )
        )

        with self._startup_lock:
            if self._scheduler_thread and self._scheduler_thread.is_alive():
                logger.warning("Scheduler is already running.")
                return

            with self._shutdown_lock:
                self._shutdown_done = False

            self.scheduler.startup_complete_event.clear()
            logger.info("Starting scheduler and background services...")
            self.scheduler.execution_manager.startup()

            self._scheduler_thread = threading.Thread(
                target=self._run_in_thread,
                name="SchedulerThread",
                daemon=True,
            )
            self._scheduler_thread.start()

            logger.info("Waiting for scheduler event loop startup...")
            if not self.scheduler.startup_complete_event.wait(timeout=startup_timeout):
                logger.error("Scheduler startup timed out.")
                timed_out = True
            else:
                logger.info("Scheduler started, publishing scheduler.started event...")
                if not self._loop:
                    logger.warning("Event loop not available; skip scheduler.started publish.")
                else:
                    try:
                        self._run_on_control_loop(
                            self.scheduler.event_bus.publish(
                                Event(
                                    name="scheduler.started",
                                    payload={"message": "Scheduler has started."},
                                )
                            ),
                            timeout=5.0,
                        )
                        logger.info("scheduler.started published.")
                    except asyncio.TimeoutError:
                        logger.error("Publishing scheduler.started timed out.")
                    except Exception as exc:
                        logger.error("Publishing scheduler.started failed: %s", exc, exc_info=True)

        if timed_out:
            self.stop()
            raise RuntimeError(f"Scheduler startup timed out after {startup_timeout} seconds.")

        if hasattr(self.scheduler, "ui_bridge"):
            self.scheduler.ui_bridge.push_update("master_status_update", {"is_running": True})

    def stop(self):
        with self._startup_lock:
            if not self._scheduler_thread or not self._scheduler_thread.is_alive():
                logger.warning("Scheduler is already stopped.")
                return

            logger.info("Stopping scheduler...")

            if hasattr(self.scheduler, "scheduling_service"):
                self.scheduler.scheduling_service.stop()
            if hasattr(self.scheduler, "interrupt_service"):
                self.scheduler.interrupt_service.stop()

            time.sleep(0.5)

            if self._loop and self._loop.is_running():
                try:
                    self._run_on_control_loop(
                        self.scheduler.event_bus.publish(
                            Event(
                                name="scheduler.stopping",
                                payload={"message": "Scheduler is stopping."},
                            )
                        ),
                        timeout=5.0,
                    )
                    logger.info("scheduler.stopping published.")
                except Exception as exc:
                    logger.error("Publishing scheduler.stopping failed: %s", exc)

                try:
                    logger.info("Clearing EventBus subscriptions...")
                    self._run_on_control_loop(
                        self.scheduler.event_bus.clear_subscriptions(keep_persistent=False),
                        timeout=5.0,
                    )
                    stats = self.scheduler.event_bus.get_stats()
                    logger.info(
                        "EventBus subscriptions cleared. remaining=%s persistent=%s transient=%s",
                        stats["total_subscriptions"],
                        stats["persistent_subscriptions"],
                        stats["transient_subscriptions"],
                    )
                except asyncio.TimeoutError:
                    logger.error("Clearing EventBus subscriptions timed out.")
                except Exception as exc:
                    logger.error("Clearing EventBus subscriptions failed: %s", exc, exc_info=True)

            if self._loop:
                if getattr(self.scheduler, "is_running", None):
                    self._loop.call_soon_threadsafe(self.scheduler.is_running.clear)
                if self._main_task:
                    self._loop.call_soon_threadsafe(self._main_task.cancel)
            else:
                logger.debug("No event loop available during shutdown.")

            logger.info("Waiting for scheduler thread to exit...")
            self._scheduler_thread.join(timeout=5)

            if self._scheduler_thread.is_alive():
                logger.warning("Scheduler thread did not stop within 5 seconds, cancelling remaining tasks.")
                if self._loop:
                    try:
                        def cancel_all():
                            for task in asyncio.all_tasks(self._loop):
                                if not task.done():
                                    task.cancel()

                        self._loop.call_soon_threadsafe(cancel_all)
                    except Exception as exc:
                        logger.error("Cancelling remaining tasks failed: %s", exc)

                self._scheduler_thread.join(timeout=5)

                if self._scheduler_thread.is_alive():
                    logger.error("Scheduler thread is still alive after repeated shutdown attempts.")

            self._scheduler_thread = None
            self._loop = None

        if hasattr(self.scheduler, "ui_bridge"):
            self.scheduler.ui_bridge.push_update("master_status_update", {"is_running": False})

        logger.info("Scheduler stopped.")

    def _cleanup_resources(self):
        with self._shutdown_lock:
            if self._shutdown_done:
                logger.debug("Scheduler resources already cleaned.")
                return

            try:
                logger.info("Cleaning scheduler resources...")
                with self.scheduler.fallback_lock:
                    self.scheduler.state.clear_active_runs()

                if hasattr(self.scheduler, "execution_manager") and self.scheduler.execution_manager:
                    try:
                        self.scheduler.execution_manager.shutdown()
                    except Exception as exc:
                        logger.error("Execution manager shutdown failed: %s", exc, exc_info=True)

                try:
                    self.scheduler.actions.clear()
                except Exception as exc:
                    logger.error("Action registry cleanup failed: %s", exc, exc_info=True)

                try:
                    self.scheduler._clear_service_registry()
                except Exception as exc:
                    logger.error("Service registry cleanup failed: %s", exc, exc_info=True)

                self._shutdown_done = True
                logger.info("Scheduler resource cleanup completed.")
            except Exception as exc:
                logger.error("Scheduler resource cleanup failed: %s", exc, exc_info=True)
            finally:
                self.scheduler.startup_complete_event.set()

    def _run_in_thread(self):
        try:
            asyncio.run(self.scheduler.run())
        except (asyncio.CancelledError, KeyboardInterrupt):
            logger.info("Scheduler runtime loop cancelled.")
        except Exception as exc:
            logger.critical("Scheduler runtime loop crashed: %s", exc, exc_info=True)
        finally:
            self._cleanup_resources()

    async def preload_configured_services(self) -> None:
        config_service = getattr(self.scheduler, "config_service", None)
        services_cfg = config_service.get("services", {}) if config_service else {}
        preload_cfg = services_cfg.get("preload", {}) if isinstance(services_cfg, dict) else {}
        enabled = bool(preload_cfg.get("enabled", False))
        warmup_default = bool(preload_cfg.get("warmup", False))
        configured_targets = preload_cfg.get("targets", []) or []

        target_list = []
        seen = set()
        if isinstance(configured_targets, list):
            for item in configured_targets:
                if isinstance(item, str) and item not in seen:
                    target_list.append(item)
                    seen.add(item)

        if isinstance(services_cfg, dict):
            for name, svc_cfg in services_cfg.items():
                if name == "preload" or not isinstance(svc_cfg, dict):
                    continue
                if svc_cfg.get("lazy_load") is False or svc_cfg.get("preload_on_start"):
                    if name not in seen:
                        target_list.append(name)
                        seen.add(name)

        if target_list:
            await self._preload_services(
                targets=target_list,
                enabled=enabled,
                warmup_default=warmup_default,
                config_service=config_service,
            )

    async def _preload_services(
        self,
        *,
        targets: Iterable[str],
        enabled: bool,
        warmup_default: bool,
        config_service: Any,
    ) -> None:
        for name in targets:
            svc_cfg = config_service.get(f"services.{name}", {}) if config_service else {}
            if not isinstance(svc_cfg, dict):
                svc_cfg = {}
            lazy_load = svc_cfg.get("lazy_load", True)
            preload_on_start = svc_cfg.get("preload_on_start", False)
            warmup = bool(svc_cfg.get("warmup", warmup_default))

            should_preload = enabled or preload_on_start or (lazy_load is False)
            if not should_preload:
                continue

            try:
                service = self.scheduler._resolve_service(name)
            except Exception as exc:
                logger.warning("Service preload skipped: '%s' not available (%s).", name, exc)
                continue

            await self._preload_service_instance(name, service, warmup=warmup)

    async def _preload_service_instance(self, name: str, service: Any, *, warmup: bool) -> None:
        candidates = (
            ("preload_engine", {"warmup": warmup}),
            ("preload", {"warmup": warmup}),
            ("initialize_engine", {}),
            ("initialize", {}),
            ("self_check", {}),
        )

        for method_name, kwargs in candidates:
            method = getattr(service, method_name, None)
            if not callable(method):
                continue
            try:
                await self._run_preload_method(method, kwargs)
                logger.info("Service '%s' preloaded via %s.", name, method_name)
            except Exception as exc:
                logger.warning("Service '%s' preload via %s failed: %s", name, method_name, exc)
            return

        logger.debug("Service '%s' has no preload method, skipping.", name)

    async def _run_preload_method(self, method, kwargs: Dict[str, Any]) -> Any:
        if inspect.iscoroutinefunction(method):
            try:
                return await method(**kwargs)
            except TypeError:
                return await method()
        return await asyncio.to_thread(self._call_preload_method, method, kwargs)

    def _call_preload_method(self, method, kwargs: Dict[str, Any]) -> Any:
        if not kwargs:
            return method()
        try:
            return method(**kwargs)
        except TypeError:
            return method()

    def initialize_async_components(self):
        from packages.aura_core.scheduler.queues.task_queue import TaskQueue

        logger.debug("Initializing async scheduler components...")

        self.scheduler.is_running = asyncio.Event()

        if not hasattr(self.scheduler, "api_log_queue") or self.scheduler.api_log_queue is None:
            self.scheduler.api_log_queue = queue.Queue(
                maxsize=max(int(get_config_value("logging.api_queue_maxsize", 2000)), 1)
            )

        self.scheduler.task_queue = TaskQueue(
            maxsize=int(get_config_value("scheduler.queue.main_maxsize", 1000))
        )
        self.scheduler.event_task_queue = TaskQueue(
            maxsize=int(get_config_value("scheduler.queue.event_maxsize", 2000))
        )
        self.scheduler.interrupt_queue = asyncio.Queue(
            maxsize=int(get_config_value("scheduler.queue.interrupt_maxsize", 100))
        )

        logger.debug("Async scheduler components initialized.")
