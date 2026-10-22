"""Plan-local confirmed clicks. No exception is not evidence of UI success."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

import asyncio
import math

import cv2
import numpy as np

from packages.aura_core.scheduler.operation_scope import (
    OperationScope, OperationTimeout, current_operation_scope,
)
from ....aura_base.src.services.interaction_service import InputSession, InteractionError, InteractionService
from ....aura_base.src.services.observation_service import ObservationService


class CheckedClickError(RuntimeError):
    def __init__(self, reason, data=None):
        super().__init__(reason)
        self.reason = reason
        self.data = dict(data or {})


_CANCEL_CHECK = ContextVar("reforming_center_cancel_check", default=None)
_INTERACTION = ContextVar("reforming_center_interaction", default=None)


@contextmanager
def interaction_context(interaction, scope):
    """Bind one injected interaction service and total budget for this action."""
    if interaction is None or not isinstance(scope, OperationScope):
        raise ValueError("An interaction service and operation scope are required")
    previous = _INTERACTION.get()
    # Nested helper calls share input invalidation with the containing action.
    # Captures and consumed-frame IDs never live on the singleton service.
    metadata = previous[2] if previous is not None and previous[0] is interaction else {
        "inputs": InputSession(), "frame": None,
    }
    token = _INTERACTION.set((interaction, scope, metadata))
    try:
        with scope.activate():
            yield
    finally:
        _INTERACTION.reset(token)


def _operation(app, timeout=None):
    bound = _INTERACTION.get()
    parent = current_operation_scope() or (bound[1] if bound is not None else None)
    target = getattr(app, "target_runtime", None)
    if parent is not None and parent.target is not None and target is not None and parent.target is not target:
        raise ValueError("Interaction app and operation scope have different targets")

    def check_cancelled():
        if parent is not None:
            parent.check_cancelled()
        _check_cancelled()

    scope = OperationScope(timeout, parent=parent,
                           target=target,
                           cancel_check=check_cancelled)
    # Legacy callers have not yet declared interaction injection. They still
    # use this same public implementation, with only a call-local facade.
    service = bound[0] if bound is not None else InteractionService(
        app=app, observation=ObservationService(app=app))
    return service, scope


@contextmanager
def cancellation_checks(check_cancelled):
    """Opt-in cancellation for one operation; leave existing callers unchanged."""
    token = _CANCEL_CHECK.set(check_cancelled)
    try:
        _check_cancelled()
        yield
    finally:
        _CANCEL_CHECK.reset(token)


def _check_cancelled():
    check = _CANCEL_CHECK.get()
    if check is not None:
        check()


def _sleep(seconds):
    bound = _INTERACTION.get()
    scope = current_operation_scope() or (bound[1] if bound is not None else None)
    if scope is None:
        scope = OperationScope(cancel_check=_check_cancelled)
    _check_cancelled()
    scope.sleep(seconds)
    _check_cancelled()


def read_frame(app):
    _check_cancelled()
    service, scope = _operation(app)
    bound = _INTERACTION.get()
    metadata = bound[2] if bound is not None else None
    if metadata is not None:
        metadata["frame"] = None
    try:
        frame = service.observation.capture(scope=scope, roi=(0, 0, 1280, 720))
        image = frame.image
        if (not isinstance(image, np.ndarray) or image.shape != (720, 1280, 3)
                or image.dtype != np.uint8):
            raise CheckedClickError("capture_failed")
        # Public capture provides RGB pixels and verifies target identity before
        # and after capture. Domain readers keep their explicit BGR contract.
        image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        scope.checkpoint()
        if metadata is not None:
            metadata["frame"] = frame
        return image
    except (OperationTimeout, CheckedClickError):
        raise
    except Exception as exc:
        raise CheckedClickError("capture_failed", {"error": str(exc)}) from exc


def rounded_point(point):
    if len(point) != 2 or any(isinstance(value, bool) or not isinstance(value, (int, float))
                              or not math.isfinite(value) for value in point):
        raise CheckedClickError("invalid_click_point")
    result = [int(round(value/5)*5) for value in point]
    if not (0 <= result[0] < 1280 and 0 <= result[1] < 720):
        raise CheckedClickError("invalid_click_point")
    return result


def send_click(app, point, record=None):
    record = record if record is not None else {}
    record.update(input_attempted=False, input_acknowledged=None, call_returned=False,
                  effect_confirmed=False, transition_ready=False)
    record["point"] = rounded_point(point)
    service, scope = _operation(app)
    bound = _INTERACTION.get()
    metadata = bound[2] if bound is not None else None
    source_frame = metadata["frame"] if metadata is not None else None
    input_exception = None

    def click():
        nonlocal input_exception
        record["input_attempted"] = True
        try:
            return app.click(x=record["point"][0], y=record["point"][1])
        except CheckedClickError as exc:
            input_exception = exc
            raise
        except Exception as exc:
            record["input_error"] = {"type": type(exc).__name__, "message": str(exc)}
            raise

    def update_receipt(receipt):
        record["input_attempted"] = receipt.attempted
        record["input_acknowledged"] = receipt.acknowledged
        # An explicit rejection must not satisfy confirm_click's old guard,
        # although the framework receipt correctly records the call's return.
        if receipt.sent and receipt.call_returned:
            record["call_returned"] = True

    try:
        receipt = service.send_input(click, scope=scope, point=tuple(record["point"]),
                                     frame=source_frame,
                                     session=metadata["inputs"] if source_frame is not None else None,
                                     bounds=(0, 0, 1280, 720), check_cancelled=_check_cancelled)
        update_receipt(receipt)
        return record
    except (InteractionError, OperationTimeout, asyncio.CancelledError) as exc:
        receipts = getattr(exc, "receipts", ())
        if receipts:
            update_receipt(receipts[-1])
        if isinstance(exc, asyncio.CancelledError):
            raise
        if input_exception is not None:
            raise input_exception
        if isinstance(exc, OperationTimeout):
            record["input_error"] = {"type": type(exc).__name__, "message": str(exc)}
        raise CheckedClickError("click_input_failed", {"click": dict(record)}) from exc
    except Exception as exc:
        record["input_error"] = {"type": type(exc).__name__, "message": str(exc)}
        raise CheckedClickError("click_input_failed", {"click": dict(record)}) from exc


def _confirm_value(app, probe, service, scope, *, poll_sec=.5, consecutive=2):
    value = service.confirm(lambda: probe(read_frame(app)), scope=scope,
                            stable_scans=int(consecutive), poll_seconds=poll_sec,
                            is_unknown=lambda value: value is None or value is False,
                            sleep=_sleep, check_cancelled=_check_cancelled,
                            before_probe=True)
    if value is None:
        raise CheckedClickError("confirmation_timeout")
    return value


def wait_confirmed(app, probe, timeout_sec=5, poll_sec=.5, consecutive=2):
    if (not 0 < poll_sec <= timeout_sec <= 30 or not isinstance(consecutive, int)
            or not 1 <= consecutive <= 5):
        raise ValueError("Invalid bounded confirmation timing")
    service, scope = _operation(app, timeout_sec)
    return _confirm_value(app, probe, service, scope, poll_sec=poll_sec, consecutive=consecutive)


def confirm_click(app, record, effect, ready=None):
    if not record.get("input_attempted") or not record.get("call_returned"):
        raise CheckedClickError("click_not_sent", {"click": dict(record)})
    service, scope = _operation(app, 10)
    phase = "effect"
    try:
        with scope.activate():
            try:
                record["effect_evidence"] = _confirm_value(app, effect, service, scope.child(5), consecutive=1)
                record["effect_confirmed"] = True
            except CheckedClickError as exc:
                raise CheckedClickError("click_effect_not_confirmed", {"cause": exc.reason, "click": dict(record)}) from exc
            phase = "transition"
            try:
                # These are fresh observations AFTER the first click effect.
                record["ready_evidence"] = _confirm_value(app, ready or effect, service, scope.child(5), consecutive=2)
                record["transition_ready"] = True
                record["transition_required"] = ready is not None
                return record
            except CheckedClickError as exc:
                raise CheckedClickError("click_transition_not_confirmed", {"cause": exc.reason, "click": dict(record)}) from exc
    except OperationTimeout as exc:
        raise CheckedClickError(f"click_{phase}_not_confirmed",
                                {"cause": "confirmation_timeout", "click": dict(record)}) from exc


def checked_click(app, point, effect, ready=None, record=None):
    _, scope = _operation(app, 10)
    # An explicitly bound action retains its frame/session. Legacy callers
    # only inherit the budget here; their domain still owns source freshness.
    with scope.activate():
        return confirm_click(app, send_click(app, point, record), effect, ready)
