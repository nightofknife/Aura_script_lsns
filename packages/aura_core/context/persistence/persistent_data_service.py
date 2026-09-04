from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from packages.aura_core.api import service_info
from packages.aura_core.observability.logging.core_logger import logger

from .persistent_data_errors import (
    PersistentDataError,
    PersistentDataNotFoundError,
    PersistentDataReadError,
    PersistentDataValidationError,
    PersistentDataWriteError,
)


_MISSING = object()
_SUPPORTED_BATCH_OPERATIONS = frozenset({"set", "merge", "increment", "append", "delete"})


@dataclass(frozen=True)
class PersistentDataResult:
    file: str
    operation: str
    path: tuple[str, ...] = ()
    changed: bool = False
    old_value: Any = None
    new_value: Any = None
    operations: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "file": self.file,
            "operation": self.operation,
            "path": list(self.path),
            "changed": bool(self.changed),
            "old_value": copy.deepcopy(self.old_value),
            "new_value": copy.deepcopy(self.new_value),
        }
        if self.operations:
            payload["operations"] = copy.deepcopy(list(self.operations))
        return payload


@service_info(
    alias="persistent_data",
    public=True,
    description="Stateless, atomic JSON persistence under the Aura user-data directory.",
)
class PersistentDataService:
    """Perform one atomic JSON-file operation per explicit call.

    The service keeps no document cache or per-file session. Every call resolves
    its target below ``user-data`` and reads the latest file contents from disk.
    """

    def __init__(self, base_path: str | Path):
        self._root = (Path(base_path).resolve() / "user-data").resolve()

    @property
    def root(self) -> Path:
        return self._root

    def read(
        self,
        file: str,
        path: Sequence[str] | None = None,
        default: Any = _MISSING,
    ) -> Any:
        relative, target = self._resolve_file(file)
        normalized_path = self._normalize_path(path, file=relative)
        if not target.is_file():
            if default is not _MISSING:
                return copy.deepcopy(default)
            raise PersistentDataNotFoundError(
                "persistent_data_file_not_found",
                f"Persistent data file does not exist: {relative}",
                file=relative,
                path=normalized_path,
            )
        document = self._load_document(target, relative)
        value = self._read_path(document, normalized_path)
        if value is _MISSING:
            if default is not _MISSING:
                return copy.deepcopy(default)
            raise PersistentDataNotFoundError(
                "persistent_data_path_not_found",
                f"Persistent data path does not exist: {list(normalized_path)!r}",
                file=relative,
                path=normalized_path,
            )
        return copy.deepcopy(value)

    def exists(self, file: str, path: Sequence[str] | None = None) -> bool:
        relative, target = self._resolve_file(file)
        normalized_path = self._normalize_path(path, file=relative)
        if not target.is_file():
            return False
        document = self._load_document(target, relative)
        return self._read_path(document, normalized_path) is not _MISSING

    def set(self, file: str, path: Sequence[str] | None, value: Any) -> PersistentDataResult:
        return self._mutate(file, "set", path, value=value)

    def merge(self, file: str, path: Sequence[str] | None, patch: Mapping[str, Any]) -> PersistentDataResult:
        return self._mutate(file, "merge", path, value=patch)

    def increment(
        self,
        file: str,
        path: Sequence[str],
        delta: int | float = 1,
        minimum: int | float | None = None,
        maximum: int | float | None = None,
    ) -> PersistentDataResult:
        return self._mutate(
            file,
            "increment",
            path,
            delta=delta,
            minimum=minimum,
            maximum=maximum,
        )

    def append(self, file: str, path: Sequence[str], value: Any) -> PersistentDataResult:
        return self._mutate(file, "append", path, value=value)

    def delete(self, file: str, path: Sequence[str]) -> PersistentDataResult:
        return self._mutate(file, "delete", path)

    def batch(self, file: str, operations: Sequence[Mapping[str, Any]]) -> PersistentDataResult:
        relative, target = self._resolve_file(file)
        if not isinstance(operations, Sequence) or isinstance(operations, (str, bytes)):
            raise PersistentDataValidationError(
                "persistent_data_invalid_operations",
                "Batch operations must be a sequence of objects.",
                file=relative,
            )

        original = self._load_for_mutation(target, relative)
        working = copy.deepcopy(original)
        operation_results: list[dict[str, Any]] = []
        for index, raw_operation in enumerate(operations):
            if not isinstance(raw_operation, Mapping):
                raise PersistentDataValidationError(
                    "persistent_data_invalid_operation",
                    f"Batch operation at index {index} must be an object.",
                    file=relative,
                    details={"index": index},
                )
            operation = str(raw_operation.get("operation") or raw_operation.get("op") or "").strip().lower()
            if operation not in _SUPPORTED_BATCH_OPERATIONS:
                raise PersistentDataValidationError(
                    "persistent_data_unsupported_operation",
                    f"Unsupported batch operation at index {index}: {operation or '<empty>'}",
                    file=relative,
                    details={"index": index, "operation": operation},
                )
            normalized_path = self._normalize_path(raw_operation.get("path"), file=relative)
            result = self._apply_operation(
                working,
                relative,
                operation,
                normalized_path,
                value=raw_operation.get("value", raw_operation.get("patch", _MISSING)),
                delta=raw_operation.get("delta", 1),
                minimum=raw_operation.get("minimum"),
                maximum=raw_operation.get("maximum"),
            )
            operation_results.append(result.to_dict())

        changed = working != original
        if changed:
            self._write_document(target, relative, working)
        logger.info(
            "Persistent data batch file=%s operations=%d changed=%s",
            relative,
            len(operation_results),
            changed,
        )
        return PersistentDataResult(
            file=relative,
            operation="batch",
            changed=changed,
            old_value=original,
            new_value=working,
            operations=tuple(operation_results),
        )

    def update(
        self,
        file: str,
        updater: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> PersistentDataResult:
        if not callable(updater):
            raise PersistentDataValidationError(
                "persistent_data_invalid_updater",
                "Persistent data updater must be callable.",
                file=str(file),
            )
        relative, target = self._resolve_file(file)
        original = self._load_for_mutation(target, relative)
        updated = updater(copy.deepcopy(original))
        if not isinstance(updated, dict):
            raise PersistentDataValidationError(
                "persistent_data_invalid_root",
                "Persistent data updater must return a JSON object.",
                file=relative,
            )
        candidate = copy.deepcopy(updated)
        changed = candidate != original
        if changed:
            self._write_document(target, relative, candidate)
        logger.info("Persistent data update file=%s changed=%s", relative, changed)
        return PersistentDataResult(
            file=relative,
            operation="update",
            changed=changed,
            old_value=original,
            new_value=candidate,
        )

    def inspect(self, file: str) -> dict[str, Any]:
        relative, target = self._resolve_file(file)
        if not target.is_file():
            return {
                "file": relative,
                "path": str(target),
                "exists": False,
                "valid": None,
                "size": 0,
                "modified_at": None,
                "error": None,
            }
        stat = target.stat()
        error = None
        valid = True
        try:
            raw = target.read_bytes()
            payload = self._decode_json(raw)
            if not isinstance(payload, dict):
                raise ValueError("root must be a JSON object")
        except Exception as exc:
            valid = False
            error = str(exc)
        return {
            "file": relative,
            "path": str(target),
            "exists": True,
            "valid": valid,
            "size": int(stat.st_size),
            "modified_at": float(stat.st_mtime),
            "error": error,
        }

    async def read_async(self, *args, **kwargs) -> Any:
        return await asyncio.to_thread(self.read, *args, **kwargs)

    async def exists_async(self, *args, **kwargs) -> bool:
        return await asyncio.to_thread(self.exists, *args, **kwargs)

    async def set_async(self, *args, **kwargs) -> PersistentDataResult:
        return await asyncio.to_thread(self.set, *args, **kwargs)

    async def merge_async(self, *args, **kwargs) -> PersistentDataResult:
        return await asyncio.to_thread(self.merge, *args, **kwargs)

    async def increment_async(self, *args, **kwargs) -> PersistentDataResult:
        return await asyncio.to_thread(self.increment, *args, **kwargs)

    async def append_async(self, *args, **kwargs) -> PersistentDataResult:
        return await asyncio.to_thread(self.append, *args, **kwargs)

    async def delete_async(self, *args, **kwargs) -> PersistentDataResult:
        return await asyncio.to_thread(self.delete, *args, **kwargs)

    async def batch_async(self, *args, **kwargs) -> PersistentDataResult:
        return await asyncio.to_thread(self.batch, *args, **kwargs)

    async def update_async(self, *args, **kwargs) -> PersistentDataResult:
        return await asyncio.to_thread(self.update, *args, **kwargs)

    async def inspect_async(self, *args, **kwargs) -> dict[str, Any]:
        return await asyncio.to_thread(self.inspect, *args, **kwargs)

    def _mutate(
        self,
        file: str,
        operation: str,
        path: Sequence[str] | None,
        **kwargs,
    ) -> PersistentDataResult:
        relative, target = self._resolve_file(file)
        normalized_path = self._normalize_path(path, file=relative)
        document = self._load_for_mutation(target, relative)
        result = self._apply_operation(document, relative, operation, normalized_path, **kwargs)
        if result.changed:
            self._write_document(target, relative, document)
        logger.info(
            "Persistent data operation file=%s operation=%s path=%s changed=%s",
            relative,
            operation,
            list(normalized_path),
            result.changed,
        )
        return result

    def _resolve_file(self, file: str) -> tuple[str, Path]:
        if not isinstance(file, str) or not file.strip():
            raise PersistentDataValidationError(
                "persistent_data_invalid_file",
                "Persistent data file must be a non-empty relative JSON path.",
            )
        relative_path = Path(file.strip())
        if relative_path.is_absolute() or relative_path.drive or ".." in relative_path.parts:
            raise PersistentDataValidationError(
                "persistent_data_path_escape",
                f"Persistent data file must stay below user-data: {file}",
                file=file,
            )
        if relative_path.suffix.lower() != ".json":
            raise PersistentDataValidationError(
                "persistent_data_invalid_extension",
                f"Persistent data file must use the .json extension: {file}",
                file=file,
            )
        target = (self._root / relative_path).resolve(strict=False)
        try:
            target.relative_to(self._root)
        except ValueError as exc:
            raise PersistentDataValidationError(
                "persistent_data_path_escape",
                f"Persistent data file must stay below user-data: {file}",
                file=file,
            ) from exc
        relative = target.relative_to(self._root).as_posix()
        return relative, target

    @staticmethod
    def _normalize_path(path: Sequence[str] | None, *, file: str) -> tuple[str, ...]:
        if path is None:
            return ()
        if not isinstance(path, Sequence) or isinstance(path, (str, bytes)):
            raise PersistentDataValidationError(
                "persistent_data_invalid_path",
                "Persistent data path must be a list of non-empty strings.",
                file=file,
            )
        normalized = []
        for item in path:
            if not isinstance(item, str) or not item:
                raise PersistentDataValidationError(
                    "persistent_data_invalid_path",
                    "Persistent data path must be a list of non-empty strings.",
                    file=file,
                    path=normalized,
                )
            normalized.append(item)
        return tuple(normalized)

    def _load_for_mutation(self, target: Path, relative: str) -> dict[str, Any]:
        if not target.is_file():
            return {}
        return self._load_document(target, relative)

    def _load_document(self, target: Path, relative: str) -> dict[str, Any]:
        try:
            raw = target.read_bytes()
        except OSError as exc:
            raise PersistentDataReadError(
                "persistent_data_read_failed",
                f"Could not read persistent data file: {relative}",
                file=relative,
                details={"error": str(exc)},
            ) from exc
        try:
            payload = self._decode_json(raw)
            if not isinstance(payload, dict):
                raise ValueError("root must be a JSON object")
            return payload
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            backup = self._preserve_corrupt_file(target)
            raise PersistentDataValidationError(
                "persistent_data_invalid_json",
                f"Persistent data file is not a valid JSON object: {relative}",
                file=relative,
                details={"backup": str(backup) if backup else None, "error": str(exc)},
            ) from exc

    def _preserve_corrupt_file(self, target: Path) -> Path | None:
        try:
            raw = target.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()[:12]
            backup = target.with_name(f"{target.stem}.corrupt-{digest}{target.suffix}")
            if not backup.exists():
                shutil.copy2(target, backup)
                logger.warning("Preserved invalid persistent data file at %s", backup)
            return backup
        except OSError as exc:
            logger.error("Could not preserve invalid persistent data file %s: %s", target, exc)
            return None

    @staticmethod
    def _read_path(document: dict[str, Any], path: tuple[str, ...]) -> Any:
        current: Any = document
        for segment in path:
            if not isinstance(current, dict) or segment not in current:
                return _MISSING
            current = current[segment]
        return current

    @staticmethod
    def _decode_json(raw: bytes) -> Any:
        def reject_constant(value: str) -> None:
            raise ValueError(f"invalid JSON numeric constant: {value}")

        return json.loads(raw.decode("utf-8"), parse_constant=reject_constant)

    def _apply_operation(
        self,
        document: dict[str, Any],
        relative: str,
        operation: str,
        path: tuple[str, ...],
        *,
        value: Any = _MISSING,
        delta: Any = 1,
        minimum: Any = None,
        maximum: Any = None,
    ) -> PersistentDataResult:
        if operation == "set" and value is _MISSING:
            self._raise_missing_value(relative, path, operation)
        if operation == "merge" and (value is _MISSING or not isinstance(value, Mapping)):
            raise PersistentDataValidationError(
                "persistent_data_invalid_merge",
                "Merge requires an object value.",
                file=relative,
                path=path,
            )
        if operation == "delete" and not path:
            raise PersistentDataValidationError(
                "persistent_data_root_delete_forbidden",
                "Deleting the persistent data document root is forbidden.",
                file=relative,
            )
        if operation in {"increment", "append"} and not path:
            raise PersistentDataValidationError(
                "persistent_data_empty_path",
                f"Persistent data operation '{operation}' requires a non-empty path.",
                file=relative,
            )

        old_value = self._read_path(document, path)
        old_for_result = None if old_value is _MISSING else copy.deepcopy(old_value)

        if operation == "set":
            replacement = copy.deepcopy(value)
            if not path:
                if not isinstance(replacement, dict):
                    raise PersistentDataValidationError(
                        "persistent_data_invalid_root",
                        "Persistent data document root must be a JSON object.",
                        file=relative,
                    )
                document.clear()
                document.update(replacement)
                new_value = document
            else:
                parent = self._ensure_parent(document, path, relative)
                parent[path[-1]] = replacement
                new_value = replacement
        elif operation == "merge":
            current = {} if old_value is _MISSING else old_value
            if not isinstance(current, dict):
                raise PersistentDataValidationError(
                    "persistent_data_merge_target_not_object",
                    "Merge target must be a JSON object.",
                    file=relative,
                    path=path,
                )
            merged = copy.deepcopy(current)
            merged.update(copy.deepcopy(dict(value)))
            if not path:
                document.clear()
                document.update(merged)
                new_value = document
            else:
                parent = self._ensure_parent(document, path, relative)
                parent[path[-1]] = merged
                new_value = merged
        elif operation == "increment":
            self._validate_number(delta, relative, path, "delta")
            if minimum is not None:
                self._validate_number(minimum, relative, path, "minimum")
            if maximum is not None:
                self._validate_number(maximum, relative, path, "maximum")
            if minimum is not None and maximum is not None and minimum > maximum:
                raise PersistentDataValidationError(
                    "persistent_data_invalid_bounds",
                    "Increment minimum cannot be greater than maximum.",
                    file=relative,
                    path=path,
                )
            current = 0 if old_value is _MISSING else old_value
            self._validate_number(current, relative, path, "current value")
            incremented = current + delta
            if minimum is not None:
                incremented = max(incremented, minimum)
            if maximum is not None:
                incremented = min(incremented, maximum)
            parent = self._ensure_parent(document, path, relative, require_nonempty=True)
            parent[path[-1]] = incremented
            new_value = incremented
        elif operation == "append":
            if value is _MISSING:
                self._raise_missing_value(relative, path, operation)
            current = [] if old_value is _MISSING else old_value
            if not isinstance(current, list):
                raise PersistentDataValidationError(
                    "persistent_data_append_target_not_array",
                    "Append target must be a JSON array.",
                    file=relative,
                    path=path,
                )
            appended = copy.deepcopy(current)
            appended.append(copy.deepcopy(value))
            parent = self._ensure_parent(document, path, relative, require_nonempty=True)
            parent[path[-1]] = appended
            new_value = appended
        elif operation == "delete":
            parent = self._find_parent(document, path, relative)
            if parent is not None and path[-1] in parent:
                del parent[path[-1]]
            new_value = None
        else:
            raise PersistentDataValidationError(
                "persistent_data_unsupported_operation",
                f"Unsupported persistent data operation: {operation}",
                file=relative,
                path=path,
            )

        new_for_result = copy.deepcopy(new_value)
        changed = old_value is _MISSING or old_for_result != new_for_result
        if operation == "delete":
            changed = old_value is not _MISSING
        return PersistentDataResult(
            file=relative,
            operation=operation,
            path=path,
            changed=changed,
            old_value=old_for_result,
            new_value=new_for_result,
        )

    @staticmethod
    def _ensure_parent(
        document: dict[str, Any],
        path: tuple[str, ...],
        relative: str,
        *,
        require_nonempty: bool = False,
    ) -> dict[str, Any]:
        if not path:
            if require_nonempty:
                raise PersistentDataValidationError(
                    "persistent_data_empty_path",
                    "This persistent data operation requires a non-empty path.",
                    file=relative,
                )
            return document
        current = document
        for index, segment in enumerate(path[:-1]):
            child = current.get(segment, _MISSING)
            if child is _MISSING:
                child = {}
                current[segment] = child
            if not isinstance(child, dict):
                raise PersistentDataValidationError(
                    "persistent_data_path_not_object",
                    f"Persistent data path segment is not an object: {segment}",
                    file=relative,
                    path=path[: index + 1],
                )
            current = child
        return current

    @staticmethod
    def _find_parent(
        document: dict[str, Any],
        path: tuple[str, ...],
        relative: str,
    ) -> dict[str, Any] | None:
        current: Any = document
        for index, segment in enumerate(path[:-1]):
            if not isinstance(current, dict):
                raise PersistentDataValidationError(
                    "persistent_data_path_not_object",
                    f"Persistent data path segment is not an object: {path[index - 1]}",
                    file=relative,
                    path=path[:index],
                )
            if segment not in current:
                return None
            current = current[segment]
        if not isinstance(current, dict):
            raise PersistentDataValidationError(
                "persistent_data_path_not_object",
                f"Persistent data path segment is not an object: {path[-2]}",
                file=relative,
                path=path[:-1],
            )
        return current

    @staticmethod
    def _validate_number(value: Any, relative: str, path: tuple[str, ...], label: str) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PersistentDataValidationError(
                "persistent_data_invalid_number",
                f"Increment {label} must be a number and cannot be boolean.",
                file=relative,
                path=path,
            )

    @staticmethod
    def _raise_missing_value(relative: str, path: tuple[str, ...], operation: str) -> None:
        raise PersistentDataValidationError(
            "persistent_data_missing_value",
            f"Persistent data operation '{operation}' requires a value.",
            file=relative,
            path=path,
        )

    def _write_document(self, target: Path, relative: str, document: dict[str, Any]) -> None:
        try:
            serialized = json.dumps(
                document,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            ) + "\n"
        except (TypeError, ValueError) as exc:
            raise PersistentDataValidationError(
                "persistent_data_not_serializable",
                f"Persistent data document is not JSON serializable: {relative}",
                file=relative,
                details={"error": str(exc)},
            ) from exc

        temp_path = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with temp_path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, target)
        except Exception as exc:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            if isinstance(exc, PersistentDataError):
                raise
            raise PersistentDataWriteError(
                "persistent_data_write_failed",
                f"Could not atomically write persistent data file: {relative}",
                file=relative,
                details={"error": str(exc)},
            ) from exc


__all__ = ["PersistentDataResult", "PersistentDataService"]
