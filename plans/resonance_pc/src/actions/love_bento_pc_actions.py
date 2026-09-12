"""Screenshot-only love-bento scanning with bounded scrolling and tuple identity."""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

from packages.aura_core.observability.logging.core_logger import logger
from .player_recovery_pc_actions import _error, check_cancelled

ROOT = Path(__file__).resolve().parents[2]


def load_love_bento_catalog(vision, *, navigation_only=False):
    catalog = json.loads((ROOT / 'data/meta/love_bento.json').read_text(encoding='utf-8'))
    if catalog.get('reference_client') != [1280, 720]:
        raise ValueError('Unsupported love-bento reference resolution')
    if navigation_only:
        return {'items': [], 'scanner': catalog['scanner']}
    def resolve(ref):
        path = Path(vision.resolve_template('resonance_pc', ref, ROOT)).resolve()
        if not path.is_relative_to(ROOT) or vision.load_image_file(path, cv2.IMREAD_UNCHANGED) is None:
            raise ValueError(f'Invalid love-bento asset: {ref}')
        return str(path)
    for section in ('items', 'roles', 'days'):
        if not catalog[section]:
            raise ValueError(f'Empty love-bento catalog: {section}')
        for row in catalog[section]:
            row['resolved'] = resolve(row['template'])
            if 'mask' in row:
                row['resolved_mask'] = resolve(row['mask'])
    for key in ('anchor_template', 'anchor_mask', 'expiration_template', 'expiration_mask'):
        catalog['scanner'][key] = resolve(catalog['scanner'][key])
    return catalog


