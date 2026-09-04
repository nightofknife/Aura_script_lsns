from __future__ import annotations

from typing import Any

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.context.persistence.persistent_data_service import PersistentDataService


@action_info(
    name="persistent_data_read",
    read_only=True,
    public=True,
    description="Read a value from an explicit persistent JSON file and structured path.",
)
@requires_services(persistent_data="core/persistent_data")
def persistent_data_read(
    file: str,
    path: list[str] | None = None,
    required: bool = True,
    default: Any = None,
    persistent_data: PersistentDataService | None = None,
) -> Any:
    if persistent_data is None:
        raise RuntimeError("persistent_data service is not available")
    if required:
        return persistent_data.read(file=file, path=path)
    return persistent_data.read(file=file, path=path, default=default)


@action_info(
    name="persistent_data_exists",
    read_only=True,
    public=True,
    description="Check whether an explicit persistent JSON file path exists.",
)
@requires_services(persistent_data="core/persistent_data")
def persistent_data_exists(
    file: str,
    path: list[str] | None = None,
    persistent_data: PersistentDataService | None = None,
) -> bool:
    if persistent_data is None:
        raise RuntimeError("persistent_data service is not available")
    return persistent_data.exists(file=file, path=path)


@action_info(
    name="persistent_data_set",
    public=True,
    description="Atomically replace a value in an explicit persistent JSON file.",
)
@requires_services(persistent_data="core/persistent_data")
def persistent_data_set(
    file: str,
    value: Any,
    path: list[str] | None = None,
    persistent_data: PersistentDataService | None = None,
) -> dict[str, Any]:
    if persistent_data is None:
        raise RuntimeError("persistent_data service is not available")
    return persistent_data.set(file=file, path=path, value=value).to_dict()


@action_info(
    name="persistent_data_merge",
    public=True,
    description="Atomically shallow-merge an object in an explicit persistent JSON file.",
)
@requires_services(persistent_data="core/persistent_data")
def persistent_data_merge(
    file: str,
    patch: dict[str, Any],
    path: list[str] | None = None,
    persistent_data: PersistentDataService | None = None,
) -> dict[str, Any]:
    if persistent_data is None:
        raise RuntimeError("persistent_data service is not available")
    return persistent_data.merge(file=file, path=path, patch=patch).to_dict()


@action_info(
    name="persistent_data_increment",
    public=True,
    description="Atomically increment a bounded number in an explicit persistent JSON file.",
)
@requires_services(persistent_data="core/persistent_data")
def persistent_data_increment(
    file: str,
    path: list[str],
    delta: int | float = 1,
    minimum: int | float | None = None,
    maximum: int | float | None = None,
    persistent_data: PersistentDataService | None = None,
) -> dict[str, Any]:
    if persistent_data is None:
        raise RuntimeError("persistent_data service is not available")
    return persistent_data.increment(
        file=file,
        path=path,
        delta=delta,
        minimum=minimum,
        maximum=maximum,
    ).to_dict()


@action_info(
    name="persistent_data_append",
    public=True,
    description="Atomically append one value to an array in an explicit persistent JSON file.",
)
@requires_services(persistent_data="core/persistent_data")
def persistent_data_append(
    file: str,
    path: list[str],
    value: Any,
    persistent_data: PersistentDataService | None = None,
) -> dict[str, Any]:
    if persistent_data is None:
        raise RuntimeError("persistent_data service is not available")
    return persistent_data.append(file=file, path=path, value=value).to_dict()


@action_info(
    name="persistent_data_delete",
    public=True,
    description="Atomically delete a path from an explicit persistent JSON file.",
)
@requires_services(persistent_data="core/persistent_data")
def persistent_data_delete(
    file: str,
    path: list[str],
    persistent_data: PersistentDataService | None = None,
) -> dict[str, Any]:
    if persistent_data is None:
        raise RuntimeError("persistent_data service is not available")
    return persistent_data.delete(file=file, path=path).to_dict()


@action_info(
    name="persistent_data_batch",
    public=True,
    description="Atomically apply an ordered batch of mutations to one persistent JSON file.",
)
@requires_services(persistent_data="core/persistent_data")
def persistent_data_batch(
    file: str,
    operations: list[dict[str, Any]],
    persistent_data: PersistentDataService | None = None,
) -> dict[str, Any]:
    if persistent_data is None:
        raise RuntimeError("persistent_data service is not available")
    return persistent_data.batch(file=file, operations=operations).to_dict()


__all__ = [
    "persistent_data_append",
    "persistent_data_batch",
    "persistent_data_delete",
    "persistent_data_exists",
    "persistent_data_increment",
    "persistent_data_merge",
    "persistent_data_read",
    "persistent_data_set",
]
