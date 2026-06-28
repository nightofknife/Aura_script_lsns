from __future__ import annotations

import time

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger

from ..platform.contracts import TargetRuntimeError
from ..platform.look_math import resolve_look_direction_vector
from ..services.app_provider_service import AppProviderService


@action_info(name="click", public=True)
@requires_services(app="app")
def click(
    app: AppProviderService,
    x: int | None = None,
    y: int | None = None,
    button: str = "left",
    clicks: int = 1,
    interval: float = 0.1,
):
    if x is not None and y is not None:
        app.click(x, y, button, clicks, interval)
    else:
        logger.info("在当前鼠标位置点击...")
        app.click(button=button, clicks=clicks, interval=interval)
    return True


@action_info(name="double_click", public=True)
@requires_services(app="app")
def double_click(app: AppProviderService, x: int | None = None, y: int | None = None):
    click(app, x, y, button="left", clicks=2, interval=0.05)
    return True


@action_info(name="right_click", public=True)
@requires_services(app="app")
def right_click(app: AppProviderService, x: int | None = None, y: int | None = None):
    click(app, x, y, button="right", clicks=1)
    return True


@action_info(name="move_to", public=True)
@requires_services(app="app")
def move_to(app: AppProviderService, x: int, y: int, duration: float = 0.25):
    app.move_to(x, y, duration)
    return True


@action_info(name="drag", public=True)
@requires_services(app="app")
def drag(
    app: AppProviderService,
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    button: str = "left",
    duration: float = 0.5,
):
    app.drag(start_x, start_y, end_x, end_y, button, duration)
    return True


@action_info(name="press_key", public=True)
@requires_services(app="app")
def press_key(app: AppProviderService, key: str, presses: int = 1, interval: float = 0.1):
    app.press_key(key, presses, interval)
    return True


@action_info(name="press_hotkey", public=True)
@requires_services(app="app")
def press_hotkey(app: AppProviderService, keys: list[str]):
    if not isinstance(keys, list) or not keys:
        logger.error("'press_hotkey' 的 'keys' 参数必须是一个非空列表。")
        return False
    logger.info("正在按下组合键: %s", keys)
    with app.hold_key(keys[0]):
        for key in keys[1:]:
            app.press_key(key)
    return True


@action_info(name="type_text", public=True)
@requires_services(app="app")
def type_text(app: AppProviderService, text: str, interval: float = 0.01):
    app.type_text(text, interval)
    return True


@action_info(name="scroll", public=True)
@requires_services(app="app")
def scroll(app: AppProviderService, direction: str, amount: int):
    normalized_direction = str(direction or "").lower()
    if normalized_direction not in {"up", "down"}:
        logger.error("无效的滚动方向: '%s'。请使用 'up' 或 'down'。", direction)
        return False
    logger.info("向 %s 滚动 %s 单位。", normalized_direction, amount)
    app.scroll(int(amount), normalized_direction)
    return True


@action_info(name="get_pixel_color", read_only=True, public=True)
@requires_services(app="app")
def get_pixel_color(app: AppProviderService, x: int, y: int) -> tuple:
    return app.get_pixel_color(x, y)


@action_info(name="mouse_move_relative", public=True)
@requires_services(app="app")
def mouse_move_relative(app: AppProviderService, dx: int, dy: int, duration: float = 0.2):
    app.move_relative(dx, dy, duration)
    return True


@action_info(name="look_delta", public=True)
@requires_services(app="app")
def look_delta(app: AppProviderService, dx: int, dy: int) -> bool:
    app.look_delta(dx, dy)
    return True


@action_info(name="look_hold", public=True)
@requires_services(app="app")
def look_hold(app: AppProviderService, vx: float, vy: float, duration_ms: int, tick_ms: int = 16) -> bool:
    app.look_hold(vx, vy, duration_ms=duration_ms, tick_ms=tick_ms)
    return True


@action_info(name="look_direction", public=True)
@requires_services(app="app")
def look_direction(
    app: AppProviderService,
    direction: str,
    strength: float = 0.4,
    duration_ms: int = 200,
    tick_ms: int = 16,
) -> bool:
    vx, vy = resolve_look_direction_vector(direction, strength)
    if vx == 0.0 and vy == 0.0:
        return True
    app.look_hold(vx, vy, duration_ms=duration_ms, tick_ms=tick_ms)
    return True


@action_info(name="look_sweep_horizontal", public=True)
@requires_services(app="app")
def look_sweep_horizontal(
    app: AppProviderService,
    total_dx: int,
    chunk_dx: int = 24,
    step_delay_ms: int = 16,
) -> bool:
    total = int(total_dx)
    if total == 0:
        return True

    chunk = int(chunk_dx)
    if chunk == 0:
        raise TargetRuntimeError(
            "look_delta_invalid",
            "look_sweep_horizontal chunk_dx must not be 0.",
            {"chunk_dx": chunk_dx},
        )
    if step_delay_ms < 0:
        raise TargetRuntimeError(
            "look_tick_invalid",
            "look_sweep_horizontal step_delay_ms must be >= 0.",
            {"step_delay_ms": step_delay_ms},
        )

    direction = 1 if total > 0 else -1
    chunk_size = abs(chunk)
    remaining = abs(total)
    while remaining > 0:
        current = min(chunk_size, remaining) * direction
        app.look_delta(current, 0)
        remaining -= min(chunk_size, remaining)
        if remaining > 0 and step_delay_ms > 0:
            time.sleep(float(step_delay_ms) / 1000.0)
    return True


@action_info(name="key_down", public=True)
@requires_services(app="app")
def key_down(app: AppProviderService, key: str):
    app.key_down(key)
    return True


@action_info(name="key_up", public=True)
@requires_services(app="app")
def key_up(app: AppProviderService, key: str):
    app.key_up(key)
    return True


@action_info(name="mouse_down", public=True)
@requires_services(app="app")
def mouse_down(app: AppProviderService, button: str = "left"):
    logger.info("按下鼠标 '%s' 键", button)
    app.mouse_down(button)
    return True


@action_info(name="mouse_up", public=True)
@requires_services(app="app")
def mouse_up(app: AppProviderService, button: str = "left"):
    logger.info("松开鼠标 '%s' 键", button)
    app.mouse_up(button)
    return True
