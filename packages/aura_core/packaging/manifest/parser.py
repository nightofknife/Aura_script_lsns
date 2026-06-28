"""Manifest parser and validator."""

from __future__ import annotations

from pathlib import Path
from typing import List

import yaml
from packaging.version import Version

from .schema import (
    BuildConfig,
    ConfigurationSpec,
    DependencySpec,
    Exports,
    ExportedAction,
    ExportedService,
    ExportedTask,
    LifecycleHooks,
    PackageInfo,
    PluginManifest,
    ResourceMapping,
    TaskConfiguration,
    TrustInfo,
)


class ManifestParser:
    """Parse and validate ``manifest.yaml`` files."""

    @staticmethod
    def parse(manifest_path: Path) -> PluginManifest:
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        package = PackageInfo(**data["package"])

        dependencies = {}
        for name, spec in data.get("dependencies", {}).items():
            if isinstance(spec, dict):
                if "path" in spec and spec["path"]:
                    spec["path"] = Path(spec["path"])
                dependencies[name] = DependencySpec(name=name, **spec)
            else:
                dependencies[name] = DependencySpec(name=name, version=spec, source="local")

        exports_data = data.get("exports", {})
        exports = Exports(
            services=[ManifestParser._parse_service(item) for item in exports_data.get("services", [])],
            actions=[ManifestParser._parse_action(item) for item in exports_data.get("actions", [])],
            tasks=[ExportedTask(**item) for item in exports_data.get("tasks", [])],
        )

        lifecycle = LifecycleHooks(**data.get("lifecycle", {}))
        configuration = ConfigurationSpec(**data.get("configuration", {}))
        resources = ResourceMapping(**data.get("resources", {}))
        build = BuildConfig(**data.get("build", {}))
        trust = TrustInfo(**data.get("trust", {}))
        task_config_data = data.get("task_paths", ["tasks"])
        task_config = TaskConfiguration(
            task_paths=task_config_data if isinstance(task_config_data, list) else ["tasks"]
        )
        task_config.validate()

        return PluginManifest(
            package=package,
            requires=data.get("requires", {}),
            dependencies=dependencies,
            pypi_dependencies=data.get("pypi-dependencies", {}),
            exports=exports,
            extends=data.get("extends", []),
            overrides=data.get("overrides", []),
            lifecycle=lifecycle,
            configuration=configuration,
            resources=resources,
            build=build,
            trust=trust,
            task_config=task_config,
            metadata=data.get("metadata", {}),
            path=manifest_path.parent,
        )

    @staticmethod
    def _parse_service(item: dict) -> ExportedService:
        payload = dict(item)
        if "source" in payload and ("module" not in payload or "class" not in payload):
            module, class_name = str(payload.pop("source")).split(":", 1)
            payload["module"] = module
            payload["class"] = class_name
        if "visibility" in payload and "public" not in payload:
            payload["public"] = payload.pop("visibility") == "public"
        payload["class_name"] = payload.pop("class", payload.pop("class_name", None))
        return ExportedService(**payload)

    @staticmethod
    def _parse_action(item: dict) -> ExportedAction:
        payload = dict(item)
        if "source" in payload and ("module" not in payload or "function" not in payload):
            module, function_name = str(payload.pop("source")).split(":", 1)
            payload["module"] = module
            payload["function"] = function_name
        if "visibility" in payload and "public" not in payload:
            payload["public"] = payload.pop("visibility") == "public"
        payload["function_name"] = payload.pop("function", payload.pop("function_name", None))
        return ExportedAction(**payload)

    @staticmethod
    def validate(manifest: PluginManifest) -> List[str]:
        errors = []
        if not manifest.package.name:
            errors.append("package.name is required")
        if not manifest.package.version:
            errors.append("package.version is required")

        try:
            Version(manifest.package.version)
        except Exception:
            errors.append(f"Invalid version: {manifest.package.version}")

        for dep_name, dep in manifest.dependencies.items():
            if dep.source == "local" and not dep.path:
                errors.append(f"Dependency {dep_name}: local source requires 'path'")
            if dep.source == "git" and not dep.git_url:
                errors.append(f"Dependency {dep_name}: git source requires 'git_url'")

        for service in manifest.exports.services:
            if not service.module:
                errors.append(f"Service '{service.name}' missing module")
            if not service.class_name:
                errors.append(f"Service '{service.name}' missing class")

        for action in manifest.exports.actions:
            if not action.module:
                errors.append(f"Action '{action.name}' missing module")
            if not action.function_name:
                errors.append(f"Action '{action.name}' missing function")

        return errors
