# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from typing import Optional, Tuple

from packages.aura_core.api import service_info

from ..platform.runtime_service import TargetRuntimeService
from .controller_service import ControllerService
from .screen_service import CaptureResult, ScreenService


@service_info(alias="app", public=True, deps={"screen": "screen", "controller": "controller", "target_runtime": "target_runtime"})
class AppProviderService:
    """High-level app interactor facade built on the runtime adapter."""

    def __init__(
        self,
        screen: ScreenService,
        controller: ControllerService,
        target_runtime: TargetRuntimeService,
    ):
        self.screen = screen
        self.controller = controller
        self.target_runtime = target_runtime
        self.window_title = None

    def capture(self, rect: Optional[Tuple[int, int, int, int]] = None) -> CaptureResult:
        return self.screen.capture(rect)

    async def capture_async(self, rect: Optional[Tuple[int, int, int, int]] = None) -> CaptureResult:
        return await self.screen.capture_async(rect)

    def get_window_size(self) -> Optional[Tuple[int, int]]:
        rect = self.screen.get_client_rect()
        if rect is None:
            return None
        return int(rect[2]), int(rect[3])

    def move_to(self, x: int, y: int, duration: float | None = None):
        self.controller.move_to(int(x), int(y), duration)

    async def move_to_async(self, x: int, y: int, duration: float | None = None):
        await self.controller.move_to_async(int(x), int(y), duration)

    def click(
        self,
        x: int | None = None,
        y: int | None = None,
        button: str = "left",
        clicks: int = 1,
        interval: float | None = None,
    ):
        self.controller.click(
            None if x is None else int(x),
            None if y is None else int(y),
            button=button,
            clicks=clicks,
            interval=interval,
        )

    async def click_async(self, x: int, y: int, button: str = "left", clicks: int = 1, interval: float | None = None):
        await self.controller.click_async(int(x), int(y), button=button, clicks=clicks, interval=interval)

    def drag(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        button: str = "left",
        duration: float | None = None,
    ):
        self.move_to(int(start_x), int(start_y), duration=0.0)
        self.controller.mouse_down(button)
        self.controller.move_to(int(end_x), int(end_y), duration=duration)
        self.controller.mouse_up(button)

    async def drag_async(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        button: str = "left",
        duration: float | None = None,
    ):
        await self.move_to_async(int(start_x), int(start_y), duration=0.0)
        await self.controller.mouse_down_async(button)
        await self.controller.move_to_async(int(end_x), int(end_y), duration=duration)
        await self.controller.mouse_up_async(button)

    def look_delta(self, dx: int, dy: int):
        self.controller.look_delta(int(dx), int(dy))

    async def look_delta_async(self, dx: int, dy: int):
        await self.controller.look_delta_async(int(dx), int(dy))

    def look_hold(
        self,
        vx: float,
        vy: float,
        *,
        duration_ms: int,
        tick_ms: int | None = None,
    ):
        self.controller.look_hold(vx, vy, duration_ms=duration_ms, tick_ms=tick_ms)

    async def look_hold_async(
        self,
        vx: float,
        vy: float,
        *,
        duration_ms: int,
        tick_ms: int | None = None,
    ):
        await self.controller.look_hold_async(vx, vy, duration_ms=duration_ms, tick_ms=tick_ms)

    def scroll(self, amount: int, direction: str = "down"):
        self.controller.scroll(int(amount), direction)

    async def scroll_async(self, amount: int, direction: str = "down"):
        await self.controller.scroll_async(int(amount), direction)

    def press_key(self, key: str, presses: int = 1, interval: float | None = None):
        self.controller.press_key(key, presses, interval)

    def move_relative(self, dx: int, dy: int, duration: float | None = None):
        self.controller.move_relative(int(dx), int(dy), duration)

    def key_down(self, key: str):
        self.controller.key_down(key)

    def key_up(self, key: str):
        self.controller.key_up(key)

    def mouse_down(self, button: str = "left"):
        self.controller.mouse_down(button)

    def mouse_up(self, button: str = "left"):
        self.controller.mouse_up(button)

    @contextmanager
    def hold_key(self, key: str):
        try:
            self.key_down(key)
            yield
        finally:
            self.key_up(key)

    @asynccontextmanager
    async def hold_key_async(self, key: str):
        try:
            await self.controller.key_down_async(key)
            yield
        finally:
            await self.controller.key_up_async(key)

    def release_all_keys(self):
        self.controller.release_all()

    def release_all(self):
        self.controller.release_all()

    def get_pixel_color(self, x: int, y: int) -> tuple[int, int, int]:
        return self.screen.get_pixel_color_at(int(x), int(y))

    async def get_pixel_color_async(self, x: int, y: int) -> tuple[int, int, int]:
        return await asyncio.to_thread(self.get_pixel_color, x, y)

    def type_text(self, text: str, interval: float | None = None):
        self.controller.type_text(text, interval)

    async def type_text_async(self, text: str, interval: float | None = None):
        await self.controller.type_text_async(text, interval)

    def focus(self) -> bool:
        return self.screen.focus()

    def focus_with_input(self, click_delay: float = 0.3) -> bool:
        return self.target_runtime.focus_with_input(click_delay)
