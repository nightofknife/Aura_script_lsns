"""Single safe workshop operations; public task owns order and round budget."""
from __future__ import annotations

from typing import Any

from packages.aura_core.api import action_info, requires_services
from . import _raw_workshop_ui as ui
from . import _reforming_center_clicks as clicks
from . import reforming_center_pc_actions as layout
from .runtime_preflight_pc_actions import resonance_pc_require_client_resolution

POINTS = {
    "deposit": (400, 440), "deposit_max": (895, 380), "deposit_confirm": (960, 535),
    "work_tab": (730, 300), "reserve_tab": (1060, 300), "choose_prisoners": (885, 670),
    "select_all": (935, 670), "staff_confirm": (1150, 670), "start": (265, 675),
    "selection_back": (95, 40),
}
RAW_ROOMS = {"植物纤维原料车间", "动物纤维原料车间", "缝纫工具原料车间", "人造纤维原料车间"}


def _room(room_name, app):
    rooms = layout._load_reforming_center_room_layout()
    room = layout._resolve_room_key(room_name, rooms, layout._build_room_alias_lookup(rooms))
    if room not in RAW_ROOMS:
        raise ValueError("Only the four raw-material workshops are supported")
    resonance_pc_require_client_resolution(app=app)
    return room


def _fraction(value):
    if not isinstance(value, dict):
        return None
    current, capacity = value.get("current"), value.get("capacity")
    if (isinstance(current, bool) or isinstance(capacity, bool)
            or not isinstance(current, int) or not isinstance(capacity, int)
            or not 0 <= current <= capacity or capacity < 1):
        return None
    return {"current": current, "capacity": capacity, "full": current == capacity}


def _canonical(value, room):
    if not isinstance(value, dict) or value.get("production") not in {"idle", "running"}:
        return None
    work, reserve = _fraction(value.get("work")), _fraction(value.get("reserve"))
    if work is None or reserve is None or value.get("tab") not in {"work", "reserve"}:
        return None
    if not isinstance(value.get("start_enabled"), bool):
        return None
    return {"room_name": room, "production": value["production"], "start_enabled": value["start_enabled"],
            "work": work, "reserve": reserve, "tab": value["tab"]}


def _probe(image, room, *, tab=None, production=None):
    value = _canonical(ui.snapshot(image, room), room)
    if value is None or (tab is not None and value["tab"] != tab) or (production is not None and value["production"] != production):
        return None
    return value


def _snapshot(app, room):
    return clicks.wait_confirmed(app, lambda image: _probe(image, room))


def _selection(image):
    value = ui.selection_state(image)
    if not isinstance(value, dict):
        return None
    count = _fraction(value.get("count"))
    if count is None or value.get("button") not in {"select_all", "cancel_all"}:
        return None
    return {"count": count, "button": value["button"], "confirm_enabled": bool(value.get("confirm_enabled"))}


def _result(room, status, snapshot, data, reason="", safe=True, **extra):
    return {"success": status not in {"partial", "blocked", "failed"}, "status": status,
            "room_name": room, "reason": reason, "safe_to_continue": safe,
            "snapshot": snapshot, "data": data, **extra}


def _failed(room, data, exc, **extra):
    data["error"] = {"reason": getattr(exc, "reason", "operation_failed"),
                     "message": str(exc), "detail": getattr(exc, "data", {})}
    return _result(room, "failed", None, data, getattr(exc, "reason", "operation_failed"), safe=False, **extra)


def _click(app, name, data, effect, ready=None):
    record = {"control": name}
    data.setdefault("clicks", []).append(record)
    return clicks.checked_click(app, POINTS[name], effect, ready, record)


def _require(app, condition, reason):
    image = clicks.read_frame(app)
    if not condition(image):
        raise clicks.CheckedClickError(reason)
    return image


@action_info(name="resonance_pc.raw_workshop_observe", public=True, read_only=True,
             description="Read stable raw-workshop production and dynamic staffing without OCR or input.")
