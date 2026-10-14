"""Template-only production quick-team staffing; no OCR or scheduling."""
from __future__ import annotations

import asyncio
from typing import Any

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested
from . import _reforming_center_clicks as clicks
from . import _reforming_center_room_templates as rooms
from . import _raw_workshop_ui as shared_ui
from . import _product_workshop_ui as ui
from . import reforming_center_pc_actions as layout
from .runtime_preflight_pc_actions import resonance_pc_require_client_resolution

def _cancelled():
    if is_current_task_cancel_requested():
        raise asyncio.CancelledError()


def _wait(app, probe):
    return clicks.wait_confirmed(app, probe, poll_sec=.2)


def _send(app, point, name, data):
    record = {"control": name}
    data["clicks"].append(record)
    if name == "save_on_exit_confirm":
        data["save_attempted"] = True
    return clicks.send_click(app, point, record)


def _control(app, name, data, expected):
    frame = clicks.read_frame(app)
    point = ui.control_point(frame, name)
    if point is None or not expected(frame):
        raise clicks.CheckedClickError("control_not_ready", {"control": name})
    return _send(app, point, name, data)


def _return_to_overview(app, data, spec):
    def physical(frame):
        return rooms.destination_confirmed(frame, spec)

    frame = clicks.read_frame(app)
    if not ui.editor_ready(frame) or ui.control_point(frame, "back") is None:
        raise clicks.CheckedClickError("editor_back_not_ready")
    leaving = _send(app, (85, 40), "editor_back", data)

    def exit_state(frame):
        # The persistent prompt overlays the editor. A direct physical-room
        # return is the client's no-unsaved-changes path.
        if physical(frame):
            return {"page": "physical_product_workshop"}
        return ui.save_prompt(frame)

    outcome = clicks.wait_confirmed(app, exit_state)
    leaving["transition_ready"] = True
    if outcome["page"] == "save_changes_prompt":
        data["save_prompt_seen"] = True
        prompt = ui.save_prompt(clicks.read_frame(app))
        if prompt is None:
            raise clicks.CheckedClickError("save_prompt_not_ready")
        data["save_prompt"] = prompt
        submitted = _send(app, prompt["confirm_point"], "save_on_exit_confirm", data)
        clicks.confirm_click(app, submitted, physical, physical)
        # SaveChanges(true) starts an async save, then GoBack runs immediately.
        # Exiting proves the confirmation was handled, NOT server persistence.
        data["save_submitted"] = True
        data["save_confirmation"] = "exit_prompt_confirmed_and_editor_closed"
    else:
        data["save_confirmation"] = "no_save_prompt_editor_closed"
    data["end_page"] = "physical_product_workshop"
    # Same return contract as raw workshops, with fresh observations before
    # input and stable overview evidence afterward.
    frame = clicks.read_frame(app)
    if not physical(frame) or not shared_ui.control_visible(frame, "selection_back"):
        raise clicks.CheckedClickError("workshop_back_not_ready")
    returned = _send(app, (95, 40), "return_overview", data)
    data["end_page"] = None
    confirmed = clicks.confirm_click(app, returned, shared_ui.overview_state,
                                     shared_ui.overview_state)
    data["overview"] = confirmed["ready_evidence"]
    data["returned_to_overview"] = True
    data["end_page"] = "reforming_center_overview"


@action_info(name="resonance_pc.product_workshop_quick_fill", public=True, read_only=False,
             description="Use templates only to quick-select production workers and confirm save on exit, then return to the overview; no OCR, Apply or scheduling.")
@requires_services(app="plans/aura_base/app")
def resonance_pc_product_workshop_quick_fill(room_name: str, app: Any = None):
    if app is None:
        raise ValueError("app service is required")
    resonance_pc_require_client_resolution(app=app)
    catalog = rooms.load_catalog()
    definitions = layout._load_reforming_center_room_layout()
    room = layout._resolve_room_key(room_name, definitions, layout._build_room_alias_lookup(definitions))
    spec = catalog["rooms"].get(room)
    if not spec or spec.get("shared_destination") != "product_workshop":
        raise ValueError("A production workshop with available navigation templates is required")
    data = {"clicks": [], "selection_confirmed": False, "save_attempted": False,
            "save_submitted": False, "save_prompt_seen": False,
            "save_confirmed": False, "returned_to_overview": False,
            "selection_rule": "native_one_click_free_or_in_prepare", "recognition": "template_only"}

    def result(status, reason=""):
        return {"success": data["returned_to_overview"] and status in
                {"save_submitted", "no_changes", "no_selection_needed"},
                "status": status, "room_name": room, "reason": reason,
                "returned_to_overview": data["returned_to_overview"], "data": data}

    def physical(frame):
        return rooms.destination_confirmed(frame, spec)

    try:
        with clicks.cancellation_checks(_cancelled):
            ui.require_assets()
            frame = clicks.read_frame(app)
            if not physical(frame):
                raise clicks.CheckedClickError("physical_workshop_required")
            edit = rooms.match_asset(frame, spec["destination_markers"][1])
            if edit is None:
                raise clicks.CheckedClickError("workshop_editor_unavailable")
            # The recognized caption is below the clickable blue icon. Live
            # 1280x720 evidence places the icon center 50 px above that caption.
            edit_point = (edit["center"][0], edit["center"][1] - 50)
            record = _send(app, edit_point, "workshop_edit_icon", data)
            _wait(app, ui.editor_ready)
            record["transition_ready"] = True
            _control(app, "quick", data, ui.editor_ready)
            before = _wait(app, ui.selection_state)
            data["before"] = before
            status = "no_selection_needed"
            if before["button"] == "select_all":
                _control(app, "all", data, lambda frame: ui.selection_state(frame) == before)
                data["one_click_selection_attempted"] = True
                def selection_effect(frame):
                    state = ui.selection_state(frame)
                    return state if state and state["button"] in {"cancel_all", "unavailable"} else None

                after = _wait(app, selection_effect)
                data["after"] = after
                _control(app, "confirm", data, lambda frame: ui.selection_state(frame) == after)
                _wait(app, ui.editor_ready)
                data["selection_confirmed"] = True  # Local only, not a server save.
            else:
                # List back hides local selections; never press native cancel-all.
                frame = clicks.read_frame(app)
                if ui.selection_state(frame) is None:
                    raise clicks.CheckedClickError("selection_back_not_ready")
                _send(app, (85, 40), "selection_back", data)
                _wait(app, ui.editor_ready)
            # Exiting opens the persistent save prompt only if the layout changed.
            _return_to_overview(app, data, spec)
            if data["save_submitted"]:
                status = "save_submitted"
            elif data["selection_confirmed"]:
                status = "no_changes"
            return result(status)
    except clicks.CheckedClickError as exc:
        data["error"] = {"reason": exc.reason, "detail": exc.data}
        # No blind save retry, discard confirmation, recipe edit or recovery input.
        return result("failed", exc.reason)
    except Exception as exc:
        data["error"] = {"reason": "operation_failed", "type": type(exc).__name__, "message": str(exc)}
        return result("failed", "operation_failed")
