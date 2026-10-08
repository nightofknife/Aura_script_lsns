"""Read missing atlas anchors without authorizing an operation or cube input."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import math
from numbers import Integral, Real
import time

import cv2
import numpy as np

from . import _deep_dive_operation_frame as frame
from ._deep_dive_layout_semantics import classify_icon
from ._deep_dive_layout_vision import LayoutScanner, _angle, _crop
from ._deep_dive_planner_rules import cell_to_slot
from ._deep_dive_target_readiness import targets_readiness


def _digest(rgb):
    return hashlib.sha256(rgb.tobytes()).hexdigest()


def _matches(cells, fit, actor):
    """Known labels identify Q; unknown labels only support pixel geometry."""
    results=[]
    for candidate in frame._q_candidates(actor,fit['actor']):
        matches=[];conflicts=[];counts=[0,0,0]
        for reading in fit['readings']:
            expected=frame._known_icon(cells[candidate['op_to_logical'][reading['operation_slot']]])
            if expected is None:continue
            if expected==reading['icon_id']:
                matches.append(reading);counts[reading['operation_slot']//9]+=1
            else:conflicts.append(reading)
        total=len(matches)+len(conflicts)
        consensus=sum(r['confidence'] for r in matches)/max(1e-9,sum(r['confidence'] for r in matches+conflicts))
        if (len(matches)>=7 and sum(n>=2 for n in counts)>=2 and len(conflicts)<=1 and consensus>=.88):
            results.append(dict(candidate,matched=len(matches),anchors=total,conflicts=len(conflicts),
                face_anchors=counts,consensus=consensus,
                score=consensus+min(total,18)*.012-fit['rmse']*.003))
    return results


def _ordinary_unknown(cell):
    if (cell.get('occupant') not in ('none','unknown') or cell.get('occupant_status')=='conflict'
            or cell.get('node_status') not in ('unknown',None) or cell.get('icon_id')):return False
    counts=cell.get('occupant_evidence_counts') or {}
    return not any(counts.get(kind,0) or any(e.get('occupant')==kind for e in cell.get('evidence',()))
                   for kind in ('player','singularity','inspiration'))


def propose_read_only_wide_reference(rgb,layout,*,target_detector=None,
                                     scan_epoch=0,map_revision=0,view_epoch=0,source_session=None):
    """Propose one Q using three-face pixels and at least two known faces.

    `reading_only` deliberately fails every existing action-reference gate.
    The caller must prove reset/unchanged gameplay and later run the original
    strict public wide builder after applying independently read node updates.
    """
    base=dict(status='waiting',reason='reading_reference_unconfirmed',input_authorized=False,
        operation_ready=False,mode='board',schema='resonance_pc.deep_dive_reading_reference.v1',
        scan_epoch=scan_epoch,map_revision=map_revision,view_epoch=view_epoch,source_session=source_session)
    deadline=time.monotonic()+3.
    try:
        if not isinstance(rgb,np.ndarray) or rgb.shape!=(720,1280,3) or rgb.dtype!=np.uint8:
            raise ValueError('reading_reference_invalid_rgb')
        if not targets_readiness(layout)['ready']:raise ValueError('reading_reference_targets_unproven')
        cells,actor=frame._layout(layout)
        observation=frame.observe(rgb)
        cyan=observation.get('cyan_pixels',{})
        if (not observation.get('player_turn') or observation.get('scene') not in ('board','unknown')
                or max(cyan.get('move',0),cyan.get('rotate',0))>=2000):
            raise ValueError('reading_reference_ordinary_board_required')
        targets=frame._current_targets(rgb,target_detector)
        scanner=LayoutScanner()
        if not scanner._bootstrap(rgb):raise ValueError('reading_reference_geometry_unconfirmed')
        features=frame._features(rgb,targets)
        solutions=[]
        for operation_actor in range(9):
            if next(frame._q_candidates(actor,operation_actor),None) is None:continue
            if time.monotonic()>=deadline:raise ValueError('reading_reference_budget_exhausted')
            fit=frame._refine(rgb,dict(actor=operation_actor,rvec=scanner.rvec.copy(),tvec=scanner.tvec.copy()),
                              frame._NORMAL_K,features)
            if fit is None:continue
            # _refine uses the original >=7 pixels/2 per face/RMSE6 gates.
            for candidate in _matches(cells,fit,actor):solutions.append((candidate,fit))
        if not solutions:raise ValueError('reading_reference_known_pattern_insufficient')
        solutions.sort(key=lambda row:-row[0]['score'])
        best,fit=solutions[0]
        survivors=[row for row in solutions if row[0]['score']>=best['score']-.08]
        if len({tuple(row[0]['op_to_logical']) for row in survivors})!=1:
            raise ValueError('reading_reference_orientation_ambiguous')
        if any(row[1]['actor']!=fit['actor'] or np.mean([np.linalg.norm(a['centre']-b['centre'])
                for a,b in zip(row[1]['surface'],fit['surface'])])>10 for row in survivors):
            raise ValueError('reading_reference_geometry_ambiguous')
        counts=best['face_anchors'];deficient=[i for i,n in enumerate(counts) if n<2]
        grid=[dict(row,centre=row['centre'].tolist(),quad=row['quad'].tolist(),
                   logical_slots=[best['op_to_logical'][row['operation_slot']]]) for row in fit['surface']]
        eligible=[best['op_to_logical'][row['operation_slot']] for row in grid
                  if row['operation_slot']//9 in deficient and _ordinary_unknown(cells[row['logical_slots'][0]])]
        return dict(base,status='reading_only',reason='unique_q_for_reading_only',actor_slot=actor,
            actor_operation_slot=fit['actor'],Q_candidates=[best],grid_cells=grid,
            layout_digest=frame._layout_digest(cells),source_rgb_digest=_digest(rgb),
            image_digest=_digest(rgb),known_face_counts=counts,deficient_faces=deficient,eligible_slots=eligible,
            pose=dict(K=frame._NORMAL_K.tolist(),rvec=fit['rvec'].ravel().tolist(),tvec=fit['tvec'].ravel().tolist()),
            geometry_quality=dict(anchors=len(fit['readings']),face_anchors=[sum(r['operation_slot']//9==f
                for r in fit['readings']) for f in range(3)],rmse_px=fit['rmse'],inliers=fit['inliers']),
            evidence=dict(known_matches=best['matched'],known_consensus=best['consensus'],known_conflicts=best['conflicts'],
                actual_readings=[dict(r,point=r['point'].tolist()) for r in fit['readings']],
                independent_known_faces=[f for f,n in enumerate(counts) if n>=2],requires_strict_public_rebuild=True))
    except Exception as error:
        return dict(base,reason=str(error) if isinstance(error,ValueError) else 'reading_reference_failed',
                    diagnostic_error=type(error).__name__)


def _validate_current_proposal(rgb,layout,proposal,targets):
    cells,actor=frame._layout(layout)
    if (proposal.get('status')!='reading_only' or proposal.get('input_authorized') is not False
            or proposal.get('schema')!='resonance_pc.deep_dive_reading_reference.v1'
            or not proposal.get('source_rgb_digest')
            or proposal.get('layout_digest')!=frame._layout_digest(cells)
            or len(proposal.get('Q_candidates',()))!=1):raise ValueError('anchor_read_proposal_stale_or_invalid')
    pose=proposal['pose'];K=np.asarray(pose['K'],float)
    if K.shape!=(3,3) or not np.array_equal(K,frame._NORMAL_K):raise ValueError('anchor_read_camera_invalid')
    rv=np.asarray(pose['rvec'],float).reshape(3,1);tv=np.asarray(pose['tvec'],float).reshape(3,1)
    if not np.isfinite(rv).all() or not np.isfinite(tv).all() or tv[2,0]<=0:raise ValueError('anchor_read_pose_invalid')
    features=frame._features(rgb,targets)
    if proposal['source_rgb_digest']!=_digest(rgb):
        # An old reading-only Q is a seed, not current-frame geometry evidence.
        # Refit the actual new pixels with the original three-face gates before
        # projecting any missing node. This avoids another full reset bootstrap.
        previous_surface=frame._surface(K,rv,tv)
        fit=frame._refine(rgb,dict(actor=proposal['actor_operation_slot'],rvec=rv.copy(),tvec=tv.copy()),K,features)
        if fit is None:raise ValueError('anchor_read_current_geometry_insufficient')
        rv,tv=fit['rvec'],fit['tvec'];surface=fit['surface'];readings=fit['readings']
        if _angle(cv2.Rodrigues(rv)[0],cv2.Rodrigues(np.asarray(pose['rvec'],float).reshape(3,1))[0])>1.:
            raise ValueError('anchor_read_current_pose_changed')
        if max(np.max(np.linalg.norm(a['quad']-b['quad'],axis=1))
                for a,b in zip(previous_surface,surface))>6:
            raise ValueError('anchor_read_current_pose_changed')
    else:
        surface=frame._surface(K,rv,tv)
        readings=frame._readings(rgb,surface,features,actor=proposal['actor_operation_slot'])
    counts=[sum(r['operation_slot']//9==f for r in readings) for f in range(3)]
    if len(readings)<7 or min(counts)<2:raise ValueError('anchor_read_current_geometry_insufficient')
    obj=np.asarray([frame._point(frame._cell(r['operation_slot'])) for r in readings])
    pixels=np.asarray([r['point'] for r in readings]);errors=np.linalg.norm(frame._project(obj,K,rv,tv)-pixels,axis=1)
    rmse=float(np.sqrt(np.mean(errors**2)))
    if rmse>6 or np.max(errors)>9:raise ValueError('anchor_read_current_residual_invalid')
    fit=dict(actor=proposal['actor_operation_slot'],readings=readings,rmse=rmse)
    candidates=_matches(cells,fit,actor)
    if not candidates:raise ValueError('anchor_read_current_known_pattern_missing')
    best=max(candidates,key=lambda q:q['score'])
    surviving=[q for q in candidates if q['score']>=best['score']-.08]
    if (len({tuple(q['op_to_logical']) for q in surviving})!=1
            or best['op_to_logical']!=proposal['Q_candidates'][0]['op_to_logical']):
        raise ValueError('anchor_read_current_q_ambiguous')
    return cells,best,surface,cv2.Rodrigues(rv)[0],tv


def read_missing_registration_anchors(rgb,layout,proposal,votes,*,source_id,source_time,
        source_session,source_generation,source_backend,target_detector=None,target_packet=None,now=None):
    """Collect three real fresh WGC captures, never promote a reference ready.

    Each source must be new, increasing, in the same session, no older than .5s
    after computation, and span >=.4s. Cached exact-current-RGB model coverage
    is usable only as a mask; this helper never emits positive entity votes.
    """
    base=dict(status='reading_only',ready=False,reason='anchor_read_needs_fresh_votes',cells=[],
              input_authorized=False,requires_strict_public_rebuild=True)
    deadline=time.monotonic()+3.
    try:
        if (not isinstance(rgb,np.ndarray) or rgb.shape!=(720,1280,3) or rgb.dtype!=np.uint8
                or not targets_readiness(layout)['ready']):raise ValueError('anchor_read_inventory_or_rgb_invalid')
        if (not isinstance(source_time,Real) or isinstance(source_time,bool) or not math.isfinite(source_time)
                or not isinstance(source_generation,Integral) or isinstance(source_generation,bool)
                or source_generation<0 or source_session is None
                or str(source_id)!=f'{source_session}:{source_generation}'
                or str(source_backend).lower() not in ('wgc','windows_graphics_capture','windowsgraphicscapture')):
            raise ValueError('anchor_read_fresh_wgc_source_required')
        if proposal.get('source_session') is not None and proposal['source_session']!=source_session:
            raise ValueError('anchor_read_source_session_changed')
        packet=target_detector(rgb) if target_packet is None and callable(target_detector) else target_packet
        if (not isinstance(packet,dict) or packet.get('coverage_valid') is not True
                or not(packet.get('model_executed') is True or packet.get('cached') is True)):
            raise ValueError('anchor_read_model_coverage_required')
        targets=frame._current_targets(rgb,lambda current:packet)
        cells,mapping,surface,rotation,translation=_validate_current_proposal(rgb,layout,proposal,targets)
        age=float(time.monotonic() if now is None else now)-float(source_time)
        base['source_age_sec']=age
        if not math.isfinite(age) or not 0<=age<=.5:raise ValueError('anchor_read_source_stale')
        signature=tuple(mapping['op_to_logical']);context=votes.get('_context')
        if context and context['session']!=source_session:raise ValueError('anchor_read_source_session_changed')
        if context and (context['layout_digest']!=proposal['layout_digest']
                or tuple(context['q'])!=signature or _angle(rotation,np.asarray(context['rotation']))>1.
                or np.max(np.linalg.norm(np.asarray([r['quad'] for r in surface])-np.asarray(context['quads']),axis=2))>6):
            raise ValueError('anchor_read_pose_or_q_unstable')
        if context and (source_generation<=context['generation'] or source_time<=context['time']):
            return dict(base,reason='anchor_read_source_not_new')
        if context is None:context=dict(session=source_session,layout_digest=proposal['layout_digest'],q=list(signature))
        context.update(generation=int(source_generation),time=float(source_time),rotation=rotation.tolist(),
                       quads=[r['quad'].tolist() for r in surface]);votes['_context']=context
        counts=mapping['face_anchors'];deficient=[f for f,n in enumerate(counts) if n<2]
        accepted={f:[] for f in deficient}
        current_pose=dict(K=frame._NORMAL_K.tolist(),rvec=cv2.Rodrigues(rotation)[0].ravel().tolist(),
                          tvec=translation.ravel().tolist())
        rgb_digest=_digest(rgb);hud_mask=frame._hud_mask(rgb.shape)
        all_pixels=np.ones(rgb.shape[:2],np.uint8)
        for item in surface:
            if time.monotonic()>=deadline:raise ValueError('anchor_read_budget_exhausted')
            op=item['operation_slot'];face=op//9;slot=signature[op];key=str(slot)
            if face not in deficient or not _ordinary_unknown(cells[slot]):continue
            quad=np.float32(item['quad'])
            blocked=not np.isfinite(quad).all() or np.any(quad<[298,82]) or np.any(quad>[955,619])
            for target in targets:
                x,y,w,h=target['box'];box=np.float32(((x,y),(x+w,y),(x+w,y+h),(x,y+h)))
                overlap,_=cv2.intersectConvexConvex(quad,box)
                if overlap>0:blocked=True;break
            inner=item['centre']+(quad-item['centre'])*.6
            yy,xx=frame._patch_pixels(all_pixels,inner)
            if not len(xx) or np.mean(hud_mask[yy,xx]>0)<.98:blocked=True
            if blocked:votes.pop(key,None);continue
            reading=classify_icon(_crop(rgb,quad));icon=reading.get('icon_id');confidence=reading.get('confidence',0)
            if (not icon or isinstance(confidence,bool) or not isinstance(confidence,Real)
                    or not math.isfinite(confidence) or not .70<=confidence<=1):votes.pop(key,None);continue
            previous=votes.get(key)
            if previous is None or previous['icon']!=icon:previous=dict(icon=icon,sources=[])
            previous['sources'].append(dict(id=str(source_id),generation=int(source_generation),time=float(source_time),
                confidence=float(confidence),quad=quad.tolist(),image_digest=rgb_digest,pose=deepcopy(current_pose)))
            previous['sources']=previous['sources'][:1]+previous['sources'][-7:] if len(previous['sources'])>8 else previous['sources']
            votes[key]=previous;sources=previous['sources']
            if len(sources)<3 or sources[-1]['time']-sources[0]['time']<.4:continue
            proof=[sources[0],sources[-2],sources[-1]]
            row=deepcopy(cells[slot]);row.update(occupant='none',occupant_status='confirmed',node_status='known',
                icon_id=icon,confidence=min(s['confidence'] for s in proof),
                node_read_evidence=dict(source='reading_only_registration_anchor',sources=deepcopy(proof),
                    source_ids=[s['id'] for s in proof],source_session=source_session,source_backend=source_backend,
                    Q_signature=list(signature),operation_slot=op,layout_digest=proposal['layout_digest'],
                    target_inventory_closed=True,model_coverage_valid=True,no_target_box_overlap=True,
                    requires_strict_public_rebuild=True))
            accepted[face].append(row)
        needed={f:2-counts[f] for f in deficient}
        age=float(time.monotonic() if now is None else now)-float(source_time)
        base['source_age_sec']=age
        if not math.isfinite(age) or not 0<=age<=.5:raise ValueError('anchor_read_source_stale')
        ready=bool(deficient) and all(len(accepted[f])>=needed[f] for f in deficient)
        updates=[row for f in deficient for row in sorted(accepted[f],key=lambda r:-r['confidence'])[:needed[f]]] if ready else []
        return dict(base,ready=ready,cells=updates,reason='missing_registration_anchors_read' if ready
                    else 'anchor_read_needs_fresh_votes',deficient_faces=deficient,needed_counts=needed,
                    current_pose=current_pose,current_rgb_digest=rgb_digest)
    except Exception as error:
        votes.clear()
        return dict(base,reason=str(error) if isinstance(error,ValueError) else 'anchor_read_failed',
                    diagnostic_error=type(error).__name__)


__all__=['propose_read_only_wide_reference','read_missing_registration_anchors']
