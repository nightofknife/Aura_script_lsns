"""GUI-neutral helpers and view models for the Resonance desktop console."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

GAME_NAME = "resonance"
PC_GAME_NAME = "resonance_pc"
PC_TRADE_TASK_REF = "tasks:auto_cycle_trade_pc.yaml:auto_cycle_trade_pc"
PC_TRADE_PREVIEW_TASK_REF = "tasks:preview_trade_plan_pc.yaml:preview_trade_plan_pc"
EMULATOR_TRADE_TASK_REF = "tasks:auto_cycle_trade_exact.yaml:auto_cycle_trade_exact"
EMULATOR_TRADE_PREVIEW_TASK_REF = "tasks:preview_trade_plan.yaml:preview_trade_plan"
PC_TRADE_PROGRESS_EVENT = "task.resonance_pc_trade_progress"
PC_TRADE_PROGRESS_SCHEMA = "resonance_pc.trade_progress.v1"
EMULATOR_TRADE_PROGRESS_EVENT = "task.resonance_trade_progress"
EMULATOR_TRADE_PROGRESS_SCHEMA = "resonance.trade_progress.v1"

# Backward-compatible aliases for existing consumers.
TRADE_PROGRESS_EVENT = PC_TRADE_PROGRESS_EVENT
TRADE_PROGRESS_SCHEMA = PC_TRADE_PROGRESS_SCHEMA


@dataclass(frozen=True)
class TradeBackendSpec:
    key: str
    label: str
    game_name: str
    run_task_ref: str
    preview_task_ref: str
    progress_event: str
    progress_schema: str


TRADE_BACKENDS = {
    "pc": TradeBackendSpec(
        key="pc",
        label="PC",
        game_name=PC_GAME_NAME,
        run_task_ref=PC_TRADE_TASK_REF,
        preview_task_ref=PC_TRADE_PREVIEW_TASK_REF,
        progress_event=PC_TRADE_PROGRESS_EVENT,
        progress_schema=PC_TRADE_PROGRESS_SCHEMA,
    ),
    "emulator": TradeBackendSpec(
        key="emulator",
        label="模拟器",
        game_name=GAME_NAME,
        run_task_ref=EMULATOR_TRADE_TASK_REF,
        preview_task_ref=EMULATOR_TRADE_PREVIEW_TASK_REF,
        progress_event=EMULATOR_TRADE_PROGRESS_EVENT,
        progress_schema=EMULATOR_TRADE_PROGRESS_SCHEMA,
    ),
}
DEFAULT_TRADE_BACKEND = "pc"


def resolve_trade_backend(value: Any) -> TradeBackendSpec:
    key = str(value or DEFAULT_TRADE_BACKEND).strip().lower()
    if key not in TRADE_BACKENDS:
        raise ValueError(f"未知运行端：{value}")
    return TRADE_BACKENDS[key]

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

TRADE_STAGE_LABELS = {
    "target": "准备目标",
    "city": "读取城市",
    "market": "刷新市场",
    "planning": "规划路线",
    "leg": "执行路线",
    "sell": "出售",
    "buy": "购买",
    "negotiation": "协商",
    "travel": "城市移动",
    "arrival": "等待到站",
    "final_sale": "终点清仓",
    "route": "执行路线",
    "task": "任务",
}


@dataclass
class TradeProgressState:
    """Reduced, presentation-ready state for one trade run."""

    cid: str = ""
    sequence: int = -1
    stage: str = "target"
    state: str = "idle"
    operation: str = ""
    leg_index: int | None = None
    leg_count: int = 0
    from_city: str = ""
    to_city: str = ""
    current_city: str = ""
    snapshot_id: str = ""
    route: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    last_data: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def stage_label(self) -> str:
        if self.stage == "negotiation":
            return "抬价" if self.operation == "raise" else "砍价"
        return TRADE_STAGE_LABELS.get(self.stage, self.stage or "待规划")


def reduce_trade_progress(
    current: TradeProgressState | None,
    event: Mapping[str, Any] | None,
    *,
    expected_cid: str = "",
) -> TradeProgressState:
    """Apply one structured progress event while rejecting stale or foreign data."""

    state = current or TradeProgressState(cid=str(expected_cid or ""))
    envelope = dict(event or {})
    event_name = str(envelope.get("name") or "")
    payload = envelope.get("payload")
    if not isinstance(payload, Mapping):
        return state
    payload = dict(payload)
    protocol = (event_name, str(payload.get("schema") or ""))
    if protocol not in {
        (PC_TRADE_PROGRESS_EVENT, PC_TRADE_PROGRESS_SCHEMA),
        (EMULATOR_TRADE_PROGRESS_EVENT, EMULATOR_TRADE_PROGRESS_SCHEMA),
    }:
        return state
    cid = str(payload.get("cid") or "")
    if expected_cid and cid != str(expected_cid):
        return state
    try:
        sequence = int(payload.get("sequence", -1))
    except (TypeError, ValueError):
        return state
    if sequence <= state.sequence:
        return state

    next_state = TradeProgressState(
        cid=cid or state.cid,
        sequence=sequence,
        stage=str(payload.get("stage") or state.stage),
        state=str(payload.get("state") or state.state),
        operation=str(payload.get("operation") or ""),
        leg_index=_optional_int(payload.get("leg_index")),
        leg_count=_int_or(payload.get("leg_count"), state.leg_count),
        from_city=str(payload.get("from_city") or ""),
        to_city=str(payload.get("to_city") or ""),
        current_city=str(payload.get("current_city") or state.current_city),
        snapshot_id=str(payload.get("snapshot_id") or state.snapshot_id),
        route=list(state.route),
        summary=dict(state.summary),
        last_data=dict(payload.get("data") or {}) if isinstance(payload.get("data"), Mapping) else {},
        events=[*state.events, envelope],
    )
    data = next_state.last_data
    if isinstance(data.get("route"), list):
        next_state.route = [dict(item) for item in data["route"] if isinstance(item, Mapping)]
    if isinstance(data.get("summary"), Mapping):
        next_state.summary = dict(data["summary"])
    return next_state


def extract_final_result(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Unwrap runner/history payload variants into the task's public result."""

    data: Any = normalize_run_payload(payload)
    for _ in range(6):
        if not isinstance(data, Mapping):
            return {}
        if isinstance(data.get("user_data"), Mapping):
            return dict(data["user_data"])
        if isinstance(data.get("final_result"), Mapping):
            data = data["final_result"]
            continue
        run = data.get("run")
        if isinstance(run, Mapping):
            detail = run.get("detail")
            if isinstance(detail, Mapping):
                data = detail
                continue
        detail = data.get("detail")
        if isinstance(detail, Mapping):
            data = detail
            continue
        return dict(data)
    return dict(data) if isinstance(data, Mapping) else {}


