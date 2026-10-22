"""Owned, cancellable four-view acquisition through the normal plan runtime."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime
import json
import math
from pathlib import Path
import time
from uuid import uuid4

import cv2
import numpy as np

from ._deep_dive_four_view_geometry import FourViewGeometry, orientation_feedback
from ._deep_dive_four_view_epoch_registration import EpochViewRegistration
from ._deep_dive_four_view_reader import read_view_nodes, source_key
from ._deep_dive_four_view_targets import associate_targets, VerticalTargetTracker
from ._deep_dive_four_view_layout import build_layout
from ._deep_dive_four_view_body_projection import project_sidepeek_candidates
from ._deep_dive_scan_budget import resolve_scan_budget
from ._deep_dive_layout_report import write_layout_report
from ._deep_dive_planner_rules import cell_to_slot
from ._deep_dive_four_view_diagnostics import FourViewDiagnostics, input_settings


def _write(path, value):
    temporary=path.with_suffix(path.suffix+'.part')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2,
        default=lambda x:x.tolist() if isinstance(x,np.ndarray) else str(x)),encoding='utf8')
    temporary.replace(path)


def bounded_feedback_step(error, previous=None, previous_dy=None, *, target_error=0.):
    """Local observations can refine a step, never turn it into a coarse jump."""
    residual=error-target_error
    dy=-20 if residual>0 else 20
    if previous is not None and previous_dy:
        slope=(error-previous)/previous_dy
        # Increasing client y must increase this local orientation observable.
        # Reject inverted/flat responses rather than amplifying noisy feedback.
        if slope>.002:
            dy=int(round(np.clip(-residual/slope,-20,20)))
            if abs(dy)<4:dy=4 if dy>=0 else -4
    return dy


async def run_four_view_scan(app, *, max_steps=96,time_budget_sec=None,output_dir=None,
                             recognition_goal='targets',expected_inspirations=None,entity_detector=None):
    # Imported only after the public entry installed its owned observer/context.
    from . import consciousness_deep_dive_scan_pc_actions as runtime
    started=time.monotonic();budget=resolve_scan_budget('four_views',time_budget_sec)
    deadline=started+budget;run_id=datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid4().hex[:8]
    output=Path(output_dir) if output_dir is not None else runtime.resolve_base_path()/'logs'/'deep_dive_scan'/run_id
    if output.exists() and any(output.iterdir()):raise ValueError('scan_output_directory_not_empty')
    output.mkdir(parents=True,exist_ok=True);(output/'frames').mkdir()
    if app is None or tuple(app.get_window_size() or ())!=(1280,720):
        raise ValueError('four_view_scan_requires_1280x720_client')
    if entity_detector is None:raise ValueError('four_view_scan_requires_entity_detector')
    if type(max_steps) is bool or not 1<=int(max_steps)<=300:raise ValueError('invalid_four_view_step_limit')
    geometry=FourViewGeometry();tracker=VerticalTargetTracker();epoch=uuid4().hex;references={};fits={};body_prior=None;provisional={}
    diagnostics=FourViewDiagnostics(output,run_id=run_id,scan_epoch=epoch,started=started,
        settings=dict(scan_route='four_views',recognition_goal=recognition_goal,
            expected_inspirations=expected_inspirations,max_steps=int(max_steps),time_budget_sec=budget,
            input_configuration=input_settings(app),
            command_duration_sec=.5,post_release_wait_sec=.35,standard_repeat_wait_sec=.35,
            supplementary_targets=[-1.2,-2.4],controller_unchanged=True))
    runtime.logger.info('[DeepDiveFourViews] run=%s diagnostics=%s',run_id,output/'control_trace.jsonl')
    active=dict(phase='initialize',view=None)
    fit_references={}
    views=[];groups=[];associations=[];frames=[];actions=[];trace=[]
    adapter=app.target_runtime._get_or_create_session();last=None;frame_id=0;last_rgb=None
    layout={};reset={};status='blocked';reason='four_view_scan_not_complete';last_scene={};unexpected_error=None
    async def owned(operation):
        value,cancelled=await runtime._await_serial(operation)
        if cancelled:raise asyncio.CancelledError()
        runtime._cancel_check();return value
    def check():
        runtime._cancel_check()
        if time.monotonic()>=deadline:raise TimeoutError('four_view_protection_budget_exhausted')
    async def detect(rgb):
        def invoke():
            if hasattr(entity_detector,'detect_packet'):value=entity_detector.detect_packet(rgb)
            elif callable(entity_detector):value=entity_detector(rgb)
            else:raise ValueError('invalid_entity_detector')
            return value if isinstance(value,dict) else dict(targets=value,coverage_valid=False)
        return await owned(asyncio.to_thread(invoke))
    async def capture(label):
        nonlocal last,frame_id,last_rgb,last_scene
        active.update(operation='capture',label=label)
        capture_started=time.monotonic();previous_source=deepcopy(last)
        check();until=min(deadline,time.monotonic()+2.)
        packet=None
        while packet is None and time.monotonic()<until:
            packet=await owned(asyncio.to_thread(adapter.capture_stream_frame,
                -1 if last is None else last['generation'],
                expected_session_id=None if last is None else last['session_id']))
            if packet is None:await asyncio.sleep(.01)
        if packet is None:
            diagnostics.event('capture_rejected',label=label,reason='four_view_fresh_frame_missing',
                              previous_source=previous_source,requested_at_monotonic=capture_started)
            raise RuntimeError('four_view_fresh_frame_missing')
        packet_received=time.monotonic()
        image=packet['capture'];source=dict(generation_source='atomic_wgc',capture_backend=image.backend,
            session_id=packet['session_id'],generation=packet['generation'],
            frame_time=packet['arrived_at_monotonic'],frame_id=frame_id,map_revision=0)
        source_key(source)
        if (not image.success or image.image is None or time.monotonic()-source['frame_time']>.5):
            diagnostics.event('capture_rejected',label=label,source=source,reason='four_view_stale_capture',
                              capture_success=image.success,capture_age_sec=time.monotonic()-source['frame_time'])
            raise RuntimeError('four_view_stale_capture')
        if last is not None and (source['generation']<=last['generation'] or source['frame_time']<=last['frame_time']):
            diagnostics.event('capture_rejected',label=label,source=source,previous_source=last,
                              reason='four_view_repeated_source')
            raise RuntimeError('four_view_repeated_source')
        last=source;last_rgb=image.image;frame_id+=1
        control=runtime._SCAN_CONTROL.get()
        if control is not None:control['latest_frame_metadata']=dict(source)
        scene=runtime._observe(image.image,source['frame_id']);last_scene=scene
        if not runtime._player_board(scene):
            diagnostics.event('capture_rejected',label=label,source=source,reason='scene_changed',scene=scene)
            raise runtime._SceneInterrupted('four_view_scene_changed:'+str(scene.get('scene')))
        detection_started=time.monotonic()
        detection=await detect(image.image)
        detection_finished=time.monotonic()
        targets=tracker.update(detection['targets'],source,vertical_verified=True)
        path=f"frames/{source['frame_id']:04d}_{label}.png"
        if not cv2.imwrite(str(output/path),cv2.cvtColor(image.image,cv2.COLOR_RGB2BGR)):raise OSError('four_view_frame_save_failed')
        frame=dict(path=path,label=label,source=dict(source),targets=targets,scene=deepcopy(scene),
            target_detection_complete=detection.get('coverage_valid') is True,
            detection_metadata={k:deepcopy(v) for k,v in detection.items() if k!='targets'})
        frames.append(frame);_write(output/'frames.json',frames)
        diagnostics.event('capture',label=label,source=source,frame_path=path,
            requested_at_monotonic=capture_started,packet_received_at_monotonic=packet_received,
            capture_age_sec=packet_received-source['frame_time'],
            previous_source=previous_source,source_interval_sec=(source['frame_time']-previous_source['frame_time']
                if previous_source is not None else None),detection_elapsed_sec=detection_finished-detection_started,
            processing_elapsed_sec=time.monotonic()-capture_started,target_count=len(targets),
            target_detection_complete=frame['target_detection_complete'])
        return frame,image.image
    async def locate(frame,rgb,view):
        active.update(operation='locate',view=view,source=dict(frame['source']))
        key=(view,frame['source']['frame_id'])
        if key in fits:
            diagnostics.event('alignment_cache_hit',view=view,source=frame['source'],
                cached_reference=fit_references.get(key),
                current_reference_stage='provisional' if view in provisional else ('formal' if view in references else 'structure'))
            return fits[key]
        locate_started=time.monotonic()
        reference_stage='provisional' if view in provisional else ('formal' if view in references else 'structure')
        reference_source=deepcopy(references[view].source) if view in references else None
        if view in references:
            alignment=await owned(asyncio.to_thread(references[view].locate,rgb,frame['source'],scan_epoch=epoch,
                exclusion_targets=frame['targets']))
            feedback=deepcopy(alignment['feedback'])
            if view in provisional and feedback.get('valid_diagnostic'):
                seed=provisional[view]
                feedback['relative_reference_error_deg']=feedback['signed_error_deg']
                feedback['signed_error_deg']+=seed['signed_error_deg']
                tolerance=seed['metrics']['arrival_tolerance_deg']
                off_axis=feedback['metrics']['off_axis_deg']+seed['metrics']['off_axis_deg']
                feedback['metrics']['off_axis_deg']=off_axis
                feedback['valid_diagnostic']=abs(off_axis)<=3.
                feedback.update(arrival_candidate=bool(alignment.get('proposal_accepted') and
                    abs(feedback['signed_error_deg'])<=tolerance and abs(off_axis)<=1.5),
                    orientation_reference='provisional_actual_epoch_rgb',
                    provisional_seed_source=deepcopy(seed['source']),
                    provisional_seed_structure_error_deg=seed['signed_error_deg'])
        else:
            alignment=await owned(asyncio.to_thread(geometry.locate,rgb,expected_view=view))
            feedback=orientation_feedback(alignment,expected_view=view)
        fits[key]=(alignment,feedback)
        fit_references[key]=dict(stage=reference_stage,source=reference_source)
        trace.append(dict(frame=frame['source']['frame_id'],view=view,source=frame['source'],feedback=feedback))
        _write(output/'feedback.json',trace)
        _write(output/f"geometry_view{view}_{frame['source']['frame_id']:04d}.json",alignment)
        diagnostics.feedback(view=view,source=frame['source'],frame_path=frame['path'],
            alignment=alignment,feedback=feedback,reference_stage=reference_stage,
            reference_source=reference_source,elapsed_sec=time.monotonic()-locate_started)
        runtime._progress(phase='four_view_alignment',view_index=view,view_count=4,
                          known_cells=len({cell_to_slot(n) for v in views for n in v['nodes'] if n['reading'].get('icon_id')}),elapsed_sec=time.monotonic()-started,
                          image_orientation_error_deg=feedback.get('signed_error_deg'))
        return alignment,feedback
    async def drag(dy,label):
        active.update(operation='drag',label=label)
        check()
        if len(actions)>=int(max_steps):raise RuntimeError('four_view_step_limit')
        dy=int(dy)
        if not dy or abs(dy)>80:raise ValueError('bounded_vertical_feedback_input_required')
        action_index=len(actions)
        command_started=time.monotonic();pressed_at=None;released_at=None
        diagnostics.event('drag_start',action_index=action_index,label=label,view=active['view'],
            dx=0,dy=dy,start=[640,480],end=[640,480+dy],duration_sec=.5,
            before_source=last,command_started_at_monotonic=command_started,
            input_semantics='mouse_commands_not_measured_rotation')
        await owned(app.controller.mouse_up_async('left'))
        await owned(app.move_to_async(640,480,duration=0.));await asyncio.sleep(.08)
        try:
            await owned(app.controller.mouse_down_async('left'));pressed_at=time.monotonic();await asyncio.sleep(.08)
            for fraction in (.25,.5,.75,1.):
                await owned(app.move_to_async(640,480+round(dy*fraction),duration=.125))
        finally:
            _,cancelled=await runtime._await_serial(app.controller.mouse_up_async('left'))
            released_at=time.monotonic()
            # Emit only after releasing the button; no diagnostic I/O during a drag.
            diagnostics.event('drag_release',action_index=action_index,label=label,
                pressed_at_monotonic=pressed_at,released_at_monotonic=released_at,
                held_sec=released_at-pressed_at if pressed_at is not None else None,
                cancelled=cancelled,input_configuration=input_settings(app))
            if cancelled:raise asyncio.CancelledError()
        actions.append(dict(dx=0,dy=dy,start=[640,480],end=[640,480+dy],before_source=deepcopy(last),
            duration=.5,horizontal_recenter_while_down=False,at=time.monotonic(),label=label))
        _write(output/'actions.json',actions);await asyncio.sleep(.35)
        result=await capture(label)
        actions[-1]['after_source']=deepcopy(result[0]['source']);_write(output/'actions.json',actions)
        diagnostics.event('drag_observed',action_index=action_index,label=label,dy=dy,
            before_source=actions[-1]['before_source'],after_source=result[0]['source'],frame_path=result[0]['path'],
            release_to_source_sec=result[0]['source']['frame_time']-released_at,
            total_elapsed_sec=time.monotonic()-command_started,settling_proven=False)
        return result
    async def arrive(view,frame,rgb,*,target_error=None,minimum_input=False):
        active.update(phase='arrive',view=view,target_error=target_error)
        arrival_started=time.monotonic()
        diagnostics.event('arrival_start',view=view,target_error=target_error,minimum_input=minimum_input,
                          source=frame['source'])
        previous=None;previous_dy=None;entered=False;moved=False
        for attempt in range(70):
            active['attempt']=attempt
            check();alignment,feedback=await locate(frame,rgb,view)
            error=feedback.get('signed_error_deg')
            valid=feedback.get('valid_diagnostic') and error is not None
            reached=(feedback.get('arrival_candidate') if target_error is None else
                     alignment.get('proposal_accepted') and valid and abs(error-target_error)<=.30)
            decision=dict(view=view,attempt=attempt,target_error=target_error,source=frame['source'],
                frame_path=frame['path'],error_deg=error,
                residual_deg=error-(target_error or 0.) if error is not None else None,
                off_axis_deg=(feedback.get('metrics') or {}).get('off_axis_deg'),
                arrival_tolerance_deg=(feedback.get('metrics') or {}).get('arrival_tolerance_deg'),
                target_tolerance_deg=.30 if target_error is not None else None,
                proposal_accepted=alignment.get('proposal_accepted'),valid_diagnostic=valid,
                arrival_candidate=feedback.get('arrival_candidate'),reached=reached,
                minimum_input=minimum_input,moved=moved,entered=entered,
                previous_error_deg=previous,previous_dy=previous_dy,
                alignment_reference=fit_references.get((view,frame['source']['frame_id'])))
            if reached and (not minimum_input or moved):
                diagnostics.event('arrival_decision',**decision,branch='return',chosen_dy=None,
                                  arrival_elapsed_sec=time.monotonic()-arrival_started)
                return frame,rgb,alignment,feedback
            dy=-80
            if valid:
                entered=True;residual=error-(target_error or 0.)
                dy=bounded_feedback_step(error,previous,previous_dy,target_error=target_error or 0.)
                slope=(error-previous)/previous_dy if previous is not None and previous_dy else None
                raw_step=-residual/slope if slope is not None and slope>.002 else None
                diagnostics.event('arrival_decision',**decision,branch='local_feedback',chosen_dy=dy,
                    slope_image_deg_per_command_px=slope,slope_used=slope is not None and slope>.002,
                    raw_secant_step_px=raw_step,
                    direction_opposes_residual=dy*residual<0)
                previous=float(error);previous_dy=dy
            elif entered:
                diagnostics.event('arrival_decision',**decision,branch='quiet_recheck',chosen_dy=None)
                # Drag effects may temporarily dominate structural lines. Hold
                # the camera and obtain new pixels; never steer on that fit.
                recovered=False
                for quiet in range(3):
                    await asyncio.sleep(.65)
                    fresh,pixels=await capture(f'view{view}_quiet_recheck_{quiet}')
                    _,fresh_feedback=await locate(fresh,pixels,view)
                    frame,rgb=fresh,pixels
                    diagnostics.event('quiet_recheck',view=view,attempt=attempt,quiet_index=quiet,
                                      source=fresh['source'],feedback=fresh_feedback)
                    if fresh_feedback.get('valid_diagnostic'):
                        recovered=True;break
                if not recovered:raise RuntimeError(f'four_view_{view}_local_feedback_lost')
                continue
            else:
                diagnostics.event('arrival_decision',**decision,branch='coarse_search',chosen_dy=dy)
            frame,rgb=await drag(dy,f'view{view}_align_{attempt}');moved=True
        raise RuntimeError(f'four_view_{view}_arrival_failed')
    async def record(frame,rgb,alignment,feedback,view,group,kind,parent=None):
        nonlocal body_prior
        active.update(phase='record',operation='record',view=view,group=group)
        record_started=time.monotonic()
        nodes=await owned(asyncio.to_thread(read_view_nodes,rgb,alignment['cells'],source=frame['source'],
            view_group=group,scan_epoch=epoch))
        for node in nodes:node.update(stable=True,kind=kind)
        rows=associate_targets(rgb,frame['targets'],alignment['cells'],source=frame['source'],view_group=group,scan_epoch=epoch)
        body=await owned(asyncio.to_thread(project_sidepeek_candidates,rgb,alignment,
            source=frame['source'],scan_epoch=epoch,distance_prior=body_prior))
        if body.get('body_pose_supported') and body.get('fit_evidence',{}).get('non_coplanar'):
            body_prior=body
        # Only unresolved side peeks receive fitted hidden-surface candidates.
        # Direct observed owners keep their actual visible pixel evidence.
        if body.get('body_pose_supported'):
            for index,row in enumerate(rows):
                if row['cell_index'] is None:
                    candidate=associate_targets(rgb,[row['target_packet']],
                        alignment['cells']+body['candidate_cells'],source=frame['source'],view_group=group,scan_epoch=epoch)[0]
                    candidate['body_candidate_fit_supported']=True
                    candidate['body_candidate_fit_source']=deepcopy(body['source'])
                    candidate['body_candidate_fit_evidence']=deepcopy(body['fit_evidence'])
                    rows[index]=candidate
        views.append(dict(view=view,kind=kind,group=group,source=frame['source'],nodes=nodes,cells=alignment['cells'],
            target_detection_complete=frame.get('target_detection_complete') is True,body_candidate_fit=body))
        associations.extend(rows)
        if not any(g['group']==group for g in groups):
            parent_record=next((g for g in groups if g['group']==parent),None)
            groups.append(dict(group=group,view_index=view,kind=kind,parent_group=parent,
                source=frame['source'],scan_epoch=epoch,actual_structure_supported=True,
                image_orientation_error_deg=feedback['signed_error_deg'],input_count=len(actions),
                vertical_input_evidence=deepcopy(actions[parent_record['input_count']:]) if parent_record else []))
        _write(output/'view_readings.json',views);_write(output/'target_associations.json',associations)
        _write(output/'view_groups.json',groups)
        diagnostics.event('view_recorded',view=view,group=group,kind=kind,source=frame['source'],
            frame_path=frame['path'],feedback=feedback,known_node_count=sum(n['reading'].get('icon_id') is not None for n in nodes),
            target_association_count=len(rows),processing_elapsed_sec=time.monotonic()-record_started)
    try:
        active.update(phase='reset',operation='reset')
        check();runtime._progress(phase='reset',view_count=4,view_index=0,known_cells=0,elapsed_sec=0.)
        reset=await runtime._reset_view(app,output,min(deadline,time.monotonic()+20.))
        await asyncio.sleep(.4);frame,rgb=await capture('reset')
        for view in range(1,5):
            diagnostics.event('view_start',view=view,previous_source=last,input_count=len(actions))
            # Three actual stopped frames at each ordered standard viewpoint.
            for retry in range(8):
                frame,rgb,alignment,feedback=await arrive(view,frame,rgb,minimum_input=view>1 and retry==0)
                if view not in references:
                    references[view]=EpochViewRegistration(rgb,alignment,epoch,frame['source'],
                        provisional=True,exclusion_targets=frame['targets'])
                    provisional[view]=dict(deepcopy(feedback),source=deepcopy(frame['source']))
                    diagnostics.event('reference_transition',view=view,stage='provisional',source=frame['source'],
                        seed_feedback=feedback,reference_path=frame['path'])
                witnesses=[(frame,rgb,alignment,feedback)]
                for repeat in range(2):
                    await asyncio.sleep(.35);fresh,pixels=await capture(f'view{view}_stable_{repeat}')
                    fit,fb=await locate(fresh,pixels,view)
                    frame,rgb=fresh,pixels
                    diagnostics.event('view_witness',view=view,retry=retry,repeat=repeat,source=fresh['source'],
                        frame_path=fresh['path'],feedback=fb,accepted=bool(fb.get('arrival_candidate')),
                        criterion='arrival_interval_only_not_measured_stop')
                    if not fb.get('arrival_candidate'):break
                    witnesses.append((fresh,pixels,fit,fb))
                if len(witnesses)==3:break
            else:raise RuntimeError(f'four_view_{view}_settling_failed')
            parent=view*100
            references[view]=EpochViewRegistration(witnesses[-1][1],witnesses[-1][2],epoch,
                witnesses[-1][0]['source'],stable_sources=[w[0]['source'] for w in witnesses],
                exclusion_targets=witnesses[-1][0]['targets'])
            diagnostics.event('reference_transition',view=view,stage='formal',
                previous_reference_source=provisional[view]['source'],
                previous_seed_feedback=provisional[view],reference_source=witnesses[-1][0]['source'],
                reference_path=witnesses[-1][0]['path'],new_reference_error_deg=0.,
                witnesses=[dict(source=w[0]['source'],frame_path=w[0]['path'],feedback=w[3]) for w in witnesses])
            provisional.pop(view,None)
            for witness in witnesses:
                primary_feedback=deepcopy(witness[3])
                primary_feedback.update(standard_structure_error_deg=primary_feedback['signed_error_deg'],
                    signed_error_deg=0.,orientation_reference='actual_epoch_standard_rgb')
                await record(witness[0],witness[1],witness[2],primary_feedback,view,parent,'standard')
            groups[-1]['verified_standard_sources']=[deepcopy(w[0]['source']) for w in witnesses]
            groups[-1]['standard_reference_status']='three_actual_sources_formal_reference'
            _write(output/'view_groups.json',groups)
            _write(output/f'view{view}_standard.json',dict(source=witnesses[-1][0],feedback=witnesses[-1][3]))
            base_error=0.
            await asyncio.sleep(.2);frame,rgb=await capture(f'view{view}_epoch_reference_probe')
            # Real nearby camera views provide ownership/negative evidence.
            # Their measured image-angle separation never claims physical 8°.
            for neighbour,offset in enumerate((-1.2,-2.4),1):
                frame,rgb,alignment,fb=await arrive(view,frame,rgb,target_error=base_error+offset)
                await asyncio.sleep(.35);frame,rgb=await capture(f'view{view}_neighbour{neighbour}')
                alignment,fb=await locate(frame,rgb,view)
                diagnostics.event('supplement_recheck',view=view,neighbour=neighbour,target_error=base_error+offset,
                    source=frame['source'],frame_path=frame['path'],feedback=fb,
                    proposal_accepted=alignment.get('proposal_accepted'),
                    target_residual_deg=fb['signed_error_deg']-(base_error+offset) if fb.get('signed_error_deg') is not None else None,
                    decision_criterion='proposal_error_present_and_distance_from_standard_at_least_0.5')
                if not alignment.get('proposal_accepted') or fb.get('signed_error_deg') is None:
                    raise RuntimeError(f'four_view_{view}_supplement_geometry_missing')
                if abs(fb['signed_error_deg']-base_error)<.5:
                    raise RuntimeError('supplement_camera_view_not_distinct')
                await record(frame,rgb,alignment,fb,view,parent+neighbour,'supplement',parent)
        # The next operation stage performs its own verified reset. Keep the
        # actually observed fourth-view supplement rather than steering back
        # to an unused zero pose and reacting to animated feature jitter.
        for final_retry in range(3):
            active.update(phase='final_registration',view=4,attempt=final_retry)
            await asyncio.sleep(.35);frame,rgb=await capture('final_registration')
            alignment,feedback=await locate(frame,rgb,4)
            if alignment.get('proposal_accepted') and feedback.get('valid_diagnostic'):break
        else:raise RuntimeError('four_view_final_registration_lost')
        layout=build_layout(views,associations,groups,expected_inspirations=expected_inspirations,
                            scan_epoch=epoch,last_source=frame['source'])
        layout.update(run_id=run_id,recognition_goal=recognition_goal)
        if recognition_goal=='full':
            layout['success']=bool(layout['targets_ready'] and layout['known_cells']==54)
            layout['layout_complete']=layout['success']
            layout['status']='completed' if layout['success'] else 'partial'
            if layout['targets_ready']:
                layout['reason']='completed' if layout['success'] else 'visible_node_patterns_incomplete'
        status=layout['status'];reason=layout['reason']
    except (asyncio.CancelledError,runtime.StopTaskException):
        status='cancelled';reason='cancel_requested'
        diagnostics.event('scan_cancelled',context=dict(active),last_source=last)
    except runtime._SceneInterrupted as exc:
        status='interrupted';reason=str(exc)
        diagnostics.failure(exc,context=dict(active),last_source=last)
    except (ValueError,RuntimeError,TimeoutError,OSError) as exc:
        status='blocked';reason=f'{type(exc).__name__}:{exc}'
        diagnostics.failure(exc,context=dict(active),last_source=last)
    except Exception as exc:
        unexpected_error=f'{type(exc).__name__}:{exc}'
        diagnostics.failure(exc,context=dict(active),last_source=last)
        raise
    finally:
        _,cancelled=await runtime._await_serial(app.controller.mouse_up_async('left'))
        if cancelled:status='cancelled';reason='cancel_requested'
        if status not in ('targets_ready','completed') and last_rgb is not None:
            try:
                saved=cv2.imwrite(str(output/'failure_frame.png'),cv2.cvtColor(last_rgb,cv2.COLOR_RGB2BGR))
                diagnostics.event('failure_frame',saved=bool(saved),path='failure_frame.png',source=last)
            except Exception as exc:
                diagnostics.event('failure_frame',saved=False,error=f'{type(exc).__name__}: {exc}',source=last)
        diagnostics.event('scan_cleanup',mouse_released=True,status=status,reason=reason,
                          context=dict(active),last_source=last)
        diagnostics.checkpoint(status='error' if unexpected_error else status,reason=unexpected_error or reason,
            phase='cleanup',steps=len(actions),frame_count=len(frames),last_source=last,
            context=dict(active))
        _write(output/'cleanup.json',dict(mouse_released=True,shared_runtime_closed=False,
            owned_operations_drained=True,reason='shared_app_and_detector_remain_owned_by_task'))
    if not layout and views and last is not None:
        layout=build_layout(views,associations,groups,expected_inspirations=expected_inspirations,
                            scan_epoch=epoch,last_source=last)
    if not layout:
        layout=dict(schema='resonance_pc.deep_dive_layout.v1',coordinate_frame='scan_local',
            scan_route='four_views',scan_epoch=epoch,success=False,status=status,reason=reason,
            targets_ready=False,layout_complete=False,cells=[],known_cells=0,faces_observed=0)
    if status in ('blocked','interrupted','cancelled'):
        layout.update(success=False,targets_ready=False,status=status,reason=reason)
    _write(output/'layout.json',layout)
    report=write_layout_report(layout,output)
    elapsed=time.monotonic()-started
    summary=dict(success=layout.get('success',False),status=status,reason=reason,
        scan_route='four_views',recognition_goal=recognition_goal,targets_ready=layout.get('targets_ready',False),
        layout_complete=layout.get('layout_complete',False),known_cells=layout.get('known_cells',0),
        faces_observed=layout.get('faces_observed',0),output_dir=str(output),run_id=run_id,
        total_elapsed_sec=round(elapsed,3),steps=len(actions),view_count=4,
        report_path=report.get('report_path'),json_path=str(output/'layout.json'),
        control_trace_path=str(output/'control_trace.jsonl'),diagnostics_path=str(output/'diagnostics.json'),
        last_frame=str(output/frames[-1]['path']) if frames else None)
    _write(output/'summary.json',summary)
    diagnostics.finish(status=status,reason=reason,steps=len(actions),frame_count=len(frames),
        view_count=len({g['view_index'] for g in groups}),last_source=last,
        readiness=layout.get('readiness'),total_elapsed_sec=round(elapsed,3))
    runtime.logger.info('[DeepDiveFourViews] status=%s reason=%s diagnostics=%s',status,reason,output)
    result=dict(success=summary['success'],status=status,reason=reason,layout=layout,
        summary=summary,last_frame=summary['last_frame'],reset=reset,latest_scene_observation=last_scene)
    control=runtime._SCAN_CONTROL.get() or {}
    if control.get('interruption'):result['interruption']=deepcopy(control['interruption'])
    runtime._progress(phase='completed' if summary['success'] else 'blocked',**summary)
    return result
