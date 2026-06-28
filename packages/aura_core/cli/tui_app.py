# -*- coding: utf-8 -*-
"""Interactive TUI mode for manual task execution."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import click
from prompt_toolkit.shortcuts import input_dialog, message_dialog, radiolist_dialog, yes_no_dialog

from packages.aura_core.config.loader import get_config_value
from packages.aura_core.runtime.bootstrap import start_runtime, stop_runtime
from packages.aura_core.scheduler import Scheduler

_TERMINAL_STATUSES = {"success", "error", "failed", "timeout", "cancelled"}
_SKIP_FIELD = object()


@dataclass
class TuiState:
    recent_cids: list[str] = field(default_factory=list)

    def remember_cid(self, cid: str | None) -> None:
        if not cid:
            return
        self.recent_cids = [item for item in self.recent_cids if item != cid]
        self.recent_cids.insert(0, cid)
        del self.recent_cids[20:]


def run_tui() -> None:
    timeout = int(
        get_config_value(
            "runtime.startup_timeout_sec",
            get_config_value("backend.scheduler_startup_timeout_sec", 10),
        )
    )
    scheduler = start_runtime(profile="tui_manual", startup_timeout_sec=timeout)
    state = TuiState()
    try:
        _main_loop(scheduler, state)
    finally:
        stop_runtime()


def _main_loop(scheduler: Scheduler, state: TuiState) -> None:
    while True:
        action = radiolist_dialog(
            title="Aura TUI (Manual Mode)",
            text="选择操作",
            values=[
                ("entry_task", "运行入口任务"),
                ("scheduled_task", "手动触发调度项"),
                ("run_status", "查看最近运行状态"),
                ("exit", "退出"),
            ],
        ).run()

        if action in (None, "exit"):
            return
        if action == "entry_task":
            _run_entry_task(scheduler, state)
        elif action == "scheduled_task":
            _run_scheduled_item(scheduler, state)
        elif action == "run_status":
            _show_recent_status(scheduler, state)


def _run_entry_task(scheduler: Scheduler, state: TuiState) -> None:
    entry_tasks = _load_entry_tasks(scheduler)
    if not entry_tasks:
        message_dialog(title="提示", text="未找到入口任务（meta.entry_point: true）。").run()
        return

    values = []
    task_map: dict[str, dict[str, Any]] = {}
    for task in entry_tasks:
        task_key = task["full_task_id"]
        title = f"{task['plan_name']}/{task['task_ref']}"
        description = ((task.get("meta") or {}).get("description") or "").strip()
        label = f"{title}  -  {description}" if description else title
        values.append((task_key, label))
        task_map[task_key] = task

    selected_key = radiolist_dialog(
        title="入口任务",
        text="上下选择任务，回车运行",
        values=values,
    ).run()
    if not selected_key:
        return

    task = task_map[selected_key]
    inputs = _collect_inputs(task.get("meta") or {})
    if inputs is None:
        return

    result = scheduler.run_ad_hoc_task(task["plan_name"], task["task_ref"], inputs)
    _handle_dispatch_result(scheduler, state, result)


def _run_scheduled_item(scheduler: Scheduler, state: TuiState) -> None:
    schedule_items = scheduler.get_schedule_status() or []
    if not schedule_items:
        message_dialog(title="提示", text="当前没有可用调度项。").run()
        return

    values = []
    item_map: dict[str, dict[str, Any]] = {}
    for item in schedule_items:
        item_id = str(item.get("id") or "")
        if not item_id:
            continue
        plan_name = item.get("plan_name") or "unknown_plan"
        task_name = item.get("task") or item.get("task_name") or "unknown_task"
        status = item.get("status") or "idle"
        label = f"{item_id}  -  {plan_name}/{task_name}  [{status}]"
        values.append((item_id, label))
        item_map[item_id] = item

    if not values:
        message_dialog(title="提示", text="调度项缺少可用 ID。").run()
        return

    selected_id = radiolist_dialog(
        title="调度项",
        text="上下选择调度项，回车手动触发",
        values=values,
    ).run()
    if not selected_id:
        return

    result = scheduler.run_manual_task(selected_id)
    _handle_dispatch_result(scheduler, state, result)


def _show_recent_status(scheduler: Scheduler, state: TuiState) -> None:
    if not state.recent_cids:
        message_dialog(title="提示", text="暂无最近运行记录。").run()
        return

    statuses = scheduler.get_batch_task_status(state.recent_cids)
    lines = []
    for item in statuses:
        cid = item.get("cid")
        status = item.get("status")
        plan_name = item.get("plan_name") or "-"
        task_name = item.get("task_name") or "-"
        lines.append(f"{cid}  |  {status}  |  {plan_name}/{task_name}")

    message_dialog(
        title="最近运行状态",
        text="\n".join(lines[:20]) or "无数据",
    ).run()


def _load_entry_tasks(scheduler: Scheduler) -> list[dict[str, Any]]:
    tasks = scheduler.get_all_task_definitions_with_meta()
    entry_tasks = []
    for task in tasks:
        meta = task.get("meta") or {}
        if bool(meta.get("entry_point", False)):
            entry_tasks.append(task)
    entry_tasks.sort(key=lambda x: str(x.get("full_task_id") or ""))
    return entry_tasks


def _collect_inputs(meta: dict[str, Any]) -> dict[str, Any] | None:
    inputs_meta = meta.get("inputs") or []
    if not isinstance(inputs_meta, list) or not inputs_meta:
        raw = input_dialog(
            title="任务输入",
            text="输入 JSON（留空使用 {}）:",
            default="{}",
        ).run()
        if raw is None:
            return None
        raw = raw.strip()
        if not raw:
            return {}
        try:
            loaded = json.loads(raw)
            if not isinstance(loaded, dict):
                message_dialog(title="输入错误", text="输入必须是 JSON 对象。").run()
                return None
            return loaded
        except Exception as exc:
            message_dialog(title="输入错误", text=f"JSON 解析失败: {exc}").run()
            return None

    collected: dict[str, Any] = {}
    for field in inputs_meta:
        if not isinstance(field, dict):
            continue
        name = str(field.get("name") or "").strip()
        if not name:
            continue
        required = bool(field.get("required", False))
        field_type = str(field.get("type") or "string").strip().lower()
        default_value = field.get("default")
        desc = str(field.get("description") or "").strip()
        hint_default = "" if default_value is None else f" (默认: {default_value})"
        prompt = f"{name} [{field_type}]{hint_default}\n{desc}".strip()

        while True:
            raw = input_dialog(title="任务输入", text=prompt, default="" if default_value is None else str(default_value)).run()
            if raw is None:
                return None
            raw = raw.strip()
            if not raw:
                if default_value is not None:
                    collected[name] = default_value
                    break
                if required:
                    message_dialog(title="输入错误", text=f"参数 {name} 为必填。").run()
                    continue
                break
            try:
                collected[name] = _convert_input(raw, field_type)
                break
            except Exception as exc:
                message_dialog(title="输入错误", text=f"参数 {name} 解析失败: {exc}").run()

    return collected


def _convert_input(raw: str, field_type: str) -> Any:
    if field_type in {"int", "integer"}:
        return int(raw)
    if field_type in {"float", "number"}:
        return float(raw)
    if field_type in {"bool", "boolean"}:
        lowered = raw.lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "no", "n", "off"}:
            return False
        raise ValueError("布尔值仅支持 true/false/yes/no/1/0")
    if field_type in {"array", "list", "object", "dict", "map"}:
        return json.loads(raw)
    return raw


def _handle_dispatch_result(scheduler: Scheduler, state: TuiState, result: dict[str, Any]) -> None:
    status = result.get("status")
    message = result.get("message") or ""
    cid = result.get("cid")
    trace_id = result.get("trace_id")

    if status != "success":
        message_dialog(title="执行失败", text=message or "任务提交失败。").run()
        return

    state.remember_cid(cid)
    text = f"提交成功\nCID: {cid}\nTraceID: {trace_id}\n{message}".strip()
    message_dialog(title="任务已提交", text=text).run()

    if cid and yes_no_dialog(title="状态跟踪", text="是否等待任务执行完成？").run():
        _watch_task_until_terminal(scheduler, cid)


def _watch_task_until_terminal(scheduler: Scheduler, cid: str, timeout_sec: int = 600) -> None:
    click.echo(f"[watch] 开始跟踪任务: {cid}")
    start = time.time()
    last_status = "queued"
    while time.time() - start < timeout_sec:
        status_rows = scheduler.get_batch_task_status([cid])
        if not status_rows:
            break
        status = str(status_rows[0].get("status") or "unknown").lower()
        if status != last_status:
            click.echo(f"[watch] {cid} -> {status}")
            last_status = status
        if status in _TERMINAL_STATUSES:
            break
        time.sleep(1)

    message_dialog(title="状态跟踪结束", text=f"CID: {cid}\n最终状态: {last_status}").run()

# --- Enhanced interactive input collection (overrides previous implementation) ---
def _collect_inputs(meta: dict[str, Any]) -> dict[str, Any] | None:
    inputs_meta = meta.get("inputs") or []
    if not isinstance(inputs_meta, list) or not inputs_meta:
        raw = input_dialog(
            title="Task Input",
            text="Input JSON (empty uses {}):",
            default="{}",
        ).run()
        if raw is None:
            return None
        raw = raw.strip()
        if not raw:
            return {}
        try:
            loaded = json.loads(raw)
            if not isinstance(loaded, dict):
                message_dialog(title="Input Error", text="Input must be a JSON object.").run()
                return None
            return loaded
        except Exception as exc:
            message_dialog(title="Input Error", text=f"JSON parse failed: {exc}").run()
            return None

    collected: dict[str, Any] = {}
    for field in inputs_meta:
        if not isinstance(field, dict):
            continue
        name = str(field.get("name") or "").strip()
        if not name:
            continue
        cancelled, value = _prompt_field_value(field=field, field_name=name)
        if cancelled:
            return None
        if value is _SKIP_FIELD:
            continue
        collected[name] = value

    return collected


def _prompt_field_value(field: dict[str, Any], field_name: str) -> tuple[bool, Any]:
    required = bool(field.get("required", False))
    has_default = "default" in field
    default_value = field.get("default")
    field_type = str(field.get("type") or "string").strip().lower()
    field_label = str(field.get("label") or field_name).strip() or field_name
    field_desc = str(field.get("description") or "").strip()
    enum_values = _extract_enum_values(field)

    if enum_values:
        return _prompt_enum_value(
            field_name=field_name,
            field_label=field_label,
            required=required,
            has_default=has_default,
            default_value=default_value,
            enum_values=enum_values,
            field_desc=field_desc,
        )

    if field_type in {"list", "array"}:
        return _prompt_list_value(
            field=field,
            field_name=field_name,
            field_label=field_label,
            required=required,
            has_default=has_default,
            default_value=default_value,
            field_desc=field_desc,
        )

    if field_type in {"dict", "object", "map"}:
        return _prompt_dict_value(
            field=field,
            field_name=field_name,
            field_label=field_label,
            required=required,
            has_default=has_default,
            default_value=default_value,
            field_desc=field_desc,
        )

    return _prompt_scalar_value(
        field_name=field_name,
        field_label=field_label,
        field_type=field_type,
        required=required,
        has_default=has_default,
        default_value=default_value,
        field_desc=field_desc,
    )


def _extract_enum_values(field: dict[str, Any]) -> list[Any]:
    raw = field.get("enum")
    if raw is None:
        raw = field.get("options")
    if isinstance(raw, list):
        return raw
    return []


def _prompt_enum_value(
    *,
    field_name: str,
    field_label: str,
    required: bool,
    has_default: bool,
    default_value: Any,
    enum_values: list[Any],
    field_desc: str,
) -> tuple[bool, Any]:
    if _should_use_cascaded_enum(field_name=field_name, enum_values=enum_values):
        return _prompt_cascaded_dotted_enum(
            field_name=field_name,
            field_label=field_label,
            required=required,
            has_default=has_default,
            enum_values=[str(v) for v in enum_values],
            field_desc=field_desc,
        )

    choices: list[tuple[str, str]] = []
    key_to_value: dict[str, Any] = {}
    if not required and not has_default:
        choices.append(("__skip__", "(Skip)"))

    default_key = "__skip__" if (not required and not has_default) else None
    for idx, item in enumerate(enum_values):
        key = str(idx)
        key_to_value[key] = item
        choices.append((key, _format_enum_label(item)))
        if default_key is None and has_default and item == default_value:
            default_key = key

    selected = radiolist_dialog(
        title=f"Input: {field_label}",
        text=f"{field_name}\n{field_desc}".strip(),
        values=choices,
        default=default_key,
    ).run()
    if selected is None:
        return True, None
    if selected == "__skip__":
        return False, _SKIP_FIELD
    return False, key_to_value[selected]


def _should_use_cascaded_enum(*, field_name: str, enum_values: list[Any]) -> bool:
    if len(enum_values) < 8:
        return False
    if "route" not in field_name.lower():
        return False
    if not all(isinstance(item, str) for item in enum_values):
        return False
    dotted = [item for item in enum_values if "." in item]
    return len(dotted) == len(enum_values)


def _prompt_cascaded_dotted_enum(
    *,
    field_name: str,
    field_label: str,
    required: bool,
    has_default: bool,
    enum_values: list[str],
    field_desc: str,
) -> tuple[bool, Any]:
    routes = sorted({str(item).strip() for item in enum_values if str(item).strip()})
    if not routes:
        return True, None

    prefix: list[str] = []
    while True:
        matched = [route for route in routes if _route_has_prefix(route, prefix)]
        if not matched:
            message_dialog(title="Input Error", text=f"No route found under prefix: {'.'.join(prefix)}").run()
            if not prefix:
                return True, None
            prefix.pop()
            continue

        exact = [".".join(prefix)] if prefix and ".".join(prefix) in matched else []
        children = sorted(
            {
                route.split(".")[len(prefix)]
                for route in matched
                if len(route.split(".")) > len(prefix)
            }
        )

        if exact and not children:
            return False, exact[0]

        choices: list[tuple[str, str]] = []
        if not required and not has_default and not prefix:
            choices.append(("__skip__", "(Skip)"))
        if prefix:
            choices.append(("__back__", "← Back"))
        if exact:
            choices.append(("__select__", f"✓ Select current: {exact[0]}"))
        for child in children:
            choices.append((f"seg:{child}", child))

        level_hint = ".".join(prefix) if prefix else "(root)"
        selected = radiolist_dialog(
            title=f"Input: {field_label}",
            text=f"{field_name}\n{field_desc}\nCurrent: {level_hint}",
            values=choices,
        ).run()
        if selected is None:
            return True, None
        if selected == "__skip__":
            return False, _SKIP_FIELD
        if selected == "__back__":
            if prefix:
                prefix.pop()
            continue
        if selected == "__select__":
            return False, ".".join(prefix)
        if selected.startswith("seg:"):
            prefix.append(selected[4:])
            continue


def _route_has_prefix(route: str, prefix: list[str]) -> bool:
    if not prefix:
        return True
    parts = route.split(".")
    if len(parts) < len(prefix):
        return False
    return parts[: len(prefix)] == prefix


def _prompt_list_value(
    *,
    field: dict[str, Any],
    field_name: str,
    field_label: str,
    required: bool,
    has_default: bool,
    default_value: Any,
    field_desc: str,
) -> tuple[bool, Any]:
    min_items = field.get("min_items", field.get("minItems", field.get("min")))
    max_items = field.get("max_items", field.get("maxItems", field.get("max")))
    min_items = int(min_items) if isinstance(min_items, (int, float)) else 0
    max_items = int(max_items) if isinstance(max_items, (int, float)) else None

    item_schema = field.get("item") or field.get("items")
    if not isinstance(item_schema, dict):
        while True:
            default_text = ""
            if has_default:
                default_text = json.dumps(default_value, ensure_ascii=False)
            raw = input_dialog(
                title=f"Input: {field_label}",
                text=f"{field_name} [list]\nEnter JSON array.\n{field_desc}".strip(),
                default=default_text,
            ).run()
            if raw is None:
                return True, None
            raw = raw.strip()
            if not raw:
                if has_default:
                    return False, default_value
                if required:
                    message_dialog(title="Input Error", text=f"{field_name} is required.").run()
                    continue
                return False, _SKIP_FIELD
            try:
                parsed = json.loads(raw)
                if not isinstance(parsed, list):
                    raise ValueError("value must be a JSON array")
                return False, parsed
            except Exception as exc:
                message_dialog(title="Input Error", text=f"{field_name} parse failed: {exc}").run()

    items: list[Any] = []
    while True:
        if max_items is not None and len(items) >= max_items:
            break
        can_stop = len(items) >= min_items
        if can_stop:
            add_more = yes_no_dialog(
                title=f"Input: {field_label}",
                text=f"{field_name}: {len(items)} item(s) added. Add one more?",
            ).run()
            if not add_more:
                break
        cancelled, value = _prompt_field_value(
            field={**item_schema, "required": True},
            field_name=f"{field_name}[{len(items)}]",
        )
        if cancelled:
            return True, None
        if value is _SKIP_FIELD:
            continue
        items.append(value)

    if len(items) < min_items:
        message_dialog(
            title="Input Error",
            text=f"{field_name} requires at least {min_items} item(s).",
        ).run()
        return _prompt_list_value(
            field=field,
            field_name=field_name,
            field_label=field_label,
            required=required,
            has_default=has_default,
            default_value=default_value,
            field_desc=field_desc,
        )

    if not items and has_default:
        return False, default_value
    if not items and not required:
        return False, _SKIP_FIELD
    return False, items


def _prompt_dict_value(
    *,
    field: dict[str, Any],
    field_name: str,
    field_label: str,
    required: bool,
    has_default: bool,
    default_value: Any,
    field_desc: str,
) -> tuple[bool, Any]:
    properties = field.get("properties")
    if not isinstance(properties, dict) or not properties:
        while True:
            default_text = ""
            if has_default:
                default_text = json.dumps(default_value, ensure_ascii=False)
            raw = input_dialog(
                title=f"Input: {field_label}",
                text=f"{field_name} [dict]\nEnter JSON object.\n{field_desc}".strip(),
                default=default_text,
            ).run()
            if raw is None:
                return True, None
            raw = raw.strip()
            if not raw:
                if has_default:
                    return False, default_value
                if required:
                    message_dialog(title="Input Error", text=f"{field_name} is required.").run()
                    continue
                return False, _SKIP_FIELD
            try:
                parsed = json.loads(raw)
                if not isinstance(parsed, dict):
                    raise ValueError("value must be a JSON object")
                return False, parsed
            except Exception as exc:
                message_dialog(title="Input Error", text=f"{field_name} parse failed: {exc}").run()

    result: dict[str, Any] = {}
    for key, schema in properties.items():
        if not isinstance(schema, dict):
            continue
        sub_field = dict(schema)
        sub_field.setdefault("name", key)
        cancelled, value = _prompt_field_value(
            field=sub_field,
            field_name=f"{field_name}.{key}",
        )
        if cancelled:
            return True, None
        if value is _SKIP_FIELD:
            continue
        result[key] = value

    if not result and has_default:
        return False, default_value
    if not result and not required:
        return False, _SKIP_FIELD
    return False, result


def _prompt_scalar_value(
    *,
    field_name: str,
    field_label: str,
    field_type: str,
    required: bool,
    has_default: bool,
    default_value: Any,
    field_desc: str,
) -> tuple[bool, Any]:
    hint_default = "" if not has_default else f" (default: {default_value})"
    prompt = f"{field_name} [{field_type}]{hint_default}\n{field_desc}".strip()
    while True:
        raw = input_dialog(
            title=f"Input: {field_label}",
            text=prompt,
            default="" if not has_default else str(default_value),
        ).run()
        if raw is None:
            return True, None
        raw = raw.strip()
        if not raw:
            if has_default:
                return False, default_value
            if required:
                message_dialog(title="Input Error", text=f"{field_name} is required.").run()
                continue
            return False, _SKIP_FIELD
        try:
            return False, _convert_input(raw, field_type)
        except Exception as exc:
            message_dialog(title="Input Error", text=f"{field_name} parse failed: {exc}").run()


def _format_enum_label(item: Any) -> str:
    if isinstance(item, dict):
        label = item.get("label")
        value = item.get("value")
        if label is not None and value is not None:
            return f"{label} ({value})"
        return json.dumps(item, ensure_ascii=False)
    if isinstance(item, (list, tuple)):
        return json.dumps(item, ensure_ascii=False)
    return str(item)


def _convert_input(raw: str, field_type: str) -> Any:
    if field_type in {"int", "integer"}:
        return int(raw)
    if field_type in {"float", "number"}:
        return float(raw)
    if field_type in {"bool", "boolean"}:
        lowered = raw.lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "no", "n", "off"}:
            return False
        raise ValueError("boolean only supports true/false/yes/no/1/0")
    if field_type in {"array", "list", "object", "dict", "map"}:
        return json.loads(raw)
    return raw
