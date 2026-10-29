"""Target-bound observations and cancellable inputs for battle operations."""
from __future__ import annotations

import asyncio
import re
from dataclasses import asdict

import numpy as np

from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested
from packages.aura_core.scheduler.operation_scope import OperationScope, current_operation_scope
from ....aura_base.src.services.interaction_service import InputSession, InteractionService
from ....aura_base.src.services.observation_service import Observation, ObservationService


def check_cancelled():
    if is_current_task_cancel_requested():
        raise asyncio.CancelledError("Battle operation cancelled")


def compact(text):
    return re.sub(r"[\s\u3000]+", "", str(text or "")).casefold()


def find_text(items, targets, *, exact=False, region=None):
    if isinstance(targets, str):
        targets = (targets,)
    candidates = []
    for item in items:
        if region is not None:
            x, y, w, h = region
            cx, cy = item["center"]
            if not (x <= cx < x+w and y <= cy < y+h):
                continue
        value = compact(item["text"])
        if any(value == compact(t) if exact else compact(t) in value for t in targets):
            candidates.append(item)
    return max(candidates, key=lambda item: item.get("confidence", 0)) if candidates else None


class BattleUi:
    """One explicit input session; captures become invalid after every input."""

    def __init__(self, app, ocr=None, interaction=None, scope=None):
        if app is None:
            raise ValueError("Battle UI requires app service")
        self.app, self.ocr = app, ocr
        self.scope = OperationScope(
            parent=scope if scope is not None else current_operation_scope(),
            target=getattr(app, "target_runtime", None), cancel_check=check_cancelled,
        )
        self.interaction = interaction if interaction is not None else InteractionService(
            app=app, observation=ObservationService(app=app))
        self.session = InputSession()

    def operation(self):
        active = current_operation_scope()
        if active is not None:
            active.checkpoint()
        return self.scope.child(active.remaining if active is not None else None)

    async def capture(self):
        scope = self.operation()
        scope.checkpoint()
        frame = await self.interaction.observation.capture_async(scope=scope, roi=(0, 0, 1280, 720))
        if not isinstance(frame.image, np.ndarray) or frame.image.shape != (720, 1280, 3):
            raise RuntimeError("battle_capture_client_size_mismatch")
        scope.checkpoint()
        return frame

    async def recognize(self, frame, region=None):
        if self.ocr is None:
            raise ValueError("Battle text observation requires OCR service")
        x, y, w, h = region or (0, 0, 1280, 720)
        image = frame.rgb_image()[y:y+h, x:x+w]
        scope = self.operation()
        result = await scope.run_async(self.ocr.recognize_all, image)
        scope.checkpoint()
        return [
            {"text": str(row.text or ""), "normalized": compact(row.text),
             "center": (int(row.center_point[0])+x, int(row.center_point[1])+y),
             "confidence": float(row.confidence or 0)}
            for row in result.results if row.center_point
        ]

    async def read(self):
        frame = await self.capture()
        return frame, await self.recognize(frame)

    async def click(self, frame, point):
        scope = self.operation()
        scope.checkpoint()
        source = Observation("present", frame_id=frame.id,
                             client_center=(int(point[0]), int(point[1])))
        receipt = await self.interaction.click_async(
            source, frame=frame, scope=scope, session=self.session, move_duration=0.)
        scope.checkpoint()
        if not receipt.sent or not receipt.call_returned or receipt.acknowledged is False:
            raise RuntimeError("battle_input_not_acknowledged")
        return asdict(receipt)

    async def sleep(self, seconds):
        await self.operation().sleep_async(max(float(seconds), 0.))
