"""Image-only recognition for the solo Calculation Recording selection page."""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image


# All regions are (x, y, width, height) in the 1280×720 client area.
BOSS_NUMBER_ROI = (701, 134, 152, 141)
PROGRESS_ROI = (1037, 199, 98, 42)
_PAGE_ROI = (439, 593, 176, 117)
_PLATE_ROI = (28, 95, 70, 504)
_LOCK_ROI = (264, 95, 88, 504)
_SETTLEMENT_NEXT_ROI = (1129, 568, 97, 87)
_CARD_CENTER_X = 63
_ROW_TOLERANCE = 15
_TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates" / "calculation_recording"


@dataclass(frozen=True)
class Card:
    center: tuple[int, int]
    locked: bool


@lru_cache(maxsize=4)
def _image(name: str, mode: str) -> np.ndarray:
    with Image.open(_TEMPLATE_DIR / name) as image:
        return np.array(image.convert(mode))


def _crop(rgb: np.ndarray, region: tuple[int, int, int, int]) -> np.ndarray:
    x, y, width, height = region
    return rgb[y:y + height, x:x + width]


def _match_centers(
    scores: np.ndarray,
    region: tuple[int, int, int, int],
    template_size: tuple[int, int],
    minimum: float,
) -> list[tuple[int, int]]:
    """Return row anchors, with neighboring peaks of one template suppressed."""
    width, height = template_size
    half_width, half_height = width // 2, height // 2
    scores = np.nan_to_num(scores, nan=-1.0, posinf=-1.0, neginf=-1.0)
    centers = []
    while True:
        _, score, _, (x, y) = cv2.minMaxLoc(scores)
        if score < minimum:
            return centers
        centers.append((x + region[0] + half_width, y + region[1] + half_height))
        scores[
            max(0, y - half_height):y + half_height + 1,
            max(0, x - half_width):x + half_width + 1,
        ] = -1.0


def is_recording_page(rgb: np.ndarray) -> bool:
    """Use the stable reward button near the bottom of the solo selection page."""
    if rgb.shape != (720, 1280, 3):
        return False
    button = _image("reward_button.png", "RGB")
    mask = _image("reward_button_mask.png", "L")
    scores = cv2.matchTemplate(_crop(rgb, _PAGE_ROI), button, cv2.TM_CCOEFF_NORMED, mask=mask)
    return bool(np.any(np.isfinite(scores) & (scores >= 0.85)))


@lru_cache(maxsize=1)
def _lock_template() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    icon = _image("locked_exclamation.png", "RGBA")
    alpha = icon[..., 3]
    silhouette = (alpha >= 128).astype(np.float32)
    red = np.median(icon[..., :3][alpha >= 220], axis=0)
    return silhouette, red, np.ones_like(silhouette)


def _locked_centers(rgb: np.ndarray, plate_height: int) -> list[tuple[int, int]]:
    silhouette, red, footprint = _lock_template()
    sample = _crop(rgb, _LOCK_ROI).astype(np.float32)
    red_pixels = (np.abs(sample - red) <= (30, 23, 23)).all(axis=2).astype(np.float32)
    intersection = cv2.matchTemplate(red_pixels, silhouette, cv2.TM_CCORR)
    sample_area = cv2.matchTemplate(red_pixels, footprint, cv2.TM_CCORR)
    scores = 2 * np.maximum(intersection, 0) / (float(silhouette.sum()) + np.maximum(sample_area, 0))
    height, width = silhouette.shape
    top = _LOCK_ROI[1] + plate_height // 2
    bottom = _LOCK_ROI[1] + _LOCK_ROI[3] - plate_height // 2
    return [
        center for center in _match_centers(scores, _LOCK_ROI, (width, height), 0.90)
        if top <= center[1] < bottom
    ]


def scan_visible_cards(rgb: np.ndarray) -> tuple[Card, ...]:
    """Find actionable card rows. An explicit red exclamation always wins."""
    if rgb.shape != (720, 1280, 3):
        return ()
    plate = _image("card_plate.png", "RGB")
    height, width = plate.shape[:2]
    squared_error = cv2.matchTemplate(_crop(rgb, _PLATE_ROI), plate, cv2.TM_SQDIFF_NORMED)
    plate_centers = _match_centers(1.0 - squared_error, _PLATE_ROI, (width, height), 0.88)
    locked_centers = _locked_centers(rgb, height)
    cards = [Card((_CARD_CENTER_X, y), True) for _, y in locked_centers]
    cards.extend(
        Card((_CARD_CENTER_X, y), False)
        for _, y in plate_centers
        if all(abs(y - lock_y) > _ROW_TOLERANCE for _, lock_y in locked_centers)
    )
    return tuple(sorted(cards, key=lambda card: card.center[1]))


def selected_card_center(rgb: np.ndarray) -> tuple[int, int] | None:
    """Find the paired black tabs protruding on both sides of one selected row."""
    if rgb.shape != (720, 1280, 3):
        return None
    # The ends of the native selection frame are solid black tabs approximately
    # ten pixels high. These bands lie outside each ordinary card's dark border.
    top = _PLATE_ROI[1]
    bottom = top + _PLATE_ROI[3]
    left_tab = (rgb[top:bottom, 7:22].max(axis=2) <= 32).sum(axis=1) >= 7
    right_tab = (rgb[top:bottom, 599:621].max(axis=2) <= 32).sum(axis=1) >= 7
    transitions = np.flatnonzero(np.diff(np.r_[False, left_tab & right_tab, False]))
    candidates = [
        (int(start) + int(end) - 1) // 2 + top
        for start, end in zip(transitions[::2], transitions[1::2])
        if 7 <= end - start <= 14
    ]
    return (_CARD_CENTER_X, candidates[0]) if len(candidates) == 1 else None


@lru_cache(maxsize=1)
def _settlement_next_template() -> tuple[np.ndarray, np.ndarray]:
    image = _image("settlement_next_button.png", "RGBA")
    return np.ascontiguousarray(image[..., :3]), np.uint8(image[..., 3] >= 220) * 255


def settlement_next_center(rgb: np.ndarray) -> tuple[int, int] | None:
    """Find the clickable white circle with a black arrow in victory settlement."""
    if rgb.shape != (720, 1280, 3):
        return None
    template, mask = _settlement_next_template()
    scores = cv2.matchTemplate(_crop(rgb, _SETTLEMENT_NEXT_ROI), template, cv2.TM_CCOEFF_NORMED, mask=mask)
    scores = np.nan_to_num(scores, nan=-1.0, posinf=-1.0, neginf=-1.0)
    _, score, _, (x, y) = cv2.minMaxLoc(scores)
    height, width = template.shape[:2]
    return (_SETTLEMENT_NEXT_ROI[0] + x + width // 2, _SETTLEMENT_NEXT_ROI[1] + y + height // 2) if score >= 0.90 else None