@requires_services(app="plans/aura_base/app")
def resonance_pc_raw_workshop_observe(room_name: str, app: Any = None):
    room, data = _room(room_name, app), {"clicks": []}
    try:
        state = _snapshot(app, room)
        return _result(room, "completed", state, data)
    except clicks.CheckedClickError as exc:
        return _failed(room, data, exc)


@action_info(name="resonance_pc.raw_workshop_deposit_max", public=True, read_only=False,
             description="Once: select maximum deposit or recognize full; never terminate running production.")
@requires_services(app="plans/aura_base/app")
def resonance_pc_raw_workshop_deposit_max(room_name: str, app: Any = None):
    room, data = _room(room_name, app), {"clicks": []}
    try:
        before = _snapshot(app, room)
        data["before"] = before
        if before["production"] == "running":
            return _result(room, "skipped_running", before, data)
        _require(app, lambda image: ui.room_ready(image, room) and ui.control_visible(image, "deposit_button"), "deposit_unavailable")
        click = _click(app, "deposit", data, lambda image: ui.deposit_outcome(image, room),
                       lambda image: ui.deposit_outcome(image, room))
        outcome = click["ready_evidence"]
        if outcome == "already_full":
            return _result(room, "already_full", _snapshot(app, room), data)
        if outcome != "dialog":
            raise clicks.CheckedClickError("deposit_outcome_unknown")
        if not ui.deposit_maximum(clicks.read_frame(app)):
            _require(app, lambda image: ui.deposit_outcome(image, room) == "dialog"
                     and ui.control_visible(image, "deposit_max"), "deposit_dialog_not_ready")
            _click(app, "deposit_max", data, lambda image: ui.deposit_maximum(image))
        _require(app, lambda image: ui.deposit_outcome(image, room) == "dialog"
                 and ui.deposit_maximum(image) and ui.control_enabled(image, "deposit_confirm"), "deposit_maximum_not_confirmed")
        data["deposit_submission_attempted"] = True
        committed = _click(app, "deposit_confirm", data,
                           lambda image: ui.room_ready(image, room) and ui.deposit_outcome(image, room) != "dialog",
                           lambda image: _probe(image, room))
        data["deposit_state"] = "submitted"
        data["deposit_amount"] = None  # No fabricated balance or confirmed cost.
        return _result(room, "submitted", committed["ready_evidence"], data)
    except clicks.CheckedClickError as exc:
        return _failed(room, data, exc)


def _cancel_selection(app, room, data):
    _require(app, lambda image: _selection(image) is not None
             and ui.control_visible(image, "selection_back"), "selection_cancel_unavailable")
    closed = _click(app, "selection_back", data, lambda image: ui.room_ready(image, room),
                    lambda image: _probe(image, room))
    return closed["ready_evidence"]


