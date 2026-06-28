# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from packages.aura_core.api import service_info

from ..platform.runtime_service import TargetRuntimeService


@service_info(alias="controller", public=True, deps={"target_runtime": "target_runtime"})
class ControllerService:
    """Runtime-backed input facade."""

    def __init__(self, target_runtime: TargetRuntimeService):
        self.target_runtime = target_runtime
        self._held_keys = set()
        self._held_mouse_buttons = set()

    def release_all(self):
        self.target_runtime.release_all()
        self._held_keys.clear()
        self._held_mouse_buttons.clear()

    def release_key(self):
        for key in list(self._held_keys):
            self.key_up(key)

    def release_mouse(self):
        for button in list(self._held_mouse_buttons):
            self.mouse_up(button)

    def move_to(self, x: int, y: int, duration: float | None = None):
        self.target_runtime.move_to(x, y, duration=duration)

    def move_relative(self, dx: int, dy: int, duration: float | None = None):
        self.target_runtime.move_relative(dx, dy, duration=duration)

    def mouse_down(self, button: str = "left"):
        self.target_runtime.mouse_down(button=button)
        self._held_mouse_buttons.add(str(button).lower())

    def mouse_up(self, button: str = "left"):
        self.target_runtime.mouse_up(button=button)
        self._held_mouse_buttons.discard(str(button).lower())

    def click(
        self,
        x: int | None = None,
        y: int | None = None,
        button: str = "left",
        clicks: int = 1,
        interval: float | None = None,
    ):
        self.target_runtime.click(x=x, y=y, button=button, clicks=clicks, interval=interval)

    def drag_to(self, x: int, y: int, button: str = "left", duration: float | None = None):
        self.target_runtime.drag_to(x, y, button=button, duration=duration)

    def look_delta(self, dx: int, dy: int):
        self.target_runtime.look_delta(dx, dy)

    def look_hold(
        self,
        vx: float,
        vy: float,
        *,
        duration_ms: int,
        tick_ms: int | None = None,
    ):
        self.target_runtime.look_hold(vx, vy, duration_ms=duration_ms, tick_ms=tick_ms)

    def scroll(self, amount: int, direction: str = "down"):
        self.target_runtime.scroll(amount, direction)

    def key_down(self, key: str):
        self.target_runtime.key_down(key)
        self._held_keys.add(str(key).lower())

    def key_up(self, key: str):
        self.target_runtime.key_up(key)
        self._held_keys.discard(str(key).lower())

    def press_key(self, key: str, presses: int = 1, interval: float | None = None):
        self.target_runtime.press_key(key, presses, interval)

    def type_text(self, text: str, interval: float | None = None):
        self.target_runtime.type_text(text, interval)

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
            await self.key_down_async(key)
            yield
        finally:
            await self.key_up_async(key)

    async def release_all_async(self):
        await asyncio.to_thread(self.release_all)

    async def release_key_async(self):
        await asyncio.to_thread(self.release_key)

    async def release_mouse_async(self):
        await asyncio.to_thread(self.release_mouse)

    async def move_to_async(self, x: int, y: int, duration: float | None = None):
        await asyncio.to_thread(self.move_to, x, y, duration)

    async def move_relative_async(self, dx: int, dy: int, duration: float | None = None):
        await asyncio.to_thread(self.move_relative, dx, dy, duration)

    async def mouse_down_async(self, button: str = "left"):
        await asyncio.to_thread(self.mouse_down, button)

    async def mouse_up_async(self, button: str = "left"):
        await asyncio.to_thread(self.mouse_up, button)

    async def click_async(
        self,
        x: int | None = None,
        y: int | None = None,
        button: str = "left",
        clicks: int = 1,
        interval: float | None = None,
    ):
        await asyncio.to_thread(self.click, x, y, button, clicks, interval)

    async def drag_to_async(self, x: int, y: int, button: str = "left", duration: float | None = None):
        await asyncio.to_thread(self.drag_to, x, y, button, duration)

    async def look_delta_async(self, dx: int, dy: int):
        await asyncio.to_thread(self.look_delta, dx, dy)

    async def look_hold_async(
        self,
        vx: float,
        vy: float,
        *,
        duration_ms: int,
        tick_ms: int | None = None,
    ):
        await asyncio.to_thread(self.look_hold, vx, vy, duration_ms=duration_ms, tick_ms=tick_ms)

    async def scroll_async(self, amount: int, direction: str = "down"):
        await asyncio.to_thread(self.scroll, amount, direction)

    async def key_down_async(self, key: str):
        await asyncio.to_thread(self.key_down, key)

    async def key_up_async(self, key: str):
        await asyncio.to_thread(self.key_up, key)

    async def press_key_async(self, key: str, presses: int = 1, interval: float | None = None):
        await asyncio.to_thread(self.press_key, key, presses, interval)

    async def type_text_async(self, text: str, interval: float | None = None):
        await asyncio.to_thread(self.type_text, text, interval)
