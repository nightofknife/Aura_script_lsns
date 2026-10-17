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
_CLICK_WAIT_SEC = 1.0
_MAX_CLICK_ATTEMPTS = 3


def _sleep(seconds):
    clicks._sleep(seconds)


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
    """Task-private retries. Shared click/navigation behavior is untouched."""
    origin = ui.observe(image, room)
    if origin is None:
        raise clicks.CheckedClickError("staff_page_unknown")
    origin_page = origin["page"]
    for attempt in range(1, _MAX_CLICK_ATTEMPTS+1):
        point = ui.edit_point(image, room) if control == "edit" else ui.point(image, control)
        if point is None:
            raise clicks.CheckedClickError("staff_control_not_recognized", {"control": control})
        record = {"control":control, "step":result["step"], "attempt":attempt,
                  "origin_page":origin_page, "wait_sec":_CLICK_WAIT_SEC}
        result["clicks"].append(record)
        clicks.send_click(app, point, record)
        _sleep(_CLICK_WAIT_SEC)
        image = clicks.read_frame(app)
        value = ui.observe(image, room)
        record["page_after_wait"] = value["page"] if value else None
        # A back icon may persist on the next page. Destination wins before
        # inspecting that icon, preventing an accidental second back operation.
        if value and predicate(value):
            record.update(effect_confirmed=True, transition_ready=True, effect_evidence=value)
            result["page_after"] = value["page"]
            return value
        if (control == "confirm" and value is not None
                and value["page"] == "selection" and value.get("button") != "cancel_all"):
            raise clicks.CheckedClickError("staff_selection_button_changed")
        same_page = value is not None and value["page"] == origin_page
        point_after = ((ui.edit_point(image, room) if control == "edit" else ui.point(image, control))
                       if same_page else None)
        record.update(origin_page_present=same_page, origin_button_present=point_after is not None)
        if not same_page or point_after is None:
            # Source disappeared or became unknown: wait for the expected next
            # page without sending any more input, including if it reappears.
            record["retry_stopped"] = "origin_disappeared_or_unknown"
            value = _wait(app, room, predicate)
            record.update(effect_confirmed=True, transition_ready=True, effect_evidence=value)
            result["page_after"] = value["page"]
            return value
        if attempt == _MAX_CLICK_ATTEMPTS:
            raise clicks.CheckedClickError("staff_click_retry_exhausted",
                                           {"control":control, "attempts":attempt, "origin_page":origin_page})
        # image is the fresh post-wait capture. Locate again on the next pass;
        # never reuse the previous click coordinates for the supplementary click.
        record["retry_scheduled"] = True


