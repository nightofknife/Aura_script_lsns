"""Research pixel-only glyph candidates, not tiles, faces, sources or votes.

Colour components propose regions. Existing classify_icon gates then classify
component-normalized96 patches; all seven original icon PNG shapes add a veto.
Confidence remains the native heuristic strength, not a calibrated probability.
The template mapping describes bbox normalization/NCC alignment, not a measured
tile homography. No R/T, expected cell/type or projected quad is accepted.
"""
from functools import lru_cache
from pathlib import Path
import time
import hashlib

import cv2
import numpy as np

from plans.resonance_pc.src.actions import _deep_dive_layout_semantics as semantics
from plans.resonance_pc.src.actions._deep_dive_known_glyph_matcher import _masks


ICONS = ('white_diamond', 'blue_scales', 'green_burst', 'purple_ring',
         'yellow_hex', 'red_single_eye', 'orange_triple_eye')
TEMPLATE_ROOT = Path(__file__).resolve().parents[1]/'plans/resonance_pc/templates/deep_dive_layout'
# Manual review of these original PNG pixels only. These are glyph bounds,
# never tile bounds, source captures, face identities or model labels.
TEMPLATE_GLYPH_ROIS = {
    'icon_white_diamond.png': (15,10,78,74),
    'icon_blue_scales.png': (15,17,82,78),
    'icon_green_burst.png': (11,9,79,75),
    'icon_purple_ring.png': (11,6,70,69),
    'icon_yellow_hex.png': (10,24,75,84),
    'icon_red_single_eye.png': (13,13,75,70),
    'icon_orange_triple_eye.png': (17,21,77,86),
    'icon_orange_triple_eye_2.png': (25,21,83,89),
    'icon_orange_triple_eye_3.png': (25,21,83,89),
}


def _bbox_shape(mask):
    y, x = np.where(mask)
    if len(x) < 45:
        return None
    x0, y0, w, h = int(x.min()), int(y.min()), int(np.ptp(x)+1), int(np.ptp(y)+1)
    if min(w, h) < 10 or len(x)/(w*h) > .70:
        return None
    shape = cv2.resize(mask[y0:y0+h, x0:x0+w].astype(np.float32), (64, 64), interpolation=cv2.INTER_AREA)
    return shape, (x0, y0, w, h)


@lru_cache(maxsize=1)
def _templates():
    templates = {}
    for name in ICONS:
        entries = []
        family = 'warm_eye' if name in ('red_single_eye', 'orange_triple_eye') else name
        for path in sorted(TEMPLATE_ROOT.glob('icon_'+name+'*.png')):
            raw = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
            if raw is None:
                continue
            rgb = cv2.cvtColor(raw, cv2.COLOR_BGR2RGB)
            mask = _masks(rgb)[family]
            x0,y0,x1,y1=TEMPLATE_GLYPH_ROIS[path.name]
            allowed=np.zeros(mask.shape,bool); allowed[y0:y1,x0:x1]=True
            mask &= allowed
            original_measured=_bbox_shape(mask)
            if original_measured is None:
                continue
            original_bbox=original_measured[1]
            for angle in range(0,360,10):
                affine=cv2.getRotationMatrix2D((47.5,47.5),angle,1.)
                affine[:,2]+=48
                rotated=cv2.warpAffine(mask.astype(np.uint8),affine,(192,192),flags=cv2.INTER_NEAREST)
                measured = _bbox_shape(rotated.astype(bool))
                if measured is not None:
                    shape, bbox = measured
                    tx,ty,tw,th=bbox
                    to_shape=np.array([[64/tw,0,(.5-tx)*64/tw-.5],
                        [0,64/th,(.5-ty)*64/th-.5],[0,0,1.]])@np.vstack([affine,[0,0,1]])
                    entries.append(dict(shape=shape,bbox=original_bbox,rotated_bbox=bbox,path=str(path),
                        template_roi=list(TEMPLATE_GLYPH_ROIS[path.name]),
                        rotation_degrees=angle,to_shape=to_shape))
        templates[name] = entries
    return templates


