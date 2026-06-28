# -*- coding: utf-8 -*-
"""Centralized canonical task reference resolver."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from .task_reference import TaskReference


@dataclass(frozen=True)
class ResolvedTaskReference:
    reference: TaskReference
    task_ref: str
    task_file_path: str
    task_key: Optional[str]
    loader_path: str
    canonical_task_id: str


class TaskRefResolver:
    """Single entrypoint for parsing canonical task references."""

    @staticmethod
    def resolve(
        task_ref: str,
        *,
        default_package: str,
        default_author: Optional[str] = None,
        enforce_package: Optional[str] = None,
        allow_cross_package: bool = False,
        allowlist: Optional[Iterable[str]] = None,
    ) -> ResolvedTaskReference:
        if allow_cross_package:
            raise ValueError("Cross-package task reference is not supported in canonical task_ref mode.")

        ref = TaskReference.from_string(
            task_ref,
            default_package=default_package,
            default_author=default_author,
        )

        if enforce_package and ref.package != enforce_package:
            raise ValueError(
                f"Task reference package mismatch: expected '{enforce_package}', got '{ref.package}'."
            )

        task_file_path = ref.task_path.replace(":", "/")
        normalized_ref = ref.task_path if not ref.task_key else f"{ref.task_path}:{ref.task_key}"
        canonical_task_id = f"{ref.package}/{ref.as_loader_path()}"

        return ResolvedTaskReference(
            reference=ref,
            task_ref=normalized_ref,
            task_file_path=task_file_path,
            task_key=ref.task_key,
            loader_path=ref.as_loader_path(),
            canonical_task_id=canonical_task_id,
        )
