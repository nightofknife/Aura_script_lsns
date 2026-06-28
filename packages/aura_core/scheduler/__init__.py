# -*- coding: utf-8 -*-
"""Scheduler package public surface."""

from importlib import import_module
from typing import Any

__all__ = ["Scheduler"]

_EXPORTS = {
    "Scheduler": (".core", "Scheduler"),
}


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = target
    value = getattr(import_module(module_name, __name__), attr_name)
    globals()[name] = value
    return value
