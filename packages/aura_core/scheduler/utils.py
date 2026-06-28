# -*- coding: utf-8 -*-
"""Scheduler 工具函数模块

包含与调度器相关的辅助工具函数，这些函数相对独立，不依赖于Scheduler类的状态。
"""

import os
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional, Set
from packages.aura_core.packaging.core.dependency_manager import DependencyManager
from packages.aura_core.types import TaskRefResolver


def resolve_base_path() -> Path:
    """解析 Aura 框架的基础路径。

    优先级：
    1. 环境变量 AURA_BASE_PATH
    2. 如果是打包的可执行文件，使用可执行文件所在目录
    3. 否则使用当前文件的两级父目录

    Returns:
        Path: Aura 框架的基础路径
    """
    env_base = os.getenv("AURA_BASE_PATH")
    if env_base:
        return Path(env_base).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3]  # 从 scheduler/utils.py 向上3级


def base36_encode(num: int) -> str:
    """将数字编码为 Base36 字符串。

    Args:
        num: 要编码的整数

    Returns:
        str: Base36 编码的字符串
    """
    if num == 0:
        return "0"
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    out = []
    n = num
    while n > 0:
        n, r = divmod(n, 36)
        out.append(chars[r])
    return "".join(reversed(out))


def short_cid_suffix(cid: Optional[str]) -> str:
    """从完整的 CID 提取短后缀（4个字符）。

    Args:
        cid: 完整的 CID 字符串

    Returns:
        str: 4个字符的后缀
    """
    if not cid:
        return "0000"
    try:
        return base36_encode(int(cid))[-4:].rjust(4, "0")
    except Exception:
        return (cid[-4:] if len(cid) >= 4 else cid.rjust(4, "0"))


def make_trace_id(plan_name: str, task_name: str, cid: str,
                  when: Optional[datetime] = None) -> str:
    """生成任务追踪ID。

    格式: {plan_name}/{task_name}@{timestamp}-{cid_suffix}
    例如: my_plan/my_task@260113-152030-A3F2

    Args:
        plan_name: Plan 名称
        task_name: 任务名称
        cid: 完整的执行 ID
        when: 时间戳（可选，默认当前时间）

    Returns:
        str: 追踪 ID
    """
    ts = when or datetime.now()
    time_part = ts.strftime("%y%m%d-%H%M%S")
    suffix = short_cid_suffix(cid)
    return f"{plan_name}/{task_name}@{time_part}-{suffix}"


def make_trace_label(plan_name: Optional[str], task_name: Optional[str],
                     all_tasks_definitions: dict) -> str:
    """生成任务追踪标签（带标题）。

    Args:
        plan_name: Plan 名称
        task_name: 任务名称
        all_tasks_definitions: 所有任务定义字典

    Returns:
        str: 任务标签（优先使用 meta.title，fallback 到 plan/task）
    """
    full_task_id = f"{plan_name}/{task_name}" if plan_name and task_name else (plan_name or task_name or "")
    task_def = all_tasks_definitions.get(full_task_id, {}) if full_task_id else {}
    title = task_def.get("meta", {}).get("title") if isinstance(task_def, dict) else None
    return title or full_task_id


def collect_requirement_names(req_file: Path, dep_mgr: DependencyManager) -> Set[str]:
    """读取 requirements 文件中的包名集合（小写），忽略无效行。

    Args:
        req_file: requirements 文件路径
        dep_mgr: 依赖管理器实例

    Returns:
        Set[str]: 包名集合（小写）
    """
    if not req_file.is_file():
        return set()
    try:
        requirements = dep_mgr._read_requirements(req_file)
    except Exception:
        return set()
    names: Set[str] = set()
    for req in requirements:
        name = getattr(req, "name", None)
        if name:
            names.add(name.lower())
    return names


def convert_task_reference_to_id(plan_name: str, task_ref: str) -> str:
    """Convert a strict task reference into canonical runtime task id."""
    resolved = TaskRefResolver.resolve(
        task_ref,
        default_package=plan_name,
        enforce_package=plan_name,
    )
    return resolved.canonical_task_id
