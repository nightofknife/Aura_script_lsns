"""Read-only classification of settlement summary/details and activity home."""
from functools import lru_cache
from pathlib import Path
import cv2
import numpy as np

ROOT=Path(__file__).resolve().parents[2]/'templates/deep_dive_cleanup'
REGIONS={'settlement_title':(480,45,810,160),'continue':(525,605,760,695),
         'confirm':(525,600,780,695),'activity_title':(480,35,825,195),
         'start_dive':(530,500,765,615)}


@lru_cache(None)
def template(name):
    im=cv2.imread(str(ROOT/(name+'.png')),0)
    if im is None:raise FileNotFoundError(name)
    return im


def observe(rgb):
    if not isinstance(rgb,np.ndarray) or rgb.shape!=(720,1280,3) or rgb.dtype!=np.uint8:
        return {'valid':False,'scene':'invalid_frame'}
    gray=cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY)
    if gray.std()<2:return {'valid':False,'scene':'blank_frame'}
    scores={};points={}
    for name,(x,y,x2,y2) in REGIONS.items():
        ref=template(name)
        r=cv2.matchTemplate(gray[y:y2,x:x2],ref,cv2.TM_CCOEFF_NORMED)
        if not np.isfinite(r).all():return {'valid':False,'scene':'uncertain_frame'}
        _,score,_,(px,py)=cv2.minMaxLoc(r)
        scores[name]=float(score);points[name]=[x+px+ref.shape[1]//2,y+py+ref.shape[0]//2]
    result={'valid':True,'scene':'unknown','scores':scores}
    if scores['activity_title']>=.85 and scores['start_dive']>=.86:
        return {**result,'scene':'activity_home'}
    if scores['settlement_title']>=.85:
        if scores['confirm']>=.87 and scores['continue']<.8:
            return {**result,'scene':'settlement_details','click':points['confirm']}
        if scores['continue']>=.87 and scores['confirm']<.8:
            return {**result,'scene':'settlement_summary','click':[1000,640]}
    return result
