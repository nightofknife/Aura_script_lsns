"""Collect once per unlocked physical workshop using existing UI operations."""
from __future__ import annotations

import asyncio
from typing import Any

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested
from . import _product_collection_ui as ui
from . import _reforming_center_clicks as clicks
from . import _raw_workshop_ui as overview_ui
from ._product_collection_matching import frame_observation, observe_frame
from . import reforming_center_pc_actions as layout
from .reforming_center_navigation_pc_actions import resonance_pc_open_reforming_center_room
from .city_trade_flow_pc_actions import resonance_pc_tap_back_once
from .runtime_preflight_pc_actions import resonance_pc_require_client_resolution

ROOMS = tuple(f"{number}号生产车间" for number in range(1, 7))
OVERVIEW = "reforming_center_overview"


def _cancelled():
    if is_current_task_cancel_requested():
        raise asyncio.CancelledError("Production-workshop collection cancelled")


def _physical(frame, room):
    return ui.physical(frame, room)


def _page(app, room=None):
    try:
        image = clicks.read_frame(app)
        with observe_frame(image):
            if ui.navigation_open(image):
                return "product_workshop_navigation"
            state = ui.collection_state(image, room) if room else None
            if state == "complete":
                return "product_collection_reward"
            if state == "capacity_blocked":
                return "product_collection_capacity_prompt"
            if state in {"collectable", "idle"} or (room and _physical(image, room)):
                return "product_workshop"
            if overview_ui.overview_state(image):
                return OVERVIEW
    except (clicks.CheckedClickError, OSError, ValueError, KeyError):
        pass
    return None


def _control(app, name, data, before, effect):
    image = clicks.read_frame(app)
    with observe_frame(image):
        if not before(image):
            raise clicks.CheckedClickError("collection_source_not_confirmed", {"control": name})
        hit = ui.control(image, name)
    if hit is None:
        raise clicks.CheckedClickError("collection_control_not_recognized", {"control": name})
    record = {"control": name}
    data["clicks"].append(record)
    return clicks.checked_click(app, hit["center"], effect, effect, record)


def _back_to_overview(app, vision, room, data):
    image = clicks.read_frame(app)
    if not _physical(image, room):
        raise clicks.CheckedClickError("collection_workshop_return_not_ready")
    _cancelled()
    # This is the existing public back operation, not another navigation path.
    data["back"] = resonance_pc_tap_back_once(wait_sec=0, app=app, vision=vision)
    _cancelled()
    data["overview"] = clicks.wait_confirmed(app, overview_ui.overview_state)
    data.update(returned_to_overview=True, page_after=OVERVIEW)


def _states(app):
    @frame_observation
    def probe(image):
        if not ui.navigation_open(image):
            return None
        cards = ui.navigation_cards(image)
        if not cards:
            return None
        pairs = sorted((card["number"], card["state"]) for card in cards)
        # Duplicate identities are not evidence of two independent rooms.
        if len({number for number, _ in pairs}) != len(pairs):
            return None
        return pairs
    return clicks.wait_confirmed(app, probe, timeout_sec=6, poll_sec=.3)


def _scroll_navigation(app, data, direction):
    image = clicks.read_frame(app)
    with observe_frame(image):
        if not ui.navigation_open(image):
            raise clicks.CheckedClickError("collection_navigation_lost")
        region = ui.navigation_region(image)
    if not region or len(region) != 4:
        raise clicks.CheckedClickError("collection_navigation_region_unknown")
    x, y, w, h = region
    left, right, cy = int(x+w*.25), int(x+w*.75), int(y+h*.5)
    start, end = ([left, cy], [right, cy]) if direction == "right" else ([right, cy], [left, cy])
    _cancelled()
    layout._perform_room_drag(app, start=start, end=end, duration_sec=.6, hold_sec=.15)
    _cancelled()
    data["scrolls"].append({"start": start, "end": end, "direction": direction})
    clicks._sleep(.5)


@action_info(name="resonance_pc.product_collection_assets", public=True, read_only=True,
             description="Prepare native collection and unlock templates before entering any workshop.")
def resonance_pc_product_collection_assets():
    try:
        ui.validate_assets()
        return {"success": True, "status": "ready", "reason": ""}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"success": False, "status": "failed", "reason": "collection_templates_invalid",
                "error": str(exc)}


@action_info(name="resonance_pc.product_collection_scan_unlocks", public=True, read_only=False,
             description="Read all six native workshop cards from room 1 and return to the overview.")
