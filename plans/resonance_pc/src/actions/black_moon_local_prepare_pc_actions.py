"""Verified transitions from a guaranteed rest menu to LOCAL batch shopping."""
from __future__ import annotations

import asyncio
import math
import time

import numpy as np

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested

from ._black_moon_local_prepare_vision import BlackMoonLocalPrepareVision
from ._black_moon_local_shop_vision import BlackMoonLocalShopRecognitionError
from ._template_click_transition import (
    TransitionProbe, run_template_transition, wait_for_transition_probe,
)


_STEP_TIMEOUT_SECONDS = 7.0
_POLL_SECONDS = .2
_CLICK_SETTLE_SECONDS = .35
_MAX_SUPPLEMENTAL_CLICKS = 3
_STEPS = {"enter_shop", "select_local", "enable_batch", "verify_ready"}
_UNKNOWN = {"page_state": "unknown", "local_selected": None, "batch_enabled": None, "ready": False}


class BlackMoonLocalPrepareError(RuntimeError):
    def __init__(self, code, message, detail=None):
        super().__init__(message)
        self.code, self.detail = code, dict(detail or {})

    def to_dict(self):
        return {"code": self.code, "message": str(self), "detail": self.detail}


def _cancel_check():
    if is_current_task_cancel_requested():
        raise BlackMoonLocalPrepareError("black_moon_prepare_cancelled", "Shopping preparation cancelled")


def _capture(app):
    _cancel_check()
    result = app.capture()
    if not result.success or result.image is None:
        raise BlackMoonLocalPrepareError("black_moon_prepare_capture_failed", "Could not capture the shopping interface")
    frame = np.asarray(result.image)
    if frame.shape != (720, 1280, 3) or frame.dtype != np.uint8:
        raise BlackMoonLocalPrepareError("black_moon_prepare_resolution_mismatch", "Preparation requires uint8 RGB 1280x720")
    return frame


def _target(step, state):
    shop = state["page_state"] in {"shop_page", "local_shop"}
    if step == "enter_shop":
        return shop
    if step == "select_local":
        return shop and state["local_selected"] is True
    return state["page_state"] == "local_shop" and state["local_selected"] is True and state["batch_enabled"] is True


def _source(step, state):
    if step == "select_local":
        return state["page_state"] in {"shop_page", "local_shop"} and state["local_selected"] is False
    return state["page_state"] == "local_shop" and state["local_selected"] is True and state["batch_enabled"] is False


def _validated_center(hit):
    center = hit.get("center")
    if (not isinstance(center, (tuple, list)) or len(center) != 2
            or any(type(value) not in (int, float) or not math.isfinite(value) for value in center)
            or not 0 <= center[0] < 1280 or not 0 <= center[1] < 720):
        raise BlackMoonLocalPrepareError("black_moon_prepare_click_missing", "Matched control has no valid client point")
    return tuple(center)


async def execute_black_moon_prepare_step(step, app, reader=None):
    if step not in _STEPS:
        raise ValueError("Unknown Black Moon preparation step")
    if app is None:
        raise RuntimeError("app service is required")
    reader = reader if reader is not None else BlackMoonLocalPrepareVision()
    clicks, last = 0, dict(_UNKNOWN)
    button = {"enter_shop": "shop_entry", "select_local": "local_tab_unselected",
              "enable_batch": "batch_off"}.get(step)

    def observe(before_click):
        nonlocal last
        frame = _capture(app)
        try:
            # The user guarantees the initial rest menu; only locate its entry.
            if before_click and step == "enter_shop" and clicks == 0:
                hit = reader.match(frame, button)
                return (TransitionProbe("source", hit=hit, immediate=True)
                        if hit["found"] else TransitionProbe())
            last = reader.observe(frame)
            if _target(step, last):
                kind, hit = "target", None
            elif before_click and button and (
                    (step == "enter_shop" and last["page_state"] == "unknown") or _source(step, last)):
                hit = reader.match(frame, button)
                kind = "source" if hit["found"] else "unknown"
            else:
                kind, hit = "unknown", None
            key = (kind, last["page_state"], last["local_selected"], last["batch_enabled"],
                   _validated_center(hit) if kind == "source" else None)
            return TransitionProbe(kind, key=key, hit=hit)
        except BlackMoonLocalShopRecognitionError as exc:
            if exc.code in {"template_load_failed", "unknown_template", "invalid_image"}:
                raise
            return TransitionProbe()

    async def wait_probe(before_click=False):
        return await wait_for_transition_probe(
            observe, allow_source=before_click, timeout_seconds=_STEP_TIMEOUT_SECONDS,
            poll_seconds=_POLL_SECONDS, monotonic=time.monotonic, sleep=asyncio.sleep,
            check_cancelled=_cancel_check,
        )

    def completed():
        _cancel_check()
        result = {"success": True, "step": step, **{key: last[key] for key in _UNKNOWN},
                  "click_count": clicks, "supplemental_clicks": max(0, clicks - 1), "skipped": clicks == 0}
        logger.info("[BlackMoonPrepare] completed=%s", result)
        return result

    if step == "verify_ready":
        observed = await wait_probe()
        if observed is not None and observed.kind == "target":
            return completed()
        raise BlackMoonLocalPrepareError("black_moon_prepare_not_ready", "LOCAL batch shopping is not confirmed", last)

    def click(observed, attempt):
        nonlocal clicks
        center = _validated_center(observed.hit)
        app.click(x=int(center[0]), y=int(center[1]))
        clicks = attempt
        logger.info("[BlackMoonPrepare] step=%s click_count=%s", step, clicks)

    await run_template_transition(
        wait_probe, click, max_clicks=_MAX_SUPPLEMENTAL_CLICKS + 1,
        settle_seconds=_CLICK_SETTLE_SECONDS, sleep=asyncio.sleep, check_cancelled=_cancel_check,
        source_unconfirmed=lambda: BlackMoonLocalPrepareError(
            "black_moon_prepare_source_unconfirmed", "Required control state is not confirmed",
            {"step": step, "click_count": clicks, **last}),
        exhausted=lambda: BlackMoonLocalPrepareError(
            "black_moon_prepare_transition_timeout", "Shopping preparation exhausted bounded clicks",
            {"step": step, "click_count": clicks, **last}),
    )
    return completed()


@action_info(name="resonance_pc.prepare_black_moon_local_shop_step", public=True, read_only=False,
             description="Verify one preparation transition without selecting, buying, refreshing, or recording stock.")
@requires_services(app="plans/aura_base/app")
async def resonance_pc_prepare_black_moon_local_shop_step(step: str, app=None) -> dict:
    return await execute_black_moon_prepare_step(step, app)
