"""Single safe workshop operations; public task owns order and round budget."""
from __future__ import annotations

import asyncio

from typing import Any

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested
from . import _raw_workshop_ui as ui
from . import _reforming_center_clicks as clicks
from . import reforming_center_pc_actions as layout
from .runtime_preflight_pc_actions import resonance_pc_require_client_resolution

POINTS = {
    "deposit": (400, 440), "deposit_max": (895, 380), "deposit_confirm": (960, 535),
    "work_tab": (730, 300), "reserve_tab": (1060, 300), "choose_prisoners": (885, 670),
    "select_all": (935, 670), "staff_confirm": (1150, 670), "start": (265, 675),
    "selection_back": (95, 40),
    "return_overview": (95, 40),
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
    data["submission_snapshot"] = saved["ready_evidence"]
    # Re-read both counters from new stable frames after confirmation.
    after = _snapshot(app, room)
    data.update(after=after, submitted=True,
                progress=(after[kind]["current"] > before[kind]["current"]
                          or after["work"]["current"] > before["work"]["current"]))
    decrease = _staff_decrease(before, after)
    if decrease:
        return _result(room, "partial", after, data, decrease, stop_staff_fill=True)
    if after[kind]["full"]:
        return _result(room, "completed", after, data)
    return _result(room, "partial", after, data, "staff_not_full")


def _staff_decrease(before, after):
    for kind in ("work", "reserve"):
        if before[kind]["capacity"] != after[kind]["capacity"]:
            raise clicks.CheckedClickError("staff_capacity_changed", {"before": before, "after": after})
    # Reserve -> work is an expected transition during production. A loss of
    # workers, or moving reserve workers while idle, must not trigger retries.
    if after["work"]["current"] < before["work"]["current"]:
        return "work_staff_decreased"
    if after["production"] == "idle" and after["reserve"]["current"] < before["reserve"]["current"]:
        return "reserve_staff_decreased"
    return ""


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
             description="One reserve refill and stable transfer observation; the staffing loop owns retries.")
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
        if result.get("stop_staff_fill") or result["reason"] in {"no_available_prisoners", "selection_would_reduce_staff"}:
            result["continue_reserve"] = False
            return result
        if after["production"] == "running":
            after = _transfer_window(app, room, after)
            data["after_transfer"] = after
        decrease = _staff_decrease(before, after)
        if decrease:
            return _result(room, "partial", after, data, decrease,
                           continue_reserve=False, stop_staff_fill=True)
        progressed = (after["work"]["current"] > before["work"]["current"]
                      or after["reserve"]["current"] > before["reserve"]["current"])
        data.update(after=after, progress=progressed)
        complete = after["reserve"]["full"]
        again = not complete
        reason = "" if complete else "staff_not_full"
        return _result(room, "completed" if complete else "partial", after, data, reason,
                       continue_reserve=again)
    except clicks.CheckedClickError as exc:
        return _failed(room, data, exc, continue_reserve=False)


@action_info(name="resonance_pc.raw_workshop_fill_list_until_full", public=True, read_only=False,
             description="Fill a staffing list with stable rereads, a shared round budget and bounded no-progress retries.")
@requires_services(app="plans/aura_base/app")
def resonance_pc_raw_workshop_fill_list_until_full(room_name: str, list_kind: str = "work",
                                                  max_rounds: int = 8, previous_result=None,
                                                  app: Any = None):
    with clicks.cancellation_checks(_check_return_cancelled):
        return _fill_staffing_list(room_name, list_kind, max_rounds, previous_result, app)


