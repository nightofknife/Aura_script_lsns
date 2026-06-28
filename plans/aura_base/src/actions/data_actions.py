from __future__ import annotations

import re
from typing import Any

from packages.aura_core.api import action_info
from packages.aura_core.context import ExecutionContext
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.utils.exceptions import StopTaskException

from ._shared import safe_math_compute


@action_info(name="log", read_only=True, public=True)
def log(message: str, level: str = "info"):
    level_str = str(level).lower()
    log_func = getattr(logger, level_str, logger.debug)
    log_func(f"[YAML Log] {message}")
    return True


@action_info(name="stop_task", read_only=True)
def stop_task(message: str = "任务已停止", success: bool = True):
    raise StopTaskException(message, success)


@action_info(name="assert_condition", read_only=True, public=True)
def assert_condition(condition: bool, message: str = "断言失败"):
    if not condition:
        raise StopTaskException(message, success=False)
    logger.info("断言成功: %s", message)
    return True


@action_info(name="set_variable", public=True)
def set_variable(context: ExecutionContext, name: str, value: Any) -> bool:
    logger.warning("Action 'set_variable' 在新的数据流模型中已弃用。")
    logger.warning("请在节点的 'outputs' 块中定义输出来传递数据。")
    logger.warning("尝试设置 '%s' = %r 的操作已被忽略。", name, value)
    return False


@action_info(name="string_format", read_only=True, public=True)
def string_format(template: str, *args, **kwargs) -> str:
    return template.format(*args, **kwargs)


@action_info(name="string_split", read_only=True, public=True)
def string_split(text: str, separator: str, max_split: int = -1) -> list[str]:
    return text.split(separator, max_split)


@action_info(name="string_join", read_only=True, public=True)
def string_join(items: list[Any], separator: str) -> str:
    return separator.join(str(item) for item in items)


@action_info(name="regex_search", read_only=True, public=True)
def regex_search(text: str, pattern: str) -> dict[str, Any] | None:
    match = re.search(pattern, text)
    if match:
        return {
            "full_match": match.group(0),
            "groups": match.groups(),
            "named_groups": match.groupdict(),
        }
    return None


@action_info(name="math_compute", read_only=True, public=True)
def math_compute(expression: str) -> Any:
    try:
        return safe_math_compute(expression)
    except Exception as exc:
        logger.error("数学表达式计算失败 '%s': %s", expression, exc)
        return None
