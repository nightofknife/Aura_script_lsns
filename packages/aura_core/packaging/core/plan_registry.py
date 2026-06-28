# -*- coding: utf-8 -*-
"""Plan registry for loading plans, schedules, interrupts, and task definitions."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from packages.aura_core.observability.logging.core_logger import logger


class PlanRegistry:
    def __init__(self, scheduler: Any):
        self._scheduler = scheduler

    def load_all(self):
        self._scheduler.plan_manager.initialize()
        self.load_plan_specific_data()

    def load_plan_specific_data(self):
        config_service = self._scheduler.config_service

        def load_core():
            logger.info("--- Loading plan-specific data ---")
            self._scheduler.state.clear_plan_runtime_data()

            # 使用 PackageManager
            plan_manager = self._scheduler.plan_manager

            for package_id, manifest in plan_manager.package_manager.loaded_packages.items():
                # 只处理计划包（在 plans/ 目录下）
                if manifest.path.parent.name != "plans":
                    continue

                plan_name = manifest.path.name
                config_path = manifest.path / "config.yaml"
                if config_path.is_file():
                    try:
                        with open(config_path, "r", encoding="utf-8") as f:
                            config_data = yaml.safe_load(f) or {}
                        config_service.register_plan_config(plan_name, config_data)
                    except Exception as exc:
                        logger.error(f"Failed to load config '{config_path}': {exc}")

                self.load_schedule_file(manifest.path, plan_name)
                self.load_interrupt_file(manifest.path, plan_name)

            self.load_all_tasks_definitions()

        with self._scheduler.fallback_lock:
            load_core()

    def load_all_tasks_definitions(self):
        logger.info("--- Loading all task definitions ---")
        self._scheduler.state.all_tasks_definitions.clear()
        self._scheduler.state.task_load_errors.clear()
        plans = self._scheduler.plan_manager.plans
        if not plans:
            logger.info("No loaded plans, skip task definition indexing")
            return

        for plan_name, orchestrator in plans.items():
            try:
                task_definitions = orchestrator.task_loader.get_all_task_definitions()
            except Exception as exc:
                logger.error(f"Failed to load task definitions for plan '{plan_name}': {exc}")
                continue

            for error in orchestrator.task_loader.get_task_load_errors():
                source_file = error.get("source_file")
                if not source_file:
                    continue
                error_key = f"{plan_name}/{source_file}"
                self._scheduler.state.task_load_errors[error_key] = error

            if not isinstance(task_definitions, dict):
                continue

            self._warn_task_export_mismatch(
                plan_name=plan_name,
                manifest=getattr(orchestrator, "loaded_package", None),
                task_definitions=task_definitions,
            )

            for task_name_in_plan, task_definition in task_definitions.items():
                if not isinstance(task_definition, dict):
                    continue
                task_definition.setdefault("execution_mode", "sync")
                full_task_id = f"{plan_name}/{task_name_in_plan}".replace("//", "/")
                self._scheduler.state.all_tasks_definitions[full_task_id] = task_definition

        logger.info(
            "Task definitions loaded: %s, task load errors: %s",
            len(self._scheduler.state.all_tasks_definitions),
            len(self._scheduler.state.task_load_errors),
        )

    def _warn_task_export_mismatch(self, plan_name: str, manifest: Any, task_definitions: Dict[str, Any]):
        """Warn when manifest exports.tasks diverges from runtime task loader index."""
        if not manifest or not hasattr(manifest, "exports"):
            return

        exported_tasks = getattr(manifest.exports, "tasks", None)
        if not isinstance(exported_tasks, list):
            return

        exported_ids = {
            task.id for task in exported_tasks
            if hasattr(task, "id") and isinstance(task.id, str) and task.id
        }
        runtime_ids = {
            task_name for task_name, task_def in task_definitions.items()
            if isinstance(task_name, str) and task_name and isinstance(task_def, dict)
        }

        if not exported_ids and runtime_ids:
            logger.warning(
                "Plan '%s': manifest exports.tasks is empty, runtime discovered %s task(s).",
                plan_name,
                len(runtime_ids),
            )
            return

        missing_in_manifest = sorted(runtime_ids - exported_ids)
        stale_in_manifest = sorted(exported_ids - runtime_ids)
        if not missing_in_manifest and not stale_in_manifest:
            return

        logger.warning(
            "Plan '%s': manifest exports.tasks mismatches runtime index "
            "(runtime=%s, exported=%s, missing=%s, stale=%s).",
            plan_name,
            len(runtime_ids),
            len(exported_ids),
            len(missing_in_manifest),
            len(stale_in_manifest),
        )
        if missing_in_manifest:
            logger.warning("  Missing in manifest (sample): %s", missing_in_manifest[:5])
        if stale_in_manifest:
            logger.warning("  Stale in manifest (sample): %s", stale_in_manifest[:5])

    def load_schedule_file(self, plan_dir: Path, plan_name: str):
        schedule_path = plan_dir / "schedule.yaml"
        if schedule_path.exists():
            try:
                with open(schedule_path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                if not isinstance(data, dict):
                    logger.error(f"Schedule file '{schedule_path}' should define a mapping with 'schedules'.")
                    return
                items = data.get("schedules", [])
                if not isinstance(items, list):
                    logger.error(f"Schedule file '{schedule_path}' has invalid 'schedules' format.")
                    return
                for idx, item in enumerate(items):
                    if not isinstance(item, dict):
                        continue
                    task_name = item.get("task")
                    if not task_name:
                        logger.warning(f"Schedule item missing task in '{schedule_path}'.")
                        continue
                    item = dict(item)
                    item["plan_name"] = plan_name
                    item.setdefault("triggers", [])
                    item_id = item.get("id") or f"{plan_name}:{task_name}:{idx}"
                    item["id"] = item_id
                    self._scheduler.schedule_items.append(item)
                    self._scheduler.run_statuses.setdefault(item_id, {"status": "idle"})
            except Exception as exc:
                logger.error(f"Failed to load schedule '{schedule_path}': {exc}")

    def load_interrupt_file(self, plan_dir: Path, plan_name: str):
        interrupt_path = plan_dir / "interrupts.yaml"
        if interrupt_path.exists():
            try:
                with open(interrupt_path, "r", encoding="utf-8") as f:
                    for rule in (yaml.safe_load(f) or {}).get("interrupts", []):
                        rule["plan_name"] = plan_name
                        self._scheduler.interrupt_definitions[rule["name"]] = rule
                        if rule.get("scope") == "global" and rule.get("enabled_by_default", False):
                            self._scheduler.user_enabled_globals.add(rule["name"])
            except Exception as exc:
                logger.error(f"Failed to load interrupts '{interrupt_path}': {exc}")

    def list_plans(self) -> list[str]:
        return self._scheduler.plan_manager.list_plans()

    def list_tasks(self, plan_name: Optional[str] = None) -> list[dict]:
        tasks = self._scheduler.get_all_task_definitions_with_meta()
        if plan_name:
            return [t for t in tasks if t.get("plan_name") == plan_name]
        return tasks