def _fill_list(app, room, kind, data):
    before = _snapshot(app, room)
    data["before"] = before
    if kind == "work" and before["production"] == "running":
        return _result(room, "skipped_running", before, data)
    if before[kind]["full"]:
        return _result(room, "already_full", before, data)
    state = before
    if state["tab"] != kind:
        _require(app, lambda image: ui.room_ready(image, room), "workshop_page_changed")
        selected = _click(app, kind+"_tab", data,
                          lambda image: _probe(image, room, tab=kind),
                          lambda image: _probe(image, room, tab=kind))
        state = selected["ready_evidence"]
    if kind == "work" and state["production"] == "running":
        return _result(room, "skipped_running", state, data)
    if state[kind]["full"]:
        return _result(room, "already_full", state, data)
    _require(app, lambda image: ((v := _probe(image, room, tab=kind)) is not None
             and (kind != "work" or v["production"] == "idle")
             and ui.control_visible(image, "choose_prisoners")), "staff_editor_unavailable")
    opened = _click(app, "choose_prisoners", data, lambda image: _selection(image), lambda image: _selection(image))
    choice = opened["ready_evidence"]
    if choice["count"]["capacity"] != state[kind]["capacity"]:
        raise clicks.CheckedClickError("staff_editor_capacity_mismatch")
    if choice["button"] == "select_all":
        prior_count = choice["count"]["current"]
        _require(app, lambda image: (_selection(image) or {}).get("button") == "select_all", "select_all_not_ready")
        selected = _click(app, "select_all", data,
                          lambda image: ((v := _selection(image)) is not None
                                         and (v["button"] == "cancel_all" or v["count"]["current"] > prior_count)),
                          lambda image: _selection(image))
        choice = selected["ready_evidence"]
    if choice["count"]["capacity"] != state[kind]["capacity"]:
        raise clicks.CheckedClickError("staff_editor_capacity_mismatch")
    if choice["count"]["current"] == 0 or choice["count"]["current"] < state[kind]["current"]:
        reason = "no_available_prisoners" if choice["count"]["current"] == 0 else "selection_would_reduce_staff"
        after = _cancel_selection(app, room, data)
        data.update(after=after, progress=False, submitted=False)
        return _result(room, "partial", after, data, reason)
    _require(app, lambda image: ((v := _selection(image)) is not None and v["confirm_enabled"]
             and v["count"]["capacity"] == state[kind]["capacity"]
             and v["count"]["current"] >= state[kind]["current"]), "staff_selection_not_ready")
    data["staff_submission_attempted"] = True
    saved = _click(app, "staff_confirm", data, lambda image: ui.room_ready(image, room),
                   lambda image: _probe(image, room, tab=kind))
    after = saved["ready_evidence"]
    data.update(after=after, submitted=True,
                progress=(after[kind]["current"] > before[kind]["current"]
                          or after["work"]["current"] > before["work"]["current"]))
    if after[kind]["full"]:
        return _result(room, "completed", after, data)
    return _result(room, "partial", after, data, "staff_not_full")


@action_info(name="resonance_pc.raw_workshop_fill_list_once", public=True, read_only=False,
             description="Once: fill work or reserve list using native one-click selection and verified confirmation.")
@requires_services(app="plans/aura_base/app")
def resonance_pc_raw_workshop_fill_list_once(room_name: str, list_kind: str = "work", app: Any = None):
    if list_kind not in {"work", "reserve"}:
        raise ValueError("list_kind must be work or reserve")
    room, data = _room(room_name, app), {"clicks": [], "list_kind": list_kind}
    try:
        return _fill_list(app, room, list_kind, data)
    except clicks.CheckedClickError as exc:
        return _failed(room, data, exc)


def _transfer_window(app, room, previous):
    last, count = None, 0
    for _ in range(10):
        clicks._sleep(.5)
        state = _probe(clicks.read_frame(app), room)
        count = count+1 if state is not None and state == last else (1 if state is not None else 0)
        last = state
        if count >= 2 and (state["work"]["current"] > previous["work"]["current"]
                           or state["reserve"]["current"] < previous["reserve"]["current"]
                           or (state["work"]["full"] and state["reserve"]["full"])):
            return state
    if count >= 2 and last is not None:
        return last
    raise clicks.CheckedClickError("staff_snapshot_unknown")


@action_info(name="resonance_pc.raw_workshop_reserve_round", public=True, read_only=False,
             description="One reserve refill/transfer observation round; task YAML owns the finite round chain.")
@requires_services(app="plans/aura_base/app")
def resonance_pc_raw_workshop_reserve_round(room_name: str, app: Any = None):
    room, data = _room(room_name, app), {"clicks": [], "list_kind": "reserve"}
    try:
        before = _snapshot(app, room)
        data["round_before"] = before
        if before["work"]["full"] and before["reserve"]["full"]:
            return _result(room, "completed", before, data, continue_reserve=False)
        result = _fill_list(app, room, "reserve", data)
        after = result["snapshot"]
        if after["production"] == "idle":
            result["continue_reserve"] = False
            return result
        if result["reason"] in {"no_available_prisoners", "selection_would_reduce_staff"}:
            result["continue_reserve"] = False
            return result
        after = _transfer_window(app, room, after)
        data["after_transfer"] = after
        progressed = (after["work"]["current"] > before["work"]["current"]
                      or after["reserve"]["current"] > before["reserve"]["current"])
        complete = after["work"]["full"] and after["reserve"]["full"]
        again = (not complete and after["production"] == "running"
                 and not after["reserve"]["full"] and progressed)
        reason = "" if complete else ("staff_not_full" if again else "staff_no_progress")
        return _result(room, "completed" if complete else "partial", after, data, reason,
                       continue_reserve=again)
    except clicks.CheckedClickError as exc:
        return _failed(room, data, exc, continue_reserve=False)


