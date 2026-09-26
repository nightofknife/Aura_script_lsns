import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
import cv2
import pytest
import yaml
from packages.aura_core.context.execution import ExecutionContext
from packages.aura_core.engine.node_executor import NodeExecutor
from packages.aura_core.engine.execution_engine import ExecutionEngine
from packages.aura_core.config.template import TemplateRenderer
from plans.resonance_pc.src.actions import consciousness_deep_dive_loop_pc_actions as loop


class Store:
    _initialized=True
    def __init__(self):self.data={}
    async def get(self,k):return copy.deepcopy(self.data.get(k))
    async def set(self,k,v):self.data[k]=copy.deepcopy(v)
    async def delete(self,k):self.data.pop(k,None)
    async def get_all_data(self):return copy.deepcopy(self.data)


def child(stage,ok=True):
    p={'success':ok,'status':'completed' if ok else 'blocked','reason':'sample blocked',
       'page_state':'deep_dive_board' if stage=='entry' else 'deep_dive_activity_home',
       'terminal':'settlement','outcome':'failure','last_frame':'saved.png','turns_completed':6}
    return {'nodes':{'enter_stage' if stage=='entry' else 'finish':{'output':p}}}


def seed(count):
    return {'schema':loop.SCHEMA,'session_key':'test','cid':'test','sequence':0,'loop_count':count,
            'completed_runs':0,'run_index':0,'stage':'ready','status':'running','reason':'',
            'last_frame':'','last_result':{}}


@pytest.mark.parametrize('value',[0,-2,1.5,True,'2',None])
def test_invalid_count(value):
    with pytest.raises(ValueError):loop.validate_count(value)


@pytest.mark.parametrize('count',[1,2,-1])
def test_cycles_count_after_cleanup_only(count):
    async def run():
        store=Store();await store.set('test',seed(count))
        for i in range(2 if count==-1 else count):
            await loop.begin_loop_run('test',state_store=store)
            for stage in ['entry','single']:
                await loop.checkpoint_loop('test',stage,child(stage),state_store=store)
                assert (await store.get('test'))['completed_runs']==i
            await loop.checkpoint_loop('test','cleanup',child('cleanup'),state_store=store)
            assert (await store.get('test'))['completed_runs']==i+1
        result=await loop.loop_result('test',state_store=store)
        assert result['status']==('running' if count==-1 else 'completed')
    asyncio.run(run())


@pytest.mark.parametrize('failed_stage',['entry','single','cleanup'])
def test_business_blocked_stops_without_count(failed_stage):
    async def run():
        store=Store();await store.set('test',seed(-1));await loop.begin_loop_run('test',state_store=store)
        for stage in ['entry','single','cleanup']:
            r=await loop.checkpoint_loop('test',stage,child(stage,stage!=failed_stage),state_store=store)
            if stage==failed_stage:
                assert not r['ok'];break
        s=await store.get('test');assert s['status']=='blocked' and s['completed_runs']==0
        assert s['last_frame']=='saved.png'
        assert not (await loop.begin_loop_run('test',state_store=store))['ok']
    asyncio.run(run())


def test_schema_completion_not_framework_success():
    assert loop.child_output('single',{'status':'SUCCESS'})=={}
    assert not loop.successful('single',{})
    assert not loop.successful('cleanup',{'success':True,'status':'completed','page_state':'deep_dive_board'})


def test_home_precheck_never_clicks_and_rejects_board(monkeypatch,tmp_path):
    root=Path(__file__).resolve().parents[2]
    monkeypatch.setattr(loop,'resolve_base_path',lambda:tmp_path)
    class App:
        def __init__(self,name):
            im=cv2.imread(str(root/'docs/developer-guide/images/resonance-pc-deep-dive-simple'/name))
            if im.shape[:2]==(802,1332):im=im[56:776,25:1305]
            self.image=cv2.cvtColor(im,cv2.COLOR_BGR2RGB)
        def capture(self):return SimpleNamespace(success=True,image=self.image)
    async def run():
        for name,status in [('49-activity-home.png','running'),('01-board.png','blocked')]:
            store=Store();r=await loop.initialize_loop(1,app=App(name),state_store=store)
            assert r['status']==status
    asyncio.run(run())


def test_infinite_engine_loop_can_exceed_1000_and_retains_one():
    async def run():
        store=Store();await store.set('running',True)
        executor=NodeExecutor(SimpleNamespace(state_store=store))
        calls=[]
        async def action(node,context):
            i=context.data['loop']['index'];calls.append(i)
            if i==1004:await store.set('running',False)
            return {'index':i}
        executor.execute_single_action=action
        result=await executor.execute_loop('cycles',{},ExecutionContext(),
            {'while':'{{ state.running }}','max_iterations':-1,'retain_last':1})
        assert len(calls)==1005 and result==[{'index':1004}]
    asyncio.run(run())


