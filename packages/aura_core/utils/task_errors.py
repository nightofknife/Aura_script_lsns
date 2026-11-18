"""Structured context for failures that reach the end of a task."""

from collections.abc import Mapping
from typing import Any


class SubtaskFailed(RuntimeError):
    def __init__(self, task_ref: str, error: Any):
        message = f"Sub-task '{task_ref}' failed."
        if isinstance(error, Mapping) and isinstance(error.get("message"), str):
            message = f"{message} {error['message']}"
        super().__init__(message)
        self.task_ref = task_ref
        self.task_error = error


def recognition_failure(
    step: str, target: str, kind: str, outcome: str, **details: Any,
) -> dict[str, Any]:
    return {"recognition": {
        "step": step, "target": target, "kind": kind, "outcome": outcome, **details,
    }}


def task_error_details(
    error: Exception, *, task: str | None = None, node: str | None = None,
    node_name: str | None = None, _depth: int = 0,
) -> dict[str, Any]:
    result = {"type": type(error).__name__, "message": str(error)}
    for key, value in (
        ("code", getattr(error, "code", None)),
        ("task", task), ("node_id", node), ("node_name", node_name),
    ):
        if isinstance(value, str) and value:
            result[key] = value

    for detail in (getattr(error, "detail", None), getattr(error, "details", None)):
        if isinstance(detail, Mapping):
            if isinstance(detail.get("code"), str):
                result.setdefault("code", detail["code"])
            if isinstance(detail.get("step"), str):
                result["step"] = detail["step"]
            if isinstance(detail.get("recognition"), Mapping):
                result["recognition"] = dict(detail["recognition"])
                break
    if isinstance(error, SubtaskFailed):
        result["cause"] = {"task": error.task_ref}
        if isinstance(error.task_error, Mapping):
            result["cause"].update(error.task_error)
    elif _depth < 8 and isinstance(error.__cause__, Exception):
        cause = task_error_details(error.__cause__, task=task, node=node, node_name=node_name, _depth=_depth + 1)
        if {"code", "recognition", "cause"} & cause.keys():
            result["cause"] = cause
    return result
