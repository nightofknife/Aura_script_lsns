"""Bounded map gestures with a separate system-input drag activation segment."""

from __future__ import annotations

import math
import time
from contextlib import nullcontext
from typing import Any, Callable, Sequence


_SYSTEM_PRIME_PIXELS = 32
_SYSTEM_FRAME_WAIT_SEC = 0.12
_SYSTEM_MIN_HOLD_SEC = 0.55
_MOVE_TICK_SEC = 0.02


def _wait(duration: float, check_cancelled: Callable[[], None]) -> None:
    deadline = time.monotonic() + duration
    while True:
        check_cancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, _MOVE_TICK_SEC))


def _points(
    effective_delta: Sequence[float], safe_rect: Sequence[int], *, system: bool
) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int], tuple[int, int]]:
    """Center the whole activation/drag path inside an (x, y, width, height) box."""
    if len(effective_delta) != 2 or len(safe_rect) != 4:
        raise ValueError("Map drag requires a two-axis delta and an (x,y,width,height) safe rectangle")
    if not all(math.isfinite(float(value)) for value in (*effective_delta, *safe_rect)):
        raise ValueError("Map drag coordinates must be finite")
    dx, dy = (int(round(float(value))) for value in effective_delta)
    x, y, width, height = (int(value) for value in safe_rect)
    if x < 0 or y < 0 or width <= 1 or height <= 1 or x + width > 1280 or y + height > 720:
        raise ValueError("Map drag safe rectangle must stay inside the 1280x720 viewport")

    length = math.hypot(dx, dy)
    if system and length:
        prime_x = int(round(_SYSTEM_PRIME_PIXELS * dx / length))
        prime_y = int(round(_SYSTEM_PRIME_PIXELS * dy / length))
    else:
        prime_x, prime_y = 0, 0

    offset_x = (0, prime_x, prime_x + dx)
    offset_y = (0, prime_y, prime_y + dy)
    if max(offset_x) - min(offset_x) > width - 1 or max(offset_y) - min(offset_y) > height - 1:
        raise ValueError("Effective map drag and its activation segment do not fit the safe rectangle")
    press = (
        int(round((2 * x + width - 1 - min(offset_x) - max(offset_x)) / 2)),
        int(round((2 * y + height - 1 - min(offset_y) - max(offset_y)) / 2)),
    )
    drag_start = (press[0] + prime_x, press[1] + prime_y)
    end = (drag_start[0] + dx, drag_start[1] + dy)
    return press, drag_start, end, (dx, dy)


def perform_map_drag(
    *,
    app: Any,
    effective_delta: Sequence[float],
    safe_rect: Sequence[int],
    check_cancelled: Callable[[], None],
    duration: float = 0.65,
    hold_sec: float = 0.55,
) -> dict[str, Any]:
    """Request an effective screen displacement without OCR or image matching.

    System input first crosses Unity's drag threshold in a separate jump. The
    requested displacement begins at that jump's endpoint, after a frame wait.
    Holding the final point reduces release inertia; it is not a measured
    guarantee of exact movement. Bridge input dispatches BeginDrag directly.
    """
    duration = float(duration)
    hold_sec = float(hold_sec)
    if not math.isfinite(duration) or duration < 0 or not math.isfinite(hold_sec) or hold_sec < 0:
        raise ValueError("Map drag timing must be finite and nonnegative")
    controller = app.controller
    bridge = getattr(controller, "bridge", None)
    bridge_enabled = bridge is not None and bridge.enabled is True
    press, drag_start, end, delta = _points(effective_delta, safe_rect, system=not bridge_enabled)
    final_hold = hold_sec if bridge_enabled else max(hold_sec, _SYSTEM_MIN_HOLD_SEC)
    evidence = {
        "backend": "bridge" if bridge_enabled else "system",
        "press": list(press),
        "drag_start": list(drag_start),
        "end": list(end),
        "effective_delta": list(delta),
        "prime_pixels": math.hypot(drag_start[0] - press[0], drag_start[1] - press[1]),
        "hold_sec": final_hold,
    }
    check_cancelled()
    if delta == (0, 0):
        evidence["skipped"] = True
        return evidence
    if bridge_enabled:
        app.drag(
            press[0], press[1], end[0], end[1],
            duration=duration, hold_before_release_sec=final_hold, stop_inertia=True,
        )
        check_cancelled()
        return evidence

    # Keep the backend fixed while a physical button is held. The outer scope
    # also covers cleanup, so a rejected mode switch cannot strand the button.
    scope = bridge.system_scope() if bridge is not None else nullcontext()
    with scope:
        app.move_to(press[0], press[1], duration=0.0)
        check_cancelled()
        try:
            controller.mouse_down("left")
            _wait(_SYSTEM_FRAME_WAIT_SEC, check_cancelled)
            app.move_to(drag_start[0], drag_start[1], duration=0.0)
            _wait(_SYSTEM_FRAME_WAIT_SEC, check_cancelled)
            steps = max(1, math.ceil(duration / _MOVE_TICK_SEC))
            move_started = time.monotonic()
            for step in range(1, steps + 1):
                check_cancelled()
                fraction = step / steps
                app.move_to(
                    int(round(drag_start[0] + delta[0] * fraction)),
                    int(round(drag_start[1] + delta[1] * fraction)),
                    duration=0.0,
                )
                _wait(max(0.0, move_started + duration * fraction - time.monotonic()), check_cancelled)
            _wait(final_hold, check_cancelled)
        finally:
            # Never check cancellation here: release must run on every exit,
            # including a partially dispatched/failed mouse_down operation.
            controller.mouse_up("left")
    check_cancelled()
    return evidence
