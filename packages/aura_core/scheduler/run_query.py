# -*- coding: utf-8 -*-
"""Read/query domain service for Scheduler."""

from typing import TYPE_CHECKING, Any, Dict, List, Optional

from packages.aura_core.observability.logging.core_logger import logger

if TYPE_CHECKING:
    from .core import Scheduler


class RunQueryService:
    """Provides query-only views over scheduler runtime and registry state."""

    def __init__(self, scheduler: "Scheduler"):
        self._scheduler = scheduler

    def get_all_task_definitions_with_meta(self) -> List[Dict[str, Any]]:
        with self._scheduler.fallback_lock:
            detailed_tasks: List[Dict[str, Any]] = []
            for full_task_id, task_def in self._scheduler.all_tasks_definitions.items():
                try:
                    if not isinstance(task_def, dict):
                        continue
                    plan_name, task_name_in_plan = full_task_id.split("/", 1)
                    task_ref = task_def.get("__task_ref__")
                    if not isinstance(task_ref, str) or not task_ref:
                        logger.warning("Skip task missing canonical task_ref: %s", full_task_id)
                        continue
                    detailed_tasks.append(
                        {
                            "full_task_id": full_task_id,
                            "plan_name": plan_name,
                            "task_name_in_plan": task_name_in_plan,
                            "task_ref": task_ref,
                            "meta": task_def.get("meta", {}),
                            "definition": task_def,
                        }
                    )
                except ValueError:
                    logger.warning("Skip malformed task id: %s", full_task_id)
            return detailed_tasks

    def get_task_load_errors(self, plan_name: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._scheduler.fallback_lock:
            errors = list(self._scheduler.task_load_errors.values())
        if plan_name:
            return [item for item in errors if item.get("plan_name") == plan_name]
        return errors

    def get_all_services_status(self) -> List[Dict[str, Any]]:
        with self._scheduler.fallback_lock:
            service_defs = self._scheduler._get_service_definitions()
            return [s.__dict__ for s in service_defs]

    def get_all_interrupts_status(self) -> List[Dict[str, Any]]:
        with self._scheduler.fallback_lock:
            status_list = []
            for name, definition in self._scheduler.interrupt_definitions.items():
                status_item = definition.copy()
                status_item["is_global_enabled"] = name in self._scheduler.user_enabled_globals
                status_list.append(status_item)
            return status_list

    def get_all_services_for_api(self) -> List[Dict[str, Any]]:
        with self._scheduler.fallback_lock:
            original_services = self._scheduler._get_service_definitions()

        api_safe_services = []
        for service_def in original_services:
            class_info = {"module": None, "name": None}
            if hasattr(service_def.service_class, "__module__") and hasattr(service_def.service_class, "__name__"):
                class_info["module"] = service_def.service_class.__module__
                class_info["name"] = service_def.service_class.__name__

            plugin_info = None
            if service_def.plugin:
                package_info = getattr(service_def.plugin, "package", None)
                plugin_info = {
                    "name": (
                        getattr(package_info, "name", None)
                        if package_info is not None
                        else getattr(service_def.plugin, "name", None)
                    ),
                    "canonical_id": (
                        getattr(package_info, "canonical_id", None)
                        if package_info is not None
                        else getattr(service_def.plugin, "canonical_id", None)
                    ),
                    "version": (
                        getattr(package_info, "version", None)
                        if package_info is not None
                        else getattr(service_def.plugin, "version", None)
                    ),
                    "plugin_type": getattr(service_def.plugin, "plugin_type", None),
                }
            api_safe_services.append(
                {
                    "alias": service_def.alias,
                    "fqid": service_def.fqid,
                    "status": service_def.status,
                    "public": service_def.public,
                    "service_class_info": class_info,
                    "plugin": plugin_info,
                }
            )
        return api_safe_services

    def get_queue_overview(self) -> Dict[str, Any]:
        return self._scheduler.observability.get_queue_overview()

    def list_queue(self, state: str, limit: int = 200) -> Dict[str, Any]:
        return self._scheduler.observability.list_queue(state, limit)

    def get_metrics_snapshot(self) -> Dict[str, Any]:
        return self._scheduler.observability.get_metrics_snapshot()

    async def persist_run_snapshot(self, cid: str, run: Dict[str, Any]):
        await self._scheduler.observability._persist_run_snapshot(cid, run)

    def list_persisted_runs(
        self,
        limit: int = 50,
        plan_name: Optional[str] = None,
        task_name: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return self._scheduler.observability.list_persisted_runs(
            limit=limit, plan_name=plan_name, task_name=task_name, status=status
        )

    def get_persisted_run(self, cid: str) -> Dict[str, Any]:
        return self._scheduler.observability.get_persisted_run(cid)

    def get_run_timeline(self, cid_or_trace: str) -> Dict[str, Any]:
        return self._scheduler.observability.get_run_timeline(cid_or_trace)

    def get_ui_event_queue(self):
        return self._scheduler.observability.get_ui_event_queue()

    def get_active_runs_snapshot(self) -> List[Dict[str, Any]]:
        return self._scheduler.observability.get_active_runs_snapshot()

    def get_batch_task_status(self, cids: List[str]) -> List[Dict[str, Any]]:
        return self._scheduler.observability.get_batch_task_status(cids)

    def list_run_history(
        self,
        limit: int = 50,
        plan_name: Optional[str] = None,
        task_name: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return self._scheduler.observability.list_run_history(
            limit=limit,
            plan_name=plan_name,
            task_name=task_name,
            status=status,
        )

    def get_run_detail(self, cid: str) -> Dict[str, Any]:
        return self._scheduler.observability.get_run_detail(cid)