@requires_services(app="plans/aura_base/app", vision="plans/aura_base/vision")
def resonance_pc_product_collection_scan_unlocks(navigation: dict, app: Any = None, vision: Any = None):
    result = {"success": False, "status": "failed", "reason": "", "unlocks": {},
              "safe_to_continue": False, "returned_to_overview": False, "page_after": None,
              "clicks": [], "scrolls": []}
    if (not isinstance(navigation, dict) or navigation.get("success") is not True
            or navigation.get("room_entered") is not True or navigation.get("room_name") != ROOMS[0]):
        result["reason"] = ((navigation or {}).get("reason") if isinstance(navigation, dict) else None) or "initial_workshop_not_entered"
        return result
    with clicks.cancellation_checks(_cancelled):
        try:
            resonance_pc_require_client_resolution(app=app)
            _control(app, "navigation_button", result, lambda f: _physical(f, ROOMS[0]), ui.navigation_open)
            seen = {}
            reset_drags = 0
            for attempt in range(9):
                current = dict(_states(app))
                for number, state in current.items():
                    if number not in range(1, 7) or state not in {"open", "locked"}:
                        raise clicks.CheckedClickError("collection_unlock_state_invalid")
                    if number in seen and seen[number] != state:
                        raise clicks.CheckedClickError("collection_unlock_state_changed", {"room": number})
                    seen[number] = state
                if len(seen) == 6:
                    break
                if attempt == 8:
                    raise clicks.CheckedClickError("collection_unlock_scan_incomplete", {"seen": seen})
                # A previously scrolled menu may initially expose only its tail.
                if 1 not in seen and reset_drags < 2:
                    direction = "right"
                    reset_drags += 1
                else:
                    direction = "left"
                _scroll_navigation(app, result, direction)
            values = [seen[number] for number in range(1, 7)]
            if values[0] != "open" or any(values[i] == "open" and values[i-1] == "locked" for i in range(1, 6)):
                raise clicks.CheckedClickError("collection_unlock_order_inconsistent", {"states": seen})
            result["unlocks"] = {ROOMS[number-1]: seen[number] for number in range(1, 7)}
            image = clicks.read_frame(app)
            with observe_frame(image):
                point = ui.navigation_close_point(image)
                if point is None or not ui.navigation_open(image):
                    raise clicks.CheckedClickError("collection_navigation_close_not_ready")
            record = {"control": "native_navigation_background"}
            result["clicks"].append(record)
            clicks.checked_click(app, point, lambda f: _physical(f, ROOMS[0]),
                                 lambda f: _physical(f, ROOMS[0]), record)
            _back_to_overview(app, vision, ROOMS[0], result)
            result.update(success=True, status="ready", safe_to_continue=True)
        except clicks.CheckedClickError as exc:
            result.update(reason=exc.reason, error_data=exc.data, page_after=_page(app, ROOMS[0]))
        except Exception as exc:
            result.update(reason="collection_unlock_scan_failed", error_type=type(exc).__name__,
                          error=str(exc), page_after=_page(app, ROOMS[0]))
    return result


@action_info(name="resonance_pc.product_collection_collect_room", public=True, read_only=False,
             description="Skip a confirmed locked room or reuse room navigation, collect once and return to overview.")
@requires_services(app="plans/aura_base/app", vision="plans/aura_base/vision")
def resonance_pc_product_collection_collect_room(room_name: str, unlocks: dict, previous: dict,
                                                max_search_steps: int = 12, settle_sec: float = .8,
                                                app: Any = None, vision: Any = None):
    if room_name not in ROOMS:
        raise ValueError("room_name must name production workshop 1 through 6")
    result = {"success": False, "status": "failed", "room_name": room_name, "reason": "",
              "safe_to_continue": False, "returned_to_overview": False, "page_after": None,
              "collection_attempted": False, "reward_seen": False, "clicks": []}
    index = ROOMS.index(room_name)
    handoff = (isinstance(previous, dict) and previous.get("safe_to_continue") is True
               and previous.get("success") is True and previous.get("returned_to_overview") is True
               and previous.get("page_after") == OVERVIEW
               and (index == 0 or previous.get("room_name") == ROOMS[index-1]))
    if not handoff:
        result["reason"] = "collection_previous_room_incomplete"
        return result
    if (not isinstance(unlocks, dict) or set(unlocks) != set(ROOMS)
            or any(value not in {"open", "locked"} for value in unlocks.values())):
        result["reason"] = "collection_unlocks_incomplete"
        return result
    if unlocks[room_name] == "locked":
        result.update(success=True, status="locked", safe_to_continue=True,
                      returned_to_overview=True, page_after=OVERVIEW)
        return result
    with clicks.cancellation_checks(_cancelled):
        try:
            _cancelled()
            navigation = resonance_pc_open_reforming_center_room(
                room_name=room_name, max_search_steps=max_search_steps, settle_sec=settle_sec, app=app,
                vision=vision)
            result["navigation"] = navigation
            _cancelled()
            if navigation.get("success") is not True or navigation.get("room_entered") is not True:
                raise clicks.CheckedClickError(navigation.get("reason") or "collection_room_not_entered")

            def ready(image):
                state = ui.collection_state(image, room_name)
                # Both states are emitted only after physical() confirms this
                # room on this frame; do not run its six-number check twice.
                return state if state in {"collectable", "idle"} else None
            state = clicks.wait_confirmed(app, ready, timeout_sec=6, poll_sec=.4)
            status = "no_collectable"
            if state == "collectable":
                image = clicks.read_frame(app)
                with observe_frame(image):
                    if ready(image) != "collectable":
                        raise clicks.CheckedClickError("collection_button_state_changed")
                    hit = ui.control(image, "collect_all")
                if hit is None:
                    raise clicks.CheckedClickError("collection_button_missing")
                record = {"control": "collect_all"}
                result["clicks"].append(record)
                result["collection_attempted"] = True
                clicks.send_click(app, hit["center"], record)

                def outcome(image):
                    value = ui.collection_state(image, room_name)
                    return value if value in {"complete", "capacity_blocked"} else None
                # The resource-changing click is never retried, including on timeout.
                after = clicks.wait_confirmed(app, outcome, timeout_sec=10, poll_sec=.3, consecutive=1)
                record.update(effect_confirmed=True, effect_evidence=after)
                if after == "capacity_blocked":
                    _control(app, "capacity_cancel", result,
                             lambda f: ui.collection_state(f, room_name) == "capacity_blocked",
                             lambda f: _physical(f, room_name))
                    status = "blocked"
                    result["reason"] = "warehouse_capacity_blocked"
                else:
                    result["reward_seen"] = True
                    # ShowItem's native full-screen close handler is reached via
                    # its own recognized blank-area exit hint, never an invented OK.
                    _control(app, "exit_hint", result,
                             lambda f: ui.collection_state(f, room_name) == "complete",
                             lambda f: _physical(f, room_name))
                    status = "collected"
            result["collection_status"] = status
            _back_to_overview(app, vision, room_name, result)
            result.update(status=status, success=status != "blocked", safe_to_continue=status != "blocked")
        except clicks.CheckedClickError as exc:
            result.update(reason=exc.reason, error_data=exc.data, page_after=_page(app, room_name))
        except Exception as exc:
            result.update(reason="collection_room_failed", error_type=type(exc).__name__,
                          error=str(exc), page_after=_page(app, room_name))
    return result


