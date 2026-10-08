"""Local pink-sphere evidence and conservative three-entity fusion.

All coordinates are full-frame RGB pixels. This module changes neither OpenCV
thread settings nor process environment and does not load a model.
"""
from __future__ import annotations

from copy import deepcopy

import cv2
import numpy as np

_YY, _XX = np.mgrid[-24:25, -24:25]
_DISTANCES = np.sqrt(_XX * _XX + _YY * _YY)
_ANGLES = np.arctan2(_YY, _XX)


def local_player_heads(rgb):
    """Confirm spherical heads with the existing .83/.14/8-sector evidence."""
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    hue, saturation, value = cv2.split(hsv)
    pink = (hue >= 132) & (hue <= 177) & (saturation > 35) & (value > 85) & (rgb[:, :, 0] > rgb[:, :, 2])
    count, _, stats, _ = cv2.connectedComponentsWithStats(pink[30:670, 232:998].astype(np.uint8), 8)
    windows = []
    for x, y, width, height, area in stats[1:]:
        if area < 40 or min(width, height) < 6 or max(width, height) > 110:
            continue
        x, y = x + 232, y + 30
        windows.append((max(232, x-24), max(30, y-24), min(998, x+width+24), min(670, y+height+24)))
    heads = []
    for x0, y0, x1, y1 in windows:
        circles = cv2.HoughCircles(cv2.medianBlur(gray[y0:y1, x0:x1], 3),
            cv2.HOUGH_GRADIENT, 1, 14, param1=100, param2=14, minRadius=8, maxRadius=16)
        if circles is None:
            continue
        for cx, cy, radius in circles[0]:
            cx, cy = float(cx+x0), float(cy+y0)
            if not (280 < cx < 950 and 78 < cy < 622):
                continue
            if (618 < cx < 663 and 506 < cy < 550) or (595 < cx < 691 and 550 < cy < 579):
                continue
            x, y = int(cx), int(cy)
            patch = hsv[y-24:y+25, x-24:x+25]
            pink_inner = (patch[:, :, 0] >= 132) & (patch[:, :, 0] <= 177) & (patch[:, :, 1] > 35) & (patch[:, :, 2] > 85)
            pale = (patch[:, :, 1] < 110) & (patch[:, :, 2] > 175)
            inner = _DISTANCES < radius*.70
            rim = (_DISTANCES > radius*.85) & (_DISTANCES < radius*1.20)
            pink_fraction, pale_fraction = float(pink_inner[inner].mean()), float(pale[rim].mean())
            sectors = sum(np.count_nonzero(pale & rim & (_ANGLES >= -np.pi+i*np.pi/6)
                & (_ANGLES < -np.pi+(i+1)*np.pi/6)) >= 2 for i in range(12))
            if pink_fraction < .83 or pale_fraction < .14 or sectors < 8:
                continue
            heads.append((float(radius), dict(kind="player", point=[cx, cy],
                box=[int(cx-radius), int(cy-radius), int(radius*2+1), int(radius*2+1)],
                anchor_type="head", confirmable=True,
                confidence=float(round(min(.85, .62+.12*pink_fraction+.08*sectors/12), 3)),
                source="deep_dive_local_pink_head")))
    result = []
    for _, head in sorted(heads, key=lambda item: item[0]):
        if not any(np.linalg.norm(np.asarray(head["point"])-other["point"]) < 50 for other in result):
            result.append(head)
    return result, dict(pink_components=count-1, local_windows=len(windows))


def _centre(target):
    x, y, width, height = target["box"]
    return np.asarray([x+width/2, y+height/2])


def _same_head(head, model):
    """Agree on one sphere, rather than a loose 30px head/body neighborhood."""
    if model['kind'] != 'player' or model.get('anchor_type') != 'head':
        return False
    hx, hy, hw, hh = head['box']; mx, my, mw, mh = model['box']
    overlap = max(0, min(hx+hw, mx+mw)-max(hx, mx))*max(0, min(hy+hh, my+mh)-max(hy, my))
    union = max(1, hw*hh+mw*mh-overlap)
    radius = min(hw, hh, mw, mh)/2
    distance = np.linalg.norm(np.asarray(head['point'])-model['point'])
    return overlap/union >= .30 and distance <= max(3., radius*.75)