def extract_trade_route(payload: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    result = extract_final_result(payload)
    return [dict(item) for item in (result.get("route") or []) if isinstance(item, Mapping)]


def trade_result_summary(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    result = extract_final_result(payload)
    route = extract_trade_route(result)
    execution = result.get("execution") if isinstance(result.get("execution"), Mapping) else {}
    initial_city = result.get("initial_city") if isinstance(result.get("initial_city"), Mapping) else {}
    final_city = ""
    if route:
        final_city = str(route[-1].get("to_city") or "")
    warnings = list(result.get("warnings") or [])
    if str(result.get("market_source") or "") == "fallback_cache":
        warnings.insert(0, "行情更新失败，已使用本地市场快照。")
    return {
        "status": str(result.get("status") or extract_status(payload) or ""),
        "reason": result.get("reason") or execution.get("reason"),
        "city_path": list(result.get("city_path") or []),
        "route": route,
        "snapshot_id": str(result.get("snapshot_id") or ""),
        "expected_profit": result.get("expected_profit"),
        "fatigue_budget": result.get("fatigue_budget"),
        "expected_fatigue_used": result.get("expected_fatigue_used"),
        "remaining_expected_fatigue": result.get("remaining_expected_fatigue"),
        "books_used": result.get("books_used"),
        "remaining_books": result.get("remaining_books"),
        "full_bargain_count": result.get("full_bargain_count"),
        "full_raise_count": result.get("full_raise_count"),
        "fatigue_medicine_used": list(result.get("fatigue_medicine_used") or []),
        "fatigue_medicine_use_count": result.get("fatigue_medicine_use_count"),
        "warnings": warnings,
        "initial_city": str(initial_city.get("city_name") or ""),
        "final_city": final_city,
        "page_state": str(result.get("page_state") or ""),
        "blocked_at": result.get("blocked_at"),
        "preview": bool(result.get("preview")),
        "market_refreshed": bool(result.get("market_refreshed")),
        "market_source": str(result.get("market_source") or ""),
        "market_stale_reason": str(result.get("market_stale_reason") or ""),
        "market_fetched_at": str(result.get("market_fetched_at") or ""),
    }


def expected_profit_per_fatigue(summary: Mapping[str, Any] | None) -> float | None:
    data = dict(summary or {})
    try:
        profit = float(data.get("expected_profit"))
        fatigue = float(data.get("expected_fatigue_used"))
    except (TypeError, ValueError):
        return None
    if fatigue <= 0:
        return None
    return profit / fatigue


def route_product_lines(leg: Mapping[str, Any]) -> list[str]:
    buys = leg.get("buys")
    lines: list[str] = []
    if isinstance(buys, list):
        for item in buys:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("product_name") or item.get("name") or item.get("product_id") or "").strip()
            quantity = item.get("quantity")
            if name:
                lines.append(f"{name} x{quantity}" if quantity not in (None, "") else name)
    if lines:
        return lines
    return [str(item) for item in (leg.get("buy_products") or []) if str(item).strip()]


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _int_or(value: Any, default: int) -> int:
    parsed = _optional_int(value)
    return default if parsed is None else parsed


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
