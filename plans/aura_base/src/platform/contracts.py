# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol, Tuple

import cv2
import numpy as np


class TargetRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = str(code or "target_runtime_error")
        self.message = str(message or "Target runtime error.")
        self.detail = dict(detail or {})

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class DeviceViewport:
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0

    def as_rect(self) -> Tuple[int, int, int, int]:
        return int(self.x), int(self.y), int(self.width), int(self.height)

    def center(self) -> Tuple[int, int]:
        return int(self.x + self.width / 2), int(self.y + self.height / 2)


@dataclass
class CaptureResult:
    success: bool
    image: np.ndarray | None = None
    window_rect: Tuple[int, int, int, int] | None = None
    relative_rect: Tuple[int, int, int, int] | None = None
    backend: str | None = None
    quality_flags: list[str] = field(default_factory=list)
    error_message: str = field(default="", repr=False)

    @property
    def image_size(self) -> Tuple[int, int] | None:
        if self.image is None:
            return None
        return int(self.image.shape[1]), int(self.image.shape[0])

    def save(self, filepath: str):
        if not self.success or self.image is None:
            return
        image_bgr = cv2.cvtColor(self.image, cv2.COLOR_RGB2BGR)
        cv2.imwrite(filepath, image_bgr)


class CaptureBackend(Protocol):
    def capture(self, rect: Tuple[int, int, int, int] | None = None) -> CaptureResult: ...

    def get_client_rect(self) -> Tuple[int, int, int, int] | None: ...

    def get_pixel_color_at(self, x: int, y: int) -> Tuple[int, int, int]: ...

    def focus(self) -> bool: ...

    def self_check(self) -> Dict[str, Any]: ...

    def close(self) -> None: ...


class InputBackend(Protocol):
    def click(
        self,
        x: int | None = None,
        y: int | None = None,
        *,
        button: str = "left",
        clicks: int = 1,
        interval: float | None = None,
    ) -> None: ...

    def move_to(self, x: int, y: int, *, duration: float | None = None) -> None: ...

    def move_relative(self, dx: int, dy: int, *, duration: float | None = None) -> None: ...

    def mouse_down(self, *, button: str = "left") -> None: ...

    def mouse_up(self, *, button: str = "left") -> None: ...

    def drag_to(self, x: int, y: int, *, button: str = "left", duration: float | None = None) -> None: ...

    def scroll(self, amount: int, direction: str = "down") -> None: ...

    def press_key(self, key: str, presses: int = 1, interval: float | None = None) -> None: ...

    def key_down(self, key: str) -> None: ...

    def key_up(self, key: str) -> None: ...

    def type_text(self, text: str, interval: float | None = None) -> None: ...

    def look_delta(self, dx: int, dy: int) -> None: ...

    def look_hold(
        self,
        vx: float,
        vy: float,
        *,
        duration_ms: int,
        tick_ms: int | None = None,
    ) -> None: ...

    def release_all(self) -> None: ...

    def capabilities(self) -> Dict[str, Any]: ...

    def self_check(self) -> Dict[str, Any]: ...

    def close(self) -> None: ...


class RuntimeAdapter(Protocol):
    def ensure_ready(self) -> None: ...

    def close(self) -> None: ...

    def capture(self, rect: Tuple[int, int, int, int] | None = None) -> CaptureResult: ...

    def get_client_rect(self) -> Tuple[int, int, int, int] | None: ...

    def get_pixel_color_at(self, x: int, y: int) -> Tuple[int, int, int]: ...

    def focus(self) -> bool: ...

    def focus_with_input(self, click_delay: float = 0.3) -> bool: ...

    def click(
        self,
        x: int | None = None,
        y: int | None = None,
        *,
        button: str = "left",
        clicks: int = 1,
        interval: float | None = None,
    ) -> None: ...

    def move_to(self, x: int, y: int, *, duration: float | None = None) -> None: ...

    def move_relative(self, dx: int, dy: int, *, duration: float | None = None) -> None: ...

    def mouse_down(self, *, button: str = "left") -> None: ...

    def mouse_up(self, *, button: str = "left") -> None: ...

    def drag_to(self, x: int, y: int, *, button: str = "left", duration: float | None = None) -> None: ...

    def scroll(self, amount: int, direction: str = "down") -> None: ...

    def press_key(self, key: str, presses: int = 1, interval: float | None = None) -> None: ...

    def key_down(self, key: str) -> None: ...

    def key_up(self, key: str) -> None: ...

    def type_text(self, text: str, interval: float | None = None) -> None: ...

    def look_delta(self, dx: int, dy: int) -> None: ...

    def look_hold(
        self,
        vx: float,
        vy: float,
        *,
        duration_ms: int,
        tick_ms: int | None = None,
    ) -> None: ...

    def release_all(self) -> None: ...

    def capabilities(self) -> Dict[str, Any]: ...

    def self_check(self) -> Dict[str, Any]: ...

    def list_capture_backends(self) -> Dict[str, Any]: ...

    def set_capture_backend(self, backend: str) -> None: ...
