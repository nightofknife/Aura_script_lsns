# -*- coding: utf-8 -*-
"""Hot-reload control domain service for Scheduler."""

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Dict

from packages.aura_core.observability.logging.core_logger import logger

if TYPE_CHECKING:
    from .core import Scheduler


class HotReloadControlService:
    """Encapsulates reload and hot-reload orchestration logic."""

    def __init__(self, scheduler: "Scheduler"):
        self._scheduler = scheduler

    async def reload_all(self) -> Dict[str, str]:
        from packages.aura_core.api import ACTION_REGISTRY, hook_manager

        logger.warning("======= Full reload started =======")
        with self._scheduler.fallback_lock:
            if self._scheduler.running_tasks:
                active_tasks = list(self._scheduler.running_tasks.keys())
                msg = f"Cannot reload: {len(active_tasks)} tasks are running: {active_tasks}"
                logger.error(msg)
                return {"status": "error", "message": msg}
            try:
                ACTION_REGISTRY.clear()
                self._scheduler._clear_service_registry()
                self._scheduler._register_core_services()
                hook_manager.clear()
            except Exception as exc:
                logger.critical("Critical error during full reload: %s", exc, exc_info=True)
                return {"status": "error", "message": f"A critical error occurred during reload: {exc}"}

        try:
            await self._scheduler.reload_plans_async()
            logger.info("======= Full reload finished =======")
            return {"status": "success", "message": "Full reload completed successfully."}
        except Exception as exc:
            logger.critical("Critical error during full reload: %s", exc, exc_info=True)
            return {"status": "error", "message": f"A critical error occurred during reload: {exc}"}

    async def reload_task_file(self, file_path: Path):
        with self._scheduler.fallback_lock:
            try:
                plan_name = file_path.relative_to(self._scheduler.base_path / "plans").parts[0]
                orchestrator = self._scheduler.plan_manager.get_plan(plan_name)
                if orchestrator:
                    orchestrator.task_loader.reload_task_file(file_path)
                    self._scheduler.plan_registry.load_all_tasks_definitions()
                    logger.info("Task file '%s' hot reloaded in plan '%s'.", file_path.name, plan_name)
                else:
                    logger.error("Hot reload failed: cannot locate plan '%s' for file '%s'.", plan_name, file_path.name)
            except Exception as exc:
                logger.error("Failed reloading task file '%s': %s", file_path.name, exc, exc_info=True)

    async def reload_plugin_from_py_file(self, file_path: Path):
        from packages.aura_core.api import ACTION_REGISTRY

        with self._scheduler.fallback_lock:
            try:
                try:
                    plan_dir_name = file_path.relative_to(self._scheduler.base_path / "plans").parts[0]
                except ValueError:
                    logger.error("Reload failed: file '%s' is outside plans directory.", file_path)
                    return

                plan_dir = (self._scheduler.base_path / "plans" / plan_dir_name).resolve()
                manifest = next(
                    (
                        m
                        for m in self._scheduler.plan_manager.package_manager.loaded_packages.values()
                        if m.path.resolve() == plan_dir
                    ),
                    None,
                )
                if not manifest:
                    logger.error("Reload failed: no package manifest found for '%s'.", plan_dir)
                    return

                package_id = manifest.package.canonical_id
                if any(task_id.startswith(f"{package_id}/") for task_id in self._scheduler.running_tasks):
                    logger.warning("Skip reloading package '%s': tasks are still running.", package_id)
                    return

                logger.info("Reloading package '%s'...", package_id)
                ACTION_REGISTRY.remove_actions_by_plugin(package_id)
                self._scheduler._remove_services_by_prefix(f"{package_id}/")

                module_prefix = ".".join(manifest.path.relative_to(self._scheduler.base_path).parts)
                modules_to_remove = [name for name in sys.modules if name.startswith(module_prefix)]
                for mod_name in modules_to_remove:
                    del sys.modules[mod_name]

                self._scheduler.plan_registry.load_all()
                logger.info("Package '%s' reloaded.", package_id)
            except Exception as exc:
                logger.error("Error reloading plugin from python file: %s", exc, exc_info=True)

    def enable_hot_reload(self):
        return self._scheduler.hot_reload.enable()

    def disable_hot_reload(self):
        return self._scheduler.hot_reload.disable()

    def is_hot_reload_enabled(self) -> bool:
        return self._scheduler.hot_reload.is_enabled()
