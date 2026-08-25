"""GUI-neutral helpers and view models for the Resonance desktop console."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

GAME_NAME = "resonance"
PC_GAME_NAME = "resonance_pc"
PC_TRADE_TASK_REF = "tasks:auto_cycle_trade_pc.yaml:auto_cycle_trade_pc"
PC_TRADE_PREVIEW_TASK_REF = "tasks:preview_trade_plan_pc.yaml:preview_trade_plan_pc"
PC_PASSENGER_TASK_REF = "tasks:auto_passenger_trips_pc.yaml:auto_passenger_trips_pc"
PC_COMBINED_COMMERCE_TASK_REF = (
    "tasks:auto_combined_commerce_pc.yaml:auto_combined_commerce_pc"
)
PC_BATTLE_TASK_REF = "tasks:auto_battle_dispatch_pc.yaml:auto_battle_dispatch_pc"
PC_BATTLE_PREVIEW_TASK_REF = (
    "tasks:auto_battle_input_preview_pc.yaml:auto_battle_input_preview_pc"
)
PC_PLAYER_DATA_REFRESH_TASK_REF = "tasks:player_data_pc.yaml:player_data_refresh"
PC_PLAYER_DATA_LATEST_TASK_REF = "tasks:player_data_pc.yaml:player_data_get_latest"
PC_TEAM_RECOMMENDATION_TASK_REF = (
    "tasks:team_recommendation_pc.yaml:team_recommendation_pc"
)
PC_CONSCIOUSNESS_DEEP_DIVE_TASK_REF = (
    "tasks:consciousness_deep_dive_pc.yaml:consciousness_deep_dive_pc"
)
TRADE_PROGRESS_EVENT = "task.resonance_pc_trade_progress"
TRADE_PROGRESS_SCHEMA = "resonance_pc.trade_progress.v1"
PASSENGER_PROGRESS_EVENT = "task.resonance_pc_passenger_progress"
PASSENGER_PROGRESS_SCHEMA = "resonance_pc.passenger_progress.v1"

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
    "investment": "投资",
    "rubbish_recycling": "倒垃圾",
    "final_sale": "终点清仓",
    "route": "执行路线",
    "task": "任务",
}

FREIGHT_PHASE_LABELS = {
    "arrival": "到达城市",
    "sell": "售出货物",
    "buy": "购买货物",
    "investment": "城市投资",
    "rubbish_recycling": "倒垃圾",
    "travel": "前往下一城市",
    "final_sale": "终点清仓",
}


@dataclass
class FreightBusinessPhase:
    """One user-meaningful business phase inside a freight city visit."""

    key: str
    label: str
    state: str = "waiting"
    detail: str = ""


@dataclass
class FreightCityStage:
    """Presentation state for one occurrence of a city in the planned route."""

    index: int
    count: int
    name: str
    role: str
    phases: list[FreightBusinessPhase] = field(default_factory=list)

    @property
    def state(self) -> str:
        states = {phase.state for phase in self.phases}
        if "failed" in states:
            return "failed"
        if "running" in states:
            return "running"
        if self.phases and states <= {"completed", "skipped"}:
            return "completed"
        return "waiting"


@dataclass
class WorkflowFreightProgressState:
    """City-oriented workflow progress reduced from additive trade events."""

    cid: str = ""
    sequence: int = -1
    state: str = "waiting"
    preparation_state: str = "waiting"
    preparation_detail: str = "等待读取目标与规划路线"
    route: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    market_source: str = ""
    cities: list[FreightCityStage] = field(default_factory=list)
    active_city_index: int | None = None
    active_phase: str = ""
    investment_enabled: bool = False
    rubbish_recycling_enabled: bool = True

    @property
    def total_units(self) -> int:
        return 1 + sum(len(city.phases) for city in self.cities)

    @property
    def completed_units(self) -> int:
        completed = 1 if self.preparation_state in {"completed", "skipped"} else 0
        return completed + sum(
            1
            for city in self.cities
            for phase in city.phases
            if phase.state in {"completed", "skipped"}
        )

    @property
    def percent(self) -> int | None:
        if not self.route and self.state not in {"completed", "success"}:
            return None
        total = max(self.total_units, 1)
        return min(100, round(self.completed_units * 100 / total))

    @property
    def current_label(self) -> str:
        if self.active_city_index is not None and 0 <= self.active_city_index < len(self.cities):
            city = self.cities[self.active_city_index]
            for phase in city.phases:
                if phase.key == self.active_phase:
                    return f"{city.name} · {phase.detail or phase.label}"
            return city.name
        return self.preparation_detail


def reduce_workflow_freight_progress(
    current: WorkflowFreightProgressState | None,
    event: Mapping[str, Any] | None,
    *,
    expected_cid: str = "",
    investment_enabled: bool | None = None,
    rubbish_recycling_enabled: bool | None = None,
) -> WorkflowFreightProgressState:
    """Reduce trade progress into route preparation and city business stages."""

    state = copy.deepcopy(current) if current is not None else WorkflowFreightProgressState()
    if investment_enabled is not None:
        state.investment_enabled = bool(investment_enabled)
    if rubbish_recycling_enabled is not None:
        state.rubbish_recycling_enabled = bool(rubbish_recycling_enabled)
    envelope = dict(event or {})
    if str(envelope.get("name") or "") != TRADE_PROGRESS_EVENT:
        return state
    payload = envelope.get("payload")
    if not isinstance(payload, Mapping):
        return state
    payload = dict(payload)
    if str(payload.get("schema") or "") != TRADE_PROGRESS_SCHEMA:
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

    state.cid = cid or state.cid
    state.sequence = sequence
    stage = str(payload.get("stage") or "task")
    event_state = str(payload.get("state") or "running").lower()
    data = dict(payload.get("data") or {}) if isinstance(payload.get("data"), Mapping) else {}

    if stage in {"target", "city", "market", "planning"}:
        state.state = "running"
        state.preparation_detail = TRADE_STAGE_LABELS.get(stage, "准备货运")
        state.preparation_state = "completed" if stage == "planning" and event_state == "completed" else "running"
        if stage == "market" and data.get("source"):
            state.market_source = str(data["source"])
        if stage == "planning" and event_state == "completed":
            route = [dict(item) for item in data.get("route", []) if isinstance(item, Mapping)]
            state.route = route
            if isinstance(data.get("summary"), Mapping):
                state.summary = dict(data["summary"])
            state.cities = _build_freight_city_stages(
                route,
                state.investment_enabled,
                state.rubbish_recycling_enabled,
            )
        return state

    if stage == "task" and event_state in {"failed", "error", "cancelled"}:
        state.state = "cancelled" if event_state == "cancelled" else "failed"
        _fail_active_freight_phase(state, str(data.get("message") or "货运执行失败"))
        return state
    if stage == "route" and event_state in {"blocked", "failed", "error"}:
        state.state = "failed"
        _fail_active_freight_phase(state, str(data.get("reason") or "路线执行失败"))
        return state
    if stage in {"task", "route"} and event_state in {"completed", "success"}:
        state.state = "completed"
        state.preparation_state = "completed"
        for city in state.cities:
            for phase in city.phases:
                if phase.state == "waiting":
                    phase.state = "skipped"
        state.active_city_index = len(state.cities) - 1 if state.cities else None
        state.active_phase = ""
        return state
    if not state.cities:
        return state

    city_index = _freight_event_city_index(payload, stage, event_state, len(state.cities))
    phase_key = (
        "travel"
        if stage == "arrival" and event_state in {"blocked", "failed", "error"}
        else _freight_event_phase_key(payload, stage, city_index, len(state.cities))
    )
    if city_index is None or phase_key is None or not 0 <= city_index < len(state.cities):
        return state
    city = state.cities[city_index]
    phase = next((item for item in city.phases if item.key == phase_key), None)
    if phase is None:
        return state

    view_state = _freight_view_state(event_state)
    if stage == "negotiation":
        operation = str(payload.get("operation") or "")
        phase.detail = "售出 · 抬价中" if operation == "raise" else "购买 · 砍价中"
        if view_state == "failed":
            phase.state = "failed"
        elif phase.state not in {"completed", "skipped"}:
            phase.state = "running"
    else:
        phase.detail = ""
        phase.state = view_state

    if stage == "arrival" and event_state == "completed" and city_index > 0:
        previous_travel = next(
            (item for item in state.cities[city_index - 1].phases if item.key == "travel"), None
        )
        if previous_travel is not None:
            previous_travel.state = "completed"
    state.active_city_index = city_index
    state.active_phase = phase_key
    state.state = "failed" if view_state == "failed" else "running"
    return state


def _build_freight_city_stages(
    route: list[dict[str, Any]],
    investment_enabled: bool,
    rubbish_recycling_enabled: bool = True,
) -> list[FreightCityStage]:
    if not route:
        return []
    names = [str(route[0].get("from_city") or "起点")]
    names.extend(str(leg.get("to_city") or f"城市 {index + 2}") for index, leg in enumerate(route))
    cities: list[FreightCityStage] = []
    city_count = len(names)
    rubbish_city_index = next(
        (
            leg_index + 1
            for leg_index, leg in enumerate(route)
            if rubbish_recycling_enabled and _is_rubbish_recycling_city(leg)
        ),
        None,
    )
    for index, name in enumerate(names):
        role = "initial" if index == 0 else ("terminal" if index == city_count - 1 else "intermediate")
        keys: list[str] = []
        if index > 0:
            keys.append("arrival")
        if index > 0 and investment_enabled and _is_cape_city(name, route[index - 1]):
            keys.append("investment")
        if index == rubbish_city_index:
            keys.append("rubbish_recycling")
        keys.extend(["sell", "buy", "travel"] if index < city_count - 1 else ["final_sale"])
        phases = [FreightBusinessPhase(key=key, label=FREIGHT_PHASE_LABELS[key]) for key in keys]
        if index < len(route) and not list(route[index].get("buy_products") or []):
            for phase in phases:
                if phase.key == "buy":
                    phase.state = "skipped"
                    phase.detail = "本城无需购买"
        cities.append(FreightCityStage(index=index, count=city_count, name=name, role=role, phases=phases))
    return cities


def _is_cape_city(name: str, incoming_leg: Mapping[str, Any]) -> bool:
    return bool(
        str(name).strip() == "海角城"
        or str(incoming_leg.get("to_city_id") or "").strip() == "11"
        or str(incoming_leg.get("to_city_key") or "").strip().lower() == "cape_city"
    )


def _is_rubbish_recycling_city(incoming_leg: Mapping[str, Any]) -> bool:
    return bool(
        str(incoming_leg.get("to_city_id") or "").strip() in {"7", "14"}
        or str(incoming_leg.get("to_city_key") or "").strip().lower()
        in {"wilderness_station", "farstar_bridge"}
        or str(incoming_leg.get("to_city") or "").strip() in {"荒原站", "远星大桥"}
    )


def _freight_event_city_index(
    payload: Mapping[str, Any], stage: str, event_state: str, city_count: int
) -> int | None:
    explicit = _optional_int(payload.get("city_index"))
    if explicit is not None:
        return explicit
    leg_index = _optional_int(payload.get("leg_index"))
    if leg_index is None:
        return city_count - 1 if stage == "final_sale" else None
    if stage in {"arrival", "investment", "rubbish_recycling"} and event_state not in {
        "blocked",
        "failed",
        "error",
    }:
        return leg_index + 1
    return min(leg_index, city_count - 1)


def _freight_event_phase_key(
    payload: Mapping[str, Any], stage: str, city_index: int | None, city_count: int
) -> str | None:
    if stage == "negotiation":
        return "sell" if str(payload.get("operation") or "") == "raise" else "buy"
    if stage == "sell" and city_index == city_count - 1:
        return "final_sale"
    return stage if stage in FREIGHT_PHASE_LABELS else None


def _freight_view_state(state: str) -> str:
    if state in {"completed", "success"}:
        return "completed"
    if state in {"skipped"}:
        return "skipped"
    if state in {"blocked", "failed", "error", "cancelled"}:
        return "failed"
    return "running"


def _fail_active_freight_phase(state: WorkflowFreightProgressState, detail: str) -> None:
    if state.active_city_index is None or not 0 <= state.active_city_index < len(state.cities):
        return
    for phase in state.cities[state.active_city_index].phases:
        if phase.key == state.active_phase:
            phase.state = "failed"
            phase.detail = detail
            return

PASSENGER_STAGE_LABELS = {
    "resolve_start": "识别起点",
    "reposition": "前往线路端点",
    "trade": "中途倒货",
    "recruit": "传单揽客",
    "travel": "跨城行驶",
    "settlement": "客运结算",
    "final_sale": "终点清仓",
    "task": "客运任务",
}


@dataclass
class TradeProgressState:
    """Reduced, presentation-ready state for one PC trade run."""

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
    if str(envelope.get("name") or "") != TRADE_PROGRESS_EVENT:
        return state
    payload = envelope.get("payload")
    if not isinstance(payload, Mapping):
        return state
    payload = dict(payload)
    if str(payload.get("schema") or "") != TRADE_PROGRESS_SCHEMA:
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


@dataclass
class PassengerProgressState:
    """Reduced presentation state for one passenger run."""

    cid: str = ""
    sequence: int = -1
    stage: str = "resolve_start"
    state: str = "idle"
    trip_index: int | None = None
    leg_index: int | None = None
    leg_count: int = 0
    source_city: str = ""
    destination_city: str = ""
    recruited_count: int | None = None
    seat_capacity: int | None = None
    expected_fatigue_used: int = 0
    expected_fatigue_total: int = 0
    leg_revenue: int | None = None
    total_revenue: int = 0
    requires_manual_completion: bool = False
    last_data: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def stage_label(self) -> str:
        return PASSENGER_STAGE_LABELS.get(self.stage, self.stage or "待开始")


def reduce_passenger_progress(
    current: PassengerProgressState | None,
    event: Mapping[str, Any] | None,
    *,
    expected_cid: str = "",
) -> PassengerProgressState:
    state = current or PassengerProgressState(cid=str(expected_cid or ""))
    envelope = dict(event or {})
    if str(envelope.get("name") or "") != PASSENGER_PROGRESS_EVENT:
        return state
    payload = envelope.get("payload")
    if not isinstance(payload, Mapping):
        return state
    payload = dict(payload)
    if str(payload.get("schema") or "") != PASSENGER_PROGRESS_SCHEMA:
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
    return PassengerProgressState(
        cid=cid or state.cid,
        sequence=sequence,
        stage=str(payload.get("stage") or state.stage),
        state=str(payload.get("state") or state.state),
        trip_index=_optional_int(payload.get("trip_index")),
        leg_index=_optional_int(payload.get("leg_index")),
        leg_count=_int_or(payload.get("leg_count"), state.leg_count),
        source_city=str(payload.get("source_city") or state.source_city),
        destination_city=str(payload.get("destination_city") or state.destination_city),
        recruited_count=_optional_int(payload.get("recruited_count")),
        seat_capacity=_optional_int(payload.get("seat_capacity")),
        expected_fatigue_used=_int_or(payload.get("expected_fatigue_used"), state.expected_fatigue_used),
        expected_fatigue_total=_int_or(payload.get("expected_fatigue_total"), state.expected_fatigue_total),
        leg_revenue=_optional_int(payload.get("leg_revenue")),
        total_revenue=_int_or(payload.get("total_revenue"), state.total_revenue),
        requires_manual_completion=bool(
            payload.get("requires_manual_completion", state.requires_manual_completion)
        ),
        last_data=dict(payload.get("data") or {}) if isinstance(payload.get("data"), Mapping) else {},
        events=[*state.events, envelope],
    )


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
