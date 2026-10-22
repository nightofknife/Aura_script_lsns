"""Native eye templates and synthetic walls retain conservative glyph gates."""

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_layout_semantics as semantics


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
