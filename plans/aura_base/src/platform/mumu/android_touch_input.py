# -*- coding: utf-8 -*-
from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..contracts import DeviceViewport, TargetRuntimeError
from .helper_manager import AndroidTouchHelperManager


class MuMuAndroidTouchInputBackend:
    PRIMARY_CONTACT = 0

    def __init__(
        self,
        helper_manager: AndroidTouchHelperManager,
        viewport_provider: Callable[[], Tuple[int, int, int, int] | None],
        touch_physical_size: Optional[Tuple[int, int]] = None,
        display_rotation: int = 0,
        config: Dict[str, Any] | None = None,
    ):
        self.helper_manager = helper_manager
        self.viewport_provider = viewport_provider
        self.touch_physical_size = (
            (int(touch_physical_size[0]), int(touch_physical_size[1])) if touch_physical_size else None
        )
        self.display_rotation = int(display_rotation or 0) % 4
        self.config = dict(config or {})
        self.default_pressure = int(self.config.get("pressure") or 50)
        self.path_fps = max(int(self.config.get("path_fps") or 60), 5)
        self._active_contacts: Dict[int, Tuple[int, int]] = {}
        self._cursor_position: Optional[Tuple[int, int]] = None

    def ensure_ready(self):
        self.helper_manager.ensure_ready()
        if self._cursor_position is None:
            self._cursor_position = self._default_point()

    def is_healthy(self) -> bool:
        return self.helper_manager.is_healthy()

    def close(self):
        self.helper_manager.close()

    def down(self, pointer_id: int, x: int, y: int, pressure: Optional[int] = None):
        self.ensure_ready()
        logical_x, logical_y = self._clamp_point(x, y)
        px, py = self._map_to_touch_point(logical_x, logical_y)
        self._active_contacts[pointer_id] = (logical_x, logical_y)
        self._cursor_position = (logical_x, logical_y)
        self.helper_manager.send_commands(
            [
                {"type": "down", "contact": int(pointer_id), "x": px, "y": py, "pressure": int(pressure or self.default_pressure)},
                {"type": "commit"},
            ]
        )

    def move(self, pointer_id: int, x: int, y: int, pressure: Optional[int] = None):
        self.ensure_ready()
        if pointer_id not in self._active_contacts:
            raise TargetRuntimeError(
                "touch_pointer_inactive",
                f"Pointer {pointer_id} is not active.",
                {"pointer_id": pointer_id},
            )
        logical_x, logical_y = self._clamp_point(x, y)
        px, py = self._map_to_touch_point(logical_x, logical_y)
        self._active_contacts[pointer_id] = (logical_x, logical_y)
        self._cursor_position = (logical_x, logical_y)
        self.helper_manager.send_commands(
            [
                {"type": "move", "contact": int(pointer_id), "x": px, "y": py, "pressure": int(pressure or self.default_pressure)},
                {"type": "commit"},
            ]
        )

    def up(self, pointer_id: int):
        self.ensure_ready()
        if pointer_id not in self._active_contacts:
            return
        self._active_contacts.pop(pointer_id, None)
        self.helper_manager.send_commands(
            [
                {"type": "up", "contact": int(pointer_id)},
                {"type": "commit"},
            ]
        )

    def commit(self):
        self.helper_manager.send_commands([{"type": "commit"}])

    def wait(self, ms: int):
        self.helper_manager.send_commands([{"type": "delay", "value": max(int(ms), 0)}])

    def click(
        self,
        x: Optional[int] = None,
        y: Optional[int] = None,
        *,
        button: str = "left",
        clicks: int = 1,
        interval: float | None = None,
    ):
        self.ensure_ready()
        self._assert_primary_button(button)
        logical_x, logical_y = self._resolve_point(x, y)
        px, py = self._map_to_touch_point(logical_x, logical_y)
        commands: List[Dict[str, Any]] = []
        for click_index in range(max(int(clicks), 1)):
            commands.extend(
                [
                    {"type": "down", "contact": self.PRIMARY_CONTACT, "x": px, "y": py, "pressure": self.default_pressure},
                    {"type": "commit"},
                    {"type": "up", "contact": self.PRIMARY_CONTACT},
                    {"type": "commit"},
                ]
            )
            if click_index < max(int(clicks), 1) - 1:
                commands.append({"type": "delay", "value": max(int((interval or 0.0) * 1000), 0)})
        self._cursor_position = (logical_x, logical_y)
        self.helper_manager.send_commands(commands)

    def mouse_down(self, *, button: str = "left"):
        self.ensure_ready()
        self._assert_primary_button(button)
        logical_x, logical_y = self._resolve_point(None, None)
        px, py = self._map_to_touch_point(logical_x, logical_y)
        if self.PRIMARY_CONTACT in self._active_contacts:
            return
        self._active_contacts[self.PRIMARY_CONTACT] = (logical_x, logical_y)
        self._cursor_position = (logical_x, logical_y)
        self.helper_manager.send_commands(
            [
                {"type": "down", "contact": self.PRIMARY_CONTACT, "x": px, "y": py, "pressure": self.default_pressure},
                {"type": "commit"},
            ]
        )

    def mouse_up(self, *, button: str = "left"):
        self.ensure_ready()
        self._assert_primary_button(button)
        if self.PRIMARY_CONTACT not in self._active_contacts:
            return
        self._active_contacts.pop(self.PRIMARY_CONTACT, None)
        self.helper_manager.send_commands(
            [
                {"type": "up", "contact": self.PRIMARY_CONTACT},
                {"type": "commit"},
            ]
        )

    def move_to(self, x: int, y: int, *, duration: float | None = None):
        self.ensure_ready()
        logical_x, logical_y = self._clamp_point(x, y)
        current = self._resolve_point(None, None)
        if self.PRIMARY_CONTACT not in self._active_contacts:
            self._cursor_position = (logical_x, logical_y)
            return
        commands = self._build_move_commands(
            start=current,
            end=(logical_x, logical_y),
            duration=duration or 0.0,
            contact=self.PRIMARY_CONTACT,
        )
        self._active_contacts[self.PRIMARY_CONTACT] = (logical_x, logical_y)
        self._cursor_position = (logical_x, logical_y)
        if commands:
            self.helper_manager.send_commands(commands)

    def move_relative(self, dx: int, dy: int, *, duration: float | None = None):
        self.ensure_ready()
        start_x, start_y = self._resolve_point(None, None)
        self.move_to(start_x + int(dx), start_y + int(dy), duration=duration)

    def drag_to(self, x: int, y: int, *, button: str = "left", duration: float | None = None):
        self.ensure_ready()
        self._assert_primary_button(button)
        if self.PRIMARY_CONTACT not in self._active_contacts:
            self.mouse_down(button=button)
        self.move_to(x, y, duration=duration)
        self.mouse_up(button=button)

    def scroll(self, direction: str, amount: int):
        self.ensure_ready()
        viewport = self._require_viewport()
        units = max(abs(int(amount)), 1)
        x = int(viewport.x + viewport.width / 2)
        top = int(viewport.y + viewport.height * 0.32)
        bottom = int(viewport.y + viewport.height * 0.68)
        if str(direction).lower() == "down":
            start = (x, bottom)
            end = (x, top)
        else:
            start = (x, top)
            end = (x, bottom)
        for _ in range(units):
            down_x, down_y = self._map_to_touch_point(start[0], start[1])
            commands = [
                {"type": "down", "contact": self.PRIMARY_CONTACT, "x": down_x, "y": down_y, "pressure": self.default_pressure},
                {"type": "commit"},
            ]
            commands.extend(self._build_move_commands(start, end, duration=0.18, contact=self.PRIMARY_CONTACT))
            commands.extend(
                [
                    {"type": "up", "contact": self.PRIMARY_CONTACT},
                    {"type": "commit"},
                ]
            )
            self.helper_manager.send_commands(commands)
            self._cursor_position = end

    def release_all(self):
        if not self._active_contacts:
            return
        commands: List[Dict[str, Any]] = []
        for pointer_id in sorted(self._active_contacts):
            commands.append({"type": "up", "contact": int(pointer_id)})
        commands.append({"type": "commit"})
        self._active_contacts.clear()
        self.helper_manager.send_commands(commands)

    def look_delta(self, dx: int, dy: int):
        raise TargetRuntimeError(
            "input_capability_unsupported",
            "MuMu android_touch backend does not support relative look input.",
            {"backend": "android_touch", "feature": "relative_look", "dx": int(dx), "dy": int(dy)},
        )

    def look_hold(
        self,
        vx: float,
        vy: float,
        *,
        duration_ms: int,
        tick_ms: int | None = None,
    ):
        raise TargetRuntimeError(
            "input_capability_unsupported",
            "MuMu android_touch backend does not support sustained relative look input.",
            {
                "backend": "android_touch",
                "feature": "relative_look",
                "vx": float(vx),
                "vy": float(vy),
                "duration_ms": int(duration_ms),
                "tick_ms": None if tick_ms is None else int(tick_ms),
            },
        )

    def capabilities(self) -> Dict[str, Any]:
        return {
            "absolute_pointer": True,
            "relative_look": False,
            "keyboard": True,
            "text_input": True,
            "background_input": True,
        }

    def self_check(self) -> Dict[str, Any]:
        viewport = self.viewport_provider()
        return {
            "ok": self.is_healthy(),
            "provider": "android_touch",
            "local_port": self.helper_manager.local_port,
            "remote_port": self.helper_manager.remote_port,
            "active_contacts": sorted(self._active_contacts),
            "cursor_position": list(self._cursor_position) if self._cursor_position else None,
            "viewport": list(viewport) if viewport else None,
            "touch_physical_size": list(self.touch_physical_size) if self.touch_physical_size else None,
            "display_rotation": self.display_rotation,
            "mapped_cursor_position": list(self._map_to_touch_point(*self._cursor_position)) if self._cursor_position else None,
            "capabilities": self.capabilities(),
        }

    def _build_move_commands(
        self,
        start: Tuple[int, int],
        end: Tuple[int, int],
        *,
        duration: float,
        contact: int,
    ) -> List[Dict[str, Any]]:
        sx, sy = start
        ex, ey = end
        distance = math.hypot(ex - sx, ey - sy)
        if distance == 0:
            return []
        steps = max(int(max(duration, 0.01) * self.path_fps), 1)
        delay_ms = max(int((max(duration, 0.01) / steps) * 1000), 1)
        commands: List[Dict[str, Any]] = []
        for step in range(1, steps + 1):
            progress = step / steps
            x = int(round(sx + (ex - sx) * progress))
            y = int(round(sy + (ey - sy) * progress))
            touch_x, touch_y = self._map_to_touch_point(x, y)
            commands.append({"type": "move", "contact": int(contact), "x": touch_x, "y": touch_y, "pressure": self.default_pressure})
            commands.append({"type": "commit"})
            if step < steps:
                commands.append({"type": "delay", "value": delay_ms})
        return commands

    def _resolve_point(self, x: Optional[int], y: Optional[int]) -> Tuple[int, int]:
        if x is not None and y is not None:
            return self._clamp_point(int(x), int(y))
        if self._cursor_position is not None:
            return self._cursor_position
        return self._default_point()

    def _default_point(self) -> Tuple[int, int]:
        viewport = self._require_viewport()
        return viewport.center()

    def _clamp_point(self, x: int, y: int) -> Tuple[int, int]:
        viewport = self._require_viewport()
        clamped_x = min(max(int(x), viewport.x), viewport.x + max(viewport.width - 1, 0))
        clamped_y = min(max(int(y), viewport.y), viewport.y + max(viewport.height - 1, 0))
        return clamped_x, clamped_y

    def _map_to_touch_point(self, x: int, y: int) -> Tuple[int, int]:
        logical_x, logical_y = self._clamp_point(x, y)
        if not self.touch_physical_size:
            return logical_x, logical_y

        width, height = self.touch_physical_size
        viewport = self._require_viewport()
        rotation = self.display_rotation % 4
        if rotation in (0, 2):
            display_width, display_height = width, height
        else:
            display_width, display_height = height, width

        if viewport.width <= 1:
            display_x = 0
        else:
            display_x = round(((logical_x - viewport.x) / (viewport.width - 1)) * max(display_width - 1, 0))
        if viewport.height <= 1:
            display_y = 0
        else:
            display_y = round(((logical_y - viewport.y) / (viewport.height - 1)) * max(display_height - 1, 0))

        if rotation == 1:
            mapped_x = width - 1 - display_y
            mapped_y = display_x
        elif rotation == 2:
            mapped_x = width - 1 - display_x
            mapped_y = height - 1 - display_y
        elif rotation == 3:
            mapped_x = display_y
            mapped_y = height - 1 - display_x
        else:
            mapped_x = display_x
            mapped_y = display_y

        mapped_x = min(max(int(mapped_x), 0), max(width - 1, 0))
        mapped_y = min(max(int(mapped_y), 0), max(height - 1, 0))
        return mapped_x, mapped_y

    def _require_viewport(self) -> DeviceViewport:
        rect = self.viewport_provider()
        if not rect:
            raise TargetRuntimeError(
                "capture_viewport_unavailable",
                "Viewport is unavailable because the capture backend has no frame yet.",
            )
        return DeviceViewport(*[int(item) for item in rect])

    def _assert_primary_button(self, button: str):
        if str(button or "left").lower() != "left":
            raise TargetRuntimeError(
                "unsupported_mouse_button",
                f"MuMu touch backend only supports left-button semantics, got '{button}'.",
            )
