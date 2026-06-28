# -*- coding: utf-8 -*-
"""Aura config package public surface."""

from importlib import import_module
from typing import Any

__all__ = [
    "get_config_value",
    "ConfigManager",
    "ConfigService",
    "validate_task_definition",
    "TemplateRenderer",
]

_EXPORTS = {
    "get_config_value": (".loader", "get_config_value"),
    "ConfigManager": (".manager", "ConfigManager"),
    "ConfigService": (".service", "ConfigService"),
    "validate_task_definition": (".validator", "validate_task_definition"),
    "TemplateRenderer": (".template", "TemplateRenderer"),
}


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = target
    value = getattr(import_module(module_name, __name__), attr_name)
    globals()[name] = value
    return value
