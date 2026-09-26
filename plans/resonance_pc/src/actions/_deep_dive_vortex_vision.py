"""Independent vortex vision, forked from healing; no OCR or game input."""
from functools import lru_cache
from pathlib import Path
import json
import math

import cv2
import numpy as np

PLAN = Path(__file__).resolve().parents[2]
ROOT = PLAN / 'templates/deep_dive_vortex'


@lru_cache(None)
def template(name, color=False):
    im = cv2.imread(str(ROOT / (name + '.png')), cv2.IMREAD_COLOR if color else cv2.IMREAD_GRAYSCALE)
    if im is None:
        raise FileNotFoundError(name)
    return im


@lru_cache(None)
def catalog():
    return json.loads((PLAN / 'data/deep_dive_vortex/catalog.json').read_text(encoding='utf8'))


def match(gray, name, box):
    x,y,x2,y2 = box
    ref = template(name)
    r = cv2.matchTemplate(gray[y:y2,x:x2], ref, cv2.TM_CCOEFF_NORMED)
    if not np.isfinite(r).all():
        return 0., None
    _, score, _, (px,py) = cv2.minMaxLoc(r)
    return float(score), [x+px+ref.shape[1]//2,y+py+ref.shape[0]//2]


def heading_present(gray):
    r = cv2.matchTemplate(gray[15:90,525:775], template('event_heading'),
                         cv2.TM_SQDIFF_NORMED, mask=template('event_heading_mask'))
    if not np.isfinite(r).all():
        return None
    return float(r.min()) < .04


def ending_emblem_present(gray):
    """Shared Deco_Title emblem moved with Group_Left by finish animation."""
    response=cv2.matchTemplate(gray[475:560,315:420],template('ending_shared_emblem'),
                              cv2.TM_SQDIFF_NORMED,mask=template('ending_shared_emblem_mask'))
    if not np.isfinite(response).all():
        return None
    return float(response.min()) < .02


def text_score(roi, name):
    ref = template(name, True)
    if roi.shape[0] < ref.shape[0] or roi.shape[1] < ref.shape[1]:
        return None
    r = cv2.matchTemplate(roi, ref, cv2.TM_CCOEFF_NORMED)
    if not np.isfinite(r).all():
        return None
    _, ncc, _, (x,y) = cv2.minMaxLoc(r)
    patch = roi[y:y+ref.shape[0],x:x+ref.shape[1]]
    mae = np.abs(patch.astype(float)-ref.astype(float)).mean()
    means = [a.mean(axis=(0,1)) for a in (patch,ref)]
    chroma = [(m-m.mean())/max(m.mean(),1.) for m in means]
    similarity = max(0.,1.-float(np.abs(chroma[0]-chroma[1]).sum()))
    return .65*float(ncc)+.20*max(0.,1.-float(mae)/64.)+.15*similarity, [x+ref.shape[1]//2,y+12]


def event_options(bgr):
    """Compare full configured option sets, grouping numeric variants by behavior."""
    data = catalog()
    by_id = {o['id']:o for o in data['options']}
    variants = []
    cache = {}
    for event in data['events']:
        count = len(event['options'])
        starts = {2:[262,412],3:[187,337,487],4:[112,262,412,562]}.get(count)
        if starts is None:
            continue
        rows = []
        for index,oid in enumerate(event['options']):
            o = by_id[oid]; y = starts[index]
            scores = []
            for state in ('normal','disabled','selected'):
                key = (count,index,o['text_key'],state)
                if key not in cache:
                    cache[key] = text_score(bgr[y:y+89,710:1110],o['text_key']+'_'+state)
                measured = cache[key]
                if measured is not None:
                    value,point = measured
                    scores.append((value,state,[point[0]+710,point[1]+y]))
            if not scores:
                break
            scores.sort(reverse=True)
            value,state,point = scores[0]
            rows.append({**o,'state':state,'point':point,'score':value,
                         'state_margin':value-scores[1][0]})
        if len(rows)==count and min(r['score'] for r in rows)>=.76:
            variants.append({'rows':rows,'score':sum(r['score'] for r in rows)/count})
    if not variants:
        return []
    # Variants differing only by percentages have the same signature and do not compete.
    grouped = {}
    for v in variants:
        signature = tuple((r['group'],r['blocked'],r['supported'],r['priority'],r['state']) for r in v['rows'])
        if signature not in grouped or v['score']>grouped[signature]['score']:
            grouped[signature]=v
    ranked=sorted(grouped.values(),key=lambda v:-v['score'])
    if len(ranked)>1 and ranked[0]['score']-ranked[1]['score']<.012:
        return []
    rows=ranked[0]['rows']
    if any(r['state_margin']<.025 for r in rows):
        return []
    return rows


def cards(gray):
    found=[]
    for name,selected in [('card_unselected',False),('card_selected',True)]:
        ref=template(name)
        response=cv2.matchTemplate(gray[470:575,90:1190],ref,cv2.TM_CCOEFF_NORMED)
        if not np.isfinite(response).all():
            return []
        for _ in range(5):
            _,score,_,(x,y)=cv2.minMaxLoc(response)
            if score<.85:break
            found.append({'point':[x+90+ref.shape[1]//2,y+470+ref.shape[0]//2-180],
                          'selected':selected,'score':score})
            response[max(0,y-40):y+41,max(0,x-90):x+91]=-1
    unique=[]
    for row in sorted(found,key=lambda r:-r['score']):
        if all(abs(row['point'][0]-o['point'][0])>100 for o in unique):unique.append(row)
    return sorted(unique,key=lambda r:r['point'][0])


def detect_event_page(rgb):
    bgr=cv2.cvtColor(rgb,cv2.COLOR_RGB2BGR);gray=cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY)
    confirm,point=match(gray,'selection_confirm',(500,600,810,675))
    if confirm>=.85:
        candidates=cards(gray)
        title=max(match(gray,n,(520,8,800,85))[0] for n in
                  ('selection_dice_title','selection_push_title','selection_reward_title'))
        if candidates and title>=.85:
            return {'scene':'event_card_selection','cards':candidates,'click':point}
    exit_score,_=match(gray,'result_exit',(500,610,790,719))
    item_exit,_=match(gray,'result_exit_item',(500,600,790,675))
    item_title,_=match(gray,'result_items_title',(510,120,790,215))
    if exit_score>=.9 or item_exit>=.9 and item_title>=.85:
        # Exit text shared by obtained/lost/intensified results, but never a selection page.
        if confirm<.75:
            return {'scene':'event_result','click':[280,630]}
    heading=heading_present(gray)
    if heading is None:
        return {'scene':'uncertain_event','valid':False}
    if heading:
        options=event_options(bgr)
        if options:return {'scene':'event_options','event_options':options}
        # NPC and portal events use the same title decoration, in a different
        # position after finish1. No character/name/narrative template required.
        emblem=ending_emblem_present(gray)
        if emblem is None:
            return {'scene':'uncertain_event','valid':False}
        if emblem:
            return {'scene':'event_ending','click':[1124,633]}
        # Positive coarse centered-portal evidence, not just missing text templates.
        hsv=cv2.cvtColor(bgr,cv2.COLOR_BGR2HSV)
        red=((hsv[:,:,0]<12)|(hsv[:,:,0]>170))&(hsv[:,:,1]>150)&(hsv[:,:,2]>90)
        centered=float(red[100:470,850:1040].mean())
        left=float(red[100:470,70:230].mean())
        if centered>.12 and left<.08:
            return {'scene':'event_ending','click':[1124,633],
                    'layout_evidence':[centered,left]}
        return {'scene':'event_options_unknown'}
    return None


def board_visible(rgb):
    gray=cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY)
    return (match(gray,'board_turn',(100,100,290,200))[0]>=.85 and
            match(gray,'board_actions_heading',(1010,320,1270,390))[0]>=.85)
