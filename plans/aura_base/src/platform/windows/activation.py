# -*- coding: utf-8 -*-
from __future__ import annotations

import time
from typing import Any

from ..contracts import TargetRuntimeError
from ..runtime_config import RuntimeActivationConfig


def execute_activation(
    *,
    target: Any,
    input_backend: Any,
    activation: RuntimeActivationConfig,
    sleep_ms_override: int | None = None,
    mode_override: str | None = None,
    click_point_override: tuple[int, int] | None = None,
    click_button_override: str | None = None,
) -> dict[str, Any]:
    mode = str(mode_override or activation.mode or "focus_sleep").strip().lower()
    if mode not in {"focus_sleep", "focus_click_sleep"}:
        raise TargetRuntimeError(
            "activation_mode_invalid",
            "Activation mode must be one of: focus_sleep, focus_click_sleep.",
            {"mode": mode},
        )

    focused = bool(target.focus())
    if not focused:
        return {
            "ok": False,
            "mode": mode,
            "focused": False,
            "clicked": False,
            "sleep_ms": 0,
            "click_point": None,
            "click_button": None,
        }

    resolved_sleep_ms = int(activation.sleep_ms if sleep_ms_override is None else sleep_ms_override)
    resolved_sleep_ms = max(resolved_sleep_ms, 0)
    click_point = click_point_override if click_point_override is not None else activation.click_point
    click_button = str(click_button_override or activation.click_button or "left").strip().lower() or "left"
    clicked = False
    resolved_click_point: tuple[int, int] | None = None

    if mode == "focus_click_sleep":
        resolved_click_point = _resolve_click_point(target, click_point)
        input_backend.click(
            x=resolved_click_point[0],
            y=resolved_click_point[1],
            button=click_button,
            clicks=1,
            interval=0.0,
        )
        clicked = True

    if resolved_sleep_ms > 0:
        time.sleep(float(resolved_sleep_ms) / 1000.0)

    return {
        "ok": True,
        "mode": mode,
        "focused": True,
        "clicked": clicked,
        "sleep_ms": resolved_sleep_ms,
        "click_point": list(resolved_click_point) if resolved_click_point is not None else None,
        "click_button": click_button if clicked else None,
    }


def _resolve_click_point(target: Any, click_point: tuple[int, int] | None) -> tuple[int, int]:
    if click_point is not None:
        return int(click_point[0]), int(click_point[1])

    _, _, width, height = target.get_client_rect()
    return int(width / 2), int(height / 2)
