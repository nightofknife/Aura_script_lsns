"""Offline glyph-bank calibration on two supplied profile-panel screenshots."""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[2]
BANK = ROOT / "plans/resonance_pc/templates/player_data/status_digits"
FIXTURES = ROOT / "tests/fixtures/profile_status_digits"


def _center_glyph(mask: np.ndarray) -> np.ndarray:
    points = np.argwhere(mask > 0)
    if not len(points):
        raise ValueError("Empty glyph")
    top, left = points.min(axis=0)
    bottom, right = points.max(axis=0) + 1
    glyph = mask[top:bottom, left:right]
    if glyph.shape[0] > 16 or glyph.shape[1] > 12:
        raise ValueError(f"Oversized glyph: {glyph.shape}")
    canvas = np.zeros((24, 20), dtype=np.uint8)
    y = (24 - glyph.shape[0]) // 2
    x = (20 - glyph.shape[1]) // 2
    canvas[y:y + glyph.shape[0], x:x + glyph.shape[1]] = glyph
    return canvas


def _observed_glyphs(image: np.ndarray) -> list[np.ndarray]:
    mask = cv2.inRange(image, np.array([155, 155, 155]), np.array([255, 255, 255]))
    _, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    components = sorted(
        ((int(x), int(y), int(w), int(h)) for x, y, w, h, area in stats[1:]
         if area >= 15 and y < 26),
        key=lambda row: row[0],
    )
    return [
        cv2.copyMakeBorder(_center_glyph(mask[y:y + h, x:x + w]), 2, 2, 2, 2,
                           cv2.BORDER_CONSTANT, value=0)
        for x, y, w, h in components
    ]


def _templates() -> dict[str, np.ndarray]:
    manifest = json.loads((BANK / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["reference_client"] == [1280, 720]
    assert manifest["template_size"] == [12, 16]
    assert set(manifest["characters"]) == set("0123456789/")
    templates = {}
    for character, filename in manifest["characters"].items():
        image = cv2.imread(str(BANK / filename), cv2.IMREAD_GRAYSCALE)
        assert image is not None and image.shape == (16, 12)
        assert set(np.unique(image)) == {0, 255}
        templates[character] = _center_glyph(image)
    return templates


@pytest.mark.parametrize(("field", "minimum_score"), [
    ("clarity", 0.75), ("fatigue", 0.75), ("cargo", 0.75),
    ("clarity_second", 0.72), ("fatigue_second", 0.72), ("cargo_second", 0.72),
])
def test_game_font_templates_match_supplied_status_fields(field, minimum_score):
    fixture = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))["fields"][field]
    image = cv2.imread(str(FIXTURES / fixture["image"]))
    assert image is not None
    assert image.shape[:2] == (fixture["client_roi"][3], fixture["client_roi"][2])
    observed = _observed_glyphs(image)
    expected = fixture["expected"]
    assert len(observed) == len(expected)
    templates = _templates()
    for glyph, character in zip(observed, expected):
        scores = {key: float(cv2.matchTemplate(glyph, template, cv2.TM_CCOEFF_NORMED).max())
                  for key, template in templates.items()}
        ranked = sorted(scores, key=scores.get, reverse=True)
        assert ranked[0] == character
        assert scores[ranked[0]] >= minimum_score
        assert scores[ranked[0]] - scores[ranked[1]] >= 0.02


def test_blank_field_has_no_digit_components():
    assert _observed_glyphs(np.zeros((25, 99, 3), dtype=np.uint8)) == []
