import asyncio
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions._deep_dive_cleanup_vision import observe
from plans.resonance_pc.src.actions import consciousness_deep_dive_cleanup_pc_actions as cleanup

IMAGES=Path(__file__).resolve().parents[2]/'docs/developer-guide/images/resonance-pc-deep-dive-simple'


def frame(name,brightness=1):
    im=cv2.imread(str(IMAGES/name));assert im is not None
    if im.shape[:2]==(802,1332):im=im[56:776,25:1305]
    return cv2.cvtColor(cv2.convertScaleAbs(im,alpha=brightness),cv2.COLOR_BGR2RGB)


@pytest.mark.parametrize('brightness',[.8,1.,1.2])
@pytest.mark.parametrize('name,scene',[
    ('47-settlement-summary.png','settlement_summary'),
    ('48-settlement-details.png','settlement_details'),
    ('49-activity-home.png','activity_home'),
])
def test_pages(name,scene,brightness):
    assert observe(frame(name,brightness))['scene']==scene


@pytest.mark.parametrize('name',[
    '01-board.png','32-battle-formation.png','33-battle-victory.png',
    '34-battle-reward-choices.png','36-battle-reward-obtained.png',
    '20-healing-stone-event-ending.png','23-shop-exit-only.png',
])
def test_no_click_on_other_confirm_buttons(name):
    o=observe(frame(name));assert o['scene']=='unknown' and 'click' not in o


def state():
    return {'status':'running','progress_at':0,'stable':0,'trace':[],
            'page_state':'unknown','reason':''}


def stable(s,o,t):
    cleanup.step(s,o,t)
    return cleanup.step(s,o,t+.4)


def test_complete_chain_waits_and_never_clicks_home():
    s=state();a=observe(frame('47-settlement-summary.png'));b=observe(frame('48-settlement-details.png'))
    home=observe(frame('49-activity-home.png'))
    assert stable(s,a,1)==('click',[1000,640])
    assert cleanup.step(s,a,1.5)==('wait',None)
    assert stable(s,b,3)==('click',b['click'])
    assert stable(s,home,5)==('completed',None)
    assert s['page_state']=='deep_dive_activity_home'
    assert cleanup.step(s,home,6)==('finished',None)


def test_resume_at_details_or_home():
    s=state();details=observe(frame('48-settlement-details.png'))
    assert stable(s,details,1)[0]=='click'
    s=state();assert stable(s,observe(frame('49-activity-home.png')),1)==('completed',None)


def test_retries_only_on_same_page_and_bounded():
    s=state();page=observe(frame('47-settlement-summary.png'))
    assert stable(s,page,1)[0]=='click'
    for t in [3,5]:assert stable(s,page,t)[0]=='retry'
    assert stable(s,page,7)==('blocked',None)


def test_invalid_frame_resets_stable_checks_and_unknown_times_out():
    s=state();home=observe(frame('49-activity-home.png'))
    assert cleanup.step(s,home,1)==('wait',None)
    assert cleanup.step(s,{'valid':False,'scene':'capture_failed'},2)==('wait',None)
    assert cleanup.step(s,home,3)==('wait',None)
    assert cleanup.step(s,{'valid':True,'scene':'unknown'},31)==('blocked',None)


def test_title_alone_cannot_click_summary_or_confirm():
    im=frame('47-settlement-summary.png');im[600:720]=0
    assert observe(im)['scene']=='unknown'


def test_async_task_returns_home_without_calling_entry(monkeypatch,tmp_path):
    clock=[0.];monkeypatch.setattr(cleanup.time,'time',lambda:clock[0])
    monkeypatch.setattr(cleanup,'resolve_base_path',lambda:tmp_path)
    async def no_sleep(*_):pass
    monkeypatch.setattr(cleanup.asyncio,'sleep',no_sleep)
    names=['47-settlement-summary.png']*2+['48-settlement-details.png']*2+['49-activity-home.png']*2
    images=[frame(n) for n in names];clicks=[]
    class Store:
        def __init__(self):self.data={}
        async def set(self,k,v):self.data[k]=v
        async def get(self,k):return self.data.get(k)
    class App:
        def capture(self):return SimpleNamespace(success=True,image=images.pop(0))
    monkeypatch.setattr(cleanup,'aura_click',lambda **kw:clicks.append((kw['x'],kw['y'])))
    async def run():
        store=Store();start=await cleanup.initialize_cleanup(state_store=store)
        for _ in range(6):
            clock[0]+=1.1
            await cleanup.advance_cleanup(start['session_key'],app=App(),state_store=store)
        return await cleanup.finish_cleanup(start['session_key'],state_store=store)
    result=asyncio.run(run())
    assert result['success'] and result['page_state']=='deep_dive_activity_home'
    assert clicks==[(1000,640),(642,645)]
    assert Path(result['last_frame']).is_file()
