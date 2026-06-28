from __future__ import annotations

"""Package-level entrypoint for Aura base action modules.

This package intentionally exports action modules by dependency/function group
instead of flattening every action callable into the package namespace. The
module list below is the supported import surface for package consumers.
"""

from importlib import import_module

_MODULE_EXPORTS = [
    "_shared",
    "atomic_actions",
    "data_actions",
    "gamepad_actions",
    "input_actions",
    "input_mapping_actions",
    "interaction_actions",
    "ocr_actions",
    "process_actions",
    "system_actions",
    "task_actions",
    "template_actions",
    "vision_actions",
    "wait_actions",
    "windows_diagnostics_actions",
    "yolo_actions",
]

__all__ = list(_MODULE_EXPORTS)


def __getattr__(name: str):
    if name in _MODULE_EXPORTS:
        module = import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + _MODULE_EXPORTS)
