# -*- coding: utf-8 -*-
"""Aura packaging subsystem."""

from __future__ import annotations

__all__ = [
    "PackageManager",
    "PlanManager",
    "PlanRegistry",
    "TaskLoader",
    "DependencyManager",
    "PluginManifest",
    "PackageInfo",
    "ManifestParser",
    "ManifestGenerator",
]


def __getattr__(name: str):
    if name in {"PackageManager", "PlanManager", "PlanRegistry", "TaskLoader", "DependencyManager"}:
        from .core import DependencyManager, PackageManager, PlanManager, PlanRegistry, TaskLoader

        return {
            "PackageManager": PackageManager,
            "PlanManager": PlanManager,
            "PlanRegistry": PlanRegistry,
            "TaskLoader": TaskLoader,
            "DependencyManager": DependencyManager,
        }[name]
    if name in {"PluginManifest", "PackageInfo", "ManifestParser", "ManifestGenerator"}:
        from .manifest import ManifestGenerator, ManifestParser, PackageInfo, PluginManifest

        return {
            "PluginManifest": PluginManifest,
            "PackageInfo": PackageInfo,
            "ManifestParser": ManifestParser,
            "ManifestGenerator": ManifestGenerator,
        }[name]
    raise AttributeError(name)
