# -*- coding: utf-8 -*-
"""Interrupt monitoring service."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Set

from packages.aura_core.config.loader import get_config_value
from packages.aura_core.observability.logging.core_logger import logger
from ...utils.asynccontext import plan_context


class InterruptService:
    """Background guardian that checks interrupt rules and submits triggers."""

    def __init__(self, scheduler: Any):
        self.scheduler = scheduler
        self.is_running: Optional[asyncio.Event] = None
        self.interrupt_last_check_times: Dict[str, datetime] = {}
        self.interrupt_cooldown_until: Dict[str, datetime] = {}
        self.poll_interval = float(get_config_value("interrupt_service.poll_sec", 1))
        self._stop_requested: Optional[asyncio.Event] = None

    def stop(self):
        logger.info("Request stopping InterruptService...")
        if self._stop_requested:
            self._stop_requested.set()

    async def run(self):
        logger.info("InterruptService starting...")
        self.is_running = asyncio.Event()
        self._stop_requested = asyncio.Event()
        self.is_running.set()

        try:
            while not self._stop_requested.is_set():
                if self.scheduler.is_running.is_set():
                    active_interrupts = await self._get_active_interrupts()
                    now = datetime.now()

                    for rule_name in active_interrupts:
                        should_check, rule = await self._should_check_interrupt(rule_name, now)
                        if not should_check or not rule:
                            continue

                        plan_name = rule.get("plan_name")
                        if not plan_name:
                            continue

                        logger.trace("Guardian checking interrupt rule '%s'...", rule_name)
                        async with plan_context(plan_name):
                            try:
                                orchestrator = self.scheduler.plans.get(plan_name)
                                if not orchestrator:
                                    continue
                                condition = rule.get("condition", {})
                                if await orchestrator.perform_condition_check(condition):
                                    logger.warning("Interrupt condition matched: '%s'. Submitting...", rule_name)
                                    await self._submit_interrupt(rule, now)
                                    break
                            except Exception as exc:
                                logger.error(
                                    "Interrupt check failed for rule '%s': %s",
                                    rule_name,
                                    exc,
                                    exc_info=True,
                                )

                try:
                    await asyncio.wait_for(self._stop_requested.wait(), timeout=self.poll_interval)
                    logger.info("InterruptService stop signal received.")
                    break
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            logger.info("InterruptService cancelled.")
            raise
        finally:
            if self.is_running:
                self.is_running.clear()
            logger.info("InterruptService stopped.")

    async def _get_active_interrupts(self) -> Set[str]:
        with self.scheduler.fallback_lock:
            interrupt_definitions = dict(self.scheduler.interrupt_definitions)
            user_enabled_globals = set(self.scheduler.user_enabled_globals)
            all_tasks_defs = dict(self.scheduler.all_tasks_definitions)

        runs_snapshot = [
            run for run in (self.scheduler.get_active_runs_snapshot() or []) if run.get("status") == "running"
        ]

        active_set = set(user_enabled_globals)
        for run in runs_snapshot:
            plan_name = run.get("plan_name")
            task_name = run.get("task_name")
            if not plan_name or not task_name:
                continue
            full_task_id = f"{plan_name}/{task_name}"
            task_def = all_tasks_defs.get(full_task_id)
            if not isinstance(task_def, dict):
                continue
            activates = task_def.get("activates_interrupts")
            if isinstance(activates, list) and activates:
                active_set.update(activates)
                logger.debug("Task '%s' activates interrupts: %s", full_task_id, activates)

        return {name for name in active_set if name in interrupt_definitions}

    async def _should_check_interrupt(self, rule_name: str, now: datetime) -> tuple[bool, Optional[Dict[str, Any]]]:
        with self.scheduler.fallback_lock:
            rule = self.scheduler.interrupt_definitions.get(rule_name)
            if not isinstance(rule, dict):
                return False, None

            cooldown_expired = now >= self.interrupt_cooldown_until.get(rule_name, datetime.min)
            if not cooldown_expired:
                return False, None

            last_check = self.interrupt_last_check_times.get(rule_name, datetime.min)
            interval_sec = float(rule.get("check_interval", 5))
            interval_passed = (now - last_check).total_seconds() >= interval_sec

            if interval_passed:
                self.interrupt_last_check_times[rule_name] = now

            return interval_passed, rule

    async def _submit_interrupt(self, rule: Dict[str, Any], now: datetime):
        with self.scheduler.fallback_lock:
            interrupt_queue = self.scheduler.interrupt_queue

        if not interrupt_queue:
            logger.error("Cannot submit interrupt: interrupt queue is not initialized.")
            return

        await interrupt_queue.put(rule)

        cooldown_seconds = float(rule.get("cooldown", 60))
        with self.scheduler.fallback_lock:
            self.interrupt_cooldown_until[rule["name"]] = now + timedelta(seconds=cooldown_seconds)
