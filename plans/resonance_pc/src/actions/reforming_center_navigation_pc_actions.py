"""Template-only room opening. Ordinary overview is a caller precondition."""
from __future__ import annotations

import math
import time
from typing import Any, Dict

import cv2
import numpy as np

from packages.aura_core.api import action_info, requires_services
from . import reforming_center_pc_actions as layout
from . import _reforming_center_room_templates as vision
from . import _reforming_center_map_navigation as room_map
from . import _reforming_center_clicks as checked
from .runtime_preflight_pc_actions import resonance_pc_require_client_resolution

_SAFE_MAP_REGION = (420, 170, 520, 360)
# Room selection is safe beyond the central drag corridor. Keep the top HUD,
# right-side event button and bottom shortcut bar outside this larger region.
_ROOM_CLICK_REGION = (320, 130, 760, 450)


def _settle(seconds):
    time.sleep(seconds)


def _read_frame(app):
    captured = app.capture(rect=(0, 0, 1280, 720))
    if (not getattr(captured, "success", False)
            or not isinstance(captured.image, np.ndarray)
            or captured.image.shape != (720, 1280, 3)):
        layout._raise_error("capture_failed", "A fresh 1280x720 RGB client image is required")
    # CaptureResult.image is RGB; OpenCV-loaded assets and vision helpers use
    # BGR. Convert once at this boundary, without mutating the captured frame.
    return cv2.cvtColor(captured.image, cv2.COLOR_RGB2BGR)


def _wait_confirmed(app, settle_sec, probe):
    previous = None
    for _ in range(6):
        _settle(settle_sec)
        value = probe(_read_frame(app))
        if value is not None and value is not False:
            if previous == value:
                return value
            previous = value
        else:
            previous = None
    return None


def _locate_and_click(app, room, rooms, catalog, max_search_steps, settle_sec):
    trace = []
    model = room_map.load_map()
    previous_pose = previous_drag = None
    stalled_count = 0
    # A final drag must still be followed by an observation before exhaustion.
    for _ in range(max_search_steps+1):
        image = _read_frame(app)
        hits = vision.room_hits(image, catalog)
        pose = room_map.locate_viewport(hits, model)
        target = next((hit for hit in hits if hit["room_name"] == room
                       and room in pose["anchors"]), None)
        if target:
            center = target["center"]
            offset = rooms[room]["click_offset"]
            point = [int(round((center[0]+offset[0])/5)*5), int(round((center[1]+offset[1])/5)*5)]
            if (layout._point_in_region(center, _ROOM_CLICK_REGION, margin=10)
                    and layout._point_in_region(point, _ROOM_CLICK_REGION, margin=10)):
                record = {"control": "room"}
                try:
                    checked.send_click(app, point, record)
                except checked.CheckedClickError as exc:
                    layout._raise_error("room_click_failed", "Room click was not delivered", exc.data)
                return {"point": point, "score": target["score"], "drag_count": len(trace), "trace": trace,
                        "click": record}
        if len(trace) >= max_search_steps:
            break
        if previous_pose is not None:
            stalled_count = room_map.drag_progress(previous_pose, pose, previous_drag, stalled_count)
        if target:
            waypoint = {"room_name": room, "screen_point": center, "route": [room]}
        else:
            waypoint = room_map.choose_waypoint(room, pose, model)
        drag = room_map.plan_drag(waypoint["screen_point"], pose["single_anchor"], region=_SAFE_MAP_REGION)
        layout._perform_room_drag(app, start=drag["start"], end=drag["end"], duration_sec=.8, hold_sec=.2)
        trace.append({**drag, "viewport": pose, "waypoint": waypoint})
        previous_pose, previous_drag = pose, drag
        _settle(settle_sec)
    layout._raise_error("reforming_center_room_not_found",
                        "Target could not be moved into the safe click region", {"trace": trace})


@action_info(name="resonance_pc.open_reforming_center_room", public=True, read_only=False,
             description="From the ordinary overview, use screenshot templates to locate a room and activate its confirmed entry button.")
@requires_services(app="plans/aura_base/app")
def resonance_pc_open_reforming_center_room(room_name: str, max_search_steps: int = 12,
                                          settle_sec: float = .8, app: Any = None) -> Dict[str, Any]:
    if (isinstance(max_search_steps, bool) or not isinstance(max_search_steps, (int, float))
            or not math.isfinite(max_search_steps) or int(max_search_steps) != max_search_steps
            or not 1 <= max_search_steps <= 24):
        raise ValueError("max_search_steps must be an integer from 1 to 24")
    if (isinstance(settle_sec, bool) or not isinstance(settle_sec, (int, float))
            or not math.isfinite(settle_sec) or not .05 <= settle_sec <= 10):
        raise ValueError("settle_sec must be between 0.05 and 10 seconds")
    resonance_pc_require_client_resolution(app=app)
    rooms = layout._load_reforming_center_room_layout()
    room = layout._resolve_room_key(room_name, rooms, layout._build_room_alias_lookup(rooms))
    catalog = vision.load_catalog()
    spec = catalog["rooms"].get(room)
    kind = spec["entry_type"] if spec else "unavailable"
    data = {}
    if spec and spec.get("shared_destination"):
        data["shared_destination"] = spec["shared_destination"]
        data["identity_evidence"] = "overview_room_template"

    def result(status, reason=""):
        return {"success": status in {"completed", "selected_only"}, "status": status,
                "room_name": room, "entry_type": kind, "room_entered": status == "completed",
                "reason": reason, "data": data}

    if spec is None:
        return result("failed", "room_template_unavailable")
    try:
        data["navigation"] = _locate_and_click(app, room, rooms, catalog,
                                             int(max_search_steps), settle_sec)
        if kind != "direct":
            def button_point(image):
                button_key = "construction" if kind == "selection_only" else kind
                hit = vision.entry_button(image, spec, catalog["buttons"][button_key])
                return tuple(hit["center"]) if hit else None

            try:
                selected = checked.confirm_click(app, data["navigation"]["click"], button_point, button_point)
            except checked.CheckedClickError as exc:
                data["error"] = {"code": exc.reason, "detail": exc.data}
                return result("failed", "target_selection_not_confirmed")
            point = checked.rounded_point(selected["ready_evidence"])
            if kind == "selection_only":
                return result("selected_only")
            record = {"control": "entry", "type": kind}
            data["entry_button"] = record
            try:
                checked.send_click(app, point, record)
            except checked.CheckedClickError as exc:
                data["error"] = {"code": exc.reason, "detail": exc.data}
                return result("failed", "entry_click_failed")
        else:
            record = data["navigation"]["click"]
        try:
            checked.confirm_click(app, record, lambda image: vision.destination_confirmed(image, spec),
                                  lambda image: vision.destination_confirmed(image, spec))
            return result("completed")
        except checked.CheckedClickError as exc:
            data["error"] = {"code": exc.reason, "detail": exc.data}
            return result("failed", "room_entry_not_confirmed")
    except room_map.RoomMapError as exc:
        data["error"] = {"code": exc.code, "detail": exc.detail}
        return result("failed", exc.code)
    except layout.ReformingCenterNavigationError as exc:
        data["error"] = exc.to_dict()
        return result("failed", exc.code)
    except checked.CheckedClickError as exc:
        data["error"] = {"code": exc.reason, "detail": exc.data}
        return result("failed", exc.reason)
