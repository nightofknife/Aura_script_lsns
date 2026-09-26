"""Offline screenshot validation for the review-only event templates. No game input."""
import argparse
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


def score_template(roi, template):
    if any(a < b for a, b in zip(roi.shape[:2], template.shape[:2])):
        return None
    response = cv2.matchTemplate(roi, template, cv2.TM_CCOEFF_NORMED)
    _, ncc, _, point = cv2.minMaxLoc(response)
    x, y = point
    patch = roi[y:y+template.shape[0], x:x+template.shape[1]]
    mae = float(np.abs(patch.astype(float)-template.astype(float)).mean())
    # Joint RGB template comparison: retain luminance/chroma information that
    # normalized correlation alone discards. These weights are review candidates.
    means = [image.mean(axis=(0,1)) for image in (patch,template)]
    chroma = [(m-m.mean())/max(m.mean(),1.) for m in means]
    chroma_similarity = max(0.,1.-float(np.abs(chroma[0]-chroma[1]).sum()))
    score = .65*ncc + .20*max(0.,1.-mae/64.) + .15*chroma_similarity
    return {'score':score, 'ncc':ncc, 'mae':mae,
            'chroma_similarity':chroma_similarity, 'point':point}


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--templates',type=Path,required=True)
    ap.add_argument('--samples',type=Path,required=True)
    ap.add_argument('--font',type=Path,required=True)
    args=ap.parse_args()
    root=args.templates
    catalog=json.loads((root/'catalog.json').read_text(encoding='utf8'))
    samples=json.loads(args.samples.read_text(encoding='utf8'))
    options={o['id']:o for o in catalog['options']}
    refs=[(t,cv2.imread(str(root/t['file']))) for t in catalog['templates']]
    result=[]
    font=ImageFont.truetype(str(args.font),16)
    sheet=Image.new('RGB',(1010,len(samples)*130+55),(28,31,37))
    draw=ImageDraw.Draw(sheet)
    draw.text((15,10),'实际截图紧裁                         游戏资源合成模板                    匹配结果（离线）',font=font,fill='white')
    annotated={}
    for i,s in enumerate(samples):
        im=cv2.imread(s['file']);x,y,x2,y2=s['roi'];roi=im[y:y2,x:x2]
        expected=options[s['option_id']]['text_key']+'_'+s['state']+'.png'
        ranked=[]
        for t,a in refs:
            row=score_template(roi,a)
            if row:ranked.append({**row,'file':t['file'],'state':t['state'],'text_key':t['text_key']})
        ranked.sort(key=lambda r:r['score'],reverse=True)
        correct=next(r for r in ranked if r['file']==expected)
        t=next(a for meta,a in refs if meta['file']==expected)
        px,py=correct['point'];crop=roi[py:py+t.shape[0],px:px+t.shape[1]]
        name=f'sample_{i:02d}'
        cv2.imwrite(str(root/(name+'_actual.png')),crop)
        sheet.paste(Image.fromarray(cv2.cvtColor(crop,cv2.COLOR_BGR2RGB)),(15,65+i*130))
        sheet.paste(Image.fromarray(cv2.cvtColor(t,cv2.COLOR_BGR2RGB)),(345,65+i*130))
        draw.text((15,42+i*130),s['label'],font=font,fill='#e2e5ee')
        draw.text((660,65+i*130),f"{'正确' if ranked[0]['file']==expected else '错误'}  {ranked[0]['score']:.3f}",font=font,fill='white')
        draw.text((660,90+i*130),f"与第二名差 {ranked[0]['score']-ranked[1]['score']:.3f}",font=font,fill='#e2b855')
        ann=annotated.setdefault(s['file'],im.copy())
        cv2.rectangle(ann,(x+px,y+py),(x+px+t.shape[1],y+py+t.shape[0]),(0,255,255),1)
        variants=[]
        for gain in [.85,1.15]:
            changed=np.clip(roi.astype(float)*gain,0,255).astype(np.uint8)
            candidates=[]
            for meta,a in refs:
                r=score_template(changed,a)
                if r:candidates.append((r['score'],meta['file']))
            candidates.sort(reverse=True)
            variants.append({'gain':gain,'top':candidates[0][1],'correct':candidates[0][1]==expected})
        result.append({'label':s['label'],'expected':expected,'correct':ranked[0]['file']==expected,
                       'top':ranked[:5],'brightness_perturbations':variants,
                       'actual_crop':[x+px,y+py,t.shape[1],t.shape[0]]})
    sheet.save(root/'actual_vs_generated.png')
    for i,(name,im) in enumerate(annotated.items()):cv2.imwrite(str(root/f'annotated_{i:02d}.png'),im)
    report={'scope':'Provided still screenshots only; not live or temporal validation',
            'thresholds_calibrated':False,'samples':result}
    (root/'validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({'correct':sum(r['correct'] for r in result),'total':len(result),
                      'brightness_correct':sum(v['correct'] for r in result for v in r['brightness_perturbations'])}))


if __name__=='__main__':main()
