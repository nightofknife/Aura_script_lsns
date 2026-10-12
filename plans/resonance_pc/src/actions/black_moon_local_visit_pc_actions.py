"""Weekly wishlist visit stages; task YAML owns preparation and navigation order."""
from __future__ import annotations

from copy import deepcopy
import uuid

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger

from ._black_moon_local_shop_policy import load_black_moon_catalog, normalize_record_scope
from ._black_moon_local_shop_state import (
    begin_selection_visit, selection_visit_completed, read_city_record,
    mark_pending_batch, confirm_batch, complete_visit, resolve_purchase_week,
)
from .black_moon_local_select_pc_actions import normalize_purchase_table, execute_black_moon_submit
from .black_moon_local_exit_pc_actions import execute_black_moon_wait_purchase, execute_black_moon_exit
from .black_moon_local_prepare_pc_actions import _cancel_check


def complete_purchase_table(value):
    known = [row["item_id"] for row in load_black_moon_catalog()["products"]]
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("Purchase table must be an item-id to bool object")
    if value:
        normalize_purchase_table(value, known)
    return {key: value.get(key, False) for key in known}


def _identity(visit):
    if not isinstance(visit, dict) or visit.get("run") is not True:
        raise ValueError("An active Black Moon visit is required")
    return {key: visit[key] for key in ("scope", "week_key", "city_key", "visit_id")}


def _current_week(visit):
    if resolve_purchase_week(load_black_moon_catalog()["refresh_policy"]) != visit["week_key"]:
        raise ValueError("Black Moon weekly refresh crossed the frozen visit")


def _selection_proof(visit, selection):
    if not isinstance(selection, dict) or selection.get("success") is not True:
        raise ValueError("Successful selection is required")
    count, items = selection.get("selected_new_count"), selection.get("selected_items")
    if (type(count) is not int or count < 0 or not isinstance(items, list) or len(items) != count
            or selection.get("bottom_confirmed") is not True or selection.get("started_empty") is not True):
        raise ValueError("A complete initially-empty wishlist scan is required")
    if any(not isinstance(item, dict) or item.get("item_id") not in visit["selected_item_ids"] for item in items):
        raise ValueError("Selected cards must belong to the frozen purchase table")
    return {"type": "wishlist_scan", "recognized": True, "bottom_confirmed": True,
            "started_empty": True, "selected_item_ids": list(visit["selected_item_ids"]),
            "selected_card_count": count, "selected_items": deepcopy(items)}


@action_info(name="resonance_pc.begin_black_moon_local_visit", public=True, read_only=False,
             description="Validate and freeze a weekly purchase table before opening the shop.")
@requires_services(resonance_pc_city_shop_data="resonance_pc_city_shop_data", persistent_data="core/persistent_data")
def resonance_pc_begin_black_moon_local_visit(city_name: str, purchase_table: dict,
                                            record_scope: str = "default", exit_target: str = "rest_menu",
                                            resonance_pc_city_shop_data=None, persistent_data=None) -> dict:
    _cancel_check()
    if exit_target not in {"rest_menu", "main"}:
        raise ValueError("Exit target must be rest_menu or main")
    table = complete_purchase_table(purchase_table)
    scope = normalize_record_scope(record_scope)
    city = resonance_pc_city_shop_data.resolve_city(city_name)
    catalog = load_black_moon_catalog()
    selected = [key for key, wanted in table.items() if wanted]
    week = resolve_purchase_week(catalog["refresh_policy"])
    visit = {"success": True, "run": False, "status": "skipped", "reason": "empty_selection",
             "city_key": city["city_key"], "city_name": city["city_name"], "week_key": week,
             "scope": scope, "exit_target": exit_target, "purchase_table": table,
             "selected_item_ids": selected, "completed_this_week": False}
    if not selected:
        return visit
    if city["city_key"] not in catalog["eligible_cities"]:
        return {**visit, "reason": "city_not_eligible"}
    record = begin_selection_visit(persistent_data, scope=scope, week_key=week,
                                   city_key=city["city_key"], selected_item_ids=selected)
    if selection_visit_completed(record, selected):
        return {**visit, "reason": "completed_this_week", "completed_this_week": True}
    return {**visit, "run": True, "status": "pending", "reason": None, "visit_id": record["visit_id"]}


@action_info(name="resonance_pc.submit_black_moon_local_visit", public=True, read_only=False,
             description="Journal verified purchase intent immediately before the single confirmation click.")
