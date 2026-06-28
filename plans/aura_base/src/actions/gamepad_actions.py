# -*- coding: utf-8 -*-
from __future__ import annotations

from packages.aura_core.api import action_info, requires_services

from ..services.gamepad_service import GamepadService


@action_info(name="gamepad.self_check", public=True, read_only=True)
@requires_services(gamepad="gamepad")
def gamepad_self_check(gamepad: GamepadService) -> dict:
    return gamepad.self_check()


@action_info(name="gamepad.press_button", public=True)
@requires_services(gamepad="gamepad")
def gamepad_press_button(gamepad: GamepadService, button: str) -> bool:
    gamepad.press_button(button)
    return True


@action_info(name="gamepad.release_button", public=True)
@requires_services(gamepad="gamepad")
def gamepad_release_button(gamepad: GamepadService, button: str) -> bool:
    gamepad.release_button(button)
    return True


@action_info(name="gamepad.tap_button", public=True)
@requires_services(gamepad="gamepad")
def gamepad_tap_button(gamepad: GamepadService, button: str, duration_ms: int = 0) -> bool:
    gamepad.tap_button(button, duration_ms=duration_ms)
    return True


@action_info(name="gamepad.tilt_stick", public=True)
@requires_services(gamepad="gamepad")
def gamepad_tilt_stick(
    gamepad: GamepadService,
    stick: str,
    x: float,
    y: float,
    duration_ms: int = 0,
    auto_center: bool = False,
) -> bool:
    gamepad.tilt_stick(stick=stick, x=x, y=y, duration_ms=duration_ms, auto_center=auto_center)
    return True


@action_info(name="gamepad.center_stick", public=True)
@requires_services(gamepad="gamepad")
def gamepad_center_stick(gamepad: GamepadService, stick: str) -> bool:
    gamepad.center_stick(stick)
    return True


@action_info(name="gamepad.set_trigger", public=True)
@requires_services(gamepad="gamepad")
def gamepad_set_trigger(
    gamepad: GamepadService,
    side: str,
    value: float,
    duration_ms: int = 0,
    auto_reset: bool = False,
) -> bool:
    gamepad.set_trigger(side=side, value=value, duration_ms=duration_ms, auto_reset=auto_reset)
    return True


@action_info(name="gamepad.reset", public=True)
@requires_services(gamepad="gamepad")
def gamepad_reset(gamepad: GamepadService) -> bool:
    gamepad.reset()
    return True