def _connected_pawn_parts(rgb, head, model):
    """Current-pixel evidence that two pink regions share a pawn silhouette."""
    if rgb is None:
        return False
    hx, hy, hw, hh = head['box']; mx, my, mw, mh = model['box']
    x0, y0 = max(0, min(hx,mx)-4), max(0, min(hy,my)-4)
    x1, y1 = min(1280, max(hx+hw,mx+mw)+5), min(720, max(hy+hh,my+mh)+5)
    hsv = cv2.cvtColor(rgb[y0:y1,x0:x1], cv2.COLOR_RGB2HSV)
    patch = rgb[y0:y1,x0:x1]
    pink = (hsv[:,:,0]>=132)&(hsv[:,:,0]<=177)&(hsv[:,:,1]>35)&(hsv[:,:,2]>85)&(patch[:,:,0]>patch[:,:,2])
    # The sphere is bounded by a pale rim, so its pink core alone is normally
    # disconnected from the pink neck. Include that same pale silhouette
    # evidence before testing connectivity to the body, without dilating it.
    pale = (hsv[:,:,1]<110)&(hsv[:,:,2]>175)
    _, labels, _, _ = cv2.connectedComponentsWithStats((pink|pale).astype(np.uint8),8)
    def at(point,radius):
        x,y=round(point[0])-x0,round(point[1])-y0
        if not (0<=x<labels.shape[1] and 0<=y<labels.shape[0]):return set()
        yy,xx=np.ogrid[:labels.shape[0],:labels.shape[1]]
        inner=(xx-x)**2+(yy-y)**2<(radius*.7)**2
        return set(labels[inner].ravel())-{0}
    return bool(at(head['point'],min(hw,hh)/2) & at(model['point'],min(mw,mh)/2))


def fuse_entities(model_targets, heads, *, image_rgb=None):
    """Require model/local-sphere agreement; unsupported circles stay masks."""
    accepted, masks, decisions = [], [], []
    confirmed_heads = []
    supported = []
    for source in heads:
        head = deepcopy(source)
        matches = [(i,q) for i,q in enumerate(model_targets) if _same_head(head,q)]
        best = max(matches, key=lambda item: item[1]['confidence']) if matches else None
        reason = None
        if best is None:
            reason = 'local_circle_without_same_sphere_model_support'
        elif not best[1].get('confirmable', False):
            # Weak model regions can corroborate a real sphere. A connected
            # near-by region of an already confirmed pawn is its body/base,
            # not independent evidence for a second player.
            for model in model_targets:
                if model['kind'] != 'player' or not model.get('confirmable',False) or _same_head(head,model):continue
                separation = np.linalg.norm(np.asarray(head['point'])-model['point'])
                if separation <= 4*max(model['box'][2:])/2 and _connected_pawn_parts(image_rgb,head,model):
                    reason = 'connected_body_circle_of_model_confirmed_pawn'
                    break
        if reason:
            head.update(confirmable=False, fusion_rejection=reason, evidence_role='mask_only',
                raw_circle_confidence=head['confidence'], confidence=min(.24,head['confidence']))
            masks.append(head)
        else:
            model_index, model = best
            # Preserve any source-frame/request metadata on the corroborating
            # model record. Circle center and model extent remain same-frame.
            merged = deepcopy(model)
            merged.update(head)
            merged.update(circle_box=head['box'], box=list(model['box']),
                model_support_confidence=model['confidence'],
                box_evidence='existing_model_box_with_confirmed_head_center')
            supported.append((model_index,merged))
            confirmed_heads.append(head)
        decisions.append(dict(kind='player',point=list(head['point']),rejected=reason))
    for source in model_targets:
        target = deepcopy(source)
        reason = None
        if target["kind"] == "inspiration":
            qx, qy, qw, qh = target["box"]
            for head in confirmed_heads:
                hx, hy, hw, hh = head["box"]
                overlap = max(0, min(qx+qw, hx+hw)-max(qx, hx)) * max(0, min(qy+qh, hy+hh)-max(qy, hy))
                fraction = overlap/max(1, qw*qh)
                if fraction >= .5 and np.linalg.norm(_centre(target)-_centre(head)) <= max(hw, hh)/2*1.25:
                    reason = "majority_box_is_confirmed_pink_head"
                    break
            cx, cy = _centre(target)
            if reason is None and 595 <= cx <= 691 and 506 <= cy <= 579:
                reason = "existing_inspiration_reset_HUD_exclusion"
        if reason is not None:
            target.update(confirmable=False, fusion_rejection=reason, evidence_role='mask_only')
            masks.append(target)
        else:
            accepted.append(target)
        decisions.append(dict(kind=target["kind"], point=list(target["point"]), rejected=reason))
    # Replace only the exact corroborating player candidate. Model NMS owns
    # duplicate boxes; a global center-distance rule could erase a real second
    # player and is deliberately not used here.
    for model_index, head in supported:
        original = model_targets[model_index]
        index = next((i for i,q in enumerate(accepted) if q['kind']=='player'
            and q['point']==original['point'] and q['box']==original['box']),None)
        if index is not None:accepted[index]=head
    return accepted+masks, decisions
