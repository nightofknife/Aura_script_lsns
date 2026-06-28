# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional, Tuple

from packages.aura_core.api import service_info
from packages.aura_core.observability.logging.core_logger import logger

from ..platform.contracts import CaptureResult, TargetRuntimeError
from ..platform.runtime_service import TargetRuntimeService


@service_info(alias="screen", public=True, deps={"target_runtime": "target_runtime"})
class ScreenService:
    """Runtime-backed screen facade."""

    def __init__(self, target_runtime: TargetRuntimeService):
        self.target_runtime = target_runtime
        self.hwnd = None

    def list_backends(self) -> Dict[str, Any]:
        return self.target_runtime.list_capture_backends()

    def set_default_backend(self, backend: str):
        self.target_runtime.set_capture_backend(backend)

    def self_check(self) -> Dict[str, Any]:
        return self.target_runtime.self_check()

    def get_client_rect(self) -> Tuple[int, int, int, int] | None:
        try:
            return self.target_runtime.get_client_rect()
        except TargetRuntimeError:
            return None

    def get_pixel_color_at(self, x: int, y: int) -> Tuple[int, int, int]:
        return self.target_runtime.get_pixel_color_at(x, y)

    def focus(self) -> bool:
        try:
            return self.target_runtime.focus()
        except TargetRuntimeError as exc:
            logger.warning("Screen focus failed: %s", exc)
            return False

    async def focus_async(self) -> bool:
        return await asyncio.to_thread(self.focus)

    def capture(
        self,
        rect: Tuple[int, int, int, int] | None = None,
        backend: Optional[str] = None,
    ) -> CaptureResult:
        return self.target_runtime.capture(rect=rect, backend=backend)

    async def capture_async(
        self,
        rect: Tuple[int, int, int, int] | None = None,
        backend: Optional[str] = None,
    ) -> CaptureResult:
        return await asyncio.to_thread(self.capture, rect, backend)
