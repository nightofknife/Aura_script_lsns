from __future__ import annotations

from copy import deepcopy
from typing import Any, Sequence


class PersistentDataError(RuntimeError):
    """Base error raised by the persistent data service."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        file: str | None = None,
        path: Sequence[str] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.file = file
        self.path = list(path or [])
        self.details = deepcopy(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "file": self.file,
            "path": list(self.path),
            "details": deepcopy(self.details),
        }


class PersistentDataNotFoundError(PersistentDataError):
    """Raised when a required persistent data file or path is missing."""


class PersistentDataValidationError(PersistentDataError):
    """Raised when a file, path, operation, or value is invalid."""


class PersistentDataReadError(PersistentDataError):
    """Raised when a persistent data file cannot be read."""


class PersistentDataWriteError(PersistentDataError):
    """Raised when an atomic write cannot be committed."""


__all__ = [
    "PersistentDataError",
    "PersistentDataNotFoundError",
    "PersistentDataReadError",
    "PersistentDataValidationError",
    "PersistentDataWriteError",
]
