# -*- coding: utf-8 -*-
"""Lazy exports for packaging core components."""

from __future__ import annotations

from importlib import import_module

__all__ = [
    "PackageManager",
    "PlanManager",
    "PlanRegistry",
    "TaskLoader",
    "DependencyManager",
]

_EXPORTS = {
    "PackageManager": ".package_manager",
    "PlanManager": ".plan_manager",
    "PlanRegistry": ".plan_registry",
    "TaskLoader": ".task_loader",
    "DependencyManager": ".dependency_manager",
}


def __getattr__(name: str):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(module_name, __name__)
    return getattr(module, name)
