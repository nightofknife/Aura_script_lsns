# -*- coding: utf-8 -*-
"""Scheduler输入验证器

职责: 验证和规范化输入参数、Schema和值
"""

import re
import json
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple, List, Union
from packages.aura_core.types import TaskRefResolver

if TYPE_CHECKING:
    from .core import Scheduler

# 特殊标记：表示输入缺失
_MISSING = object()


class InputValidator:
    """输入验证器

    管理输入参数的验证和规范化，包括:
    - Schema规范化
    - 默认值构建
    - 输入值验证
    - 枚举类型推断
    """

    def __init__(self, scheduler: 'Scheduler'):
        """初始化输入验证器

        Args:
            scheduler: 父调度器实例
        """
        self.scheduler = scheduler

    def infer_enum_type(self, enum_vals: Any) -> Optional[str]:
        """推断枚举值的类型

        实现来自: scheduler.py 行862-876

        Args:
            enum_vals: 枚举值列表

        Returns:
            推断的类型('boolean', 'number', 'string')或None
        """
        if not isinstance(enum_vals, list) or not enum_vals:
            return None
        kinds = set()
        for val in enum_vals:
            if isinstance(val, bool):
                kinds.add("boolean")
            elif isinstance(val, (int, float)):
                kinds.add("number")
            elif isinstance(val, str):
                kinds.add("string")
            else:
                return None
        return kinds.pop() if len(kinds) == 1 else None

    def normalize_input_schema(self, schema: Dict[str, Any]) -> Dict[str, Any]:
        """规范化meta.inputs字段定义

        实现来自: scheduler.py 行877-955

        支持list<type>/dict/enum/count等写法。

        Args:
            schema: 原始Schema定义

        Returns:
            规范化后的Schema
        """
        if not isinstance(schema, dict):
            return {"type": "string"}
        normalized = dict(schema)

        # 1. 统一 enum 和 options
        enum_vals = normalized.get("enum")
        if enum_vals is None and "options" in normalized:
            enum_vals = normalized.get("options")
        if enum_vals is not None:
            normalized["enum"] = enum_vals or []

        # 2. 处理 count 语法糖
        if "count" in normalized:
            count = normalized["count"]

            if isinstance(count, int):
                # count: 3 → min: 3, max: 3
                normalized["min"] = count
                normalized["max"] = count
            elif isinstance(count, str):
                # count: "<=5" → max: 5
                max_match = re.match(r'^<=(\d+)$', count)
                if max_match:
                    normalized["max"] = int(max_match.group(1))

                # count: ">=2" → min: 2
                min_match = re.match(r'^>=(\d+)$', count)
                if min_match:
                    normalized["min"] = int(min_match.group(1))

                # count: "1-3" → min: 1, max: 3
                range_match = re.match(r'^(\d+)-(\d+)$', count)
                if range_match:
                    normalized["min"] = int(range_match.group(1))
                    normalized["max"] = int(range_match.group(2))
            elif isinstance(count, list) and len(count) == 2:
                # count: [1, 3] → min: 1, max: 3
                normalized["min"] = count[0]
                normalized["max"] = count[1]

        # 3. 保留 ui 字段（前端使用，不验证）
        if "ui" in schema:
            normalized["ui"] = schema["ui"]

        # 4. 类型规范化
        type_raw = normalized.get("type")
        if type_raw is None or type_raw == "":
            type_raw = self.infer_enum_type(normalized.get("enum")) or "string"
        else:
            type_raw = str(type_raw).lower()
        if type_raw == "enum":
            type_raw = self.infer_enum_type(normalized.get("enum")) or "string"

        list_match = re.match(r"^list<(.+)>$", type_raw)
        if list_match:
            normalized["type"] = "list"
            item_schema = normalized.get("item") or normalized.get("items") or {"type": list_match.group(1)}
            normalized["item"] = self.normalize_input_schema(item_schema)
        else:
            allowed = {"string", "number", "boolean", "list", "dict"}
            if type_raw == "integer":
                raise ValueError(
                    "Unsupported input type 'integer'. "
                    "Please use 'number'."
                )
            if type_raw == "array":
                raise ValueError(
                    "Unsupported input type 'array'. "
                    "Please use 'list'."
                )
            if type_raw == "object":
                raise ValueError(
                    "Unsupported input type 'object'. "
                    "Please use 'dict'."
                )
            if type_raw not in allowed:
                raise ValueError(
                    f"Unsupported input type '{type_raw}'. "
                    "Allowed types: string, number, boolean, list, dict."
                )
            normalized["type"] = type_raw
            if normalized["type"] == "list":
                item_schema = normalized.get("item") or normalized.get("items")
                if item_schema is not None:
                    normalized["item"] = self.normalize_input_schema(item_schema)
            if normalized["type"] == "dict":
                props = {}
                for k, v in (normalized.get("properties") or {}).items():
                    if isinstance(v, dict):
                        props[k] = self.normalize_input_schema(v)
                normalized["properties"] = props
        return normalized

    def build_default_from_schema(self, schema: Dict[str, Any]):
        """递归构造默认值（若有），用于填充缺失字段

        实现来自: scheduler.py 行956-995

        Args:
            schema: Schema定义

        Returns:
            默认值
        """
        schema_n = self.normalize_input_schema(schema or {})
        if "default" in schema_n:
            try:
                return json.loads(json.dumps(schema_n.get("default")))
            except Exception:
                return schema_n.get("default")
        enum_vals = schema_n.get("enum") or []
        if enum_vals:
            try:
                return json.loads(json.dumps(enum_vals[0]))
            except Exception:
                return enum_vals[0]
        t = schema_n.get("type")
        if t == "list":
            if isinstance(schema_n.get("default"), list):
                try:
                    return json.loads(json.dumps(schema_n.get("default")))
                except Exception:
                    return list(schema_n.get("default"))
            return []
        if t == "dict":
            if isinstance(schema_n.get("default"), dict):
                try:
                    return json.loads(json.dumps(schema_n.get("default")))
                except Exception:
                    return dict(schema_n.get("default"))
            result = {}
            for k, v in (schema_n.get("properties") or {}).items():
                child_default = self.build_default_from_schema(v)
                if child_default is not None:
                    result[k] = child_default
            return result
        if t == "boolean":
            return False
        if t == "number":
            return 0
        return ""

    def validate_input_value(self, schema: Dict[str, Any], value: Any, path: str) -> Tuple[bool, Any, Optional[str]]:
        """递归校验单个字段值

        实现来自: scheduler.py 行996-1088

        Args:
            schema: Schema定义
            value: 待验证的值
            path: 字段路径（用于错误消息）

        Returns:
            (是否成功, 规范化后的值, 错误消息)
        """
        s = self.normalize_input_schema(schema or {})
        required = bool(s.get("required"))
        has_default = "default" in s
        if value is _MISSING or value is None:
            if value is None and not required and not has_default:
                return True, None, None
            if has_default:
                return True, self.build_default_from_schema(s), None
            if required:
                return False, None, f"Missing required input: {path}"
            return True, None, None

        t = s.get("type", "string")
        if t == "string":
            val = str(value)
        elif t == "number":
            try:
                if isinstance(value, bool):
                    val = 1 if value else 0
                elif isinstance(value, (int, float)):
                    val = value
                else:
                    val = float(value)
            except Exception:
                return False, None, f"Input '{path}' must be a number."
            if "min" in s and val < s["min"]:
                return False, None, f"Input '{path}' must be >= {s['min']}."
            if "max" in s and val > s["max"]:
                return False, None, f"Input '{path}' must be <= {s['max']}."
        elif t == "boolean":
            if isinstance(value, bool):
                val = value
            elif isinstance(value, str):
                low = value.lower()
                if low in {"true", "1", "yes", "y"}:
                    val = True
                elif low in {"false", "0", "no", "n"}:
                    val = False
                else:
                    return False, None, f"Input '{path}' must be a boolean."
            else:
                val = bool(value)
        elif t == "list":
            if not isinstance(value, list):
                return False, None, f"Input '{path}' must be a list."
            # 使用统一的 min/max 字段（优先使用新的 min/max）
            min_items = s.get("min")
            if min_items is None:
                min_items = s.get("min_items") if s.get("min_items") is not None else s.get("minItems")
            max_items = s.get("max")
            if max_items is None:
                max_items = s.get("max_items") if s.get("max_items") is not None else s.get("maxItems")

            count = len(value)
            if min_items is not None and count < min_items:
                return False, None, f"Input '{path}' must contain at least {min_items} items (got {count})."
            if max_items is not None and count > max_items:
                return False, None, f"Input '{path}' must contain no more than {max_items} items (got {count})."

            # If list item schema is not explicitly declared, keep item types as-is.
            # This avoids silently coercing objects to strings through the implicit
            # default schema {"type": "string"}.
            item_schema = s.get("item") or s.get("items")
            if item_schema is None:
                try:
                    val = json.loads(json.dumps(value))
                except Exception:
                    val = list(value)
            else:
                validated_list = []
                for idx, item in enumerate(value):
                    ok, v, err = self.validate_input_value(item_schema, item, f"{path}[{idx}]")
                    if not ok:
                        return False, None, err
                    validated_list.append(v)
                val = validated_list
        elif t == "dict":
            if not isinstance(value, dict):
                return False, None, f"Input '{path}' must be an object."
            properties = s.get("properties") or {}
            extra = set(value.keys()) - set(properties.keys())
            if extra:
                return False, None, f"Input '{path}' has unexpected fields: {', '.join(sorted(extra))}."
            validated = {}
            for key, subschema in properties.items():
                ok, v, err = self.validate_input_value(subschema, value.get(key, _MISSING), f"{path}.{key}")
                if not ok:
                    return False, None, err
                if v is not None or "default" in subschema or subschema.get("required"):
                    validated[key] = v
            val = validated
        else:
            return False, None, f"Input '{path}' has unsupported type '{t}'."
        allowed = s.get("enum")
        if allowed is not None:
            allowed = allowed or []
            if val not in allowed:
                return False, None, f"Input '{path}' must be one of {allowed}."
        return True, val, None

    def validate_inputs_against_meta(
        self, inputs_meta: List[Dict[str, Any]], provided_inputs: Dict[str, Any]
    ) -> Tuple[bool, Union[str, Dict[str, Any]]]:
        """Validate and normalize user inputs against task meta.inputs."""
        if not isinstance(inputs_meta, list):
            return False, "Task meta.inputs must be a list."
        provided_inputs = provided_inputs or {}
        if not isinstance(provided_inputs, dict):
            return False, "Inputs must be an object/dict."

        expected_names = [item.get("name") for item in inputs_meta if isinstance(item, dict) and item.get("name")]
        extra = set(provided_inputs.keys()) - set(expected_names)
        if extra:
            return False, f"Unexpected inputs provided: {', '.join(extra)}"

        full_params: Dict[str, Any] = {}
        for item in inputs_meta:
            if not isinstance(item, dict) or "name" not in item:
                continue
            name = item["name"]
            ok, val, err = self.validate_input_value(item, provided_inputs.get(name, _MISSING), name)
            if not ok:
                return False, err
            if val is not None or "default" in item or item.get("required"):
                full_params[name] = val
        return True, full_params

    def resolve_and_validate_task_inputs(
        self,
        *,
        plan_name: str,
        task_ref: str,
        provided_inputs: Optional[Dict[str, Any]] = None,
        enforce_package: Optional[str] = None,
    ) -> Tuple[bool, Union[str, Dict[str, Any]]]:
        """Resolve task reference and validate inputs against task meta.inputs."""
        scheduler = self.scheduler
        if scheduler is None:
            return False, "InputValidator is not attached to Scheduler."
        if not plan_name or not task_ref:
            return False, "plan_name and task_ref are required."

        provided = provided_inputs or {}
        if not isinstance(provided, dict):
            return False, "Inputs must be an object/dict."

        try:
            resolved = TaskRefResolver.resolve(
                task_ref,
                default_package=plan_name,
                enforce_package=enforce_package or plan_name,
            )
        except Exception as exc:
            return False, f"Invalid task reference '{task_ref}': {exc}"

        full_task_id = resolved.canonical_task_id
        task_def = {}
        if isinstance(getattr(scheduler, "all_tasks_definitions", None), dict):
            task_def = scheduler.all_tasks_definitions.get(full_task_id) or {}

        if not isinstance(task_def, dict) or not task_def:
            orchestrator = None
            try:
                orchestrator = scheduler.plan_manager.get_plan(plan_name)
            except Exception:
                orchestrator = None
            if orchestrator:
                try:
                    loader_path = resolved.loader_path
                    task_def = orchestrator.task_loader.get_task_data(loader_path) or {}
                except Exception:
                    task_def = {}

        if not isinstance(task_def, dict) or not task_def:
            task_error = self._find_task_load_error(plan_name=plan_name, loader_path=resolved.loader_path)
            if task_error:
                return False, (
                    f"Task definition invalid for '{full_task_id}': "
                    f"{task_error.get('message', 'Task file is invalid.')}"
                )
            return False, f"Task '{task_ref}' not found in plan '{plan_name}'."

        inputs_meta = (task_def.get("meta", {}) or {}).get("inputs", [])
        ok, validated_or_error = self.validate_inputs_against_meta(inputs_meta, provided)
        if not ok:
            return False, f"Task '{full_task_id}' inputs invalid: {validated_or_error}"

        return True, {
            "resolved": resolved,
            "full_task_id": full_task_id,
            "task_def": task_def,
            "inputs_meta": inputs_meta,
            "validated_inputs": validated_or_error,
        }

    def _find_task_load_error(self, *, plan_name: str, loader_path: str) -> Optional[Dict[str, Any]]:
        scheduler = self.scheduler
        if scheduler is None:
            return None

        candidate_files = []
        parts = [part for part in str(loader_path or "").split("/") if part]
        if not parts:
            return None
        candidate_files.append("/".join(parts) + ".yaml")
        file_parts = parts[:-1] if len(parts) > 1 else parts
        candidate_files.append("/".join(file_parts) + ".yaml")

        task_load_errors = getattr(scheduler, "task_load_errors", {}) or {}
        for candidate_file in candidate_files:
            record = task_load_errors.get(f"{plan_name}/{candidate_file}")
            if record:
                return record
        return None
