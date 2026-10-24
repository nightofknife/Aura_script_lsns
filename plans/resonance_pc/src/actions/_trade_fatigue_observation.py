"""Read fatigue on a stable trade page without leaving or operating the page."""
from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any

from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested
from packages.aura_core.scheduler.operation_scope import OperationTimeout
from packages.aura_core.utils.exceptions import StopTaskException, TaskControlException
from ....aura_base.src.services.observation_service import CaptureError, ObservationService
from ._fatigue_observation import FatigueConfirmation, read_fatigue_pair


# 1280x720 game-client coordinates. Exclude the adjacent fatigue '+' button.
TRADE_FATIGUE_ROI = (938, 14, 95, 24)


def _check_cancelled() -> None:
    if is_current_task_cancel_requested():
        raise StopTaskException("Trade fatigue reading cancelled", success=False)


def read_trade_fatigue(
    app: Any, ocr: Any, vision: Any = None, *,
    check_cancelled: Callable[[], None] | None = None,
    max_attempts: int = 4, timeout_sec: float = 3.0, poll_seconds: float = 0.08,
) -> dict:
    """Return two matching fresh observations, or unknown for navigation fallback.

    The caller must have confirmed the trade page and dismissed settlement
    overlays. No screenshot or observation survives an input or polling round.
    Cancellation and the enclosing operation deadline always propagate.
    """
    if type(max_attempts) is not int or not 2 <= max_attempts <= 8:
        raise ValueError("max_attempts must be an integer between 2 and 8")
    if not math.isfinite(timeout_sec) or timeout_sec <= 0:
        raise ValueError("timeout_sec must be finite and positive")
    if not math.isfinite(poll_seconds) or poll_seconds < 0:
        raise ValueError("poll_seconds must be finite and nonnegative")
    guard = check_cancelled or _check_cancelled
    guard()
    deadline = time.monotonic() + timeout_sec
    confirmation = FatigueConfirmation()
    attempts, reason = 0, "fatigue_unrecognized"
    observation = ObservationService(app=app)
    while attempts < max_attempts:
        guard()
        if time.monotonic() >= deadline:
            reason = "observation_timeout"
            break
        attempts += 1
        value = None
        frame_id = None
        try:
            frame = observation.capture_regions((TRADE_FATIGUE_ROI,))
            frame_id = frame.id
            guard()
            value, reason = read_fatigue_pair(frame, TRADE_FATIGUE_ROI, ocr, guard=guard)
        except (OperationTimeout, TaskControlException):
            raise
        except CaptureError:
            guard()
            reason = "capture_failed"
        except Exception:
            guard()
            reason = "ocr_failed"
        guard()
        # A slow capture/OCR result cannot authorize a recovery after its budget.
        if time.monotonic() >= deadline:
            reason = "observation_timeout"
            break
        if confirmation.feed(value, frame_id):
            return {"success": True, "current_fatigue": value[0], "max_fatigue": value[1],
                    "reason": "stable", "source": "trade_page", "attempts": attempts}
        if value is not None:
            reason = "unstable_fatigue"
        if attempts < max_attempts:
            guard()
            time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))
    guard()
    return {"success": False, "current_fatigue": None, "max_fatigue": None,
            "reason": reason, "source": "trade_page", "attempts": attempts}
