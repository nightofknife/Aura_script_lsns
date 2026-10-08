"""Pure, same-source local plane grip proposal; no input or target authority.

Caller supplies current banks.local_visible rows and its actual UI mask. Pixel
source equality is checked here; freshness, pose reliability and motion licence
remain caller responsibilities. Local/shared-unknown poses are never promoted
to full cube coordinates or entity evidence.
"""
from copy import deepcopy
import math

import cv2
import numpy as np


KINDS = frozenset(('local_face_plane_geometry_only',
                   'local_face_geometry_shared_orientation_unknown'))


def official_ui_mask():
    mask=np.zeros((720,1280),np.uint8)
    mask[82:619,295:951]=255
    mask[494:579,605:677]=0
    return mask


def _source_valid(source):
    if not isinstance(source,dict): return False
    required=('session_id','generation','frame_id','frame_time','map_revision')
    if any(k not in source for k in required): return False
    if (type(source['session_id']) not in (str,int) or source['session_id']=='' or
        any(type(source[k]) is not int or source[k]<0 for k in ('generation','frame_id','map_revision')) or
        type(source['frame_time']) not in (int,float) or not math.isfinite(source['frame_time']) or
        source['frame_time']<0): return False
    return all(type(k) is str and type(v) in (str,int,float,bool,type(None))
               and (type(v) is not float or math.isfinite(v)) for k,v in source.items())


def _same_source(left,right):
    return (_source_valid(left) and left.keys()==right.keys() and
            all(type(left[k]) is type(right[k]) and left[k]==right[k] for k in left))


def _number(value):
    return type(value) in (int,float) and math.isfinite(value)


def _box_clearance(point,box):
    x,y,w,h=box
    lo=np.array((x-12,y-12));hi=np.array((x+w+12,y+h+12))
    outside=np.maximum(np.maximum(lo-point,point-hi),0.)
    return float(np.linalg.norm(outside))


