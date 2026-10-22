"""Synthetic warm walls and seams cannot establish operation anchors."""
import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_operation_frame as operation




def reading_patch(patch):
    image = np.zeros((720, 1280, 3), np.uint8)
    image[200:296, 450:546] = patch
    surface = [dict(operation_slot=0, centre=np.array([497.5, 247.5]),
                    quad=np.array([[450., 200.], [545., 200.], [545., 295.], [450., 295.]]))]
    return image, surface


@pytest.mark.parametrize('hue', [0, 7, 15, 23, 162, 170])
def test_bright_warm_wall_is_not_a_shape_or_anchor(hue):
    hsv = np.full((96, 96, 3), [hue, 220, 230], np.uint8)
    patch = cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)
    assert operation._classify_operation_crop(patch)[0]['icon_id'] is None
    image, surface = reading_patch(patch)
    assert not operation._readings(image, surface, operation._features(image, []))


def test_warm_border_seams_remain_unclassified():
    hsv = np.zeros((96, 96, 3), np.uint8)
    hsv[:12] = hsv[84:] = [7, 220, 230]
    hsv[:, :12] = hsv[:, 84:] = [7, 220, 230]
    patch = cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)
    assert operation._classify_operation_crop(patch)[0]['icon_id'] is None
    image, surface = reading_patch(patch)
    assert not operation._readings(image, surface, operation._features(image, []))


