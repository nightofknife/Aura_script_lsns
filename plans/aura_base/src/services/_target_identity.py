"""Comparable target binding and geometry, excluding input-driven focus state."""
from collections.abc import Mapping
from typing import Any


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return tuple(sorted((str(key), _freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def freeze_target_identity(summary: Any) -> Any:
    # SendInput may focus the same window during move/click. Foreground state
    # therefore cannot identify a binding. Keep every other field, including
    # runtime-specific epochs and geometry used to interpret client positions.
    if isinstance(summary, Mapping):
        summary = {key: value for key, value in summary.items() if key != "foreground"}
    return _freeze(summary)


def read_target_identity(runtime: Any) -> Any:
    summary = getattr(runtime, "target_summary", None)
    return freeze_target_identity(summary()) if callable(summary) else None