@action_info(name="resonance_pc.raw_workshop_start_once", public=True, read_only=False,
             description="Start current recipe at most once when enabled; never stop or accelerate existing production.")
@requires_services(app="plans/aura_base/app")
def resonance_pc_raw_workshop_start_once(room_name: str, app: Any = None):
    room, data = _room(room_name, app), {"clicks": []}
    try:
        state = _snapshot(app, room)
        data["before"] = state
        if state["production"] == "running":
            return _result(room, "already_running", state, data)
        if not state["start_enabled"]:
            return _result(room, "blocked", state, data, "start_disabled")
        _require(app, lambda image: ui.room_ready(image, room) and ui.control_enabled(image, "start_enabled"), "start_not_ready")
        data["start_attempted"] = True
        started = _click(app, "start", data,
                         lambda image: ui.room_ready(image, room) and ui.production_state(image) == "running",
                         lambda image: _probe(image, room, production="running"))
        return _result(room, "completed", started["ready_evidence"], data)
    except clicks.CheckedClickError as exc:
        return _failed(room, data, exc)


@action_info(name="resonance_pc.raw_workshop_summarize", public=True, read_only=True,
             description="Summarize actual business snapshots and confirmations, never framework node success.")
def resonance_pc_raw_workshop_summarize(room_name: str, navigation=None, observation=None,
                                      deposit=None, work=None, reserve_rounds=None, start=None,
                                      max_reserve_rounds: int = 8, before_start=None, final_observe=None):
    if (isinstance(max_reserve_rounds, bool) or not isinstance(max_reserve_rounds, int)
            or not 1 <= max_reserve_rounds <= 8):
        raise ValueError("max_reserve_rounds must be an integer from 1 to 8")
    rounds = [item for item in (reserve_rounds or []) if isinstance(item, dict)]
    operations = [item for item in [observation, deposit, work, *rounds, before_start, start, final_observe]
                  if isinstance(item, dict) and item]
    failed = next((item for item in operations if not item.get("safe_to_continue", False)), None)
    last = next((item.get("snapshot") for item in reversed(operations) if item.get("snapshot") is not None), None)
    navigation_ok = isinstance(navigation, dict) and navigation.get("room_entered") is True
    snapshot = last if navigation_ok and failed is None else None
    if not navigation_ok:
        status, reason = "failed", (navigation or {}).get("reason", "room_entry_failed")
    elif failed is not None:
        status, reason = "failed", failed.get("reason", "operation_unconfirmed")
    elif snapshot is None:
        status, reason = "failed", "final_snapshot_unknown"
    elif snapshot["production"] == "idle":
        status, reason = "blocked", (start or {}).get("reason") or "production_not_started"
    elif snapshot["work"]["full"] and snapshot["reserve"]["full"]:
        status, reason = "completed", ""
    else:
        exhausted = len(rounds) >= max_reserve_rounds and rounds[-1].get("continue_reserve", False)
        status, reason = "partial", "reserve_round_limit" if exhausted else "staff_not_full"
    return {"success": status == "completed", "status": status, "room_name": room_name, "reason": reason,
            "room_entered": navigation_ok, "snapshot": snapshot,
            "data": {"navigation": navigation, "deposit": deposit,
                     "work": work, "reserve_rounds": rounds, "reserve_round_count": len(rounds),
                     "before_start": before_start, "start": start, "final_observe": final_observe,
                     "last_confirmed_snapshot": last}}
