"""Read selectable depth and strategy controls from a single 1280x720 client frame."""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

from ._deep_dive_opening_options import DIVE_DEPTHS


Point = tuple[int, int]
Rect = tuple[int, int, int, int]
_TEMPLATES = Path(__file__).resolve().parents[2] / 'templates/deep_dive/consciousness_deep_dive'
_STRATEGY_ICON_REGIONS = {
    'source': (450, 550, 808, 636),
    'waxing_waning': (220, 550, 574, 636),
    'revival': (104, 342, 457, 437),
    'defense': (220, 139, 574, 231),
}


@dataclass(frozen=True)
class OpeningChoice:
    center: Point
    state: str


@lru_cache(maxsize=24)
def _template(name: str) -> np.ndarray:
    path = _TEMPLATES / f'{name}.png'
    data = np.fromfile(path, dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise RuntimeError(f'无法载入识海深潜开局模板：{path.name}')
    if name.startswith('opening/depth_') and name.rsplit('_', 1)[-1].isdigit():
        image = _depth_glyph(image)
    return image


def _depth_glyph(gray: np.ndarray) -> np.ndarray:
    # Available, locked, and selected cards use the same glyph outline at different brightness levels.
    return cv2.threshold(gray, 100, 255, cv2.THRESH_BINARY)[1]


def _match(image: np.ndarray, name: str, area: Rect, threshold: float) -> Point | None:
    template = _template(name)
    left, top, right, bottom = area
    left, top = max(left, 0), max(top, 0)
    region = image[top:bottom, left:right]
    h, w = template.shape
    if region.shape[0] < h or region.shape[1] < w:
        return None
    scores = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)
    _, score, _, (x, y) = cv2.minMaxLoc(scores)
    return (left + x + w // 2, top + y + h // 2) if score >= threshold else None


def _near(x: int, y: int, half_width: int, half_height: int) -> Rect:
    return (x - half_width, y - half_height, x + half_width, y + half_height)


def _red(rgb: np.ndarray) -> np.ndarray:
    signed = rgb.astype(np.int16)
    r, g, b = np.moveaxis(signed, -1, 0)
    return (r >= 115) & (r - g >= 55) & (r - b >= 45)


def _red_ring(rgb: np.ndarray, center: Point) -> bool:
    x, y = center
    ring_radius = 51
    crop = rgb[y - ring_radius:y + ring_radius + 1, x - ring_radius:x + ring_radius + 1]
    if crop.shape[:2] != (103, 103):
        return False
    dy, dx = np.ogrid[-ring_radius:ring_radius + 1, -ring_radius:ring_radius + 1]
    ring = (43**2 <= dx**2 + dy**2) & (dx**2 + dy**2 <= ring_radius**2)
    return bool(np.mean(_red(crop)[ring]) >= 0.23)


class OpeningFrame:
    def __init__(self, rgb: np.ndarray):
        self.rgb = rgb
        self.gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)

    def depth_page(self) -> bool:
        return _match(self.gray, 'opening/depth_heading', (530, 15, 750, 72), 0.82) is not None

    def strategy_page(self) -> bool:
        return _match(self.gray, 'opening/strategy_heading', (280, 15, 744, 79), 0.82) is not None

    def formation_page(self) -> bool:
        return (_match(self.gray, 'opening/formation_depth_label', (614, 657, 734, 707), 0.82) is not None
                and _match(self.gray, 'formation_confirm', (960, 565, 1280, 715), 0.86) is not None)

    def back(self) -> Point | None:
        return _match(self.gray, 'opening/back', (30, 0, 140, 85), 0.78)

    def depths(self) -> dict[int, OpeningChoice]:
        glyphs = _depth_glyph(self.gray)
        result = {}
        for depth in range(1, len(DIVE_DEPTHS) + 1):
            # In the horizontal carousel odd/even cards remain on separate rows.
            row = 212 if depth % 2 else 431
            center = _match(glyphs, f'opening/depth_{depth}', (0, row - 33, 1280, row + 33), 0.80)
            if center is None:
                continue
            x, y = center
            if _match(self.gray, 'opening/depth_lock', _near(x, y + 53, 19, 18), 0.79):
                state = 'locked'
            else:
                corner = _match(self.gray, 'opening/depth_selected_corner', _near(x - 83, y - 75, 20, 20), 0.76)
                selected = False
                if corner is not None:
                    cx, cy = corner
                    selected = bool(np.mean(_red(self.rgb[cy - 13:cy + 13, cx - 13:cx + 13])) >= 0.20)
                state = 'selected' if selected else 'available'
            result[depth] = OpeningChoice(center, state)
        return result

    def strategy(self, key: str) -> OpeningChoice | None:
        center = _match(self.gray, f'opening/strategy_{key}_icon', _STRATEGY_ICON_REGIONS[key], 0.78)
        if center is None:
            return None
        title = _match(self.gray, f'opening/strategy_{key}_title', (325, 503, 459, 547), 0.80)
        selected = title is not None and _red_ring(self.rgb, center)
        return OpeningChoice(center, 'selected' if selected else 'available')

    def start_game(self) -> Point | None:
        return _match(self.gray, 'start_game', (950, 585, 1220, 685), 0.86)

    def confirm_strategy(self) -> Point | None:
        return _match(self.gray, 'strategy_confirm', (860, 580, 1225, 690), 0.86)
