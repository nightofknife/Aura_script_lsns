"""One template-gated operation per DAG step; no OCR or gameplay API calls."""
from __future__ import annotations

from copy import deepcopy
import asyncio

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested
from . import _product_workshop_staff_ui as ui
from . import _reforming_center_clicks as clicks
from .runtime_preflight_pc_actions import resonance_pc_require_client_resolution

STEPS = ("edit", "quick", "select_all", "confirm", "request_save", "apply", "return_overview")


@action_info(name="resonance_pc.product_workshop_staff_assets", public=True, read_only=True,
             description="Validate native production staffing templates before navigation; no game input.")
def resonance_pc_product_workshop_staff_assets():
    try:
        ui.validate_assets()
        return {"success": True, "reason": ""}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"success": False, "reason": "staff_templates_invalid", "error": str(exc)}


def _read(app, room):
    return ui.observe(clicks.read_frame(app), room)


def _wait(app, room, predicate):
    def probe(image):
        value = ui.observe(image, room)
        return value if value and predicate(value) else None
    # A matching observation is sufficient; do not require a transient prompt
    # or toast to survive a second recognition.
    return clicks.wait_confirmed(app, probe, timeout_sec=6, poll_sec=.4, consecutive=1)


def _click(app, room, result, control, predicate, image):
    point = ui.edit_point(image, room) if control == "edit" else ui.point(image, control)
    if point is None:
        raise clicks.CheckedClickError("staff_control_not_recognized", {"control": control})
    record = {"control": control}
    result["clicks"].append(record)
    clicks.send_click(app, point, record)
    value = _wait(app, room, predicate)
    record.update(effect_confirmed=True, transition_ready=True, effect_evidence=value)
    result["page_after"] = value["page"]
    return value


@action_info(name="resonance_pc.product_workshop_staff_step", public=True,
             description="Perform one native quick-formation staffing step with fresh template checks and a single click.")
@requires_services(app="plans/aura_base/app")
def resonance_pc_product_workshop_staff_step(room_name: str, step: str, previous: dict, app=None):
    with clicks.cancellation_checks(_check_cancelled):
        return _staff_step(room_name, step, previous, app)


def _check_cancelled():
    if is_current_task_cancel_requested():
        raise asyncio.CancelledError("Production-workshop staffing cancelled")


def _staff_step(room_name, step, previous, app):
    if room_name not in ui.ROOMS or step not in STEPS:
        raise ValueError("Unsupported production workshop or staffing step")
    result = deepcopy(previous) if isinstance(previous, dict) else {}
    result.update(room_name=room_name, step=step, safe_to_continue=False, success=False, reason="")
    result.setdefault("clicks", [])
    result.setdefault("applied", False)
    result.setdefault("returned_to_overview", False)
    if not isinstance(previous, dict) or previous.get("room_name") != room_name:
        result["reason"] = "staff_previous_result_invalid"
        return result
    if step == "edit":
        allowed = previous.get("success") is True and previous.get("room_entered") is True
    else:
        allowed = previous.get("safe_to_continue") is True and previous.get("step") == STEPS[STEPS.index(step)-1]
    if not allowed:
        result["reason"] = "staff_previous_step_incomplete"
        return result
    try:
        resonance_pc_require_client_resolution(app=app)
        image = clicks.read_frame(app)
        before = ui.observe(image, room_name)
        if before is None:
            raise clicks.CheckedClickError("staff_page_unknown")
        result["page_after"] = before["page"]
        if step == "edit":
            if before["page"] != "product_workshop":
                raise clicks.CheckedClickError("staff_workshop_not_confirmed")
            after = _click(app, room_name, result, "edit", lambda v: v["page"] == "editor" and v.get("count") is not None, image)
            result["editor_initial_count"] = after["count"]
        elif step == "quick":
            if before["page"] != "editor":
                raise clicks.CheckedClickError("staff_editor_not_confirmed")
            after = _click(app, room_name, result, "quick", lambda v: v["page"] == "selection" and v.get("count") is not None and v.get("button") is not None, image)
            if after["count"] != result["editor_initial_count"]:
                raise clicks.CheckedClickError("staff_initial_count_mismatch")
            result.update(initial_count=after["count"], selected_count=after["count"], capacity=after["capacity"])
        elif step == "select_all":
            if before["page"] != "selection" or before.get("count") != result["initial_count"] or before.get("capacity") != result["capacity"]:
                raise clicks.CheckedClickError("staff_selection_changed")
            if before["count"] == before["capacity"]:
                result["selection_outcome"] = "already_full"
            else:
                if before.get("button") != "select_all":
                    raise clicks.CheckedClickError("staff_select_all_not_available")
                # Require visible progress. If native selection finds no new
                # candidate, stop without confirming or clicking cancellation.
                point = ui.point(image, "select_all")
                if point is None:
                    raise clicks.CheckedClickError("staff_control_not_recognized")
                record = {"control": "select_all"}
                result["clicks"].append(record)
                clicks.send_click(app, point, record)
                after = _wait(app, room_name, lambda v: v["page"] == "selection" and v.get("capacity") == result["capacity"] and v.get("count") is not None and v["count"] > result["initial_count"])
                record.update(effect_confirmed=True, transition_ready=True, effect_evidence=after)
                result.update(selected_count=after["count"], selection_outcome="full" if after["count"] == after["capacity"] else "partial")
        elif step == "confirm":
            if before["page"] != "selection" or before.get("count") != result["selected_count"] or before.get("capacity") != result["capacity"]:
                raise clicks.CheckedClickError("staff_selection_changed")
            if before["count"] <= 0:
                raise clicks.CheckedClickError("staff_empty_selection")
            after = _click(app, room_name, result, "confirm", lambda v: v["page"] == "editor" and v.get("count") == result["selected_count"], image)
            result.update(confirmed=True, local_count=after["count"])
        elif step == "request_save":
            if before["page"] != "editor" or before.get("count") != result["selected_count"] or result.get("confirmed") is not True:
                raise clicks.CheckedClickError("staff_confirm_not_verified")
            changed = result["selected_count"] > result["initial_count"]
            after = _click(app, room_name, result, "back", lambda v: v["page"] == "save_prompt" or (not changed and v["page"] in {"product_workshop", "reforming_center_overview"}), image)
            result["save_required"] = after["page"] == "save_prompt"
        elif step == "apply":
            if result.get("save_required"):
                if before["page"] != "save_prompt":
                    raise clicks.CheckedClickError("staff_save_prompt_not_confirmed")
                after = _click(app, room_name, result, "apply", lambda v: v["page"] in {"product_workshop", "editor"} and (v["page"] != "editor" or v.get("count") == result["selected_count"]), image)
                # Native confirmCallback sends an async save and immediately
                # GoBack(). Page return proves submission, not server persistence.
                result.update(applied=True, save_verified=False, page_after=after["page"])
            elif before["page"] not in {"product_workshop", "reforming_center_overview"}:
                raise clicks.CheckedClickError("staff_no_change_exit_not_confirmed")
        elif step == "return_overview":
            if before["page"] == "reforming_center_overview":
                after = before
            elif before["page"] in {"product_workshop", "editor"}:
                after = _click(app, room_name, result, "back", lambda v: v["page"] == "reforming_center_overview", image)
            else:
                raise clicks.CheckedClickError("staff_saved_page_not_confirmed")
            result.update(returned_to_overview=True, page_after=after["page"])
        result.update(success=True, safe_to_continue=True)
    except clicks.CheckedClickError as exc:
        # No fallback/back/submit clicks after an unknown state or missed input.
        result.update(reason=exc.reason, error_data=exc.data)
        try:
            value = _read(app, room_name)
            result["page_after"] = value["page"] if value else None
        except clicks.CheckedClickError:
            result["page_after"] = None
    return result


