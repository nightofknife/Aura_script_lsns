"""Independent settlement-to-activity-home task. Never starts another dive."""
import asyncio
import copy
import time
from pathlib import Path
from uuid import uuid4

import cv2
from packages.aura_core.api import action_info, requires_services
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested
from packages.aura_core.scheduler.utils import resolve_base_path
from packages.aura_core.utils.exceptions import StopTaskException
from ....aura_base.src.actions.input_actions import click as aura_click
from ._deep_dive_cleanup_vision import observe

SCHEMA='resonance_pc.deep_dive_cleanup.v1'


def cancel_check():
    if is_current_task_cancel_requested():
        raise StopTaskException('识海深潜收尾已取消',success=False)


def step(state,o,now):
    if state['status']!='running':return 'finished',None
    if now-state['progress_at']>=30:
        state.update(status='blocked',reason='收尾页面30秒未得到可确认的进展')
        return 'blocked',None
    if not o.get('valid'):
        state['stable']=0;return 'wait',None
    scene=o['scene']
    if scene==state.get('last_scene'):state['stable']+=1
    else:state['last_scene']=scene;state['stable']=1
    if state['stable']<2:return 'wait',None
    if scene=='activity_home':
        state.update(status='completed',page_state='deep_dive_activity_home')
        state['trace'].append({'confirmed':'activity_home','at':now})
        return 'completed',None
    pending=state.get('pending')
    if pending:
        if now-pending['at']<1:return 'wait',None
        if scene==pending['scene']:
            if pending['clicks']>=3:
                state.update(status='blocked',reason='收尾点击3次后页面仍未切换：'+scene)
                return 'blocked',None
            pending.update(at=now,clicks=pending['clicks']+1)
            state['stable']=0
            state['trace'].append({'retry':scene,'at':now})
            return 'retry',list(o['click'])
        if pending['scene']=='settlement_summary' and scene=='settlement_details':
            state['pending']=None;state['progress_at']=now
            state['trace'].append({'confirmed':'settlement_details','at':now})
        else:return 'wait',None
    if scene in {'settlement_summary','settlement_details'}:
        state['pending']={'scene':scene,'at':now,'clicks':1}
        state['stable']=0;state['progress_at']=now
        state['trace'].append({'click':scene,'at':now})
        return 'click',list(o['click'])
    return 'wait',None


@action_info(name='resonance_pc.deep_dive_cleanup_initialize',public=True,read_only=False,
             description='Initialize independent settlement cleanup; no game input.')
@requires_services(state_store='core/state_store')
async def initialize_cleanup(state_store=None):
    cancel_check()
    key='deep_dive_cleanup:'+uuid4().hex
    state={'schema':SCHEMA,'session_key':key,'status':'running','progress_at':time.time(),
           'stable':0,'last_scene':'','page_state':'unknown','reason':'','trace':[], 'last_frame':''}
    await state_store.set(key,state)
    return {'session_key':key,'status':'running'}


@action_info(name='resonance_pc.deep_dive_cleanup_advance',public=True,read_only=False,
             description='Observe and perform at most one verified settlement cleanup click.')
@requires_services(app='plans/aura_base/app',state_store='core/state_store')
async def advance_cleanup(session_key:str,app=None,state_store=None):
    cancel_check()
    state=await state_store.get(session_key)
    if not isinstance(state,dict) or state.get('schema')!=SCHEMA or state.get('session_key')!=session_key:
        raise RuntimeError('收尾会话不存在')
    if state['status']!='running':return {'status':state['status']}
    capture=await asyncio.to_thread(app.capture)
    image=capture.image if getattr(capture,'success',False) else None
    o=observe(image) if image is not None else {'valid':False,'scene':'capture_failed'}
    action,point=step(state,o,time.time())
    if point is not None:
        try:
            cancel_check()
            await asyncio.to_thread(aura_click,app=app,x=int(point[0]),y=int(point[1]))
            cancel_check()
        except StopTaskException:raise
        except Exception as exc:state.update(status='blocked',reason='收尾点击失败：'+str(exc))
    if state['status'] in {'completed','blocked'} and image is not None:
        folder=resolve_base_path()/'logs/deep_dive_cleanup'/session_key.split(':')[-1]
        folder.mkdir(parents=True,exist_ok=True)
        path=folder/(state['status']+'.png')
        if not cv2.imwrite(str(path),cv2.cvtColor(image,cv2.COLOR_RGB2BGR)):
            raise RuntimeError('无法保存收尾截图')
        state['last_frame']=str(path)
    await state_store.set(session_key,copy.deepcopy(state))
    if state['status']=='running':await asyncio.sleep(.35)
    return {'status':state['status'],'scene':o['scene'],'action':action}


@action_info(name='resonance_pc.deep_dive_cleanup_finish',public=True,read_only=True,
             description='Return independent cleanup result for later task composition.')
@requires_services(state_store='core/state_store')
async def finish_cleanup(session_key:str,state_store=None):
    state=await state_store.get(session_key)
    if not isinstance(state,dict) or state.get('schema')!=SCHEMA or state.get('session_key')!=session_key:
        raise RuntimeError('收尾会话不存在')
    return {'success':state['status']=='completed','status':state['status'],
            'page_state':state['page_state'],'reason':state['reason'],
            'last_frame':state['last_frame'],'trace':state['trace']}
