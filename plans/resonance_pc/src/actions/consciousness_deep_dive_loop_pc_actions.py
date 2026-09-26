"""Loop bookkeeping/checkpoints only; child task orchestration lives in YAML."""
import asyncio
from uuid import uuid4
import cv2
from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.events import Event
from packages.aura_core.observability.logging.core_logger import current_cid, logger
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested
from packages.aura_core.scheduler.utils import resolve_base_path
from packages.aura_core.utils.exceptions import StopTaskException
from ._deep_dive_cleanup_vision import observe

SCHEMA='resonance_pc.deep_dive_loop.v1'
PROGRESS='task.resonance_pc_deep_dive_loop_progress'


def cancel_check():
    if is_current_task_cancel_requested():raise StopTaskException('识海深潜循环已取消',success=False)


def validate_count(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or int(value)!=value or value==0 or value < -1:
        raise ValueError('循环次数必须是正整数，或 -1（持续循环）')
    return int(value)


def child_output(stage,result):
    node='enter_stage' if stage=='entry' else 'finish'
    return ((result or {}).get('nodes',{}).get(node,{}) or {}).get('output',{})


def successful(stage,payload):
    if not isinstance(payload,dict) or payload.get('success') is not True or payload.get('status')!='completed':return False
    if stage=='entry':return payload.get('page_state')=='deep_dive_board'
    if stage=='single':return payload.get('terminal')=='settlement'
    return payload.get('page_state')=='deep_dive_activity_home'


async def save(state,store,bus):
    state['sequence']+=1
    await store.set(state['session_key'],dict(state))
    if bus:
        payload={k:state[k] for k in ('cid','sequence','loop_count','completed_runs','run_index','stage','status','reason','last_frame')}
        payload['schema']=SCHEMA
        try:await bus.publish(Event(name=PROGRESS,payload=payload))
        except Exception as exc:logger.warning('Deep Dive loop progress delivery failed: %s',exc)


async def load(key,store):
    s=await store.get(key)
    if not isinstance(s,dict) or s.get('schema')!=SCHEMA or s.get('session_key')!=key:raise RuntimeError('循环会话不存在')
    return s


@action_info(name='resonance_pc.deep_dive_loop_initialize',public=True,read_only=False,
             description='Validate loop count and activity-home start without clicking.')
@requires_services(app='plans/aura_base/app',state_store='core/state_store',event_bus='core/event_bus')
async def initialize_loop(loop_count:int=1,app=None,state_store=None,event_bus=None):
    count=validate_count(loop_count);cancel_check()
    key='deep_dive_loop:'+uuid4().hex
    s={'schema':SCHEMA,'session_key':key,'cid':str(current_cid() or ''),'sequence':0,
       'loop_count':count,'completed_runs':0,'run_index':0,'stage':'ready','status':'running',
       'reason':'','last_frame':'','last_result':{}}
    image=None
    for _ in range(2):
        cancel_check();capture=await asyncio.to_thread(app.capture)
        image=capture.image if getattr(capture,'success',False) else None
        if image is None or observe(image)['scene']!='activity_home':
            s.update(status='blocked',reason='完整循环必须从识海深潜活动首页启动')
            if image is not None:
                path=resolve_base_path()/'logs/deep_dive_loop'/key.split(':')[-1]/'start_blocked.png'
                path.parent.mkdir(parents=True,exist_ok=True)
                if cv2.imwrite(str(path),cv2.cvtColor(image,cv2.COLOR_RGB2BGR)):s['last_frame']=str(path)
            break
        await asyncio.sleep(.35)
    await save(s,state_store,event_bus)
    return {'session_key':key,'status':s['status']}


@action_info(name='resonance_pc.deep_dive_loop_begin',public=True,read_only=False,
             description='Start bookkeeping for one cycle with a fresh child session.')
@requires_services(state_store='core/state_store',event_bus='core/event_bus')
async def begin_loop_run(session_key:str,state_store=None,event_bus=None):
    cancel_check();s=await load(session_key,state_store)
    if s['status']!='running':return {'ok':False}
    s.update(run_index=s['completed_runs']+1,stage='entry',last_result={})
    await save(s,state_store,event_bus)
    return {'ok':True,'run_index':s['run_index']}


@action_info(name='resonance_pc.deep_dive_loop_checkpoint',public=True,read_only=False,
             description='Require child business completion before advancing; count only after cleanup.')
@requires_services(state_store='core/state_store',event_bus='core/event_bus')
async def checkpoint_loop(session_key:str,stage:str,child_result:dict,state_store=None,event_bus=None):
    cancel_check();s=await load(session_key,state_store)
    expected={'entry':'entry','single':'single','cleanup':'cleanup'}
    if stage not in expected or s['status']!='running' or s['stage']!=stage:raise RuntimeError('循环阶段顺序不符')
    result=child_output(stage,child_result)
    if not successful(stage,result):
        result=result if isinstance(result,dict) else {}
        s.update(status='blocked',reason=f"第{s['run_index']}局 {stage}："+str(result.get('reason') or '子任务未返回已确认的完成结果'),
                 last_frame=str(result.get('last_frame') or ''))
    else:
        if stage=='entry':s['stage']='single'
        elif stage=='single':
            s['stage']='cleanup'
            s['last_result']={k:result.get(k) for k in ('outcome','turns_completed','last_frame')}
        else:
            s['completed_runs']+=1;s['stage']='activity_home';s['last_frame']=result.get('last_frame','')
            if s['loop_count']!=-1 and s['completed_runs']>=s['loop_count']:s['status']='completed'
    if stage=='cleanup':
        child_key=child_result.get('nodes',{}).get('initialize',{}).get('output',{}).get('session_key','')
        if isinstance(child_key,str) and child_key.startswith('deep_dive_cleanup:'):await state_store.delete(child_key)
    await save(s,state_store,event_bus)
    return {'ok':s['status']!='blocked','status':s['status'],'completed_runs':s['completed_runs']}


@action_info(name='resonance_pc.deep_dive_loop_result',public=True,read_only=False,
             description='Return compact loop summary; optionally release outer session.')
@requires_services(state_store='core/state_store')
async def loop_result(session_key:str,release:bool=False,state_store=None):
    s=await load(session_key,state_store)
    out={k:s[k] for k in ('status','stage','loop_count','completed_runs','run_index','reason','last_frame','last_result')}
    out['success']=s['status']=='completed'
    if release:await state_store.delete(session_key)
    return out