@action_info(name="resonance_pc.product_workshop_staff_summarize", public=True, read_only=True,
             description="Summarize production staffing counts, save evidence and final overview; no game input.")
def resonance_pc_product_workshop_staff_summarize(room_name: str, results: list, preflight: dict = None, navigation: dict = None):
    rows = [value for value in results if isinstance(value, dict)]
    last = rows[-1] if rows else {}
    initial, selected, capacity = last.get("initial_count"), last.get("local_count"), last.get("capacity")
    counts = (all(type(value) is int for value in (initial, selected, capacity))
              and 0 <= initial <= selected <= capacity and capacity > 0)
    returned = last.get("returned_to_overview") is True and last.get("page_after") == "reforming_center_overview"
    complete_steps = len(results) == len(STEPS) and len(rows) == len(STEPS) and all(row.get("success") is True and row.get("safe_to_continue") is True and row.get("room_name") == room_name and row.get("step") == step for row, step in zip(rows, STEPS))
    submitted = counts and (selected == initial or last.get("applied") is True)
    if not complete_steps or not counts or not submitted or not returned:
        status = "failed"
        reason = next((row.get("reason") for row in rows if row.get("reason")), "staff_completion_not_confirmed")
        if preflight is not None and preflight.get("success") is not True:
            reason = preflight.get("reason") or "staff_templates_invalid"
        elif navigation is not None and (navigation.get("success") is not True or navigation.get("room_entered") is not True):
            reason = navigation.get("reason") or "staff_navigation_not_confirmed"
    else:
        status = "partial" if selected < capacity else "submitted" if last.get("applied") else "completed"
        reason = "staff_not_full" if status == "partial" else "save_persistence_unverified" if status == "submitted" else ""
    unchanged = counts and selected == initial
    # No extra reopen/readback is added to the user-confirmed native workflow.
    # Only an unchanged roster has a known persisted count in this version.
    final = initial if unchanged and complete_steps and returned else None
    return {"room_name": room_name, "success": status in {"completed", "submitted"}, "status": status, "reason": reason,
            "initial_count": initial, "selected_count": selected, "final_count": final, "capacity": capacity,
            "selected_added_count": selected-initial if counts else None,
            "added_count": 0 if final is not None else None,
            "remaining_count": capacity-selected if counts else None,
            "count_scope": "local_formation", "save_verified": False,
            "applied": last.get("applied") is True, "returned_to_overview": returned,
            "page_after": last.get("page_after"), "data": {"steps": rows, "clicks": last.get("clicks", []), "preflight": preflight, "navigation": navigation}}
