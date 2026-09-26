"""Build pixel-preserving follow-up template review assets; no runtime registration."""
from pathlib import Path
import json
import shutil
import html

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT/'docs/developer-guide/images/resonance-pc-deep-dive-simple'
OUT = DOC/'followup-template-review'
OLD = ROOT/'plans/resonance_pc/templates/consciousness_deep_dive_single_run'


def client(path):
    im=Image.open(path).convert('RGB')
    if im.size==(1332,802):return im.crop((25,56,1305,776))
    if im.size==(1280,720):return im
    raise ValueError((str(path),im.size))


def heading_mask_review(sources):
    ref=np.array(Image.open(OUT/'event_heading.png').convert('RGB'))
    # Only neutral, bright stroke interiors. Exclude red light and antialiased
    # edges where the animated background can bleed into the glyphs.
    mask=((ref.min(axis=2)>=190)&(np.ptp(ref.astype(np.int16),axis=2)<=24)).astype(np.uint8)*255
    mask=cv2.erode(mask,np.ones((2,2),np.uint8))
    if np.count_nonzero(mask)<100:raise ValueError('Insufficient title stroke pixels')
    Image.fromarray(mask).save(OUT/'event_heading_mask.png')
    rgba=np.dstack([ref,mask]);Image.fromarray(rgba).save(OUT/'event_heading_transparent.png')
    # Alpha is only a preview; callers MUST supply the mask to the matcher.
    checks=[]
    sample_images={**sources,
        'old_options':client(DOC/'15-healing-stone-three-options.png'),
        'old_ending':client(DOC/'20-healing-stone-event-ending.png'),
        'workshop_options':client(DOC/'21-purple-workshop-options-disabled.png')}
    for name,image in sample_images.items():
        roi=cv2.cvtColor(np.array(image)[15:90,525:775],cv2.COLOR_RGB2GRAY)
        gray=cv2.cvtColor(ref,cv2.COLOR_RGB2GRAY)
        response=cv2.matchTemplate(roi,gray,cv2.TM_SQDIFF_NORMED,mask=mask)
        finite=np.isfinite(response)
        score=None if not finite.any() else float(np.min(response[finite]))
        checks.append({'source':name,'masked_normalized_error':score})
    # Explicitly verify that excluded pixels cannot affect the metric.
    changed=ref.copy();rng=np.random.default_rng(37)
    changed[mask==0]=rng.integers(0,256,size=(np.count_nonzero(mask==0),3),dtype=np.uint8)
    gray=cv2.cvtColor(ref,cv2.COLOR_RGB2GRAY)
    changed_gray=cv2.cvtColor(changed,cv2.COLOR_RGB2GRAY)
    error=float(cv2.matchTemplate(changed_gray,gray,cv2.TM_SQDIFF_NORMED,mask=mask)[0,0])
    report={'matcher':'TM_SQDIFF_NORMED; lower is better; non-finite is unknown',
            'retained_pixels':int(np.count_nonzero(mask)), 'total_pixels':mask.size,
            'randomized_excluded_background_error':error,'checks':checks,
            'scope':'Offline still images only. Auxiliary event-page feature, not ending classification.'}
    (OUT/'heading_mask_validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    sheet=Image.new('RGB',(920,160),(28,31,37));draw=ImageDraw.Draw(sheet)
    font=ImageFont.truetype(str(ROOT/'.pytest_tmp/event_templates/assets/SourceHanSansCN-Medium.ttf'),16)
    for i,(label,image) in enumerate([('原始裁剪',Image.fromarray(ref)),('白色区域参与匹配',Image.fromarray(mask).convert('RGB')),('只保留文字内部',Image.fromarray(rgba))]):
        x=15+i*305;draw.text((x,12),label,font=font,fill='white')
        large=image.resize((image.width*1,image.height*1))
        sheet.paste(large,(x,49),large if large.mode=='RGBA' else None)
    draw.text((15,115),'红色环、字间空隙及文字抗锯齿边缘均不参与匹配。',font=font,fill='#bac4d4')
    sheet.save(OUT/'heading_mask_sheet.png')


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    sources={}
    for name,uid in [('dice_unselected','07354c88-6bec-4f71-9556-8b18d6890be0'),
                     ('event_selected','9cb7e168-74fc-4c63-9632-ae8cd56aa28e'),
                     ('dice_selected','6916c71b-a67a-4e00-aec6-f6f577ea16d4'),
                     ('dice_lost','46f0384b-c17e-4ab6-948a-0428c7702a59'),
                     ('event_ending','6080cdc9-1287-4dac-8315-3272430eee05'),
                     ('board_negative','b06980e6-5fe1-49f3-b610-78d909c994ee')]:
        source=OUT/(name+'_source.png')
        if not source.exists():
            client(Path('C:/Users/356/AppData/Local/Temp')/f'codex-clipboard-{uid}.png').save(source)
        sources[name]=Image.open(source).convert('RGB')
    sources['push_unselected']=client(DOC/'17-push-discard-selection.png')
    sources['reward']=client(DOC/'36-battle-reward-obtained.png')
    rows=[]
    definitions=[
        ('selection_dice_title','失去骰子：选择页标题','dice_unselected',(560,23,725,52),'selection'),
        ('selection_push_title','失去推力：选择页标题','push_unselected',(585,23,694,52),'selection'),
        ('result_lost_title','失去：结果展示标题','dice_lost',(612,83,666,111),'result'),
        ('event_heading','奇遇事件：公共标题，非结束依据','event_ending',(560,25,725,67),'ending'),
        ('board_actions_heading','魔方：行动步骤标题（无次数）','board_negative',(1043,340,1247,369),'return'),
    ]
    for name,label,source,box,group in definitions:
        sources[source].crop(box).save(OUT/(name+'.png'))
        rows.append({'name':name,'label':label,'source':source,'box':list(box),'group':group})
    heading_mask_review(sources)
    for row in rows:
        if row['name']=='event_heading':
            row.update(mask='event_heading_mask.png',matcher='TM_SQDIFF_NORMED',requires_mask=True)
    for name,old,label,group in [
        ('selection_reward_title','battle_reward_title','请选择推力：领取选择页标题','selection'),
        ('card_unselected','battle_card_unselected','卡片未选中：公共勾选标记','selection'),
        ('card_selected','battle_card_selected','卡片已选中：公共勾选标记','selection'),
        ('selection_confirm','battle_reward_confirm','道具选择页：独立确定按钮','selection'),
        ('result_obtained_title','reward_obtained_title','恭喜获得：结果展示标题','result'),
        ('board_turn','player_turn','回到魔方：玩家行动中','return'),
        ('run_settlement','settlement_failure','任务结算：探索中断','return'),
    ]:
        shutil.copyfile(OLD/(old+'.png'),OUT/(name+'.png'))
        rows.append({'name':name,'label':label,'source':str((OLD/(old+'.png')).relative_to(ROOT)), 'group':group})
    Image.open(OLD/'reward_exit_prompt.png').crop((50,6,155,21)).save(OUT/'result_exit.png')
    rows.append({'name':'result_exit','label':'触碰空白区域退出：仅紧裁文字','source':'reward_exit_prompt','box':[50,6,155,21],'group':'result'})
    # Retire only the generated review files removed from this manifest.
    for name in ['selection_zero','selection_one','ending_stone_name','board_move_done','board_rotate_pending']:
        (OUT/(name+'.png')).unlink(missing_ok=True)
    # Verify reusable controls against NEW dice screenshots, separate from their
    # original battle screenshot sources. Do not equate this with live testing.
    checks=[]
    for sample in ['dice_unselected','dice_selected','dice_lost','board_negative']:
        im=cv2.cvtColor(np.array(sources[sample]),cv2.COLOR_RGB2GRAY)
        names=['card_unselected','card_selected','selection_confirm'] if sample in ('dice_unselected','dice_selected') else ['result_lost_title','result_exit'] if sample=='dice_lost' else ['card_unselected','card_selected','selection_confirm','result_exit','board_turn','board_actions_heading']
        for name in names:
            ref=cv2.imread(str(OUT/(name+'.png')),0)
            region=(100,100,290,200) if name=='board_turn' else (1010,320,1270,390) if name=='board_actions_heading' else (80,470,1190,575) if name.startswith('card_') else (570,660,780,710) if name in ('selection_zero','selection_one') else (500,600,800,670) if name=='selection_confirm' else (500,60,790,125) if name=='result_lost_title' else (500,650,790,719)
            x,y,x2,y2=region
            _,score,_,point=cv2.minMaxLoc(cv2.matchTemplate(im[y:y2,x:x2],ref,cv2.TM_CCOEFF_NORMED))
            checks.append({'source':sample,'template':name,'score':float(score),'point':[x+point[0],y+point[1]]})
    fontpath=ROOT/'.pytest_tmp/event_templates/assets/SourceHanSansCN-Medium.ttf'
    font=ImageFont.truetype(str(fontpath),18)
    small=ImageFont.truetype(str(fontpath),15)
    for group,title in [('selection','道具选择：领取／丢弃共用'),('result','结果展示：获得／失去'),('ending','事件结束叙述：组合判断'),('return','返回魔方与任务结算')]:
        items=[r for r in rows if r['group']==group]
        sheet=Image.new('RGB',(920,65+140*((len(items)+1)//2)),(28,31,37));draw=ImageDraw.Draw(sheet)
        draw.text((18,15),title,font=font,fill='white')
        for i,r in enumerate(items):
            x=18+(i%2)*450;y=63+(i//2)*140
            draw.text((x,y),r['label'],font=small,fill='#e0e5ee')
            im=Image.open(OUT/(r['name']+'.png')).convert('RGB')
            if r['name']=='event_heading':
                im=Image.open(OUT/'event_heading_transparent.png')
                sheet.paste(im,(x,y+29),im)
            else:sheet.paste(im,(x,y+29))
            draw.text((x,y+102),f'{im.width} × {im.height} px',font=small,fill='#9da8b9')
        sheet.save(OUT/(group+'_sheet.png'))
    for source in ['dice_unselected','dice_selected','dice_lost','event_ending','board_negative']:
        ann=sources[source].copy();draw=ImageDraw.Draw(ann)
        for r in rows:
            if r['source']==source:draw.rectangle(r['box'],outline='#ffe600',width=2)
        for r in checks:
            if r['source']==source and r['score']>.85:
                im=Image.open(OUT/(r['template']+'.png'));x,y=r['point']
                draw.rectangle((x,y,x+im.width,y+im.height),outline='#ffe600',width=2)
        if source=='event_ending':
            draw.rectangle((700,155,1250,585),outline='#ff9630',width=2)
            draw.text((705,135),'检查选项是否仍存在（非模板）',font=small,fill='#ff9630')
            draw.ellipse((1115,624,1133,642),outline='#65e7ee',width=2)
            draw.text((963,650),'候选空白点击点，待实机验证',font=small,fill='#65e7ee')
        ann.save(OUT/(source+'_annotated.png'))
    (OUT/'manifest.json').write_text(json.dumps({'status':'review_only','client_size':[1280,720], 'templates':rows,'offline_checks':checks},ensure_ascii=False,indent=2),encoding='utf8')
    page='<!doctype html><meta charset="utf-8"><title>事件后续流程模板审阅</title><style>body{background:#181b20;color:#eee;font:17px Arial;margin:24px}img{max-width:100%;display:block;margin:16px 0}p{line-height:1.7;color:#bdc6d2}</style><h1>事件后续流程：模板审阅</h1><p>原始截图裁剪／复用既有控件模板，无 OCR，无生成重绘。黄色框为模板位置；橙框为结束页选项检查区，不是需要全图匹配的模板。尚未接入点击流程。不再识别八音石名称或选择数量；选中检查只确认目标卡片的勾选状态。返回魔方联合检查玩家行动中与行动步骤标题，不使用移动／旋转旁的0/1数字。</p>'
    for group in ['selection','result','ending','return']:page+=f'<img src="{group}_sheet.png">'
    page+='<h2>奇遇事件标题：文字笔画蒙版</h2><img src="heading_mask_sheet.png"><p>必须使用 event_heading_mask.png 作为匹配掩码；透明预览不能代替 matcher 的 mask 参数。红色环和文字边缘不参与匹配。该标题仅辅助确认事件页面，不单独判断结束。</p>'
    page+='<h2>原图范围</h2>'
    for source in ['dice_unselected','dice_selected','dice_lost','event_ending','board_negative']:page+=f'<img src="{source}_annotated.png">'
    page+='<p>强化结果标题、其他事件结束锚点、血量／货币短暂提示目前缺实际截图，不以虚构模板代替。结束判断须结合已提交事件的流程状态、选项消失、无选择／展示弹窗以及连续稳定画面；名称不参与识别，不能只凭标题或单帧选项缺失点空白。卡片位置通过公共勾选标记定位，不识别道具图案。不读取选择数量，当前按单选道具流程设计。</p>'
    (OUT/'index.html').write_text(page,encoding='utf8')
    print(json.dumps({'templates':len(rows),'checks':checks},ensure_ascii=False))


if __name__=='__main__':main()
