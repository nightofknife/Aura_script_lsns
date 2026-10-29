"""Validated handoffs for the city -> collection -> withdrawal -> city task.

The YAML owns sequencing and invokes complete children through aura.run_task.
These actions validate inputs, interpret one handoff, navigate one exit, or
summarize. They never schedule tasks or replay a resource-changing operation.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

import cv2

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.context.execution import ExecutionContext
from packages.aura_core.engine import ExecutionEngine
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested
from packages.aura_core.scheduler.operation_scope import OperationScope, current_operation_scope
from packages.aura_core.utils.exceptions import StopTaskException
from ....aura_base.src.services.interaction_service import InputSession, TransitionPolicy, TransitionProbe, TransitionState
from ._city_panel_vision import CITY_REGION, probe_city, wait_city
from ._prison_warehouse_ledger import normalize_request
from ._prison_warehouse_ui import PrisonWarehouseUi
from .runtime_preflight_pc_actions import resonance_pc_require_client_resolution

PLAN = Path(__file__).resolve().parents[2]
OPERATIONS = ("collect_products", "withdraw_products")
OVERVIEW = "reforming_center_overview"


def _check_cancelled():
    if is_current_task_cancel_requested():
        import asyncio
        raise asyncio.CancelledError("Product collection/withdrawal cancelled")


@action_info(name="resonance_pc.product_workflow_prepare", public=True, read_only=True,
             description="Validate operation switches and conditional withdrawal inputs before game input.")
def resonance_pc_product_workflow_prepare(operations: Any = None, goods: Any = None,
                                        quantity_threshold: Any = None) -> dict:
    chosen = list(OPERATIONS) if operations is None else operations
    if not isinstance(chosen, list) or any(not isinstance(value, str) or value not in OPERATIONS for value in chosen):
        raise ValueError("operations must list collect_products and/or withdraw_products")
    chosen = [value for value in OPERATIONS if value in chosen]
    request = {"operations": chosen, "run_collect": OPERATIONS[0] in chosen,
               "run_withdraw": OPERATIONS[1] in chosen, "goods": None, "quantity_threshold": None}
    if request["run_withdraw"]:
        if goods is not None and not isinstance(goods, list):
            raise ValueError("goods must be a list or null when withdrawal is enabled")
        products = json.loads((PLAN / "data/meta/products.json").read_text(encoding="utf-8"))
        parsed = normalize_request(goods, quantity_threshold, products)
        catalog = json.loads((PLAN / "data/meta/prison_warehouse_goods_templates_pc.json").read_text(encoding="utf-8"))
        unsupported = [identity for identity in parsed["goods_ids"] or []
                       if not catalog["products"].get(identity, {}).get("supported")]
        if unsupported:
            raise ValueError("No supported warehouse icon templates for requested goods: " + ", ".join(unsupported))
        request.update(goods=parsed["goods_ids"], quantity_threshold=parsed["quantity_threshold"])
    return request


@action_info(name="resonance_pc.product_workflow_require_city", public=True, read_only=True,
             description="Confirm the actual city building panel and city identity without navigation or input.")
@requires_services(app="plans/aura_base/app", vision="plans/aura_base/vision")
def resonance_pc_product_workflow_require_city(app: Any = None, vision: Any = None) -> dict:
    _check_cancelled()
    try:
        resonance_pc_require_client_resolution(app=app)
        ready = wait_city(app=app, vision=vision, check_cancelled=_check_cancelled, timeout_sec=3)
        _check_cancelled()
        valid = ready.get("success") is True and ready.get("page_state") == "city_panel" and bool(ready.get("city_key"))
        return {"success": valid, "page_state": "city_panel" if valid else "unknown",
                "city_key": ready.get("city_key") if valid else None,
                "reason": "" if valid else "product_workflow_city_required", "city": ready}
    except Exception as exc:
        _check_cancelled()
        return {"success": False, "page_state": "unknown", "reason": "product_workflow_city_required",
                "error": {"type": type(exc).__name__, "message": str(exc)}}


def _city_from_frame(frame, app, vision):
    x, y, width, height = CITY_REGION
    return probe_city(app=app, vision=vision, check_cancelled=_check_cancelled,
                      source_image=frame.image[y:y+height, x:x+width])


@action_info(name="resonance_pc.product_workflow_enter_from_city", public=True, read_only=False,
             description="Enter the Reforming Center directly from its confirmed city building panel, without visiting the train main screen.")
@requires_services(app="plans/aura_base/app", vision="plans/aura_base/vision",
                   interaction="plans/aura_base/interaction",
                   resonance_pc_city_shop_data="resonance_pc_city_shop_data")
def resonance_pc_product_workflow_enter_from_city(city_key: Any = None, app: Any = None, vision: Any = None,
                                                interaction: Any = None, resonance_pc_city_shop_data: Any = None) -> dict:
    _check_cancelled()
    scope = OperationScope(parent=current_operation_scope(), target=getattr(app, "target_runtime", None),
                           timeout=30, cancel_check=_check_cancelled)
    attempted = False
    city = None
    try:
        with scope.activate():
            resonance_pc_require_client_resolution(app=app)
            city = wait_city(app=app, vision=vision, check_cancelled=_check_cancelled, timeout_sec=3)
            if (city.get("success") is not True or city.get("page_state") != "city_panel"
                    or not city.get("city_key") or city_key is not None and city.get("city_key") != city_key):
                raise ValueError("The original city building panel was not confirmed")
            point = resonance_pc_city_shop_data.resolve_shop_point(city_name=city["city_key"], shop_name="整顿中心")
            frame = interaction.observation.capture(scope=scope, roi=(0, 0, 1280, 720))
            fresh = _city_from_frame(frame, app, vision)
            if fresh.get("found") is not True or fresh.get("city_key") != city["city_key"]:
                raise ValueError("City identity changed before the Reforming Center input")
            ui = PrisonWarehouseUi()
            scope.checkpoint()
            attempted = True
            receipt = interaction.send_input(lambda: app.click(x=point["x"], y=point["y"]), scope=scope,
                                             point=(point["x"], point["y"]), frame=frame,
                                             session=InputSession(),
                                             bounds=(0, 0, 1280, 720), check_cancelled=_check_cancelled)

            def overview():
                current = interaction.observation.capture(scope=scope, roi=(0, 0, 1280, 720))
                return ui.probe(cv2.cvtColor(current.image, cv2.COLOR_RGB2BGR), read_list=False)

            ready = interaction.confirm(overview, scope=scope.child(20), stable_scans=2, poll_seconds=.3,
                                        accept=lambda value: value.get("page") == "overview",
                                        equivalent=lambda a, b: a.get("page") == b.get("page"),
                                        sleep=scope.sleep, check_cancelled=_check_cancelled)
            if ready is None:
                return {"success": False, "page_state": "unknown", "reason": "product_workflow_entry_unconfirmed",
                        "entry_attempted": True, "city_key": city["city_key"], "input_sent": receipt.sent}
            return {"success": True, "status": "entered", "page_state": OVERVIEW,
                    "city_key": city["city_key"], "entry_attempted": True, "click": point}
    except Exception as exc:
        _check_cancelled()
        return {"success": False, "page_state": "unknown", "reason": "product_workflow_city_entry_failed",
                "entry_attempted": attempted, "city_key": city.get("city_key") if isinstance(city, dict) else None,
                "error": {"type": type(exc).__name__, "message": str(exc)}}


def _skipped(enabled):
    return {"enabled": enabled, "success": not enabled,
            "status": "not_processed" if enabled else "skipped",
            "reason": "previous_step_incomplete" if enabled else "disabled"}


def _base(request):
    return {"operations": list(request["operations"]), "success": False, "status": "pending", "reason": "",
            "collection_result": _skipped(request["run_collect"]),
            "withdrawal_result": _skipped(request["run_withdraw"]),
            "goods_taken": [], "total_taken": 0, "withdrawal_quantity_known": True,
            "returned_to_city": False, "page_after": "unknown", "city_key": None,
            "failure_stage": None, "issues": [], "can_enter": False, "can_collect": False,
            "can_withdraw": False, "can_return": False}


def _failed_node(state):
    return isinstance(state, dict) and str(state.get("status", "")).upper() in {"FAILED", "CANCELLED"}


def _overview(result):
    return (result.get("returned_to_overview") is True
            and result.get("page_after") in {"overview", OVERVIEW})


def _problem(state, stage, reason, status="failed"):
    state["issues"].append({"stage": stage, "reason": reason, "status": status})
    if state["failure_stage"] is None:
        state.update(failure_stage=stage, reason=reason)


def _ledger(result, request):
    rows = result.get("goods_taken")
    total = result.get("total_taken")
    if (result.get("reconcile_complete") is not True or result.get("unknown_goods") != []
            or not isinstance(rows, list) or type(total) is not int or total < 0):
        return None
    allowed = request["goods"]
    if allowed is None:
        catalog = json.loads((PLAN / "data/meta/prison_warehouse_goods_templates_pc.json").read_text(encoding="utf-8"))
        allowed = {identity for identity, entry in catalog["products"].items() if entry.get("supported")}
    seen = set()
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("goods_id"), str)
                or row["goods_id"] not in allowed or row["goods_id"] in seen
                or not isinstance(row.get("goods"), str) or not row["goods"].strip()
                or type(row.get("quantity")) is not int or row["quantity"] <= 0):
            return None
        seen.add(row["goods_id"])
    return deepcopy(rows) if total == sum(row["quantity"] for row in rows) else None


@action_info(name="resonance_pc.product_workflow_evaluate", public=True, read_only=True,
             description="Interpret one child business result and authorize only the next safe handoff.")
def resonance_pc_product_workflow_evaluate(stage: str, request: dict, previous: Any = None,
                                         child: Any = None, child_state: Any = None) -> dict:
    if stage not in {"city", "enter", "collect", "withdraw"}:
        raise ValueError("Unknown product workflow stage")
    old = previous if isinstance(previous, dict) else _base(request)
    state = deepcopy(old)
    state.update(can_enter=False, can_collect=False, can_withdraw=False)
    value = deepcopy(child) if isinstance(child, dict) else {}
    failed = _failed_node(child_state)
    if stage == "city":
        if not failed and value.get("success") is True and value.get("page_state") == "city_panel" and value.get("city_key"):
            state.update(returned_to_city=True, page_after="city_panel", city_key=value["city_key"],
                         can_enter=bool(request["operations"]))
        else:
            _problem(state, "city", value.get("reason") or "product_workflow_city_required")
        return state
    if stage == "enter":
        if not old.get("can_enter"):
            return state
        state.update(returned_to_city=False, page_after="unknown", can_return=False)
        state["entry_result"] = value if not failed else {"success": False, "error": deepcopy(child_state)}
        if not failed and value.get("success") is True and value.get("page_state") == OVERVIEW:
            state.update(page_after=OVERVIEW, can_return=True, can_collect=request["run_collect"],
                         can_withdraw=request["run_withdraw"] and not request["run_collect"])
        else:
            _problem(state, "enter", value.get("reason") or "product_workflow_entry_failed")
        return state
    enabled = request["run_collect"] if stage == "collect" else request["run_withdraw"]
    if not enabled:
        if stage == "collect":
            state["can_withdraw"] = old.get("can_withdraw") is True
        return state
    allowed = old.get("can_collect") if stage == "collect" else old.get("can_withdraw")
    if allowed is not True:
        return state
    key = "collection_result" if stage == "collect" else "withdrawal_result"
    state.update(page_after="unknown", returned_to_city=False, can_return=False)
    if stage == "withdraw":
        # A failed full child can have sent a goods input before failing. Its
        # absent output is unknown consumption, never evidence of zero transfer.
        state.update(goods_taken=None, total_taken=None, withdrawal_quantity_known=False)
    if failed or not value:
        state[key] = {"enabled": True, "success": False, "status": "failed",
                      "reason": f"product_workflow_{stage}_task_failed", "error": deepcopy(child_state)}
        _problem(state, stage, state[key]["reason"])
        return state
    state[key] = {**value, "enabled": True}
    safe = _overview(value)
    if safe:
        state.update(page_after=OVERVIEW, can_return=True)
    if stage == "collect":
        completed = value.get("success") is True and value.get("status") == "completed" and safe
        capacity_blocked = (value.get("success") is False and value.get("status") == "blocked"
                            and value.get("reason") == "warehouse_capacity_blocked" and safe)
        state["can_withdraw"] = request["run_withdraw"] and (completed or capacity_blocked)
        if not completed:
            _problem(state, stage, value.get("reason") or "product_workflow_collection_incomplete",
                     "blocked" if capacity_blocked else "failed")
        return state
    ledger = _ledger(value, request)
    if ledger is not None:
        state.update(goods_taken=ledger, total_taken=value["total_taken"], withdrawal_quantity_known=True)
    # Unfiltered mode asks for one attempt, not an empty warehouse. Known
    # remaining stock alone is acceptable; unknown quantities/coverage are not.
    leftovers_only = (request["goods"] is None and value.get("status") == "partial"
                      and value.get("issues") == ["eligible_goods_remaining"]
                      and value.get("stop_reason") in {"no_eligible_goods", "no_visible_eligible_goods", "train_full"})
    completed = (value.get("success") is True and safe and ledger is not None
                 and value.get("scan_complete") is True and value.get("unresolved") == []
                 and ((value.get("status") == "completed" and value.get("issues") == []) or leftovers_only))
    if not completed:
        partial = value.get("success") is True and value.get("status") == "partial" and safe and ledger is not None
        reason = "product_workflow_withdrawal_partial" if partial else (
            "product_workflow_withdrawal_result_invalid" if ledger is None or value.get("success") is True else
            value.get("reason") or "product_workflow_withdrawal_incomplete")
        _problem(state, stage, reason, "partial" if partial else "failed")
    return state


@action_info(name="resonance_pc.product_workflow_return_city", public=True, read_only=False,
             description="Return from the confirmed overview with its back arrow, and confirm the original city building panel.")
@requires_services(app="plans/aura_base/app", vision="plans/aura_base/vision", interaction="plans/aura_base/interaction")
def resonance_pc_product_workflow_return_city(city_key: Any = None, app: Any = None, vision: Any = None,
                                            interaction: Any = None) -> dict:
    _check_cancelled()
    scope = OperationScope(parent=current_operation_scope(), target=getattr(app, "target_runtime", None),
                           timeout=20, cancel_check=_check_cancelled)
    try:
        with scope.activate():
            resonance_pc_require_client_resolution(app=app)
            ui = PrisonWarehouseUi()
            policy = TransitionPolicy(max_clicks=4, allow_retry=True, settle_seconds=.8, latch_source_loss=True)
            transition = TransitionState(policy)
            city_seen = {}

            def observe(allow_source):
                frame = interaction.observation.capture(scope=scope, roi=(0, 0, 1280, 720))
                view = ui.probe(cv2.cvtColor(frame.image, cv2.COLOR_RGB2BGR), read_list=False)
                city = _city_from_frame(frame, app, vision)
                if (view.get("page") != "overview" and city.get("found") is True
                        and city.get("city_key") and (city_key is None or city.get("city_key") == city_key)):
                    city_seen.update(city)
                    return TransitionProbe("target", key=("city_panel", city["city_key"]), frame=frame)
                back = view.get("controls", {}).get("back", {})
                if (view.get("page") == "overview" and city.get("found") is not True
                        and back.get("valid") is True and back.get("found") is True and back.get("point")):
                    return TransitionProbe("source", key="reforming_center_overview",
                                           hit={"center_point": back["point"]}, frame=frame)
                return TransitionProbe("unknown", key="exit_source_unknown", frame=frame)

            def probe(allow_source):
                return interaction.probe_transition(observe, allow_source=allow_source, scope=scope.child(3),
                                                     stable_scans=2, poll_seconds=.3, allow_immediate_source=False,
                                                     state=transition, sleep=scope.sleep, check_cancelled=_check_cancelled)

            def click(observed, attempt):
                x, y = observed.hit["center_point"]
                return app.click(x=int(x), y=int(y))

            interaction.run_transition(probe, click, scope=scope, policy=policy, state=transition,
                                       sleep=scope.sleep, check_cancelled=_check_cancelled,
                                       source_unconfirmed=lambda: ValueError("product_workflow_exit_source_unconfirmed"),
                                       exhausted=lambda: ValueError("product_workflow_city_return_unconfirmed"))
            scope.checkpoint()
            return {"success": True, "page_state": "city_panel", "city_key": city_seen["city_key"],
                    "city": city_seen, "click_attempts": len(transition.receipts),
                    "reason": "returned_to_city_panel" if transition.receipts else "already_on_city_panel"}
    except Exception as exc:
        _check_cancelled()
        reason = str(exc) if str(exc).startswith("product_workflow_") else "product_workflow_city_return_failed"
        return {"success": False, "page_state": "unknown", "reason": reason,
                "error": {"type": type(exc).__name__, "message": str(exc)}}


def _progress(state):
    for key in ("collection_result", "withdrawal_result"):
        result = state[key]
        if (result.get("enabled") is True and result.get("success") is True
                and result.get("status") == "completed" and _overview(result)
                and (key == "collection_result" or state.get("withdrawal_quantity_known") is True)):
            return True
    count = state["collection_result"].get("collected_count")
    total = state["total_taken"]
    return (type(total) is int and total > 0) or (type(count) is int and count > 0)


@action_info(name="resonance_pc.product_workflow_finish", public=True, read_only=True,
             description="Summarize the city workflow, preserve failure details and require all enabled operations to complete.")
def resonance_pc_product_workflow_finish(state: dict, city_result: Any = None, city_state: Any = None,
                                        engine: ExecutionEngine = None, context: ExecutionContext = None) -> dict:
    _check_cancelled()
    result = deepcopy(state)
    if result["can_return"]:
        returned = (not _failed_node(city_state) and isinstance(city_result, dict)
                    and city_result.get("success") is True and city_result.get("page_state") == "city_panel"
                    and city_result.get("city_key") == result["city_key"])
        result.update(returned_to_city=returned, page_after="city_panel" if returned else "unknown",
                      return_result=deepcopy(city_result))
        if not returned:
            _problem(result, "return_city", (city_result or {}).get("reason", "product_workflow_city_return_failed")
                     if isinstance(city_result, dict) else "product_workflow_city_return_failed")
    if not result["issues"] and result["returned_to_city"] is not True:
        _problem(result, "return_city", "product_workflow_final_city_unconfirmed")
    if result["issues"]:
        if _progress(result) or any(issue["status"] == "partial" for issue in result["issues"]):
            status = "partial"
        else:
            status = "blocked" if all(issue["status"] == "blocked" for issue in result["issues"]) else "failed"
        result.update(success=False, status=status)
    else:
        result.update(success=True, status="completed" if result["operations"] else "skipped",
                      reason="completed" if result["operations"] else "no_operations")
    for key in ("can_enter", "can_collect", "can_withdraw", "can_return"):
        result.pop(key, None)
    # Ordinary returns are not rendered for FAILED tasks. Keep one explicit
    # summary in the root framework_data as well as the exception diagnostics.
    sink = getattr(engine, "root_context", None) or context
    if sink is not None:
        sink.data["product_workflow_result"] = deepcopy(result)
    logger.info("[ProductWorkflow] result=%s", result)
    if result["success"] is not True:
        raise StopTaskException("Product workflow incomplete: " + result["reason"], success=False,
                                details={"product_workflow": result})
    return result