def _fill_staffing_list(room_name, list_kind, max_rounds, previous_result, app):
    if list_kind not in {"work", "reserve"}:
        raise ValueError("list_kind must be work or reserve")
    if (isinstance(max_rounds, bool) or not isinstance(max_rounds, int)
            or not 1 <= max_rounds <= 8):
        raise ValueError("max_rounds must be an integer from 1 to 8")
    room = _room(room_name, app)
    if previous_result is not None and not isinstance(previous_result, dict):
        raise ValueError("previous_result must be a staffing-loop result")
    previous_data = (previous_result or {}).get("data", {})
    if not isinstance(previous_data, dict):
        raise ValueError("previous_result.data must be an object")
    prior_count = previous_data.get("round_count", 0)
    if previous_result is not None and (
            not isinstance(previous_result, dict) or previous_result.get("room_name") != room
            or previous_data.get("list_kind") != list_kind
            or previous_data.get("max_rounds") != max_rounds):
        raise ValueError("previous_result must belong to the same room, list and round budget")
    if (isinstance(prior_count, bool) or not isinstance(prior_count, int)
            or not 0 <= prior_count <= max_rounds):
        raise ValueError("previous_result exceeds the round budget")
    data = {"clicks": [], "list_kind": list_kind, "max_rounds": max_rounds,
            "rounds": [], "prior_round_count": prior_count,
            "round_count": prior_count, "no_progress_streak": 0}
    try:
        before = _snapshot(app, room)
        data["before"] = before
        if previous_result is not None:
            if not previous_result.get("safe_to_continue", False):
                raise clicks.CheckedClickError("previous_staff_result_unconfirmed")
            if previous_result.get("stop_staff_fill"):
                return _result(room, "partial", before, data, previous_result["reason"], stop_staff_fill=True)
        if list_kind == "work" and before["production"] == "running":
            return _result(room, "skipped_running", before, data)
        state = before
        if list_kind == "reserve" and state["production"] == "running":
            # A full reserve tab may be about to transfer workers into empty
            # work slots. Observe that transition before declaring it full.
            state = _transfer_window(app, room, state)
            decrease = _staff_decrease(before, state)
            if decrease:
                return _result(room, "partial", state, data, decrease, stop_staff_fill=True)
        no_progress = 0
        if previous_result is not None:
            # Starting production can transfer reserves, so count progress from
            # the last phase before carrying a no-progress streak forward.
            old = previous_result.get("snapshot")
            decrease = _staff_decrease(old, state) if old is not None else ""
            if decrease:
                return _result(room, "partial", state, data, decrease, stop_staff_fill=True)
            gained = old is not None and any(state[k]["current"] > old[k]["current"] for k in ("work", "reserve"))
            if not gained:
                no_progress = int(previous_data.get("no_progress_streak", 0))
        if state[list_kind]["full"]:
            return _result(room, "already_full", state, data)
        if previous_result is not None and previous_result.get("reason") == "no_available_prisoners":
            return _result(room, "partial", state, data, "no_available_prisoners")
        data["no_progress_streak"] = no_progress
        if no_progress >= 2:
            return _result(room, "partial", state, data, "staff_no_progress")
        while data["round_count"] < max_rounds:
            if is_current_task_cancel_requested():
                raise asyncio.CancelledError("Raw-workshop staffing cancelled")
            round_before = state
            action = (resonance_pc_raw_workshop_reserve_round if list_kind == "reserve"
                      else resonance_pc_raw_workshop_fill_list_once)
            result = action(room_name=room, app=app)
            data["rounds"].append(result)
            data["round_count"] += 1
            data["clicks"].extend(result.get("data", {}).get("clicks", []))
            if not result.get("safe_to_continue", False) or result.get("snapshot") is None:
                raise clicks.CheckedClickError(result.get("reason") or "staff_snapshot_unknown", {"round": result})
            state = result["snapshot"]
            data["after"] = state
            progressed = any(state[k]["current"] > round_before[k]["current"]
                             for k in (("work",) if list_kind == "work" else ("work", "reserve")))
            no_progress = 0 if progressed else no_progress + 1
            data["no_progress_streak"] = no_progress
            result["data"].update(round_number=data["round_count"], progress=progressed,
                                  no_progress_streak=no_progress)
            decrease = _staff_decrease(round_before, state)
            if decrease or result.get("stop_staff_fill") or result.get("reason") == "selection_would_reduce_staff":
                return _result(room, "partial", state, data, decrease or result["reason"], stop_staff_fill=True)
            if state[list_kind]["full"]:
                return _result(room, "completed", state, data)
            if result.get("reason") == "no_available_prisoners":
                return _result(room, "partial", state, data, "no_available_prisoners")
            if list_kind == "work" and state["production"] == "running":
                return _result(room, "skipped_running", state, data)
            if no_progress >= 2:
                return _result(room, "partial", state, data, "staff_no_progress")
        return _result(room, "partial", state, data, list_kind + "_round_limit")
    except clicks.CheckedClickError as exc:
        return _failed(room, data, exc)


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


def _check_return_cancelled():
    if is_current_task_cancel_requested():
        raise asyncio.CancelledError("Raw-workshop operation cancelled")


@action_info(name="resonance_pc.raw_workshop_return_overview", public=True, read_only=False,
             description="From a confirmed target workshop, click back once and confirm overview; no selection-outline checks or background clicks.")
