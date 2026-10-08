"""Owned live four-view research broker. Commands never replay old screenshots."""
from pathlib import Path
import asyncio,json,sys,time,os
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import cv2
import numpy as np
from packages.aura_game import EmbeddedGameRunner
from plans.resonance_pc.src.actions import consciousness_deep_dive_scan_pc_actions as scan
from plans.resonance_pc.src.actions._deep_dive_planned_run_vision import observe


def write(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=lambda v:v.tolist() if isinstance(v,np.ndarray) else str(v)),'utf8')


async def broker(app,folder,**unused):
    folder.mkdir(parents=True,exist_ok=True);commands=folder/'commands';commands.mkdir(exist_ok=True)
    token=scan._SCAN_CONTROL.set(dict(observe_fn=observe,latest_frame_metadata={}))
    adapter=app.target_runtime._session;last=None;frame_id=0;history=[];moving=False
    async def capture(label):
        nonlocal last,frame_id
        packet=None;until=time.monotonic()+2.
        while packet is None and time.monotonic()<until:
            packet,cancelled=await scan._await_serial(asyncio.to_thread(adapter.capture_stream_frame,
                -1 if last is None else last['generation'],expected_session_id=None if last is None else last['session_id']))
            if cancelled:raise asyncio.CancelledError()
            if packet is None:await asyncio.sleep(.01)
        if packet is None:raise RuntimeError('no_fresh_atomic_frame')
        image=packet['capture'];last=dict(generation_source='atomic_wgc',capture_backend=image.backend,
            generation=packet['generation'],session_id=packet['session_id'],frame_time=packet['arrived_at_monotonic'],
            frame_id=frame_id,map_revision=0)
        if not image.success or image.image is None or image.backend!='wgc' or time.monotonic()-last['frame_time']>.5:
            raise RuntimeError('invalid_current_wgc')
        scene=observe(image.image)
        path=f'frame_{frame_id:04d}_{label}.png'
        cv2.imwrite(str(folder/path),cv2.cvtColor(image.image,cv2.COLOR_RGB2BGR))
        row=dict(source=last.copy(),path=path,label=label,scene={k:scene.get(k) for k in (
            'scene','player_turn','plane_index','move_pending','move_done','rotate_pending','rotate_done','rotation_mode_evidence')})
        history.append(row);frame_id+=1;write(folder/'frames.json',history)
        if not scan._player_board(scene):raise RuntimeError('ordinary_player_board_required:'+str(scene.get('scene')))
        return row
    try:
        first=await capture('ready');write(folder/'ready.json',dict(ready=True,frame=first))
        next_command=1
        deadline=time.monotonic()+2400
        while time.monotonic()<deadline:
            scan._cancel_check();path=commands/f'{next_command:04d}.json'
            if not path.exists():await asyncio.sleep(.08);continue
            # The external producer publishes with replace(), but tolerate an
            # old producer's file-create/write race without abandoning capture.
            try:command=json.loads(path.read_text('utf8'))
            except (json.JSONDecodeError,UnicodeDecodeError,PermissionError):
                await asyncio.sleep(.05);continue
            op=command['op'];result=dict(command=command)
            try:
                if op=='close':
                    write(commands/f'{next_command:04d}_result.json',dict(status='closed'));break
                if op=='capture':
                    await asyncio.sleep(float(command.get('wait',.2)))
                    result['frame']=await capture(command.get('label','capture'))
                elif op=='reset':
                    reset_dir=folder/f'reset_{next_command:04d}';reset_dir.mkdir(exist_ok=True)
                    await scan._await_serial(app.controller.mouse_up_async('left'))
                    result['reset']=await scan._reset_view(app,reset_dir,time.monotonic()+20.)
                    await asyncio.sleep(.4);result['frame']=await capture(command.get('label','reset'))
                elif op=='drag':
                    dy=int(command['dy']);duration=float(command.get('duration',.5))
                    if dy==0 or abs(dy)>240 or not .1<=duration<=1.:raise ValueError('bounded_vertical_drag_required')
                    result['before']=await capture('before_drag')
                    start=(640,480);end=(640,480+dy)
                    await scan._await_serial(app.controller.mouse_up_async('left'))
                    await scan._await_serial(app.move_to_async(*start,duration=0.))
                    await asyncio.sleep(.08)
                    try:
                        await scan._await_serial(app.controller.mouse_down_async('left'));moving=True
                        await asyncio.sleep(.08)
                        for fraction in (.25,.5,.75,1.):
                            point=(640,480+round(dy*fraction))
                            await scan._await_serial(app.move_to_async(*point,duration=duration/4))
                        result['executed']=dict(start=list(start),end=list(end),dx=0,dy=dy,
                            duration=duration,coordinate_system='client_1280x720',horizontal_recenter_while_down=False)
                    finally:
                        await scan._await_serial(app.controller.mouse_up_async('left'));moving=False
                    await asyncio.sleep(.35)
                    result['frame']=await capture(command.get('label','after_drag'))
                else:raise ValueError('invalid_op')
                result['status']='success'
            except Exception as exc:
                result.update(status='failed',error=f'{type(exc).__name__}:{exc}')
                await scan._await_serial(app.controller.mouse_up_async('left'));moving=False
            write(commands/f'{next_command:04d}_result.json',result)
            print('BROKER_COMMAND',next_command,result['status'],flush=True);next_command+=1
    finally:
        await scan._await_serial(app.controller.mouse_up_async('left'));moving=False
        scan._SCAN_CONTROL.reset(token)
        backend=adapter.capture_backend;native=getattr(backend,'_session',None)
        await scan._await_serial(asyncio.to_thread(adapter.close))
        write(folder/'cleanup.json',dict(mouse_released=True,capture_closed=True,
            health=native.health() if native is not None else None,
            backend_session_released=getattr(backend,'_session',None) is None))
    return dict(research_only=True,frames=frame_id,targets_ready=False,formal_success=False)


def main():
    folder=(ROOT/Path(sys.argv[1])).resolve()
    if not folder.is_relative_to(ROOT/'.pytest_tmp'):raise ValueError('repository_test_scope_required')
    folder.mkdir(parents=True,exist_ok=True)
    cv2.setNumThreads(1)
    original=scan.run_layout_scan
    async def configured(app,**kwargs):return await broker(app,folder,**kwargs)
    scan.run_layout_scan=configured;runner=EmbeddedGameRunner(profile='embedded_full');cid=None
    try:
        record=runner.run_task(game_name='resonance_pc',task_ref='tasks:consciousness_deep_dive_scan_pc.yaml:consciousness_deep_dive_scan_pc',inputs={'time_budget_sec':90},wait=False)
        cid=str(record['cid'])
        for _ in range(5000):
            current=runner.get_run(cid)
            if str(current.get('status','')).lower() in {'success','failed','error','cancelled','stopped'} and not current.get('execution_pending'):
                write(folder/'dispatch.json',current);break
            time.sleep(.5)
        else:runner.cancel_task(cid);runner.wait_for_run(cid,timeout_sec=20)
    finally:
        if cid and runner.get_run(cid).get('execution_pending'):
            runner.cancel_task(cid);runner.wait_for_run(cid,timeout_sec=20)
        runner.close();scan.run_layout_scan=original
        print('OWNED_RUNTIME_CLOSED',flush=True)

if __name__=='__main__':main()
