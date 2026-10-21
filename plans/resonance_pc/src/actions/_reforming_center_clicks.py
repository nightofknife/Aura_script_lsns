"""Plan-local confirmed clicks. No exception is not evidence of UI success."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

import math
import time

import cv2
import numpy as np
from ._cancellable_wait import sleep_slices


class CheckedClickError(RuntimeError):
    def __init__(self, reason, data=None):
        super().__init__(reason)
        self.reason = reason
        self.data = dict(data or {})


_CANCEL_CHECK = ContextVar("reforming_center_cancel_check", default=None)


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
    if _CANCEL_CHECK.get() is None:
        time.sleep(seconds)
        return
    sleep_slices(seconds, sleep=time.sleep, check_cancelled=_check_cancelled)


def read_frame(app):
    _check_cancelled()
    try:
        captured = app.capture(rect=(0, 0, 1280, 720))
    except Exception as exc:
        raise CheckedClickError("capture_failed", {"error": str(exc)}) from exc
    _check_cancelled()
    image = getattr(captured, "image", None)
    if (not getattr(captured, "success", False) or not isinstance(image, np.ndarray)
            or image.shape != (720, 1280, 3) or image.dtype != np.uint8):
        raise CheckedClickError("capture_failed")
    return cv2.cvtColor(image, cv2.COLOR_RGB2BGR)


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
    record.update(point=rounded_point(point), input_attempted=False,
                  input_acknowledged=None, effect_confirmed=False, transition_ready=False)
    try:
        _check_cancelled()
        record["input_attempted"] = True
        returned = app.click(x=record["point"][0], y=record["point"][1])
        explicit = returned if isinstance(returned, bool) else getattr(returned, "success", None)
        if explicit is not None:
            record["input_acknowledged"] = bool(explicit)
            if not explicit:
                raise CheckedClickError("click_input_failed", {"click": dict(record)})
        # app.click normally returns None. Leave its acknowledgement UNKNOWN.
        record["call_returned"] = True
        return record
    except CheckedClickError:
        raise
    except Exception as exc:
        record["input_error"] = {"type": type(exc).__name__, "message": str(exc)}
        raise CheckedClickError("click_input_failed", {"click": dict(record)}) from exc


def wait_confirmed(app, probe, timeout_sec=5, poll_sec=.5, consecutive=2):
    if (not 0 < poll_sec <= timeout_sec <= 30 or not isinstance(consecutive, int)
            or not 1 <= consecutive <= 5):
        raise ValueError("Invalid bounded confirmation timing")
    previous, count = None, 0
    for _ in range(math.ceil(timeout_sec/poll_sec)):
        _check_cancelled()
        _sleep(poll_sec)
        value = probe(read_frame(app))
        _check_cancelled()
        if value is None or value is False:
            previous, count = None, 0
            continue
        count = count+1 if value == previous else 1
        previous = value
        if count >= consecutive:
            return value
    raise CheckedClickError("confirmation_timeout")


def confirm_click(app, record, effect, ready=None):
    if not record.get("input_attempted") or not record.get("call_returned"):
        raise CheckedClickError("click_not_sent", {"click": dict(record)})
    try:
        record["effect_evidence"] = wait_confirmed(app, effect, consecutive=1)
        record["effect_confirmed"] = True
    except CheckedClickError as exc:
        raise CheckedClickError("click_effect_not_confirmed", {"cause": exc.reason, "click": dict(record)}) from exc
    try:
        # These are fresh observations AFTER the first click-effect evidence.
        record["ready_evidence"] = wait_confirmed(app, ready or effect, consecutive=2)
        record["transition_ready"] = True
        record["transition_required"] = ready is not None
        return record
    except CheckedClickError as exc:
        raise CheckedClickError("click_transition_not_confirmed", {"cause": exc.reason, "click": dict(record)}) from exc


def checked_click(app, point, effect, ready=None, record=None):
    return confirm_click(app, send_click(app, point, record), effect, ready)
