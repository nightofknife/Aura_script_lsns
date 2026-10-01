"""Independent current pixels can recover glyphs cut by a drifting crop."""
import json
from pathlib import Path
import cv2
import numpy as np
from plans.resonance_pc.src.actions import _deep_dive_layout_vision as vision

FIXTURE=Path(__file__).resolve().parents[1]/'fixtures/deep_dive_anchor'

def load_case():
    data=json.loads((FIXTURE/'oblique_630.json').read_text(encoding='utf8'))
    scanner=vision.LayoutScanner()
    for key in ('rvec','tvec','rotation','pivot_reference'):
        setattr(scanner,key,np.asarray(data['pose'][key],float))
    scanner.ready=True;scanner.quality=.97
    scanner.icon_anchors={int(key):value for key,value in data['known_icons'].items()}
    image=cv2.cvtColor(cv2.imread(str(FIXTURE/'oblique_630.png')),cv2.COLOR_BGR2RGB)
    scanner._anchor_source=dict(source_frame_id=630,source_frame_time=100.,source_map_revision=0)
    return scanner,image

def test_oblique_real_glyphs_recover_with_original_joint_fit_bounds(monkeypatch):
    scanner,image=load_case();targets=vision.detect_targets(image)
    original=vision._localized_known_glyph
    monkeypatch.setattr(vision,'_localized_known_glyph',lambda *args:None)
    scanner._refine_centres(image,targets)
    assert not scanner.refine_diagnostic['renewed']
    assert scanner.refine_diagnostic['confirmed']==3
    scanner,image=load_case()
    monkeypatch.setattr(vision,'_localized_known_glyph',original)
    scanner._refine_centres(image,targets)
    evidence=scanner.refine_diagnostic
    assert evidence['renewed']
    assert evidence['accepted_faces']['D']>=2
    assert evidence['accepted_faces']['L']>=2
    assert len(evidence['localized_known_cell_indices'])>=4
    assert evidence['median_after']<=3
    assert evidence['max_after']<=6
    assert evidence['angle_deg']<=4
    assert scanner.glyph_anchor_frame_id==630
    assert scanner.glyph_anchor_at==100.
    assert all(not entries for entries in scanner.evidence)

def test_known_atlas_without_current_pixels_cannot_renew():
    scanner,image=load_case()
    scanner._refine_centres(np.zeros_like(image),[])
    assert scanner.refine_diagnostic['localized_known_cell_indices']==[]
    assert not scanner.refine_diagnostic['renewed']

def test_filled_coloured_wall_is_not_a_local_glyph():
    image=np.zeros((720,1280,3),np.uint8);image[160:320,380:540]=(0,0,255)
    quad=np.array([[410.,190.],[510.,190.],[510.,290.],[410.,290.]])
    assert vision._localized_known_glyph(image,dict(quad=quad,centre=quad.mean(axis=0)),'blue_scales') is None
