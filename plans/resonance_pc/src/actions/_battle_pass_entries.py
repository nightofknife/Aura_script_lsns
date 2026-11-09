"""Locate battle-pass reminders and selection within the native card list."""
from __future__ import annotations

import cv2
import numpy as np

from ....aura_base.src.services.observation_service import Observation


LIST_ROI = (12, 75, 200, 427)
CARD_X = 110
CARD_CENTER_OFFSET = 27
# Width of the opaque white edge along the selected card's inward chevron.
_SELECTED_EDGE = (2, 2, 2, 3, 3, 4, 5, 6, 6, 5, 4, 3, 3, 2, 2, 2)


def reminder_control(row_y: int) -> str:
    return f"pass_reminder:{row_y}"


def reminder_observation(row_y: int, frame_id: str) -> Observation:
    return Observation("present", frame_id=frame_id, semantic_key=reminder_control(row_y),
                       client_center=(CARD_X, row_y))


def read_reminders(roi, badge_height, find_badge) -> tuple[int, ...]:
    """Find each instance of the native badge in the fixed right edge strip."""
    x, top, width, height = roi
    regions = [(top, top + height)]
    rows = []
    while regions:
        start, end = regions.pop()
        if end - start < badge_height:
            continue
        hit = find_badge((x, start, width, end - start))
        if hit.state != "present":
            continue
        _, badge_top, _, matched_height = hit.value.rect
        rows.append(round(hit.client_center[1]) + CARD_CENTER_OFFSET)
        regions.extend(((start, badge_top), (badge_top + matched_height, end)))
    return tuple(sorted(rows))


def read_selected_row(frame) -> int | None:
    """Read the selected card's fixed chevron; no card text or art is required."""
    list_y, list_height = LIST_ROI[1], LIST_ROI[3]
    strip = frame.crop((20, list_y, 14, list_height)).rgb_image()
    white = cv2.cvtColor(strip, cv2.COLOR_RGB2GRAY) >= 230
    candidates = set()
    for offset in range(6):
        widths = np.logical_and.accumulate(white[:, offset:offset + 8], axis=1).sum(axis=1)
        windows = np.lib.stride_tricks.sliding_window_view(widths, len(_SELECTED_EDGE))
        distances = np.abs(windows - _SELECTED_EDGE).sum(axis=1)
        candidates.update(list_y + int(index) + 7 for index in np.flatnonzero(distances <= 6))
    if not candidates or max(candidates) - min(candidates) > 2:
        return None
    return round(sum(candidates) / len(candidates))