@requires_services(app="plans/aura_base/app", persistent_data="core/persistent_data")
async def resonance_pc_submit_black_moon_local_visit(visit: dict, selection: dict, app=None,
                                                     persistent_data=None) -> dict:
    identity = _identity(visit)
    proof = _selection_proof(visit, selection)
    _current_week(visit)
    batch_id = uuid.uuid4().hex

    def journal():
        _current_week(visit)
        mark_pending_batch(persistent_data, **identity, batch_id=batch_id, items=proof["selected_items"])

    result = await execute_black_moon_submit(selection, app, before_submit=journal)
    return {**result, "batch_id": batch_id if result["purchase_submitted"] else None, "scan": proof}


@action_info(name="resonance_pc.complete_black_moon_local_visit", public=True, read_only=False,
             description="Wait read-only for purchase settlement and persist the wishlist check before exit.")
@requires_services(app="plans/aura_base/app", persistent_data="core/persistent_data")
async def resonance_pc_complete_black_moon_local_visit(visit: dict, submission: dict, app=None,
                                                       persistent_data=None) -> dict:
    identity = _identity(visit)
    _current_week(visit)
    if not isinstance(submission, dict) or submission.get("success") is not True:
        raise ValueError("Successful submission or explicit no-goods skip is required")
    proof = submission["scan"]
    if submission.get("purchase_submitted") is True:
        settled = await execute_black_moon_wait_purchase(submission, app)
        # No await between recognized settlement and durable evidence writes.
        confirm_batch(persistent_data, **identity, batch_id=submission["batch_id"], evidence={
            "type": "wishlist_purchase_settled", "recognized": True,
            "purchase_applied": True, "ui_settled": True,
        })
        reason = "wishlist_processed"
    elif submission.get("purchase_submitted") is False and submission.get("status") == "skipped" and proof["selected_card_count"] == 0:
        settled = {"success": True, "ready_to_exit": True, "purchase_verified": False, "page_state": "local_shop"}
        reason = "no_matching_goods"
    else:
        raise ValueError("Uncertain purchase submission cannot complete a visit")
    _current_week(visit)
    complete_visit(persistent_data, **identity, reason=reason, evidence=proof)
    return {**settled, "status": "completed", "reason": reason, "completed_this_week": True,
            "selected_card_count": proof["selected_card_count"], "selected_items": deepcopy(proof["selected_items"])}


@action_info(name="resonance_pc.finish_black_moon_local_visit", public=True, read_only=False,
             description="Restore the requested endpoint after a processed or skipped weekly visit.")
@requires_services(app="plans/aura_base/app", persistent_data="core/persistent_data")
async def resonance_pc_finish_black_moon_local_visit(visit: dict, completion: dict, app=None,
                                                     persistent_data=None) -> dict:
    _cancel_check()
    if not isinstance(visit, dict) or visit.get("success") is not True:
        raise ValueError("Validated visit is required")
    target = visit["exit_target"]
    if visit["run"]:
        if not isinstance(completion, dict) or completion.get("completed_this_week") is not True:
            raise ValueError("A durably completed visit is required before exit")
        record = read_city_record(persistent_data, **{key: visit[key] for key in ("scope", "week_key", "city_key")})
        if record is None or record["visit_id"] != visit["visit_id"] or not selection_visit_completed(record, visit["selected_item_ids"]):
            raise ValueError("Persisted visit no longer matches the endpoint operation")
        exited = await execute_black_moon_exit(completion, target, app)
    elif target == "main":
        exited = await execute_black_moon_exit({"success": True, "ready_to_exit": True}, target, app)
    else:
        exited = {"success": True, "page_state": "rest_menu", "exited": False, "click_count": 0}
    count = completion.get("selected_card_count", 0) if visit["run"] else 0
    items = completion.get("selected_items", []) if visit["run"] else []
    result = {"success": True, "status": completion["status"] if visit["run"] else visit["status"],
              "reason": completion["reason"] if visit["run"] else visit["reason"],
              "city_key": visit["city_key"], "week_key": visit["week_key"],
              "completed_this_week": True if visit["run"] else visit["completed_this_week"],
              "selected_card_count": count, "selected_items": deepcopy(items),
              "purchase_verified": completion.get("purchase_verified", False) if visit["run"] else False,
              "page_state": exited["page_state"], "exit": exited}
    logger.info("[BlackMoonLocalVisit] result=%s", result)
    return result
