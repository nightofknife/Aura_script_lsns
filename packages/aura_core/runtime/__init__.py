"""Runtime bootstrap and profile utilities for Aura."""

from importlib import import_module
from typing import Any

__all__ = [
    "RuntimeProfile",
    "resolve_runtime_profile",
    "AdminPrivilegeRequiredError",
    "ensure_admin_startup",
    "is_running_as_admin",
    "create_runtime",
    "get_runtime",
    "start_runtime",
    "stop_runtime",
]

_EXPORTS = {
    "RuntimeProfile": (".profiles", "RuntimeProfile"),
    "resolve_runtime_profile": (".profiles", "resolve_runtime_profile"),
    "AdminPrivilegeRequiredError": (".privilege", "AdminPrivilegeRequiredError"),
    "ensure_admin_startup": (".privilege", "ensure_admin_startup"),
    "is_running_as_admin": (".privilege", "is_running_as_admin"),
    "create_runtime": (".bootstrap", "create_runtime"),
    "get_runtime": (".bootstrap", "get_runtime"),
    "start_runtime": (".bootstrap", "start_runtime"),
    "stop_runtime": (".bootstrap", "stop_runtime"),
}


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = target
    value = getattr(import_module(module_name, __name__), attr_name)
    globals()[name] = value
    return value
