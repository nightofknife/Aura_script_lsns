"""Independent current pixels can recover glyphs cut by a drifting crop."""
import numpy as np
from plans.resonance_pc.src.actions import _deep_dive_layout_vision as vision


def test_filled_coloured_wall_is_not_a_local_glyph():
    image=np.zeros((720,1280,3),np.uint8);image[160:320,380:540]=(0,0,255)
    quad=np.array([[410.,190.],[510.,190.],[510.,290.],[410.,290.]])
    assert vision._localized_known_glyph(image,dict(quad=quad,centre=quad.mean(axis=0)),'blue_scales') is None
