"""Shared task placement rules for discovery and manual entry surfaces.

Task YAML metadata owns placement. Internal tasks stay resolvable by the runtime
but are excluded from ordinary user entry lists.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

TASK_ROLES = ("lifecycle", "orchestration", "user_task", "user_tool", "internal", "developer")
PUBLIC_TASK_ROLES = ("lifecycle", "orchestration", "user_task", "user_tool")
ROLE_LABELS = {
    "lifecycle": "生命周期", "orchestration": "流程编排", "user_task": "用户任务",
    "user_tool": "用户工具", "internal": "任务内部", "developer": "开发工具",
}


def task_role(meta: Mapping[str, Any] | None) -> str:
    meta = meta or {}
    role = meta.get("role")
    if role not in TASK_ROLES:
        raise ValueError(f"Task metadata requires an explicit role from {TASK_ROLES}, got {role!r}")
    return str(role)


def is_user_entry(meta: Mapping[str, Any] | None) -> bool:
    return task_role(meta) in PUBLIC_TASK_ROLES


def catalog_group(meta: Mapping[str, Any] | None) -> str:
    meta = meta or {}
    role = task_role(meta)
    domain = str(meta.get("domain") or "").strip()
    return f"{ROLE_LABELS[role]} / {domain}" if domain else ROLE_LABELS[role]


def select_tasks(tasks, *, roles=PUBLIC_TASK_ROLES):
    """Select catalog roles, combining duplicate records from discovery sources."""
    selected = {}
    for task in tasks:
        meta = task.get("meta") or {}
        key = (task.get("plan_name"), task.get("task_ref"))
        if key[1] and task_role(meta) in roles:
            selected.setdefault(key, task)
    return sorted(selected.values(), key=lambda task: (
        TASK_ROLES.index(task_role(task.get("meta"))),
        str((task.get("meta") or {}).get("domain") or ""),
        str(task.get("full_task_id") or task.get("task_ref") or ""),
    ))
