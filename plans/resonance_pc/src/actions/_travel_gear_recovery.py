"""Recover from brake gear while the intercity arrival loop owns input."""

from __future__ import annotations

from functools import lru_cache
import math
from pathlib import Path
import time

import cv2
import numpy as np

from packages.aura_core.observability.logging.core_logger import logger

from ._intercity_map_geometry import REFERENCE_CLIENT


_TEMPLATES = Path(__file__).resolve().parents[2] / "templates/navigation/travel"
_GEAR_REGION = (1150, 588, 128, 86)
_BRAKE_REGION = (1150, 590, 86, 84)
_DRIVE_REGION = (1218, 588, 60, 56)
_DRIVE_OFFSET = (54, -23)
_TEMPLATE_SIZE = 38
_THRESHOLD = 0.90
_CHECK_INTERVAL = 10.0
_CONFIRM_DELAY = 0.5


@lru_cache(maxsize=2)
def _template(gear: str) -> tuple[np.ndarray, np.ndarray]:
    data = (_TEMPLATES / f"gear_{gear}_selected.png").read_bytes()
    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    if image is None or image.shape != (_TEMPLATE_SIZE, _TEMPLATE_SIZE, 4):
        raise ValueError(f"Invalid travel gear template: {gear}")
    return cv2.cvtColor(image, cv2.COLOR_BGRA2RGB), (image[:, :, 3] == 255).astype(np.uint8) * 255


class TravelGearRecovery:
    def __init__(self, app, vision, *, started: float, check_cancelled) -> None:
        self.app = app
        self.vision = vision
        self.check_cancelled = check_cancelled
        self.paused = False
        self.next_check_at = started + _CHECK_INTERVAL
        self._brake_hits = 0
        self._pending_drive_point: tuple[int, int] | None = None
        self._attempt = 0

    def _capture(self) -> np.ndarray | None:
        self.check_cancelled()
        try:
            if self.app.get_window_size() != REFERENCE_CLIENT:
                return None
            capture = self.app.capture(rect=_GEAR_REGION)
        except Exception:
            logger.debug("[TravelGear] capture failed", exc_info=True)
            return None
        self.check_cancelled()
        image = getattr(capture, "image", None)
        if (getattr(capture, "success", False) and isinstance(image, np.ndarray)
                and image.dtype == np.uint8 and image.shape == (_GEAR_REGION[3], _GEAR_REGION[2], 3)):
            return image
        return None

    def _match(self, image: np.ndarray, gear: str, region: tuple[int, int, int, int]):
        x, y, width, height = region
        left, top = x - _GEAR_REGION[0], y - _GEAR_REGION[1]
        try:
            template, mask = _template(gear)
            match = self.vision.find_template(
                source_image=image[top:top + height, left:left + width],
                template_image=template, mask_image=mask, threshold=_THRESHOLD,
                use_grayscale=False, match_method=cv2.TM_SQDIFF_NORMED,
            )
            if (type(match.found) is bool and math.isfinite(match.confidence)
                    and not match.debug_info.get("error")):
                return match
        except Exception:
            logger.warning("[TravelGear] matching failed gear=%s", gear, exc_info=True)
        return None

    def _brake_point(self, image: np.ndarray) -> tuple[int, int] | None:
        match = self._match(image, "b", _BRAKE_REGION)
        if match is None or not match.found or match.center_point is None:
            return None
        x = _BRAKE_REGION[0] + match.center_point[0] + _DRIVE_OFFSET[0]
        y = _BRAKE_REGION[1] + match.center_point[1] + _DRIVE_OFFSET[1]
        left, top, width, height = _DRIVE_REGION
        margin = _TEMPLATE_SIZE // 2
        if left + margin <= x <= left + width - margin and top + margin <= y <= top + height - margin:
            return x, y
        return None

    def _click_drive(self, point: tuple[int, int], deadline: float) -> None:
        self.check_cancelled()
        if time.monotonic() >= deadline:
            return
        self._attempt += 1
        logger.info("[TravelGear] click D attempt=%s point=%s", self._attempt, point)
        self.app.click(x=point[0], y=point[1])
        self._pending_drive_point = point
        self.next_check_at = time.monotonic() + _CONFIRM_DELAY

    def _resume_pickup(self) -> None:
        logger.info("[TravelGear] D confirmed; resume pickup")
        self.paused = False
        self._brake_hits = 0

    def _confirm_drive(self, image: np.ndarray | None, deadline: float) -> None:
        point = self._pending_drive_point
        self._pending_drive_point = None
        if image is not None:
            drive = self._match(image, "d", _DRIVE_REGION)
            if drive is not None and drive.found:
                self._resume_pickup()
                return
            if drive is not None and self._attempt == 1 and self._brake_point(image) is not None:
                self._click_drive(point, deadline)
                return
        logger.info("[TravelGear] D unconfirmed; keep pickup paused")

    def tick(self, deadline: float) -> None:
        now = time.monotonic()
        if now >= deadline or now < self.next_check_at:
            return
        if now >= self.next_check_at + _CHECK_INTERVAL:
            self._brake_hits = 0
        self.next_check_at = now + _CHECK_INTERVAL
        image = self._capture()
        if self._pending_drive_point is not None:
            self._confirm_drive(image, deadline)
            return
        if image is None:
            self._brake_hits = 0
            return
        if self.paused:
            drive = self._match(image, "d", _DRIVE_REGION)
            if drive is not None and drive.found:
                self._resume_pickup()
                return
        if self._brake_point(image) is None:
            self._brake_hits = 0
            return
        self._brake_hits += 1
        if self._brake_hits < 2:
            return

        self._brake_hits = 0
        self._attempt = 0
        if not self.paused:
            self.paused = True
            logger.info("[TravelGear] B confirmed twice; pause pickup")
        fresh = self._capture()
        if fresh is not None and (point := self._brake_point(fresh)) is not None:
            self._click_drive(point, deadline)
