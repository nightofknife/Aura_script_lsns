from pathlib import Path

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_layout_semantics as semantics


def test_filled_yellow_wall_and_curved_ring_do_not_establish_hex_outline():
    wall=np.zeros((96,96),np.uint8);wall[19:77,19:77]=1
    ring=np.zeros_like(wall);cv2.circle(ring,(48,48),28,1,5)
    assert not semantics._hex_outline(wall)
    assert not semantics._hex_outline(ring)


@pytest.mark.parametrize('name',['red_single_eye','orange_triple_eye'])
def test_warm_eye_coloured_like_overlap_band_is_not_reclassified_as_hex(name):
    root=Path(semantics.__file__).resolve().parents[2]/'templates/deep_dive_layout'
    image=cv2.cvtColor(cv2.imread(str(root/f'icon_{name}.png')),cv2.COLOR_BGR2HSV)
    image[:,:,0]=23
    result=semantics.classify_icon(cv2.cvtColor(image,cv2.COLOR_HSV2RGB))
    assert result['icon_id']!='yellow_hex'
