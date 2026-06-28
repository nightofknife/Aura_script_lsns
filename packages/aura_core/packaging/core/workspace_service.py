# -*- coding: utf-8 -*-
"""Plan workspace/file operations service."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Set

from packages.aura_core.observability.logging.core_logger import logger

try:
    from packages.aura_core.packaging.core.dependency_manager import DependencyManager
except ImportError:
    DependencyManager = None


class PlanWorkspaceService:
    """Plan workspace operations extracted from scheduler façade."""

    def __init__(self, scheduler: Any):
        self.scheduler = scheduler

    def delete_plan(self, plan_name: str, *, dry_run: bool = False, backup: bool = True, force: bool = False) -> Dict[str, Any]:
        plan_dir = (self.scheduler.base_path / "plans" / plan_name).resolve()
        plans_root = (self.scheduler.base_path / "plans").resolve()
        if not plan_dir.is_dir() or plans_root not in plan_dir.parents:
            return {"status": "error", "message": f"Plan '{plan_name}' not found."}

        if not DependencyManager:
            logger.warning("DependencyManager not available, skipping dependency cleanup")
            unique_packages = []
            uninstall_output = ""
        else:
            dep_mgr = DependencyManager(self.scheduler.base_path)
            req_name = dep_mgr._requirements_file_name()

            target_requirements = _collect_requirement_names(plan_dir / req_name, dep_mgr)
            other_requirements = set()
            for child in plans_root.iterdir():
                if child.is_dir() and child.name != plan_name:
                    other_requirements |= _collect_requirement_names(child / req_name, dep_mgr)
            other_requirements |= _collect_requirement_names(self.scheduler.base_path / "requirements.txt", dep_mgr)

            unique_packages = sorted(target_requirements - other_requirements)
            uninstall_output = ""
            if unique_packages and not dry_run:
                cmd = [sys.executable, "-m", "pip", "uninstall", "-y", *unique_packages]
                try:
                    logger.info("Uninstalling unique dependencies for plan '%s': %s", plan_name, ", ".join(unique_packages))
                    result = subprocess.run(cmd, capture_output=True, text=True)
                    uninstall_output = (result.stdout or "") + (result.stderr or "")
                    if result.returncode != 0 and not force:
                        return {
                            "status": "error",
                            "message": f"Failed to uninstall dependencies (code {result.returncode}).",
                            "uninstall_output": uninstall_output,
                            "packages": unique_packages,
                        }
                except Exception as exc:
                    if not force:
                        return {"status": "error", "message": f"Uninstall failed: {exc}", "packages": unique_packages}
                    uninstall_output = str(exc)

        backup_path = None
        if backup and not dry_run:
            backup_root = self.scheduler.base_path / "backups"
            backup_root.mkdir(exist_ok=True)
            backup_path = backup_root / f"{plan_name}-{int(time.time())}"
            shutil.copytree(plan_dir, backup_path)

        if not dry_run:
            shutil.rmtree(plan_dir, ignore_errors=False)
            try:
                self.scheduler.reload_plans()
            except Exception as exc:
                return {
                    "status": "error",
                    "message": f"Plan removed but reload failed: {exc}",
                    "backup_path": str(backup_path) if backup_path else None,
                }

        return {
            "status": "success",
            "message": f"Plan '{plan_name}' removed" + (" (dry-run)" if dry_run else ""),
            "packages_uninstalled": unique_packages,
            "backup_path": str(backup_path) if backup_path else None,
            "dry_run": dry_run,
            "uninstall_output": uninstall_output,
        }

    def get_all_plans(self) -> List[str]:
        with self.scheduler.fallback_lock:
            return self.scheduler.plan_manager.list_plans()

    def get_plan_files(self, plan_name: str) -> Dict[str, Any]:
        logger.debug("Request plan file tree: %s", plan_name)
        plan_path = self.scheduler.base_path / "plans" / plan_name
        if not plan_path.is_dir():
            raise FileNotFoundError(f"Plan directory not found for plan '{plan_name}' at path: {plan_path}")

        tree: Dict[str, Any] = {}
        for path in sorted(plan_path.rglob('*')):
            if any(part in ['.git', '__pycache__', '.idea'] for part in path.parts):
                continue
            relative_parts = path.relative_to(plan_path).parts
            current_level = tree
            for part in relative_parts[:-1]:
                current_level = current_level.setdefault(part, {})
            final_part = relative_parts[-1]
            if path.is_file():
                current_level[final_part] = None
            elif path.is_dir() and not any(path.iterdir()):
                current_level.setdefault(final_part, {})
        return tree

    def get_tasks_for_plan(self, plan_name: str) -> List[str]:
        with self.scheduler.fallback_lock:
            tasks = []
            prefix = f"{plan_name}/"
            for task_id in self.scheduler.all_tasks_definitions.keys():
                if task_id.startswith(prefix):
                    tasks.append(task_id[len(prefix):])
            return sorted(tasks)

    async def get_file_content(self, plan_name: str, relative_path: str) -> str:
        orchestrator = self.scheduler.plan_manager.get_plan(plan_name)
        if not orchestrator:
            raise FileNotFoundError(f"Plan '{plan_name}' not found or not loaded.")
        return await orchestrator.get_file_content(relative_path)


def _collect_requirement_names(req_file: Path, dep_mgr: Any) -> Set[str]:
    if not req_file.is_file():
        return set()
    try:
        requirements = dep_mgr._read_requirements(req_file)
    except Exception:
        return set()

    names: Set[str] = set()
    for requirement in requirements:
        name = getattr(requirement, "name", None)
        if name:
            names.add(str(name).lower())
    return names
