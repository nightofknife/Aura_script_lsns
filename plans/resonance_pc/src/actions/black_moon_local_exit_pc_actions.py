"""Purchase settling and bounded destination-aware exit; never resubmit payment."""
from __future__ import annotations

import asyncio
import time

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger

from ._black_moon_local_exit_vision import BlackMoonLocalExitVision
from .black_moon_local_prepare_pc_actions import _capture, _cancel_check, _validated_center
from .black_moon_local_select_pc_actions import _same_view
from ._black_moon_local_scan_contract import validated_scan_completion, validated_selection_presence


_WAIT_SECONDS = 7.
_PURCHASE_WAIT_SECONDS = 30.
_POLL_SECONDS = .2
_SETTLE_SECONDS = .5
_MAX_SUPPLEMENTAL_CLICKS = 3
_TARGETS = {"rest_menu", "main"}


class BlackMoonLocalExitError(RuntimeError):
    def __init__(self, code, message, detail=None):
        super().__init__(message)
        self.code, self.detail = code, dict(detail or {})

    def to_dict(self):
        return {"code": self.code, "message": str(self), "detail": self.detail}


def _settled(state):
    return (state.get("page_state") == "local_shop" and state.get("shop_ready") is True
            and state.get("total_zero") is True)


async def execute_black_moon_wait_purchase(submission, app, reader=None):
    _cancel_check()
    if not isinstance(submission, dict) or submission.get("success") is not True:
        raise ValueError("Successful submission result is required")
    if submission.get("purchase_submitted") is False and submission.get("status") == "skipped":
        scan = submission.get("scan")
        if scan is not None:
            validated_scan_completion(scan)
            if validated_selection_presence(scan)["has_selected_goods"] is not False:
                raise ValueError("No-goods exit requires a completed scan without selected targets")
            return {"success": True, "status": "skipped", "reason": "no_selected_goods",
                    "ready_to_exit": True, "purchase_verified": False, "page_state": "local_shop"}
        return {"success": True, "status": "skipped", "reason": "no_purchase_submitted",
                "ready_to_exit": False, "purchase_verified": False}
    if submission.get("purchase_submitted") is not True:
        raise ValueError("Purchase submission must be explicitly confirmed")
    reader = reader if reader is not None else BlackMoonLocalExitVision()
    deadline = time.monotonic() + _PURCHASE_WAIT_SECONDS
    previous, last = None, {}
    while time.monotonic() < deadline:
        frame = _capture(app)
        last = reader.observe(frame)
        if _settled(last):
            if previous is not None and _same_view(previous, frame, reader.VIEWPORT):
                _cancel_check()
                result = {"success": True, "status": "purchase_settled", "ready_to_exit": True,
                          "purchase_verified": True, "page_state": "local_shop"}
                logger.info("[BlackMoonExit] purchase_settled=%s", result)
                return result
            previous = frame
        else:
            previous = None
        await asyncio.sleep(_POLL_SECONDS)
    raise BlackMoonLocalExitError("black_moon_purchase_not_settled",
                                 "Purchase did not return to a stable zero-total LOCAL shop; no exit input", last)


async def execute_black_moon_exit(purchase_result, exit_target, app, reader=None):
    _cancel_check()
    if not isinstance(exit_target, str) or exit_target not in _TARGETS:
        raise ValueError("Exit target must be rest_menu or main")
    if not isinstance(purchase_result, dict) or purchase_result.get("success") is not True:
        raise ValueError("Successful purchase-settling result is required")
    if purchase_result.get("ready_to_exit") is False:
        return {"success": True, "status": "skipped", "reason": "no_purchase_submitted",
                "exit_target": exit_target, "exited": False, "click_count": 0, "supplemental_clicks": 0}
    if purchase_result.get("ready_to_exit") is not True:
        raise ValueError("Purchase settling must explicitly allow exit")
    reader = reader if reader is not None else BlackMoonLocalExitVision()
    clicks, last = 0, {}
    # No-target visits exit directly; zero-total evidence is only required after
    # a submitted purchase has been verified, never as an initial condition.
    require_zero_total = purchase_result.get("purchase_verified") is True

    async def probe(allow_source):
        nonlocal last
        deadline = time.monotonic() + _WAIT_SECONDS
        previous_frame, previous_key, stable = None, None, 0
        while time.monotonic() < deadline:
            frame = _capture(app)
            last = reader.observe(frame)
            kind, hit, key = None, None, None
            if last.get("page_state") == exit_target:
                kind, key = "target", ("target", exit_target)
            elif allow_source and ((last.get("page_state") == "local_shop"
                    and last.get("shop_ready") is True
                    and (not require_zero_total or last.get("total_zero") is True)) or (
                    exit_target == "main" and last.get("page_state") == "rest_menu")):
                hit = reader.button(frame, exit_target)
                if hit["found"]:
                    kind, key = "source", ("source", _validated_center(hit))
            if kind is not None:
                stable = stable + 1 if (key == previous_key and (
                    kind == "target" or _same_view(previous_frame, frame, reader.VIEWPORT))) else 1
                previous_key, previous_frame = key, frame
                if stable >= 2:
                    return kind, hit
            else:
                previous_key, previous_frame, stable = None, None, 0
            await asyncio.sleep(_POLL_SECONDS)
        return None, None

    def completed():
        _cancel_check()
        result = {"success": True, "status": "exited", "exit_target": exit_target,
                  "exited": True, "page_state": exit_target, "click_count": clicks,
                  "supplemental_clicks": max(0, clicks - 1)}
        logger.info("[BlackMoonExit] completed=%s", result)
        return result

    for _ in range(_MAX_SUPPLEMENTAL_CLICKS + 1):
        kind, hit = await probe(allow_source=True)
        if kind == "target":
            return completed()
        if kind != "source":
            raise BlackMoonLocalExitError("black_moon_exit_source_unknown",
                                         "Ready LOCAL shop and fresh exit button are not confirmed", last)
        point = _validated_center(hit)
        _cancel_check()
        app.click(x=int(point[0]), y=int(point[1]))
        clicks += 1
        await asyncio.sleep(_SETTLE_SECONDS)
        kind, _ = await probe(allow_source=False)
        if kind == "target":
            return completed()
    raise BlackMoonLocalExitError("black_moon_exit_click_exhausted",
                                 "Exit exhausted initial click and three supplements",
                                 {"exit_target": exit_target, "click_count": clicks, **last})


@action_info(name="resonance_pc.wait_black_moon_local_purchase", public=True, read_only=True,
             description="Wait for stable LOCAL shop and zero total without clicking or resubmitting.")
@requires_services(app="plans/aura_base/app")
async def resonance_pc_wait_black_moon_local_purchase(submission: dict, app=None) -> dict:
    return await execute_black_moon_wait_purchase(submission, app)


@action_info(name="resonance_pc.exit_black_moon_local_shop", public=True, read_only=False,
             description="Exit settled LOCAL shopping to rest menu or main screen using bounded feedback.")
@requires_services(app="plans/aura_base/app")
async def resonance_pc_exit_black_moon_local_shop(purchase_result: dict, exit_target: str = "rest_menu", app=None) -> dict:
    return await execute_black_moon_exit(purchase_result, exit_target, app)
