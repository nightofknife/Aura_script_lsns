# -*- coding: utf-8 -*-
"""Canonical task reference model.

Supported syntax (only):
- tasks:<dir>:<file>.yaml
- tasks:<dir>:<file>.yaml:<task_key>
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Optional, Tuple
import re


@dataclass(frozen=True)
class TaskReference:
    """Immutable canonical task reference."""

    package: str
    task_path: str
    author: Optional[str] = None
    task_key: Optional[str] = None

    _VALID_NAME_PATTERN: ClassVar[re.Pattern] = re.compile(r"^[A-Za-z0-9_-]+$")
    _VALID_PATH_PATTERN: ClassVar[re.Pattern] = re.compile(r"^tasks:[A-Za-z0-9_:-]+\.yaml$")

    def __post_init__(self):
        self.validate()

    @classmethod
    def from_string(
        cls,
        ref: str,
        default_package: Optional[str] = None,
        default_author: Optional[str] = None,
    ) -> "TaskReference":
        if not ref or not ref.strip():
            raise ValueError("Task reference cannot be empty")
        if not default_package:
            raise ValueError("Canonical task reference requires default_package")

        raw = ref.strip()
        if "/" in raw or "\\" in raw:
            raise ValueError(
                f"Invalid task reference '{raw}': slash format is not supported. "
                "Use canonical form 'tasks:<path>.yaml[:task_key]'."
            )
        if not raw.startswith("tasks:"):
            raise ValueError(
                f"Invalid task reference '{raw}': must start with 'tasks:' "
                "and end with '.yaml'."
            )

        task_key = None
        task_path = raw
        if ".yaml:" in raw:
            base, key = raw.rsplit(".yaml:", 1)
            task_path = f"{base}.yaml"
            task_key = key or None

        return cls(
            package=default_package,
            task_path=task_path,
            task_key=task_key,
            author=default_author,
        )

    def infer_task_key(self) -> str:
        if self.task_key:
            return self.task_key
        filename = self.task_path[:-5].split(":")[-1]
        return filename

    def as_file_path(self, include_extension: bool = True) -> str:
        path = self.task_path
        if not include_extension and path.endswith(".yaml"):
            path = path[:-5]
        return path.replace(":", "/")

    def as_fqid(self, include_author: bool = True) -> str:
        if include_author and self.author:
            return f"{self.author}/{self.package}/{self.task_path}"
        return f"{self.package}/{self.task_path}"

    def as_id(self) -> str:
        return f"{self.package}/{self.task_path}"

    def as_relative(self) -> str:
        return self.task_path

    def as_loader_path(self) -> str:
        path = self.task_path
        if path.startswith("tasks:"):
            path = path[6:]
        if path.endswith(".yaml"):
            path = path[:-5]
        normalized = path.replace(":", "/")
        if self.task_key:
            return f"{normalized}/{self.task_key}"
        return normalized

    def as_dict_key(self) -> str:
        path = self.task_path
        if path.startswith("tasks:"):
            path = path[6:]
        if path.endswith(".yaml"):
            path = path[:-5]
        normalized = path.replace(":", "/")
        return f"{normalized}/{self.infer_task_key()}"

    @property
    def path_parts(self) -> Tuple[str, ...]:
        path = self.task_path[:-5] if self.task_path.endswith(".yaml") else self.task_path
        return tuple(path.split(":"))

    @property
    def file_name(self) -> str:
        return self.path_parts[-1] + ".yaml"

    @property
    def directory(self) -> str:
        return "/".join(self.path_parts[:-1])

    @property
    def task_name(self) -> str:
        return self.path_parts[-1]

    def with_package(self, package: str) -> "TaskReference":
        return TaskReference(author=self.author, package=package, task_path=self.task_path, task_key=self.task_key)

    def with_author(self, author: Optional[str]) -> "TaskReference":
        return TaskReference(author=author, package=self.package, task_path=self.task_path, task_key=self.task_key)

    def with_task_key(self, task_key: Optional[str]) -> "TaskReference":
        return TaskReference(author=self.author, package=self.package, task_path=self.task_path, task_key=task_key)

    def validate(self) -> None:
        if not self.package:
            raise ValueError("Package name cannot be empty")
        if not self._VALID_NAME_PATTERN.match(self.package):
            raise ValueError(f"Invalid package name '{self.package}'")
        if self.author and not self._VALID_NAME_PATTERN.match(self.author):
            raise ValueError(f"Invalid author name '{self.author}'")
        if not self.task_path:
            raise ValueError("Task path cannot be empty")
        if ".." in self.task_path:
            raise ValueError(f"Security: task path contains path traversal sequence '..': {self.task_path}")
        if self.task_path.startswith("/") or self.task_path.startswith("\\"):
            raise ValueError(f"Security: task path cannot be an absolute path: {self.task_path}")
        if not self._VALID_PATH_PATTERN.match(self.task_path):
            raise ValueError(
                f"Invalid task path '{self.task_path}': must match 'tasks:<path>.yaml' canonical format"
            )
        if self.task_key and not self._VALID_NAME_PATTERN.match(self.task_key):
            raise ValueError(f"Invalid task key '{self.task_key}'")


def parse_task_reference(
    ref: str,
    default_package: Optional[str] = None,
    default_author: Optional[str] = None,
) -> TaskReference:
    return TaskReference.from_string(ref, default_package, default_author)
