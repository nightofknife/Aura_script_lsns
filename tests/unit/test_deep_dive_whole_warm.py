"""Actual anti-aliased eye glyph regressions, with unchanged conservative gates."""
from pathlib import Path

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_layout_semantics as semantics


FIXTURES = Path(__file__).parents[1] / 'fixtures/deep_dive_anchor'
NATIVE = Path(semantics.__file__).resolve().parents[2] / 'templates/deep_dive_layout'


def read_rgb(path):
    return cv2.cvtColor(cv2.imread(str(path)), cv2.COLOR_BGR2RGB)


@pytest.mark.parametrize('frame_id', [386, 391])
def test_complete_actual_triple_eye_survives_connected_component_changes(frame_id):
    image = read_rgb(FIXTURES / f'whole_warm_D10_{frame_id}.png')
    # These true glyphs are rejected in the original maximum-domain space.
    old_shape = semantics._warm_glyph(image)
    assert max(score for score, _ in semantics._eye_correlation_scores(old_shape)) < .78
    result = semantics.classify_icon(image)
    assert result['icon_id'] == 'orange_triple_eye'
    assert result['confidence'] >= .80


@pytest.mark.parametrize('name', ['red_single_eye', 'orange_triple_eye',
                                  'orange_triple_eye_2', 'orange_triple_eye_3'])
@pytest.mark.parametrize('rotation', [0, 1, 2, 3])
def test_existing_native_eye_references_keep_their_class_under_right_angle_rotation(name, rotation):
    image = np.ascontiguousarray(np.rot90(read_rgb(NATIVE / f'icon_{name}.png'), rotation))
    expected = 'orange_triple_eye' if name.startswith('orange') else 'red_single_eye'
    assert semantics.classify_icon(image)['icon_id'] == expected


def test_actual_boss_glow_does_not_establish_an_eye_glyph():
    image = read_rgb(FIXTURES / 'whole_warm_boss_glow_130.png')
    assert semantics.classify_icon(image)['icon_id'] is None


@pytest.mark.parametrize('hue', [0, 7, 15, 23, 170])
def test_uniform_warm_walls_are_still_rejected(hue):
    hsv = np.empty((96, 96, 3), np.uint8)
    hsv[:] = [hue, 220, 230]
    assert semantics.classify_icon(cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB))['icon_id'] is None


def test_warm_border_seams_do_not_supply_a_central_shape():
    hsv = np.zeros((96, 96, 3), np.uint8)
    hsv[:12] = [7, 220, 230]
    hsv[84:] = [7, 220, 230]
    hsv[:, :12] = [7, 220, 230]
    hsv[:, 84:] = [7, 220, 230]
    image = cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)
    assert semantics._warm_glyph(image, whole=True) is None
    assert semantics.classify_icon(image)['icon_id'] is None