def test_infinite_loop_responds_to_async_cancellation():
    async def run():
        executor=NodeExecutor(SimpleNamespace(state_store=Store()));ready=asyncio.Event()
        async def action(*args):ready.set();return None
        executor.execute_single_action=action
        task=asyncio.create_task(executor.execute_loop('x',{},ExecutionContext(),
            {'while':True,'max_iterations':-1,'retain_last':0}))
        await ready.wait();task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
    asyncio.run(run())


def test_finite_engine_loop_default_retains_all():
    async def run():
        executor=NodeExecutor(SimpleNamespace(state_store=Store()))
        async def action(node,ctx):return ctx.data['loop']['index']
        executor.execute_single_action=action
        result=await executor.execute_loop('x',{},ExecutionContext(),{'while':True,'max_iterations':3})
        assert result==[0,1,2]
    asyncio.run(run())


def test_yaml_stages_and_termination_contract():
    root=Path(__file__).resolve().parents[2]
    tasks=yaml.safe_load((root/'plans/resonance_pc/tasks/consciousness_deep_dive_loop_pc.yaml').read_text(encoding='utf8'))
    outer=tasks['consciousness_deep_dive_loop_pc']['steps']
    assert outer['cycles']['loop']['max_iterations']==-1
    assert outer['cycles']['loop']['retain_last']==1 and outer['cycles']['timeout']==-1
    stages=tasks['deep_dive_cycle_pc']['steps']
    for stage in ['entry','single','cleanup']:
        assert stages[stage]['action']=='aura.run_task'
        assert "status == 'running'" in stages[stage]['when']
        assert stages['check_'+stage]['params']['child_result']=='{{ nodes.'+stage+'.output }}'


@pytest.mark.parametrize('failed_stage',[None,'entry','single','cleanup'])
def test_real_cycle_dag_skips_later_children_on_business_failure(failed_stage):
    root=Path(__file__).resolve().parents[2]
    task=yaml.safe_load((root/'plans/resonance_pc/tasks/consciousness_deep_dive_loop_pc.yaml').read_text(encoding='utf8'))['deep_dive_cycle_pc']
    async def run():
        store=Store();await store.set('test',seed(1));calls=[]
        engine=ExecutionEngine(SimpleNamespace(services={'state_store':store},debug_mode=False),None)
        async def execute(node,ctx):
            params=await TemplateRenderer(ctx,store).render(node.get('params',{}))
            name=node['action']
            if name=='aura.run_task':
                ref=params['task_ref']
                stage='cleanup' if 'cleanup' in ref else 'single' if 'single_run' in ref else 'entry'
                calls.append(stage)
                return child(stage,stage!=failed_stage)
            if name.endswith('loop_begin'):return await loop.begin_loop_run(**params,state_store=store)
            if name.endswith('loop_checkpoint'):return await loop.checkpoint_loop(**params,state_store=store)
            return await loop.loop_result(**params,state_store=store)
        engine.node_executor.execute_single_action=execute
        context=await engine.run(task,'cycle',ExecutionContext(inputs={'session_key':'test'}))
        output=context.data['nodes']['finish']['output']
        assert output['completed_runs']==(1 if failed_stage is None else 0)
        expected=['entry','single','cleanup']
        if failed_stage:expected=expected[:expected.index(failed_stage)+1]
        assert calls==expected
    asyncio.run(run())


def test_full_outer_dag_runs_exactly_two_complete_cycles():
    root=Path(__file__).resolve().parents[2]
    tasks=yaml.safe_load((root/'plans/resonance_pc/tasks/consciousness_deep_dive_loop_pc.yaml').read_text(encoding='utf8'))
    async def run():
        store=Store();calls=[]
        async def run_task(key,inputs):
            engine=ExecutionEngine(SimpleNamespace(services={'state_store':store},debug_mode=False),None)
            async def execute(node,ctx):
                params=await TemplateRenderer(ctx,store).render(node.get('params',{}))
                name=node['action']
                if name=='resonance_pc.require_client_resolution':return True
                if name.endswith('loop_initialize'):
                    await store.set('test',seed(params['loop_count']))
                    return {'session_key':'test','status':'running'}
                if name=='aura.run_task':
                    ref=params['task_ref']
                    if ref.endswith(':deep_dive_cycle_pc'):
                        return (await run_task('deep_dive_cycle_pc',params['inputs'])).data
                    stage='cleanup' if 'cleanup' in ref else 'single' if 'single_run' in ref else 'entry'
                    calls.append(stage);return child(stage)
                if name.endswith('loop_begin'):return await loop.begin_loop_run(**params,state_store=store)
                if name.endswith('loop_checkpoint'):return await loop.checkpoint_loop(**params,state_store=store)
                return await loop.loop_result(**params,state_store=store)
            engine.node_executor.execute_single_action=execute
            return await engine.run(tasks[key],key,ExecutionContext(inputs=inputs))
        context=await run_task('consciousness_deep_dive_loop_pc',{'loop_count':2})
        result=context.data['nodes']['finish']['output']
        assert result['success'] and result['completed_runs']==2
        assert calls==['entry','single','cleanup']*2
        assert len(context.data['nodes']['cycles']['output'])==1
        assert await store.get('test') is None
    asyncio.run(run())