@requires_services(app="plans/aura_base/app")
def resonance_pc_raw_workshop_return_overview(room_name: str, app: Any = None):
    with clicks.cancellation_checks(_check_return_cancelled):
        room, data = _room(room_name, app), {"clicks": []}
        try:
            data["before"] = _snapshot(app, room)
            _check_return_cancelled()
            _require(app, lambda image: ui.room_ready(image, room)
                     and ui.control_visible(image, "selection_back"), "workshop_back_not_ready")
            _check_return_cancelled()
            returned = _click(app, "return_overview", data,
                              lambda image: ui.overview_state(image),
                              lambda image: ui.overview_state(image))
            _check_return_cancelled()
            data["overview"] = returned["ready_evidence"]
            return _result(room, "completed", None, data, returned_to_overview=True,
                           page_after="reforming_center_overview")
        except clicks.CheckedClickError as exc:
            return _failed(room, data, exc, returned_to_overview=False, page_after=None)


@action_info(name="resonance_pc.raw_workshop_summarize", public=True, read_only=True,
             description="Summarize actual business snapshots and confirmations, never framework node success.")
def resonance_pc_raw_workshop_summarize(room_name: str, navigation=None, observation=None,
                                      deposit=None, work=None, reserve_rounds=None, start=None,
                                      max_reserve_rounds: int = 8, before_start=None, final_observe=None,
                                      return_overview=None, require_return: bool = False,
                                      reserve_after_start=None):
    if (isinstance(max_reserve_rounds, bool) or not isinstance(max_reserve_rounds, int)
            or not 1 <= max_reserve_rounds <= 8):
        raise ValueError("max_reserve_rounds must be an integer from 1 to 8")
    rounds = [item for item in (reserve_rounds or []) if isinstance(item, dict)]
    operations = [item for item in [observation, deposit, work, *rounds, before_start, start, reserve_after_start, final_observe]
                  if isinstance(item, dict) and item]
    failed = next((item for item in operations if not item.get("safe_to_continue", False)), None)
    staff_stopped = next((item for item in operations if item.get("stop_staff_fill")), None)
    last = next((item.get("snapshot") for item in reversed(operations) if item.get("snapshot") is not None), None)
    navigation_ok = isinstance(navigation, dict) and navigation.get("room_entered") is True
    snapshot = last if navigation_ok and failed is None else None
    if not navigation_ok:
        status, reason = "failed", (navigation or {}).get("reason", "room_entry_failed")
    elif failed is not None:
        status, reason = "failed", failed.get("reason", "operation_unconfirmed")
    elif snapshot is None:
        status, reason = "failed", "final_snapshot_unknown"
    elif staff_stopped is not None:
        status, reason = "partial", staff_stopped.get("reason", "staff_fill_stopped")
    elif snapshot["production"] == "idle":
        status, reason = "blocked", (start or {}).get("reason") or "production_not_started"
    elif snapshot["work"]["full"] and snapshot["reserve"]["full"]:
        status, reason = "completed", ""
    else:
        loop_results = [item for item in [work, *rounds, reserve_after_start] if isinstance(item, dict)]
        latest_reason = next((item.get("reason") for item in reversed(loop_results)
                              if item.get("status") == "partial" and item.get("reason")), "staff_not_full")
        exhausted = len(rounds) >= max_reserve_rounds and rounds[-1].get("continue_reserve", False)
        status, reason = "partial", "reserve_round_limit" if exhausted else latest_reason
    maintenance = {"success": status == "completed", "status": status,
                   "reason": reason, "snapshot": snapshot}
    returned = (isinstance(return_overview, dict) and return_overview.get("success") is True
                and return_overview.get("returned_to_overview") is True)
    page_after = "reforming_center_overview" if returned else ("raw_material_workshop" if snapshot is not None else None)
    if require_return and status != "failed" and not returned:
        status = "failed"
        reason = (return_overview or {}).get("reason") or "overview_return_not_confirmed"
        snapshot, page_after = None, None
    return {"success": status == "completed", "status": status, "room_name": room_name, "reason": reason,
            "room_entered": navigation_ok, "snapshot": snapshot,
            "returned_to_overview": returned, "page_after": page_after,
            "data": {"navigation": navigation, "deposit": deposit,
                     "work": work, "reserve_rounds": rounds,
                     "work_round_count": (work or {}).get("data", {}).get("round_count", 0),
                     "reserve_round_count": ((reserve_after_start or (rounds[-1] if rounds else {}))
                                             .get("data", {}).get("round_count", len(rounds))),
                     "reserve_after_start": reserve_after_start,
                     "before_start": before_start, "start": start, "final_observe": final_observe,
                     "last_confirmed_snapshot": last, "maintenance": maintenance,
                     "return_overview": return_overview}}
