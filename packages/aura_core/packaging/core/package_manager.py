# -*- coding: utf-8 -*-
"""Package manager based on `manifest.yaml`."""

from __future__ import annotations

import logging
import sys
from graphlib import TopologicalSorter
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from ...api import ACTION_REGISTRY, ActionDefinition, ServiceDefinition, service_registry
from ...config.loader import get_config_value
from ..manifest import ManifestGenerator, ManifestParser, PluginManifest

logger = logging.getLogger(__name__)


class PackageManager:
    """Discover, validate, resolve and load manifest-based packages."""

    def __init__(self, packages_dir: Path, plans_dir: Path):
        self.packages_dir = packages_dir
        self.plans_dir = plans_dir
        self.base_path = packages_dir.parent
        self.games_dir = self.base_path / "games"

        manifest_mode = str(
            get_config_value("package.manifest_mode", "", base_path=str(self.base_path)) or ""
        ).strip().lower()
        if not manifest_mode:
            manifest_mode = "hybrid"

        if manifest_mode not in {"strict", "hybrid", "off"}:
            logger.warning("Unknown package.manifest_mode '%s', fallback to hybrid", manifest_mode)
            manifest_mode = "hybrid"

        self.manifest_mode = manifest_mode
        self.auto_sync_manifest = bool(
            get_config_value(
                "package.auto_sync_manifest_on_startup",
                False,
                base_path=str(self.base_path),
            )
        )

        self.loaded_packages: Dict[str, PluginManifest] = {}

    @property
    def _is_hybrid_mode(self) -> bool:
        return self.manifest_mode in {"hybrid", "off"}

    def _iter_package_dirs(self) -> Iterable[Path]:
        candidates = [
            (self.packages_dir, False),
            (self.plans_dir, True),
            (self.games_dir, True),
        ]
        for base_dir, include_all in candidates:
            if not base_dir.exists() or not base_dir.is_dir():
                continue

            for item in base_dir.iterdir():
                if not item.is_dir():
                    continue
                if item.name.startswith(".") or item.name.startswith("__"):
                    continue

                if include_all:
                    yield item
                    continue

                manifest_path = item / "manifest.yaml"
                has_runtime_layout = (item / "src").is_dir() or (item / "tasks").is_dir()
                if manifest_path.is_file() or has_runtime_layout:
                    yield item

    def _auto_sync_manifests(self):
        if not self.auto_sync_manifest:
            return

        synced_count = 0
        for package_dir in self._iter_package_dirs():
            try:
                generator = ManifestGenerator(package_dir)
                try:
                    manifest_data = generator.generate(preserve_manual_edits=True)
                except Exception as e:
                    if not self._is_hybrid_mode:
                        raise
                    logger.warning(
                        "Manifest merge failed for '%s', fallback to generated-only manifest: %s",
                        package_dir,
                        e,
                    )
                    manifest_data = generator.generate(preserve_manual_edits=False)

                generator.save(manifest_data)
                synced_count += 1
            except Exception as e:
                if self._is_hybrid_mode:
                    logger.warning("Manifest auto-sync skipped for '%s': %s", package_dir, e)
                    continue
                raise

        if synced_count:
            logger.info("Manifest auto-sync completed: %s package(s)", synced_count)

    def _build_fallback_manifest(self, package_dir: Path, reason: str) -> Optional[PluginManifest]:
        try:
            generator = ManifestGenerator(package_dir)
            manifest_data = generator.generate(preserve_manual_edits=False)
            generator.save(manifest_data)
            manifest = ManifestParser.parse(package_dir / "manifest.yaml")
            logger.warning("Using generated fallback manifest for '%s' (%s)", package_dir, reason)
            return manifest
        except Exception as e:
            logger.error("Fallback manifest generation failed for '%s': %s", package_dir, e)
            return None

    def load_all_packages(self):
        logger.info("======= PackageManager: start loading all packages =======")

        self._unload_loaded_packages()

        if self.auto_sync_manifest:
            self._auto_sync_manifests()

        manifests = self._discover_packages()
        self._validate_manifests(manifests)
        load_order = self._resolve_dependencies(manifests)
        self._load_in_order(load_order, manifests)

        try:
            service_registry.validate_no_circular_dependencies()
        except ValueError as e:
            logger.error("Service dependency validation failed: %s", e)
            raise

        logger.info("======= loaded %s packages =======", len(self.loaded_packages))

    def _unload_loaded_packages(self):
        if not self.loaded_packages:
            return

        for package_id in list(self.loaded_packages.keys()):
            ACTION_REGISTRY.remove_actions_by_plugin(package_id)
            service_registry.remove_services_by_prefix(f"{package_id}/")
        self.loaded_packages.clear()

    def _discover_packages(self) -> Dict[str, PluginManifest]:
        manifests: Dict[str, PluginManifest] = {}
        discovered_dirs: set[Path] = set()

        for base_dir in [self.packages_dir, self.plans_dir, self.games_dir]:
            if not base_dir.exists():
                continue

            for manifest_name in ("manifest.yaml", "game.yaml"):
                for manifest_path in base_dir.rglob(manifest_name):
                    discovered_dirs.add(manifest_path.parent.resolve())
                    try:
                        manifest = ManifestParser.parse(manifest_path)
                        package_id = manifest.package.canonical_id
                        if package_id in manifests:
                            logger.warning(
                                "Duplicate package canonical_id '%s', last one wins: %s",
                                package_id,
                                manifest_path,
                            )
                        manifests[package_id] = manifest
                        logger.info("Discovered package %s v%s", manifest.package.canonical_id, manifest.package.version)
                    except Exception as e:
                        if self._is_hybrid_mode:
                            logger.warning("Manifest parse failed for '%s': %s", manifest_path, e)
                            fallback = self._build_fallback_manifest(
                                manifest_path.parent,
                                reason=f"parse failed: {e}",
                            )
                            if fallback:
                                manifests[fallback.package.canonical_id] = fallback
                        else:
                            raise ValueError(f"Failed to parse manifest '{manifest_path}': {e}")

        if self._is_hybrid_mode:
            for package_dir in self._iter_package_dirs():
                resolved_dir = package_dir.resolve()
                if resolved_dir in discovered_dirs:
                    continue

                fallback = self._build_fallback_manifest(package_dir, reason="manifest missing")
                if fallback:
                    manifests[fallback.package.canonical_id] = fallback

        return manifests

    def _validate_manifests(self, manifests: Dict[str, PluginManifest]):
        for package_id, manifest in manifests.items():
            errors = ManifestParser.validate(manifest)
            if errors:
                if self._is_hybrid_mode:
                    logger.warning("Package %s has invalid manifest, continue in hybrid mode:", package_id)
                    for error in errors:
                        logger.warning("  - %s", error)
                    continue

                logger.error("Package %s manifest validation failed:", package_id)
                for error in errors:
                    logger.error("  - %s", error)
                raise ValueError(f"Package {package_id} has invalid manifest")

    def _resolve_dependencies(self, manifests: Dict[str, PluginManifest]) -> List[str]:
        graph: Dict[str, List[str]] = {}
        missing_deps: Dict[str, list] = {}

        for package_id, manifest in manifests.items():
            deps: List[str] = []
            for dep_spec in manifest.dependencies.values():
                dep_id = dep_spec.name.lstrip("@")

                if not dep_spec.optional and dep_id not in manifests:
                    missing_deps.setdefault(package_id, []).append((dep_id, dep_spec))
                    continue

                if dep_id in manifests:
                    deps.append(dep_id)

            graph[package_id] = deps

        if missing_deps:
            error_lines = ["Missing package dependencies detected:"]
            for pkg_id, deps in missing_deps.items():
                error_lines.append(f"\nPackage '{pkg_id}' missing required dependencies:")
                for _, dep_spec in deps:
                    error_lines.append(
                        f"  - {dep_spec.name} (version: {dep_spec.version}, source: {dep_spec.source})"
                    )

            if self._is_hybrid_mode:
                logger.warning("\n".join(error_lines))
            else:
                raise ValueError("\n".join(error_lines))

        sorter = TopologicalSorter(graph)
        try:
            load_order = list(sorter.static_order())
            logger.info("Dependency resolved, load order: %s", " -> ".join(load_order))
            return load_order
        except Exception as e:
            logger.error("Dependency resolution failed (possible cycle): %s", e)
            for pkg_id, deps in graph.items():
                logger.error("  %s -> %s", pkg_id, deps)
            if self._is_hybrid_mode:
                logger.warning("Dependency graph has cycle, fallback to discovery order in hybrid mode")
                return list(manifests.keys())
            raise ValueError(f"Cyclic package dependency detected, cannot load. detail: {e}")

    def _load_in_order(self, load_order: List[str], manifests: Dict[str, PluginManifest]):
        for package_id in load_order:
            if package_id not in manifests:
                continue

            manifest = manifests[package_id]
            logger.info("Loading package: %s", package_id)

            try:
                if manifest.lifecycle.on_load:
                    self._call_hook(manifest, manifest.lifecycle.on_load)

                self._register_services(manifest)
                self._register_actions(manifest)
                self._register_tasks(manifest)

                self.loaded_packages[package_id] = manifest
                logger.info("Package %s loaded", package_id)
            except Exception as e:
                logger.error("Package %s load failed: %s", package_id, e)
                raise

    def _call_hook(self, manifest: PluginManifest, hook: str):
        try:
            module_path, func_name = hook.split(":")
            module = self._import_plugin_module(manifest, module_path)
            func = getattr(module, func_name)

            logger.info("Calling hook: %s", hook)
            func()
        except Exception as e:
            logger.warning("Hook call failed %s: %s", hook, e)

    def _import_plugin_module(self, manifest: PluginManifest, module_path: str):
        import importlib

        normalized = str(module_path).strip()
        if not normalized:
            raise ImportError(f"Invalid module path: {module_path!r}")

        normalized = normalized.replace("\\", ".").replace("/", ".")
        if normalized.endswith(".py"):
            normalized = normalized[:-3]
        normalized = normalized.strip(".")

        module = self._import_module_with_recovery(normalized)
        if module is not None:
            return module

        package_prefix = self._derive_package_import_prefix(manifest.path)
        if package_prefix:
            for candidate in (
                f"{package_prefix}.{normalized}",
                f"{package_prefix}.src.{normalized}" if not normalized.startswith("src.") else None,
            ):
                if not candidate:
                    continue
                module = self._import_module_with_recovery(candidate)
                if module is not None:
                    return module
        raise ImportError(f"Unable to import module '{module_path}' for package '{manifest.package.canonical_id}'.")

    def _import_module_with_recovery(self, module_name: str):
        import importlib

        try:
            return importlib.import_module(module_name)
        except Exception:
            self._reset_module_namespace(module_name)
            importlib.invalidate_caches()
            try:
                return importlib.import_module(module_name)
            except Exception:
                return None

    @staticmethod
    def _reset_module_namespace(module_name: str):
        parts = module_name.split(".")
        if not parts:
            return

        prefixes = [module_name]
        root = parts[0]
        if root in {"plans", "games"}:
            prefixes.append(root)
            if len(parts) > 1:
                prefixes.append(".".join(parts[:2]))

        for prefix in prefixes:
            for loaded_name in list(sys.modules):
                if loaded_name == prefix or loaded_name.startswith(f"{prefix}."):
                    sys.modules.pop(loaded_name, None)

    def _derive_package_import_prefix(self, package_path: Path) -> str:
        parts = [package_path.name]
        cursor = package_path.parent
        while (cursor / "__init__.py").exists():
            parts.append(cursor.name)
            cursor = cursor.parent
        return ".".join(reversed(parts))

    def _resolve_dependency_service_id(
        self,
        manifest: PluginManifest,
        dependency_id: str,
        local_service_names: set[str],
    ) -> str:
        token = str(dependency_id).strip()
        if not token:
            raise ValueError(f"Empty service dependency in package '{manifest.package.canonical_id}'.")
        if "/" not in token:
            if token not in local_service_names:
                raise ValueError(
                    f"Service dependency '{token}' in package '{manifest.package.canonical_id}' must be a local exported service alias."
                )
            return f"{manifest.package.canonical_id}/{token}"

        package_id, service_name = token.rsplit("/", 1)
        if package_id == "core":
            return token
        if package_id == manifest.package.canonical_id:
            if service_name not in local_service_names:
                raise ValueError(
                    f"Service dependency '{token}' references missing local service '{service_name}'."
                )
            return token

        declared_dependencies = {name.lstrip("@") for name in manifest.dependencies.keys()}
        if package_id not in declared_dependencies:
            raise ValueError(
                f"Service dependency '{token}' is not declared in manifest.dependencies for '{manifest.package.canonical_id}'."
            )
        target_definition = service_registry._fqid_map.get(token)
        if target_definition is None:
            raise ValueError(
                f"Service dependency '{token}' is not available while loading '{manifest.package.canonical_id}'."
            )
        if not target_definition.public:
            raise ValueError(
                f"Service dependency '{token}' is not public and cannot be consumed by '{manifest.package.canonical_id}'."
            )
        return token

    def _register_services(self, manifest: PluginManifest):
        try:
            local_service_names = {service.name for service in manifest.exports.services}
            for service in manifest.exports.services:
                module = self._import_plugin_module(manifest, service.module)
                service_class = getattr(module, service.class_name)
                service_meta = getattr(service_class, "__aura_service__", {}) or {}
                raw_deps = dict(service_meta.get("deps") or {})
                resolved_deps = {
                    alias: self._resolve_dependency_service_id(manifest, dep_id, local_service_names)
                    for alias, dep_id in raw_deps.items()
                }

                service_fqid = f"{manifest.package.canonical_id}/{service.name}"

                definition = ServiceDefinition(
                    alias=service.name,
                    fqid=service_fqid,
                    service_class=service_class,
                    plugin=manifest,
                    public=service.public,
                    domain="package",
                    replace=service.replace,
                    singleton=service.singleton,
                    service_deps=resolved_deps,
                    description=service.description or service_meta.get("description", ""),
                )
                service_registry.register(definition)

                logger.info("  [OK] Register service: %s", service_fqid)
        except Exception as e:
            logger.error("Register service failed (package: %s): %s", manifest.package.canonical_id, e)
            raise

    def _register_actions(self, manifest: PluginManifest):
        import inspect

        try:
            local_service_names = {service.name for service in manifest.exports.services}
            for action in manifest.exports.actions:
                module = self._import_plugin_module(manifest, action.module)
                action_func = getattr(module, action.function_name)

                canonical_id = manifest.package.canonical_id.lstrip("@")
                parts = canonical_id.split("/")
                if len(parts) != 2:
                    logger.warning("Package canonical_id format is invalid; expected @author/package.")
                    action_fqid = f"{canonical_id}/{action.name}"
                else:
                    author, package_name = parts
                    action_fqid = f"{author}/{package_name}/{action.name}"

                raw_service_deps = getattr(action_func, "_service_dependencies", {})
                service_deps = {
                    alias: self._resolve_dependency_service_id(manifest, dep_id, local_service_names)
                    for alias, dep_id in raw_service_deps.items()
                }

                definition = ActionDefinition(
                    func=action_func,
                    name=action.name,
                    read_only=action.read_only,
                    public=action.public,
                    service_deps=service_deps,
                    plugin=manifest,
                    is_async=inspect.iscoroutinefunction(action_func),
                    timeout=action.timeout,
                    description=action.description or "",
                )
                ACTION_REGISTRY.register(definition)

                logger.info("  [OK] Register action: %s", action_fqid)
        except Exception as e:
            logger.error("Register action failed (package: %s): %s", manifest.package.canonical_id, e)
            raise

    def _register_tasks(self, manifest: PluginManifest):
        if manifest.exports.tasks:
            logger.info(
                "Manifest exports.tasks for %s are metadata only; runtime task index comes from TaskLoader/task_paths.",
                manifest.package.canonical_id,
            )
        for task in manifest.exports.tasks:
            logger.info("  [OK] Discover task: %s/%s", manifest.package.canonical_id, task.id)

    def get_package(self, package_id: str) -> Optional[PluginManifest]:
        return self.loaded_packages.get(package_id)
