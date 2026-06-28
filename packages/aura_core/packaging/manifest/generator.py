# -*- coding: utf-8 -*-
"""Manifest generator backed by static AST scanning."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import yaml

from packages.aura_core.observability.logging.core_logger import logger

from .scanner import ExportScanner


class ManifestGenerator:
    """Generate ``manifest.yaml`` from declarative code metadata."""

    def __init__(self, package_path: Path):
        self.package_path = package_path
        self.src_path = package_path / "src"
        self.task_paths = [package_path / "tasks"]
        self.tasks_path = self.task_paths[0]

    def generate(self, preserve_manual_edits: bool = True) -> Dict[str, Any]:
        existing_manifest = self._load_existing_manifest()
        self.task_paths = self._resolve_task_paths(existing_manifest)
        self.tasks_path = self.task_paths[0] if self.task_paths else (self.package_path / "tasks")

        scanner = ExportScanner(self.package_path, existing_manifest or self._create_default_manifest())
        services = scanner.scan_services()
        actions = scanner.scan_actions()
        tasks = self._scan_tasks()

        new_exports = {"services": services, "actions": actions, "tasks": tasks}

        if preserve_manual_edits and existing_manifest:
            merged_manifest = self._merge_manifests(existing_manifest, new_exports)
        else:
            merged_manifest = existing_manifest or self._create_default_manifest()
            merged_manifest["exports"] = new_exports
        return merged_manifest

    def _load_existing_manifest(self) -> Dict[str, Any]:
        manifest_path = self.package_path / "manifest.yaml"
        if manifest_path.exists():
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
            except Exception as exc:
                logger.warning("Existing manifest is unreadable for '%s': %s", self.package_path, exc)
        return {}

    def _create_default_manifest(self) -> Dict[str, Any]:
        return {
            "package": {
                "name": f"@{self.package_path.parent.name}/{self.package_path.name}",
                "version": "0.1.0",
                "description": "",
                "license": "MIT",
            },
            "requires": {"aura": ">=2.0.0"},
            "dependencies": {},
            "pypi-dependencies": {},
            "exports": {"services": [], "actions": [], "tasks": []},
            "metadata": {
                "generated_by": "ManifestGenerator",
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            },
        }

    def _resolve_task_paths(self, existing_manifest: Dict[str, Any]) -> List[Path]:
        configured = existing_manifest.get("task_paths", ["tasks"])
        if not isinstance(configured, list) or not configured:
            configured = ["tasks"]

        resolved_paths: List[Path] = []
        for item in configured:
            if not isinstance(item, str):
                continue
            task_path = (self.package_path / item).resolve()
            try:
                task_path.relative_to(self.package_path.resolve())
            except Exception:
                logger.warning("Ignore unsafe task path in manifest: %s", item)
                continue
            if task_path.is_dir():
                resolved_paths.append(task_path)

        if not resolved_paths:
            fallback = self.package_path / "tasks"
            if fallback.is_dir():
                resolved_paths.append(fallback.resolve())
        return resolved_paths

    def _scan_tasks(self) -> List[Dict[str, Any]]:
        tasks = []
        package_root = self.package_path.resolve()
        for task_dir in self.task_paths:
            if not task_dir.exists():
                continue
            for yaml_file in task_dir.rglob("*.yaml"):
                try:
                    with open(yaml_file, "r", encoding="utf-8") as f:
                        task_data = yaml.safe_load(f)
                    rel_path = yaml_file.resolve().relative_to(package_root)
                    relative_id = yaml_file.relative_to(task_dir).with_suffix("").as_posix()
                    source = str(rel_path).replace("\\", "/")
                    if isinstance(task_data, dict) and isinstance(task_data.get("steps"), dict):
                        meta = task_data.get("meta") or {}
                        tasks.append(
                            {
                                "id": relative_id,
                                "title": meta.get("title", ""),
                                "source": source,
                                "description": meta.get("description", ""),
                            }
                        )
                        continue

                    if not isinstance(task_data, dict):
                        continue

                    for task_key, task_def in task_data.items():
                        if not isinstance(task_def, dict) or not isinstance(task_def.get("steps"), dict):
                            continue
                        meta = task_def.get("meta") or {}
                        tasks.append(
                            {
                                "id": f"{relative_id}/{task_key}",
                                "title": meta.get("title", ""),
                                "source": source,
                                "description": meta.get("description", ""),
                            }
                        )
                        if task_key == Path(relative_id).name:
                            tasks.append(
                                {
                                    "id": relative_id,
                                    "title": meta.get("title", ""),
                                    "source": source,
                                    "description": meta.get("description", ""),
                                }
                            )
                except Exception as e:
                    logger.warning("Scan task file failed %s: %s", yaml_file, e)
        return tasks

    def _merge_manifests(self, existing: Dict[str, Any], new_exports: Dict[str, Any]) -> Dict[str, Any]:
        merged = existing.copy()
        merged["exports"] = new_exports
        metadata = dict(existing.get("metadata", {}) or {})
        metadata["generated_by"] = "ManifestGenerator"
        if existing.get("exports", {}) != new_exports:
            metadata["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        merged["metadata"] = metadata
        return merged

    @staticmethod
    def _serialize_manifest(manifest_data: Dict[str, Any]) -> str:
        return yaml.safe_dump(
            manifest_data,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )

    def save(self, manifest_data: Dict[str, Any]) -> bool:
        manifest_path = self.package_path / "manifest.yaml"
        serialized = self._serialize_manifest(manifest_data)
        if manifest_path.exists():
            existing = manifest_path.read_text(encoding="utf-8")
            if existing == serialized:
                return False

        temp_path = manifest_path.with_suffix(".yaml.tmp")
        temp_path.write_text(serialized, encoding="utf-8")
        temp_path.replace(manifest_path)
        logger.info("Manifest 已保存到 %s", manifest_path)
        return True