def _shape_match(shape, entries):
    best = None
    alternatives=[]
    padded = np.pad(shape, 4)
    for entry in entries:
        for k in (0,):
            template = np.ascontiguousarray(entry['shape'])
            scores = cv2.matchTemplate(padded, template, cv2.TM_CCORR_NORMED)
            _, score, _, location = cv2.minMaxLoc(scores)
            if not np.isfinite(score):
                continue
            alternatives.append(dict(score=float(score),rotation_degrees=entry['rotation_degrees'],
                                     template_path=entry['path']))
            if best is None or score > best['score']:
                angle=entry['rotation_degrees']
                best = dict(score=float(score), rotation_quarters=(angle//90 if angle%90==0 else None),
                            location=location, entry=entry)
    if best is not None:
        alternatives.sort(key=lambda p:p['score'],reverse=True)
        best['alternatives']=alternatives
    return best


def _components(mask):
    _, labels, stats, centres = cv2.connectedComponentsWithStats(
        cv2.dilate(mask.astype(np.uint8), np.ones((3, 3), np.uint8)), 8)
    available = []
    for i in range(1, len(stats)):
        x, y, w, h, area = map(int, stats[i])
        if not 45 <= area <= 7000 or max(w, h) > 140 or min(w, h) < 8:
            continue
        available.append(i)
    used = set()
    for index in sorted(available, key=lambda i:stats[i, cv2.CC_STAT_AREA], reverse=True):
        if index in used:
            continue
        group = [index]
        for other in available:
            if other in used or other == index or np.linalg.norm(centres[other]-centres[index]) >= 32:
                continue
            combined = stats[group+[other]]
            extent = np.max(combined[:, :2]+combined[:, 2:4], axis=0)-np.min(combined[:, :2], axis=0)
            if max(extent) <= 100:
                group.append(other)
        used.update(group)
        yield np.isin(labels, group) & mask


def _template_mapping(match, source_bbox):
    x, y, w, h = source_bbox
    dx, dy = np.asarray(match['location'])-4
    shift = np.array([[1, 0, dx],[0, 1, dy],[0, 0, 1]])
    to_source = np.array([[w/64, 0, x+w/128-.5], [0, h/64, y+h/128-.5], [0, 0, 1]])
    return (to_source@shift@match['entry']['to_shape']).tolist()


def _normalized_patch(raw, mask):
    y,x=np.where(mask)
    x0,y0,x1,y1=int(x.min()),int(y.min()),int(x.max()+1),int(y.max()+1)
    raw=raw[y0:y1,x0:x1].copy(); raw[~mask[y0:y1,x0:x1]]=0
    h,w=raw.shape[:2]; scale=58/max(w,h)
    nw,nh=max(1,round(w*scale)),max(1,round(h*scale))
    patch=np.zeros((96,96,3),np.uint8); nx,ny=(96-nw)//2,(96-nh)//2
    patch[ny:ny+nh,nx:nx+nw]=cv2.resize(raw,(nw,nh),interpolation=cv2.INTER_AREA)
    return patch


def _rectified_patch(normalized, angle):
    affine=cv2.getRotationMatrix2D((47.5,47.5),-angle,1.)
    rotated=cv2.warpAffine(normalized,affine,(96,96),flags=cv2.INTER_LINEAR)
    mask=_masks(rotated)['warm_eye']
    return _normalized_patch(rotated,mask) if mask.any() else rotated


def _warm_reading(shape, normalized, threshold, deadline):
    hypotheses={name:_shape_match(shape,_templates()[name])
                for name in ('red_single_eye','orange_triple_eye')}
    angles=sorted({a['rotation_degrees'] for m in hypotheses.values() for a in m['alternatives']
                   if a['score']>=threshold})
    checks=[]; valid=[]
    for angle in angles:
        if time.perf_counter()>=deadline:
            return None,None,checks,'warm_preprocessing_budget_exhausted'
        patch=_rectified_patch(normalized,angle)
        reading=semantics.classify_icon(patch)
        shape_support=[dict(icon=name,score=a['score'],template_path=a['template_path'])
                       for name,m in hypotheses.items() for a in m['alternatives']
                       if a['rotation_degrees']==angle and a['score']>=threshold]
        checks.append(dict(rotation_degrees=angle,native_reading=reading,shape_hypotheses=shape_support,
                           rectified_rgb_sha256=hashlib.sha256(patch.tobytes()).hexdigest()))
        name=reading.get('icon_id')
        matching=[a for a in shape_support if a['icon']==name]
        if matching and reading.get('confidence',0)>=.70:
            support=max(matching,key=lambda a:a['score'])
            valid.append((support['score'],name,reading,angle,support['template_path']))
    names={v[1] for v in valid}
    if len(names)>1:
        return None,None,checks,'ambiguous_warm_type'
    if not valid:
        return None,None,checks,'warm_rectified_native_rejected'
    _,name,reading,angle,path=max(valid,key=lambda v:v[0])
    selected=_shape_match(shape,[e for e in _templates()[name]
                                if e['rotation_degrees']==angle and e['path']==path])
    selected['alternatives']=hypotheses[name]['alternatives']
    return reading,selected,checks,None


def propose_pixel_glyphs(rgb, roi=(300,84,950,615), *, shape_threshold=.78,
                         max_components=128, max_seconds=30.):
    """Return all accepted and rejected pixel regions for manual review.

    Existing warm .78/.055 and all native colour/shape gates remain unchanged.
    shape_threshold is a separate conservative research veto for seven PNG
    shapes, not a reused calibrated probability or relaxed native threshold.
    """
    result = dict(status='proposal_only', proposals=[], geometry_proposals=[], rejected=[],
        tile_corners_measured=False, face_bound=False, body_bound=False, source_bound=False,
        targets_ready=False, input_ready=False, incomplete=False,
        classification_domain='isolated_component_normalized96_not_full_tile',
        shape_threshold=shape_threshold)
    started = time.perf_counter()
    if (not isinstance(rgb, np.ndarray) or rgb.ndim != 3 or rgb.shape[2] != 3 or rgb.dtype != np.uint8 or
            not isinstance(roi, (tuple,list)) or len(roi) != 4 or any(type(v) is not int for v in roi) or
            isinstance(shape_threshold,bool) or not isinstance(shape_threshold,(float,int)) or
            not np.isfinite(shape_threshold) or not .78 <= shape_threshold <= 1 or
            type(max_components) is not int or not 1 <= max_components <= 256 or
            isinstance(max_seconds,bool) or not isinstance(max_seconds,(float,int)) or
            not np.isfinite(max_seconds) or not 0 < max_seconds <= 30):
        return dict(result, status='rejected', reason='invalid_input')
    left, top, right, bottom = roi
    if not 0 <= left < right <= rgb.shape[1] or not 0 <= top < bottom <= rgb.shape[0]:
        return dict(result, status='rejected', reason='invalid_roi')
    image = rgb[top:bottom, left:right]
    masks = _masks(image)
    templates = _templates()
    seen = []
    evaluated = 0
    for family, mask in masks.items():
        for component in _components(mask):
            if time.perf_counter()-started >= max_seconds or evaluated >= max_components:
                result.update(incomplete=True, reason='component_or_time_budget_exhausted')
                break
            measured = _bbox_shape(component)
            if measured is None:
                continue
            shape, (x,y,w,h) = measured
            evaluated += 1
            yy, xx = np.where(component)
            center = np.array([(np.percentile(xx,3)+np.percentile(xx,97))/2+left,
                               (np.percentile(yy,3)+np.percentile(yy,97))/2+top])
            normalized = _normalized_patch(image,component)
            reading = semantics.classify_icon(normalized)
            raw_reading=reading.copy(); warm_checks=[]; warm_reason=None; warm_match=None
            if family=='warm_eye':
                refined,warm_match,warm_checks,warm_reason=_warm_reading(
                    shape,normalized,shape_threshold,started+max_seconds)
                if refined is not None:
                    reading=refined
            contours,_ = cv2.findContours(component.astype(np.uint8),cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
            entry = dict(family=family, pixel_bbox=[x+left,y+top,w,h], centre=center.tolist(),
                contours=[(c.reshape(-1,2)+[left,top]).tolist() for c in contours],
                pixel_mass=len(xx), native_reading=reading)
            entry.update(raw_normalized_native_reading=raw_reading,
                normalized_rgb_sha256=hashlib.sha256(normalized.tobytes()).hexdigest(),
                warm_rectified_checks=warm_checks)
            if family=='warm_eye' and warm_reason:
                result['rejected'].append(dict(entry,reason=warm_reason))
                if warm_reason=='warm_preprocessing_budget_exhausted':
                    result.update(incomplete=True,reason='component_or_time_budget_exhausted')
                    break
                continue
            name = reading.get('icon_id')
            expected_family = 'warm_eye' if name in ('red_single_eye','orange_triple_eye') else name
            if name not in ICONS or expected_family != family or reading.get('confidence',0) < .70:
                result['rejected'].append(dict(entry,reason='native_classification_rejected'))
                continue
            match = warm_match if warm_match is not None else _shape_match(shape,templates[name])
            # Keep native-normalized candidates visible even when the extra
            # shape veto rejects them. These are review regions, not support.
            geometry = dict(entry, proposed_icon=name, shape_veto_passed=bool(
                match is not None and match['score'] >= shape_threshold),
                ordinary_support=False, corners=None, status='raw_component_for_review',
                shape_score=match['score'] if match else None)
            if match is not None:
                geometry.update(template_path=match['entry']['path'],
                    template_glyph_bbox=list(match['entry']['bbox']),
                    template_rotation_quarters=match['rotation_quarters'],
                    template_to_image=_template_mapping(match,(x+left,y+top,w,h)),
                    mapping_kind='bbox_normalization_ncc_alignment_not_tile_homography')
                geometry.update(template_glyph_roi=match['entry']['template_roi'],
                    template_rotation_degrees=match['entry']['rotation_degrees'],
                    rotation_alternatives=[a for a in match['alternatives'] if a['score']>=shape_threshold],
                    top_shape_alternatives=match['alternatives'][:8],
                    rotation_unique=len({a['rotation_degrees'] for a in match['alternatives']
                                         if a['score']>=shape_threshold})==1,
                    orientation_proven=False,
                    template_annotation_source='manual_original_template_pixels_only')
            result['geometry_proposals'].append(geometry)
            if match is None or match['score'] < shape_threshold:
                result['rejected'].append(dict(entry,reason='seven_template_shape_veto',
                    shape_score=match['score'] if match else None))
                continue
            if any(n == name and np.linalg.norm(center-c) < 12 for n,c in seen):
                result['rejected'].append(dict(entry,reason='same_pixel_duplicate'))
                continue
            seen.append((name,center))
            entry.update(icon_id=name, confidence=reading['confidence'], shape_score=match['score'],
                template_path=match['entry']['path'], template_glyph_bbox=list(match['entry']['bbox']),
                template_rotation_quarters=match['rotation_quarters'],
                template_to_image=_template_mapping(match,(x+left,y+top,w,h)),
                mapping_kind='bbox_normalization_ncc_alignment_not_tile_homography',
                corners=None, status='glyph_proposal_only', expected_cell=None)
            entry.update({key:geometry[key] for key in ('template_glyph_roi','template_rotation_degrees',
                'rotation_alternatives','top_shape_alternatives','rotation_unique','orientation_proven',
                'template_annotation_source')})
            result['proposals'].append(entry)
        if result['incomplete']:
            break
    result.update(evaluated_components=evaluated, elapsed_sec=time.perf_counter()-started)
    return result
