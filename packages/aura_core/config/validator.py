# -*- coding: utf-8 -*-
"""JSON Schema 验证模块

使用 fastjsonschema 进行高性能的任务定义验证。
"""
import json
from pathlib import Path
from typing import Dict, Any, Tuple, Optional, TYPE_CHECKING

try:
    import fastjsonschema
    FASTJSONSCHEMA_AVAILABLE = True
except ImportError:
    FASTJSONSCHEMA_AVAILABLE = False
    fastjsonschema = None  # type: ignore

if TYPE_CHECKING:
    from typing import Callable

from packages.aura_core.observability.logging.core_logger import logger

# 缓存编译后的验证函数
_task_schema_validator = None


def get_task_schema_validator() -> Optional['Callable']:
    """获取任务 Schema 验证器（编译并缓存）

    Returns:
        编译后的 Schema 验证函数，如果加载失败则返回 None
    """
    global _task_schema_validator

    if _task_schema_validator is None:
        if not FASTJSONSCHEMA_AVAILABLE:
            logger.warning("fastjsonschema is not installed; task schema validation is disabled.")
            return None

        # validator.py -> config -> aura_core -> packages -> <repo_root>
        schema_path = Path(__file__).resolve().parents[3] / 'docs' / 'schemas' / 'task-schema.json'

        if not schema_path.exists():
            logger.warning(f"任务 Schema 文件不存在: {schema_path}")
            return None

        try:
            with open(schema_path, 'r', encoding='utf-8') as f:
                schema = json.load(f)

            # 使用 fastjsonschema 编译（比 jsonschema 快 5-10 倍）
            _task_schema_validator = fastjsonschema.compile(schema)
            logger.info("任务 Schema 验证器加载成功")
        except Exception as e:
            logger.error(f"加载任务 Schema 失败: {e}")
            return None

    return _task_schema_validator


def validate_task_definition(task_data: Dict[str, Any]) -> Tuple[bool, str]:
    """验证任务定义是否符合 Schema

    Args:
        task_data: 任务定义字典（YAML 解析后的数据）

    Returns:
        (is_valid, error_message) - 如果验证通过，error_message 为空字符串
    """
    validator = get_task_schema_validator()

    if validator is None:
        # Schema 未加载，跳过验证（向后兼容）
        logger.debug("Schema 验证器未加载，跳过验证")
        return True, ""

    try:
        schema_payload = _normalize_task_data_for_schema(task_data)
        validator(schema_payload)
        logger.debug("任务定义 Schema 验证通过")
        return True, ""
    except fastjsonschema.JsonSchemaException as e:
        # 构建友好的错误消息
        path = '.'.join(map(str, e.path)) if e.path else 'root'
        error_msg = f"任务定义不符合 Schema: {e.message} (路径: {path})"
        logger.warning(error_msg)
        return False, error_msg
    except Exception as e:
        # 捕获其他可能的异常
        error_msg = f"Schema 验证过程中发生错误: {str(e)}"
        logger.error(error_msg)
        return False, error_msg


def _normalize_task_data_for_schema(task_data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize runtime-supported task file shapes before schema validation."""
    if not isinstance(task_data, dict):
        return task_data

    # Runtime still accepts root-level single task shape:
    # {meta: {...}, steps: {...}, ...}
    # The canonical schema uses task-map shape:
    # {task_name: {meta: {...}, steps: {...}}, ...}
    if isinstance(task_data.get("meta"), dict) and isinstance(task_data.get("steps"), dict):
        return {"__root__": task_data}
    return task_data


def validate_concurrency_config(concurrency: Any) -> Tuple[bool, str]:
    """验证并发配置是否有效

    这是一个辅助函数，用于单独验证并发配置的有效性。

    Args:
        concurrency: 并发配置（可以是 None, str, 或 dict）

    Returns:
        (is_valid, error_message)
    """
    # None 是有效的（表示使用默认值）
    if concurrency is None:
        return True, ""

    # 字符串简写形式
    if isinstance(concurrency, str):
        valid_modes = ['exclusive', 'concurrent', 'shared']
        if concurrency not in valid_modes:
            return False, f"并发模式必须是 {valid_modes} 之一，当前值: '{concurrency}'"
        return True, ""

    # 完整配置对象
    if isinstance(concurrency, dict):
        mode = concurrency.get('mode')
        if mode is not None:
            valid_modes = ['exclusive', 'concurrent', 'shared']
            if mode not in valid_modes:
                return False, f"并发模式必须是 {valid_modes} 之一，当前值: '{mode}'"

        # 验证 resources 字段
        resources = concurrency.get('resources')
        if resources is not None:
            if not isinstance(resources, list):
                return False, "resources 字段必须是数组"
            for res in resources:
                if not isinstance(res, str):
                    return False, f"资源标签必须是字符串，当前值: {res}"

        # 验证 max_instances 字段
        max_instances = concurrency.get('max_instances')
        if max_instances is not None:
            if not isinstance(max_instances, int) or max_instances < 1:
                return False, f"max_instances 必须是大于 0 的整数，当前值: {max_instances}"

        # 验证 mutex_group 字段
        mutex_group = concurrency.get('mutex_group')
        if mutex_group is not None and not isinstance(mutex_group, str):
            return False, f"mutex_group 必须是字符串，当前值: {mutex_group}"

        return True, ""

    return False, f"并发配置必须是字符串或对象，当前类型: {type(concurrency).__name__}"
