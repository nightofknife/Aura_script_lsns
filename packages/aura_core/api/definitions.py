# -*- coding: utf-8 -*-
"""Core data definitions for Aura APIs."""

from __future__ import annotations

import inspect
import textwrap
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, Literal, Optional

from packages.aura_core.observability.logging.core_logger import logger

if TYPE_CHECKING:
    from ..packaging.manifest.schema import PluginManifest


@dataclass
class ActionDefinition:
    func: Callable
    name: str
    read_only: bool
    public: bool
    service_deps: Dict[str, str]
    plugin: "PluginManifest"
    is_async: bool = False
    timeout: Optional[int] = None
    description: str = ""

    @property
    def signature(self) -> inspect.Signature:
        return inspect.signature(self.func)

    @property
    def docstring(self) -> str:
        doc = inspect.getdoc(self.func)
        return textwrap.dedent(doc).strip() if doc else "No docstring provided."

    @property
    def fqid(self) -> str:
        canonical_id = self.plugin.package.canonical_id.lstrip("@")
        parts = canonical_id.split("/")
        if len(parts) == 2:
            author, package_name = parts
            return f"{author}/{package_name}/{self.name}"
        logger.warning(
            "Package '%s' canonical_id is not in '@author/package' format; falling back to '%s/%s'.",
            canonical_id,
            canonical_id,
            self.name,
        )
        return f"{canonical_id}/{self.name}"


@dataclass
class ServiceDefinition:
    alias: str
    fqid: str
    service_class: type
    plugin: Optional["PluginManifest"]
    public: bool
    domain: Literal["core", "package"] = "package"
    replace: Optional[str] = None
    instance: Any = None
    status: str = "defined"
    replaced_target_fqid: Optional[str] = None
    singleton: bool = True
    service_deps: Dict[str, str] = field(default_factory=dict)
    description: str = ""


@dataclass
class HookResult:
    func: Callable
    ok: bool
    result: Any = None
    error: Optional[str] = None
