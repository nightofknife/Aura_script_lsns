"""Complete curved-hole evidence handles actual oversized purple glyphs."""
from pathlib import Path

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_layout_semantics as semantics


FIXTURES = Path(__file__).parents[1] / 'fixtures/deep_dive_anchor'
NATIVE = Path(semantics.__file__).resolve().parents[2] / 'templates/deep_dive_layout'


def read_rgb(path):
    return cv2.cvtColor(cv2.imread(str(path)), cv2.COLOR_BGR2RGB)


def purple_mask(image):
    h, s, v = cv2.split(cv2.cvtColor(image, cv2.COLOR_RGB2HSV))
    return (h >= 126) & (h <= 148) & (s > 120) & (v > 130)


@pytest.mark.parametrize('frame_id', [217, 222, 241])
def test_actual_large_complete_purple_ring_survives_pixel_count_limit(frame_id):
    image = read_rgb(FIXTURES / f'large_purple_{frame_id}.png')
    mask = purple_mask(image)
    assert mask[19:77, 19:77].sum() > 1650
    assert semantics._purple_outline(mask)
    result = semantics.classify_icon(image)
    assert result['icon_id'] == 'purple_ring'
    assert result['confidence'] >= .80


@pytest.mark.parametrize('rotation', [0, 1, 2, 3])
def test_native_purple_reference_retains_outline_and_class(rotation):
    image = np.ascontiguousarray(np.rot90(read_rgb(NATIVE / 'icon_purple_ring.png'), rotation))
    assert semantics._purple_outline(purple_mask(image))
    assert semantics.classify_icon(image)['icon_id'] == 'purple_ring'


@pytest.mark.parametrize('name', ['purple_wall_0', 'purple_wall_1', 'purple_wall_2',
                                  'purple_wall_3', 'purple_partial_hud_883'])
def test_actual_purple_wall_or_partial_border_does_not_establish_complete_ring(name):
    image = read_rgb(FIXTURES / f'{name}.png')
    assert not semantics._purple_outline(purple_mask(image))
    assert semantics.classify_icon(image)['icon_id'] != 'purple_ring'


@pytest.mark.parametrize('side', ['left', 'right', 'top', 'bottom'])
def test_large_missing_side_is_not_closed_into_false_ring(side):
    image = read_rgb(FIXTURES / 'large_purple_217.png')
    if side == 'left':
        image[:, :48] = 20
    elif side == 'right':
        image[:, 48:] = 20
    elif side == 'top':
        image[:48] = 20
    else:
        image[48:] = 20
    assert not semantics._purple_outline(purple_mask(image))


def test_filled_wall_and_straight_square_border_do_not_supply_curved_cavity():
    wall = np.ones((96, 96), bool)
    border = np.zeros((96, 96), np.uint8)
    cv2.rectangle(border, (18, 18), (77, 77), 1, 6)
    assert not semantics._purple_outline(wall)
    assert not semantics._purple_outline(border)
