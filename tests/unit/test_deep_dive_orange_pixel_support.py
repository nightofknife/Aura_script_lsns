"""Independent eye classification and its measured pixels share the warm domain."""
from pathlib import Path

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_layout_vision as vision


NATIVE = Path(vision.__file__).resolve().parents[2] / 'templates/deep_dive/deep_dive_layout'


def read_rgb(path):
    return cv2.cvtColor(cv2.imread(str(path)), cv2.COLOR_BGR2RGB)


def inner_mass(mask):
    return int(mask[10:86, 10:86].sum())


@pytest.mark.parametrize('rotation', range(4))
def test_actual_single_eye_cannot_become_expected_triple_eye(rotation):
    patch = np.ascontiguousarray(np.rot90(read_rgb(NATIVE / 'icon_red_single_eye.png'), rotation))
    assert vision.classify_icon(patch)['icon_id'] == 'red_single_eye'
    # The shared foreground is not permission to replace independent shape.
    assert inner_mass(vision._icon_mask(patch, 'orange_triple_eye')) > 50
    item = dict(quad=np.float32(((0, 0), (95, 0), (95, 95), (0, 95))),
                centre=np.array((47.5, 47.5)))
    assert vision._localized_known_glyph(patch, item, 'orange_triple_eye') is None


@pytest.mark.parametrize('hue', [0, 15, 170])
def test_filled_warm_wall_never_proves_expected_triple_eye(hue):
    image = np.zeros((96, 96, 3), np.uint8)
    image[20:76, 20:76] = cv2.cvtColor(np.uint8([[[hue, 220, 220]]]), cv2.COLOR_HSV2RGB)[0, 0]
    assert vision.classify_icon(image)['icon_id'] is None
    item = dict(quad=np.float32(((0, 0), (95, 0), (95, 95), (0, 95))),
                centre=np.array((47.5, 47.5)))
    assert vision._localized_known_glyph(image, item, 'orange_triple_eye') is None


def test_other_six_colour_masks_remain_exactly_unchanged():
    rng = np.random.default_rng(18)
    image = rng.integers(0, 256, (96, 96, 3), dtype=np.uint8)
    h, s, v = cv2.split(cv2.cvtColor(image, cv2.COLOR_RGB2HSV))
    expected = {
        'white_diamond': (s < 62) & (v > 170),
        'blue_scales': (h >= 100) & (h <= 125) & (s > 100) & (v > 135),
        'green_burst': (h >= 40) & (h <= 91) & (s > 130) & (v > 135),
        'purple_ring': (h >= 126) & (h <= 148) & (s > 120) & (v > 130),
        'yellow_hex': (h >= 21) & (h <= 39) & (s > 110) & (v > 160),
        'red_single_eye': ((h <= 6) | (h >= 168)) & (s > 140) & (v > 140),
    }
    for name, mask in expected.items():
        np.testing.assert_array_equal(vision._icon_mask(image, name), mask)
