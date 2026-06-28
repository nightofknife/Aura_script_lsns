"""Manifest schema models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from packaging.specifiers import SpecifierSet
from packaging.version import Version


@dataclass
class PackageInfo:
    name: str
    version: str
    description: str
    license: str
    authors: List[Dict[str, str]] = field(default_factory=list)
    homepage: Optional[str] = None
    repository: Optional[str] = None
    keywords: List[str] = field(default_factory=list)
    categories: List[str] = field(default_factory=list)

    @property
    def canonical_id(self) -> str:
        return self.name.lstrip("@")


@dataclass
class DependencySpec:
    name: str
    version: str
    source: Literal["local", "git"]
    path: Optional[Path] = None
    git_url: Optional[str] = None
    git_ref: Optional[str] = None
    optional: bool = False
    features: List[str] = field(default_factory=list)

    def is_version_compatible(self, version: str) -> bool:
        try:
            spec = SpecifierSet(self.version)
            return Version(version) in spec
        except Exception:
            return False


@dataclass
class ExportedService:
    name: str
    module: str
    class_name: str
    public: bool = True
    singleton: bool = True
    description: Optional[str] = None
    replace: Optional[str] = None

    @property
    def visibility(self) -> str:
        return "public" if self.public else "private"


@dataclass
class ExportedAction:
    name: str
    module: str
    function_name: str
    public: bool = True
    read_only: bool = False
    description: Optional[str] = None
    timeout: Optional[int] = None
    parameters: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def visibility(self) -> str:
        return "public" if self.public else "private"


@dataclass
class ExportedTask:
    id: str
    title: str
    source: str
    description: Optional[str] = None
    visibility: Literal["public", "private"] = "public"
    schedule: Optional[str] = None
    config_template: Optional[str] = None


@dataclass
class Exports:
    services: List[ExportedService] = field(default_factory=list)
    actions: List[ExportedAction] = field(default_factory=list)
    tasks: List[ExportedTask] = field(default_factory=list)


@dataclass
class LifecycleHooks:
    on_install: Optional[str] = None
    on_uninstall: Optional[str] = None
    on_load: Optional[str] = None
    on_unload: Optional[str] = None


@dataclass
class BuildConfig:
    include: List[str] = field(default_factory=list)
    exclude: List[str] = field(default_factory=list)
    scripts: Dict[str, str] = field(default_factory=dict)


@dataclass
class TrustInfo:
    signature: Optional[Dict[str, str]] = None


@dataclass
class ConfigurationSpec:
    default_config: Optional[str] = "config/default.yaml"
    config_schema: Optional[str] = "config/schema.json"
    user_template: Optional[str] = "config/template.yaml"
    allow_user_override: bool = True
    merge_strategy: Literal["merge", "replace"] = "merge"


@dataclass
class ResourceMapping:
    templates: Dict[str, str] = field(default_factory=dict)
    data: Dict[str, str] = field(default_factory=dict)
    assets: Dict[str, str] = field(default_factory=dict)


@dataclass
class TaskConfiguration:
    task_paths: List[str] = field(default_factory=lambda: ["tasks"])

    def validate(self):
        if not self.task_paths:
            raise ValueError("task_paths cannot be empty")
        for path in self.task_paths:
            if ".." in path or path.startswith("/") or path.startswith("\\"):
                raise ValueError(f"Invalid task path: {path}")


@dataclass
class PluginManifest:
    package: PackageInfo
    requires: Dict[str, str] = field(default_factory=dict)
    dependencies: Dict[str, DependencySpec] = field(default_factory=dict)
    pypi_dependencies: Dict[str, str] = field(default_factory=dict)
    exports: Exports = field(default_factory=Exports)
    extends: List[Dict[str, Any]] = field(default_factory=list)
    overrides: List[Dict[str, str]] = field(default_factory=list)
    lifecycle: LifecycleHooks = field(default_factory=LifecycleHooks)
    configuration: ConfigurationSpec = field(default_factory=ConfigurationSpec)
    resources: ResourceMapping = field(default_factory=ResourceMapping)
    build: BuildConfig = field(default_factory=BuildConfig)
    trust: TrustInfo = field(default_factory=TrustInfo)
    task_config: TaskConfiguration = field(default_factory=TaskConfiguration)
    metadata: Dict[str, Any] = field(default_factory=dict)
    path: Optional[Path] = None

    def is_compatible_with_aura(self, aura_version: str) -> bool:
        constraint = self.requires.get("aura", "*")
        spec = SpecifierSet(constraint)
        return Version(aura_version) in spec

    def verify_signature(self) -> bool:
        return True

    def get_config_path(self, filename: str = None) -> Path:
        if filename is None:
            filename = self.configuration.default_config
        return self.path / filename

    def get_template_path(self, name_or_path: str) -> Path:
        if name_or_path in self.resources.templates:
            return self.path / self.resources.templates[name_or_path]
        return self.path / name_or_path

    def get_data_path(self, name_or_path: str) -> Path:
        if name_or_path in self.resources.data:
            return self.path / self.resources.data[name_or_path]
        return self.path / name_or_path

    def get_asset_path(self, name_or_path: str) -> Path:
        if name_or_path in self.resources.assets:
            return self.path / self.resources.assets[name_or_path]
        return self.path / name_or_path

    def read_config(self, filename: str = None) -> dict:
        import yaml

        config_path = self.get_config_path(filename)
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
        return {}

    def read_template(self, name_or_path: str) -> str:
        template_path = self.get_template_path(name_or_path)
        return template_path.read_text(encoding="utf-8")

    def read_data(self, name_or_path: str) -> Any:
        import json
        import yaml

        data_path = self.get_data_path(name_or_path)
        with open(data_path, "r", encoding="utf-8") as f:
            if data_path.suffix in [".json"]:
                return json.load(f)
            if data_path.suffix in [".yaml", ".yml"]:
                return yaml.safe_load(f)
            return f.read()
