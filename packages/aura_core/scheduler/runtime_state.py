# -*- coding: utf-8 -*-
"""Mutable runtime state for Scheduler."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Set


@dataclass(slots=True)
class SchedulerRuntimeState:
    """Central container for mutable scheduler runtime data."""

    run_statuses: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    running_tasks: Dict[str, Any] = field(default_factory=dict)
    running_task_meta: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    schedule_items: List[Dict[str, Any]] = field(default_factory=list)
    interrupt_definitions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    user_enabled_globals: Set[str] = field(default_factory=set)
    all_tasks_definitions: Dict[str, Any] = field(default_factory=dict)
    task_load_errors: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def clear_plan_runtime_data(self) -> None:
        self.schedule_items.clear()
        self.interrupt_definitions.clear()
        self.user_enabled_globals.clear()
        self.all_tasks_definitions.clear()
        self.task_load_errors.clear()

    def clear_active_runs(self) -> None:
        self.running_tasks.clear()
        self.running_task_meta.clear()