class LoveBentoScanner:
    def __init__(self, reader, catalog):
        self.reader, self.catalog = reader, catalog
        self.cfg = catalog['scanner']
        self.deadline = time.monotonic() + self.cfg['total_timeout_sec']

    def guard(self):
        check_cancelled()
        if time.monotonic() >= self.deadline:
            raise _error('love-bento scan exceeded total timeout')

    def capture(self):
        self.guard()
        if not self.reader.is_page('bento_cabinet'):
            raise _error('love-bento cabinet disappeared')
        return self.reader.capture(self.cfg['capture_roi'])

    @staticmethod
    def unchanged(a, b):
        # Ignore a few bright/animated pixels; movement must affect very little area.
        delta = np.max(np.abs(a.astype(np.int16)-b.astype(np.int16)), axis=2)
        return float(np.mean(delta > 15)) < 0.01

    def stable_frame(self):
        until = min(self.deadline, time.monotonic()+self.cfg['page_timeout_sec'])
        previous = self.capture()
        stable = 0
        while time.monotonic() < until:
            time.sleep(self.cfg['poll_interval_sec'])
            current = self.capture()
            stable = stable+1 if self.unchanged(previous, current) else 0
            if stable >= 2:
                return current
            previous = current
        raise _error('love-bento page did not settle')

    def classify(self, frame, point, section):
        self.guard()
        box_key = {'items':'food_crop_xyxy', 'roles':'role_crop_xyxy', 'days':'days_crop_xyxy'}[section]
        x1,y1,x2,y2 = self.catalog['geometry'][box_key]
        x,y = point
        margin = self.cfg['roi_margin']
        crop = frame[max(0,y+y1-margin):min(frame.shape[0],y+y2+margin),
                     max(0,x+x1-margin):min(frame.shape[1],x+x2+margin)]
        rows = self.catalog[section]
        key = {'items':'food', 'roles':'role', 'days':'days'}[section]
        results = self.reader.vision.find_templates_batch(
            source_image=crop, template_images=[r['resolved'] for r in rows],
            mask_images=[r['resolved_mask'] for r in rows] if section=='days' else None,
            threshold=self.cfg[key+'_threshold'], use_grayscale=section!='items',
            # Mean-centered correlation separates digit strokes from their
            # shared background brightness. Keep the existing digit mask.
            match_method=cv2.TM_CCOEFF_NORMED,
            preprocess='none')
        if len(results)!=len(rows) or any(getattr(r,'debug_info',{}).get('error') for r in results):
            raise _error(f'love-bento {section} matching failed')
        if section == 'days' and any(not math.isfinite(float(r.confidence)) for r in results):
            # A flat/degenerate ROI can produce NaN/Inf in masked correlation.
            # Do not silently drop a competitor and accept another digit.
            logger.info('[LoveBento] uncertain section=days point=%s reason=non_finite_score', point)
            return None
        ranked=sorted(zip(rows,results),key=lambda r:float(r[1].confidence),reverse=True)
        best,hit=ranked[0]
        score=float(hit.confidence)
        second=float(ranked[1][1].confidence) if len(ranked)>1 else 0.
        if section == 'days':
            reason = ('below_threshold' if not hit.found or score < self.cfg['days_threshold']
                      else 'insufficient_margin' if score-second < self.cfg['days_margin']
                      else 'accepted')
            logger.info(
                '[LoveBento] section=days point=%s method=TM_CCOEFF_NORMED '
                'candidates=%s threshold=%s margin=%s required_margin=%s result=%s',
                point, [{'days': row['value'], 'score': float(result.confidence)}
                        for row, result in ranked[:3]],
                self.cfg['days_threshold'], score-second, self.cfg['days_margin'], reason)
        if not math.isfinite(score) or not hit.found or score < self.cfg[key+'_threshold'] or score-second < self.cfg[key+'_margin']:
            logger.info('[LoveBento] uncertain section=%s point=%s score=%s margin=%s',section,point,score,score-second)
            return None
        return best

    def recognition_rois_visible(self, frame, point):
        """Require the actual field ROIs, not the decorative card rectangle."""
        rx, ry, _, _ = self.cfg['capture_roi']
        vx, vy, vw, vh = self.cfg['viewport']
        x, y = point
        margin = self.cfg['roi_margin']
        for key in ('food_crop_xyxy', 'role_crop_xyxy', 'days_crop_xyxy'):
            x1, y1, x2, y2 = self.catalog['geometry'][key]
            left, top = x+x1-margin, y+y1-margin
            right, bottom = x+x2+margin, y+y2+margin
            if (left < 0 or top < 0 or right > frame.shape[1] or bottom > frame.shape[0]
                    or left+rx < vx or top+ry < vy
                    or right+rx > vx+vw or bottom+ry > vy+vh):
                return False
        return True

    def read_frame(self, frame):
        multi=self.reader.vision.find_all_templates(
            source_image=frame,template_image=self.cfg['anchor_template'],mask_image=self.cfg['anchor_mask'],
            threshold=self.cfg['anchor_threshold'],use_grayscale=True,match_method=cv2.TM_SQDIFF_NORMED,
            nms_threshold=0.3,preprocess='none')
        if getattr(multi,'debug_info',{}).get('error'):
            raise _error('love-bento card detection failed')
        values=[]
        anchors=[]
        for hit in sorted(multi.matches or [],key=lambda h:(h.top_left[1],h.top_left[0])):
            x,y=map(int,hit.top_left)
            if not self.recognition_rois_visible(frame, (x, y)):
                continue
            anchors.append((x,y))
            food=self.classify(frame,(x,y),'items')
            role=self.classify(frame,(x,y),'roles')
            day=self.classify(frame,(x,y),'days')
            if food is None or role is None or day is None:
                return None
            if food['id'] not in role['food_ids']:
                logger.warning('[LoveBento] incompatible role=%s food=%s',role['id'],food['id'])
                return None
            values.append({'role_id':role['id'],'role_name':role['name'],
                           'food_id':food['id'],'food_name':food['name'],'remaining_days':day['value']})
        # A visible expiry marker without a card match must not silently become
        # an empty result. Cards with clipped field ROIs are left for the next page.
        markers=self.reader.vision.find_all_templates(
            source_image=frame,template_image=self.cfg['expiration_template'],
            mask_image=self.cfg['expiration_mask'],threshold=self.cfg['anchor_threshold'],
            use_grayscale=True,match_method=cv2.TM_SQDIFF_NORMED,nms_threshold=0.3,preprocess='none')
        if getattr(markers,'debug_info',{}).get('error'):
            raise _error('love-bento expiry marker detection failed')
        dx,dy,_,_=self.catalog['geometry']['days_crop_xyxy']
        for marker in markers.matches or []:
            mx,my=marker.top_left
            x,y=int(mx)-dx,int(my)-dy
            if self.recognition_rois_visible(frame, (x, y)):
                if not any(abs(x-ax)<=8 and abs(y-ay)<=8 for ax,ay in anchors):
                    return None
        return sorted(values,key=lambda row:(row['role_id'],row['food_id'],row['remaining_days']))

    def read_page(self):
        until=min(self.deadline,time.monotonic()+self.cfg['page_timeout_sec'])
        previous=None
        while time.monotonic()<until:
            frame=self.stable_frame()
            values=self.read_frame(frame)
            if values is not None and previous==values:
                return frame,values
            previous=values
        raise _error('love-bento card remained unrecognized; old snapshot retained')

    def read(self):
        found={}
        frame,rows=self.read_page()
        stationary=0
        for drag_index in range(self.cfg['max_drags']+1):
            self.guard()
            for row in rows:
                found[(row['role_id'],row['food_id'],row['remaining_days'])]=row
            if stationary>=self.cfg['stationary_confirmations']:
                return {'count':len(found),'items':list(found.values()),
                        'updated_at':datetime.now(timezone.utc).isoformat()}
            if drag_index==self.cfg['max_drags']:
                break
            vx,vy,vw,vh=self.cfg['viewport']
            x=round(vx+vw*0.5)
            start=round(vy+vh*0.8)
            self.guard()
            result=self.reader.app.drag(x,start,x,start-self.cfg['scroll_distance'],duration=0.6,hold_before_release_sec=0.2)
            if getattr(result,'success',True) is False:
                raise _error('love-bento scroll input failed')
            next_frame,rows=self.read_page()
            stationary=stationary+1 if self.unchanged(frame,next_frame) else 0
            frame=next_frame
            logger.info('[LoveBento] page=%s unique=%s stationary=%s',drag_index+1,len(found),stationary)
        raise _error('love-bento scan exceeded maximum drags')