@action_info(name="resonance_pc.product_collection_summarize", public=True, read_only=True,
             description="Aggregate six collection results and the confirmed final page without game input.")
def resonance_pc_product_collection_summarize(preflight=None, scan=None, results: list = None):
    slots = results if isinstance(results, list) and len(results) == 6 else [None]*6
    rows, stopped, reason = [], None, ""
    status = "completed"
    last = scan if isinstance(scan, dict) else {}
    if not isinstance(preflight, dict) or preflight.get("success") is not True:
        status, reason = "failed", (preflight or {}).get("reason", "collection_templates_invalid") if isinstance(preflight, dict) else "collection_templates_invalid"
    elif not isinstance(scan, dict) or scan.get("success") is not True or scan.get("safe_to_continue") is not True:
        status, reason = "failed", (scan or {}).get("reason", "collection_unlock_scan_missing") if isinstance(scan, dict) else "collection_unlock_scan_missing"
    for room, value in zip(ROOMS, slots):
        if status != "completed" or not isinstance(value, dict):
            rows.append({"room_name": room, "status": "not_processed", "success": False,
                         "reason": "previous_step_incomplete"})
            if status == "completed":
                status, reason, stopped = "failed", "collection_room_result_missing", room
            continue
        row = dict(value)
        returned = row.get("returned_to_overview") is True and row.get("page_after") == OVERVIEW
        valid = row.get("room_name") == room and returned
        if row.get("status") in {"collected", "no_collectable", "locked"}:
            valid = valid and row.get("success") is True and row.get("safe_to_continue") is True
            if row["status"] == "collected":
                valid = valid and row.get("collection_attempted") is True and row.get("reward_seen") is True
            valid = valid and isinstance(scan.get("unlocks"), dict) and scan["unlocks"].get(room) == ("locked" if row["status"] == "locked" else "open")
        elif row.get("status") == "blocked":
            valid = (valid and row.get("reason") == "warehouse_capacity_blocked"
                     and row.get("collection_attempted") is True and row.get("collection_status") == "blocked")
        else:
            valid = False
        if not valid:
            row.update(status="failed", success=False, reason=row.get("reason") or "collection_result_not_confirmed")
        rows.append(row)
        last = row
        if row["status"] in {"failed", "blocked"}:
            status, reason, stopped = row["status"], row.get("reason") or "collection_failed", room
    returned = last.get("returned_to_overview") is True and last.get("page_after") == OVERVIEW
    if status == "completed" and not returned:
        status, reason = "failed", "collection_final_overview_unconfirmed"
    return {"success": status == "completed", "status": status, "reason": reason,
            "results": rows, "unlocks": (scan or {}).get("unlocks", {}) if isinstance(scan, dict) else {},
            "visited_count": sum(isinstance(row.get("navigation"), dict)
                                 and row["navigation"].get("room_entered") is True for row in rows),
            "collected_count": sum(row["status"] == "collected" for row in rows),
            "skipped_count": sum(row["status"] in {"locked", "no_collectable"} for row in rows),
            "returned_to_overview": returned, "page_after": last.get("page_after"), "stopped_room": stopped}
