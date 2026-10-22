"""Native purple glyphs and synthetic walls require curved-hole evidence."""
from pathlib import Path

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_layout_semantics as semantics


NATIVE = Path(semantics.__file__).resolve().parents[2] / 'templates/deep_dive_layout'


def read_rgb(path):
    return cv2.cvtColor(cv2.imread(str(path)), cv2.COLOR_BGR2RGB)


def purple_mask(image):
    h, s, v = cv2.split(cv2.cvtColor(image, cv2.COLOR_RGB2HSV))
    return (h >= 126) & (h <= 148) & (s > 120) & (v > 130)


@pytest.mark.parametrize('rotation', [0, 1, 2, 3])
def test_native_purple_reference_retains_outline_and_class(rotation):
    image = np.ascontiguousarray(np.rot90(read_rgb(NATIVE / 'icon_purple_ring.png'), rotation))
    assert semantics._purple_outline(purple_mask(image))
    assert semantics.classify_icon(image)['icon_id'] == 'purple_ring'


def test_filled_wall_and_straight_square_border_do_not_supply_curved_cavity():
    wall = np.ones((96, 96), bool)
    border = np.zeros((96, 96), np.uint8)
    cv2.rectangle(border, (18, 18), (77, 77), 1, 6)
    assert not semantics._purple_outline(wall)
    assert not semantics._purple_outline(border)
