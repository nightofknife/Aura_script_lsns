"""Small GUI-neutral helpers for the Resonance workbench."""

from __future__ import annotations

import json
from typing import Any, Mapping

GAME_NAME = "resonance"

TERMINAL_STATUSES = {"success", "error", "failed", "timeout", "cancelled"}
STATUS_LABELS = {
    "queued": "排队中",
    "running": "运行中",
    "success": "成功",
    "failed": "失败",
    "error": "错误",
    "timeout": "超时",
    "cancelled": "已取消",
}


def parse_inputs_json(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("任务参数必须是 JSON object。")
    return payload


def pretty_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)


def normalize_run_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    data = dict(payload or {})
    if "final_result_json" in data and "final_result" not in data:
        try:
            data["final_result"] = json.loads(str(data.get("final_result_json") or "null"))
        except Exception:
            data["final_result"] = {"raw": data.get("final_result_json")}
    if "plan_name" in data and "game_name" not in data:
        data["game_name"] = data.get("plan_name")
    return data


def extract_run_id(payload: Mapping[str, Any] | None) -> str:
    data = dict(payload or {})
    for key in ("cid", "run_id", "id"):
        value = data.get(key)
        if value:
            return str(value)
    dispatch = data.get("dispatch")
    if isinstance(dispatch, Mapping):
        return extract_run_id(dispatch)
    summary = data.get("summary")
    if isinstance(summary, Mapping):
        return extract_run_id(summary)
    return ""


def extract_status(payload: Mapping[str, Any] | None) -> str:
    data = dict(payload or {})
    for key in ("status", "state"):
        value = data.get(key)
        if value:
            return str(value).lower()
    run = data.get("run")
    if isinstance(run, Mapping):
        summary = run.get("summary")
        if isinstance(summary, Mapping):
            return extract_status(summary)
    summary = data.get("summary")
    if isinstance(summary, Mapping):
        return extract_status(summary)
    return ""


def render_result_text(payload: Mapping[str, Any] | None) -> str:
    data = normalize_run_payload(payload)
    if not data:
        return ""
    run_id = extract_run_id(data)
    status = extract_status(data)
    lines: list[str] = []
    if run_id:
        lines.append(f"Run: {run_id}")
    if status:
        lines.append(f"Status: {STATUS_LABELS.get(status, status)}")

    run = data.get("run")
    if isinstance(run, Mapping):
        detail = run.get("detail")
        if isinstance(detail, Mapping):
            final_result = detail.get("final_result")
            if final_result is not None:
                lines.append("")
                lines.append(pretty_json(final_result))
                return "\n".join(lines)

    final_result = data.get("final_result")
    if final_result is not None:
        lines.append("")
        lines.append(pretty_json(final_result))
        return "\n".join(lines)

    lines.append("")
    lines.append(pretty_json(data))
    return "\n".join(lines)