def select_local_grip(projected,direction,max_distance_px,current_source,
                      target_boxes=(),allowed_mask=None):
    """Rank distance first, then the minimum remaining safety margin.

    Actual integer endpoints and their straight segment are tested at <=1px
    intervals. Every sample must be >=3px inside the actual convex quad, inside
    official UI AND caller mask eroded by a 13x13 square, and outside every
    target AABB expanded12px. Only2..24px actual endpoint distance is approved.
    No fixed centre ROI, >=.4 facing rule, or >=20px drag requirement is used.
    """
    out=dict(status='rejected',reason=None,grip=None,rejections=[],
             full_cube_coordinates_valid=False,target_evidence=False,
             targets_ready=False,input_authorized=False,freshness_evaluated=False,
             motion_licence_evaluated=False,target_box_source_policy='caller_supplied_actual_boxes',
             limits=dict(cosine_min=.20,quad_area_min=800,quad_inset_px=3,
                         navigation_erosion_px=6,target_expansion_px=12,
                         minimum_distance_px=2,maximum_distance_px=24))
    def reject(reason):
        out['reason']=reason; return out
    if not _source_valid(current_source): return reject('invalid_current_source')
    if not isinstance(projected,(list,tuple)): return reject('invalid_projected')
    if not _number(max_distance_px) or not 2<=max_distance_px<=24:
        return reject('invalid_distance')
    try:
        if isinstance(direction,(list,tuple)) and any(type(v) is bool for v in direction):
            return reject('invalid_direction')
        if isinstance(direction,np.ndarray) and direction.dtype==np.dtype('bool'):
            return reject('invalid_direction')
        vector=np.asarray(direction,float)
    except (ValueError,TypeError): return reject('invalid_direction')
    if vector.shape!=(2,) or not np.isfinite(vector).all() or np.linalg.norm(vector)<1e-8:
        return reject('invalid_direction')
    vector=vector/np.linalg.norm(vector)
    if allowed_mask is None: allowed_mask=official_ui_mask()
    if (not isinstance(allowed_mask,np.ndarray) or allowed_mask.shape!=(720,1280) or
        allowed_mask.dtype not in (np.dtype('uint8'),np.dtype('bool'))):
        return reject('invalid_allowed_mask')
    original=((allowed_mask>0)&(official_ui_mask()>0)).astype(np.uint8)
    permitted=cv2.erode(original,np.ones((13,13),np.uint8),borderType=cv2.BORDER_CONSTANT,borderValue=0)
    distances=cv2.distanceTransform(original,cv2.DIST_L2,cv2.DIST_MASK_PRECISE)
    if not isinstance(target_boxes,(list,tuple,np.ndarray)): return reject('invalid_target_boxes')
    boxes=[]
    for target in target_boxes:
        if isinstance(target,dict):
            if 'source' in target and not _same_source(target['source'],current_source):
                return reject('target_source_mismatch')
            target=target.get('box')
        if (isinstance(target,(list,tuple)) and any(type(v) is bool for v in target) or
            isinstance(target,np.ndarray) and target.dtype==np.dtype('bool')):
            return reject('invalid_target_box')
        try: box=np.asarray(target,float)
        except (ValueError,TypeError): return reject('invalid_target_box')
        if box.shape!=(4,) or not np.isfinite(box).all() or np.any(box[2:]<=0):
            return reject('invalid_target_box')
        boxes.append(box)
    candidates=[]
    lengths=sorted({float(n) for n in range(2,int(max_distance_px)+1)}|{float(max_distance_px)},reverse=True)
    for item_number,item in enumerate(projected):
        def skip(reason): out['rejections'].append(dict(item=item_number,reason=reason))
        if (not isinstance(item,dict) or item.get('projection_kind') not in KINDS or
            item.get('current') is False):
            skip('not_actual_local_projection');continue
        if not _same_source(item.get('source'),current_source):
            skip('source_mismatch');continue
        if (item.get('face') not in ('U','R','F','D','L','B') or
            any(type(item.get(k)) is not int or not 0<=item[k]<=2 for k in ('row','col'))):
            skip('invalid_cell_identity');continue
        cosine=item.get('cosine')
        if not _number(cosine) or not .20<=cosine<=1:
            skip('insufficient_or_invalid_cosine');continue
        try: quad=np.asarray(item.get('quad'),float);centre=np.asarray(item.get('centre'),float)
        except (ValueError,TypeError): skip('invalid_quad');continue
        if quad.shape!=(4,2) or centre.shape!=(2,) or not np.isfinite(quad).all() or not np.isfinite(centre).all():
            skip('invalid_quad');continue
        contour=quad.astype(np.float32)
        if not cv2.isContourConvex(contour) or len(np.unique(quad,axis=0))!=4:
            skip('nonconvex_quad');continue
        area=abs(float(cv2.contourArea(contour)))
        if area<800: skip('insufficient_quad_area');continue
        if np.any(quad<0) or np.any(quad[:,0]>=1280) or np.any(quad[:,1]>=720):
            skip('quad_outside_image');continue
        if cv2.pointPolygonTest(contour,tuple(centre),True)<0:
            skip('centre_outside_quad');continue
        for point in [centre]+[centre+.30*(vertex-centre) for vertex in quad]:
            start=np.rint(point).astype(int)
            for length in lengths:
                end=np.rint(point+vector*length).astype(int)
                distance=float(np.linalg.norm(end-start))
                if not 2-1e-9<=distance<=max_distance_px+1e-9: continue
                samples=np.linspace(start,end,int(math.ceil(distance))+1)
                pixels=np.rint(samples).astype(int)
                if np.any(pixels<0) or np.any(pixels[:,0]>=1280) or np.any(pixels[:,1]>=720): continue
                if not permitted[pixels[:,1],pixels[:,0]].all(): continue
                checks=np.vstack((samples,pixels))
                quad_margin=min(cv2.pointPolygonTest(contour,tuple(map(float,sample)),True) for sample in checks)
                if quad_margin<3: continue
                target_margin=min((_box_clearance(sample,b) for sample in checks for b in boxes),default=math.inf)
                if target_margin<=0: continue
                mask_margin=float(distances[pixels[:,1],pixels[:,0]].min())-6
                safety=min(quad_margin-3,mask_margin,target_margin)
                candidates.append(dict(face=item['face'],row=item['row'],col=item['col'],
                    source=deepcopy(current_source),projection_kind=item['projection_kind'],
                    start=start.tolist(),end=end.tolist(),approved_distance_px=distance,
                    path_samples=samples.tolist(),sample_pixels=pixels.tolist(),
                    margin_px=safety,quad_margin_px=quad_margin,mask_margin_px=mask_margin,
                    expanded_target_margin_px=None if not boxes else target_margin,
                    cosine=cosine,quad_area=area,source_exact_match=True))
                break
    if not candidates: return reject('no_safe_local_grip')
    best=max(candidates,key=lambda c:(c['approved_distance_px'],c['margin_px'],c['cosine']))
    out.update(status='local_grip_proposal',reason=None,grip=deepcopy(best),
               candidate_count=len(candidates),source=deepcopy(current_source),
               ranking='actual_distance_then_remaining_safety_margin_then_cosine')
    return out