@action_info(name="resonance_pc.product_workshop_staff_step", public=True,
             description="Perform one production staffing step; wait 1 second and retry only its still-visible source button, at most 3 clicks.")
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
    result.update(room_name=room_name, step=step, status="failed", safe_to_continue=False, success=False, reason="")
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
            _click(app, room_name, result, "edit", lambda v: v["page"] == "editor", image)
        elif step == "quick":
            if before["page"] != "editor":
                raise clicks.CheckedClickError("staff_editor_not_confirmed")
            _click(app, room_name, result, "quick", lambda v: v["page"] == "selection" and v.get("button") in {"select_all","cancel_all"}, image)
        elif step == "select_all":
            if before["page"] != "selection" or before.get("button") not in {"select_all","cancel_all"}:
                raise clicks.CheckedClickError("staff_selection_not_confirmed")
            if before["button"] == "cancel_all":
                result["selection_action"] = "unchanged"
            else:
                _click(app, room_name, result, "select_all", lambda v: v["page"] == "selection" and v.get("button") == "cancel_all", image)
                result["selection_action"] = "selected"
        elif step == "confirm":
            if before["page"] != "selection" or before.get("button") not in {"select_all","cancel_all"}:
                raise clicks.CheckedClickError("staff_selection_not_confirmed")
            if result.get("selection_action") == "unchanged":
                if before["button"] != "cancel_all":
                    raise clicks.CheckedClickError("staff_selection_button_changed")
                _click(app, room_name, result, "back", lambda v: v["page"] in {"editor","product_workshop","reforming_center_overview"}, image)
                result["confirmed"] = False
            elif result.get("selection_action") == "selected":
                if before["button"] != "cancel_all":
                    raise clicks.CheckedClickError("staff_selection_button_changed")
                _click(app, room_name, result, "confirm", lambda v: v["page"] == "editor", image)
                result["confirmed"] = True
            else:
                raise clicks.CheckedClickError("staff_selection_action_unknown")
        elif step == "request_save":
            if result.get("selection_action") == "unchanged":
                if before["page"] == "editor":
                    _click(app, room_name, result, "back", lambda v: v["page"] in {"product_workshop","reforming_center_overview"}, image)
                elif before["page"] not in {"product_workshop","reforming_center_overview"}:
                    raise clicks.CheckedClickError("staff_no_change_exit_not_confirmed")
                result["save_required"] = False
            elif before["page"] == "editor" and result.get("selection_action") == "selected" and result.get("confirmed") is True:
                _click(app, room_name, result, "back", lambda v: v["page"] == "save_prompt", image)
                result["save_required"] = True
            else:
                raise clicks.CheckedClickError("staff_confirm_not_verified")
        elif step == "apply":
            if result.get("save_required"):
                if before["page"] != "save_prompt":
                    raise clicks.CheckedClickError("staff_save_prompt_not_confirmed")
                after = _click(app, room_name, result, "apply", lambda v: v["page"] in {"product_workshop", "editor"}, image)
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
        result.update(status="ready", success=True, safe_to_continue=True)
    except clicks.CheckedClickError as exc:
        # No fallback clicks after retries are exhausted or the page is unknown.
        result.update(reason=exc.reason, error_data=exc.data)
        try:
            value = _read(app, room_name)
            result["page_after"] = value["page"] if value else None
        except clicks.CheckedClickError:
            result["page_after"] = None
    return result


@action_info(name="resonance_pc.product_workshop_staff_summarize", public=True, read_only=True,
             description="Summarize button-based production staffing, save submission and final overview; no game input or headcount recognition.")
def resonance_pc_product_workshop_staff_summarize(room_name: str, results: list, preflight: dict = None, navigation: dict = None):
    rows = [value for value in results if isinstance(value, dict)]
    last = rows[-1] if rows else {}
    selection = last.get("selection_action")
    returned = last.get("returned_to_overview") is True and last.get("page_after") == "reforming_center_overview"
    complete_steps = len(results) == len(STEPS) and len(rows) == len(STEPS) and all(row.get("success") is True and row.get("safe_to_continue") is True and row.get("room_name") == room_name and row.get("step") == step for row, step in zip(rows, STEPS))
    submitted = (selection == "selected" and last.get("confirmed") is True and last.get("applied") is True)
    unchanged = selection == "unchanged" and last.get("confirmed") is not True and last.get("applied") is not True
    if not complete_steps or not (submitted or unchanged) or not returned:
        status = "failed"
        reason = next((row.get("reason") for row in rows if row.get("reason")), "staff_completion_not_confirmed")
        if preflight is not None and preflight.get("success") is not True:
            reason = preflight.get("reason") or "staff_templates_invalid"
        elif navigation is not None and (navigation.get("success") is not True or navigation.get("room_entered") is not True):
            reason = navigation.get("reason") or "staff_navigation_not_confirmed"
    else:
        status = "submitted" if submitted else "unchanged"
        reason = "save_persistence_unverified" if submitted else ""
    return {"room_name": room_name, "success": status in {"unchanged", "submitted"}, "status": status, "reason": reason,
            "selection_action": selection, "save_verified": False,
            "applied": last.get("applied") is True, "returned_to_overview": returned,
            "page_after": last.get("page_after"), "data": {"steps": rows, "clicks": last.get("clicks", []), "preflight": preflight, "navigation": navigation}}
