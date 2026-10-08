"""Independent geometry, page checks, latest-slot semantics, and evidence writing."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import hashlib
import math
from queue import Full, Queue
import threading
import time

import cv2
import numpy as np

from ._deep_dive_single_run_vision import observe as observe_scene
from ._deep_dive_semantic_explanations import SemanticTargetExplainer
from . import _deep_dive_scan_scene as scan_scene

_NATIVE_SCAN_OBSERVER = scan_scene.observe


def _angle(left, right):
    return float(np.degrees(np.linalg.norm(cv2.Rodrigues(left @ right.T)[0])))


def _json_value(value):
    if isinstance(value,np.ndarray):return value.tolist()
    if isinstance(value,np.generic):return value.item()
    if isinstance(value,dict):return {key:_json_value(child) for key,child in value.items()}
    if isinstance(value,(tuple,list)):return [_json_value(child) for child in value]
    return value


def _accepted_face_observation(scanner, frame_id, observation):
    """Retain source pose and actual votes, independent of top-score previews."""
    diagnostic = observation.get('refine_diagnostic') or {}
    if (not diagnostic.get('renewed') or observation.get('fusion_paused')
            or diagnostic.get('source_frame_id') != frame_id
            or scanner.glyph_anchor_frame_id != frame_id
            or scanner.glyph_anchor_rotation is None):
        return None
    rotation = np.asarray(scanner.glyph_anchor_rotation, float)
    if rotation.shape != (3, 3) or not np.isfinite(rotation).all():
        return None
    indices = [i for i, entries in enumerate(scanner.evidence)
               if any(entry.get('frame_id') == frame_id for entry in entries)]
    if not indices:
        return None
    return dict(frame_id=int(frame_id), rotation=rotation.tolist(), cell_indices=indices)


def _geometry_body_basis(basis, correction, applied):
    """Only applied coordinate corrections change the navigation basis."""
    if not applied:
        return np.asarray(basis, float).copy()
    body = np.asarray((correction or {}).get('body_rotation'), float)
    if (body.shape != (3, 3) or not np.isfinite(body).all()
            or not np.allclose(body.T @ body, np.eye(3), atol=1e-5)
            or abs(np.linalg.det(body)-1.) > 1e-5):
        raise RuntimeError('applied_body_correction_invalid')
    return np.asarray(basis, float) @ body


def _accepted_anchor_observation(scanner, packet, observation):
    """A proven source pose is navigation history, never a fresh current pose."""
    diagnostic = observation.get('refine_diagnostic') or {}
    try:
        if (not diagnostic.get('renewed') or observation.get('fusion_paused')
                or diagnostic.get('source_frame_id') != packet['frame_id']
                or scanner.glyph_anchor_frame_id != packet['frame_id']
                or diagnostic.get('source_map_revision') != packet['map_revision']
                or scanner.glyph_anchor_map_revision != packet['map_revision']
                or abs(float(diagnostic['source_frame_time'])-packet['frame_time']) > 1e-6):
            return None
        source_rotation = np.asarray(scanner.glyph_anchor_rotation, float)
        packet_rotation = np.asarray(packet['pose']['rotation'], float)
        packet_basis = np.asarray(packet['geometry_body_basis'], float)
        for matrix in (source_rotation, packet_rotation, packet_basis):
            if (matrix.shape != (3, 3) or not np.isfinite(matrix).all()
                    or not np.allclose(matrix.T @ matrix, np.eye(3), atol=1e-5)
                    or abs(np.linalg.det(matrix)-1.) > 1e-5):
                return None
        source_basis = packet_basis @ packet_rotation.T @ source_rotation
        return dict(frame_id=packet['frame_id'], session_id=packet['session_id'],
                    map_revision=packet['map_revision'], frame_time=packet['frame_time'],
                    rotation=source_rotation.tolist(), body_basis=source_basis.tolist(),
                    accepted_faces=deepcopy(diagnostic.get('accepted_faces') or {}),
                    accepted_cell_indices=deepcopy(diagnostic.get('accepted_cell_indices')),
                    accepted_confirmed_cell_indices=deepcopy(diagnostic.get('accepted_confirmed_cell_indices')))
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


class ScanVisionStream:
    """Tracker and semantic scanners each have exactly one owning thread."""
    def __init__(self, scanner, adapter, output_dir, frames, started, *, observe_fn=None,
                 evidence_kind='real_game', refresh_semantic_source=False):
        if not scanner.ready:
            raise ValueError('stream_scanner_not_initialized')
        self._scanner = scanner
        self._semantic = scanner.fork_semantic()
        self._target_explainer = SemanticTargetExplainer()
        self._adapter = adapter
        self._observe = observe_fn or observe_scene
        self._refresh_semantic_source = bool(refresh_semantic_source and self._observe is _NATIVE_SCAN_OBSERVER)
        self._last_consumed_semantic_source = None
        self._evidence_kind = evidence_kind
        self._directory = Path(output_dir)
        (self._directory / 'frames').mkdir(parents=True, exist_ok=True)
        self._frames, self._started = frames, started
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._writes = Queue(maxsize=16)
        self._thread = self._scene_thread = self._semantic_thread = self._writer = None
        self._closed, self._error = False, None
        self._slot = self._latest_packet = None
        self._scene_slot = None
        self._semantic_metadata = None
        self._correction_slot = None
        self._geometry_basis = np.eye(3)
        self._axes = deepcopy(scanner.response_axes)
        self._semantic_result = deepcopy(self._semantic.result())
        self._semantic_state = self._result_state(self._semantic_result, copy_values=False)
        self._sealed_result = None
        self._semantic_state.update(glyph_anchor_at=scanner.glyph_anchor_at,
            glyph_anchor_rotation=(scanner.glyph_anchor_rotation.tolist()
                if scanner.glyph_anchor_rotation is not None else None),
            glyph_anchor_map_revision=scanner.glyph_anchor_map_revision)
        self._scene, self._scene_valid = 'unknown', False
        self._scene_time = time.monotonic()
        self._scene_observation = None
        self._scene_metadata = None
        self._mask_cache = dict(mask_observation=deepcopy(scanner.target_mask_observation()), frame_time=self._scene_time)
        self._semantic_revision = 0
        self._stats = dict(processed_frames=0, dropped_frames=0, written_frames=0,
                           evidence_kind=evidence_kind, scene_frames=0, scene_replaced=0,
                           scene_total_sec=0., scene_latency_total_sec=0.,
                           semantic_frames=0, generation=None, session_id=None,
                           writer_queue_peak=0, semantic_queue_peak=0,
                           semantic_replaced=0, semantic_latency_total_sec=0.,
                           pose_corrections_proposed=0,pose_corrections_applied=0,
                           pose_corrections_stale=0,pose_corrections_replaced=0,
                           pose_corrections_rejected=0,pose_correction_events=[],
                           tracking_total_sec=0., semantic_total_sec=0., error=None)
        self._next_frame = max((f.get('frame_id',-1) for f in frames),default=-1)+1
        self._stream_started = None
        self._snapshot = dict(seq=0, frame_time=started, published_at=time.monotonic(),
                              geometry_body_basis=self._geometry_basis.copy(),
                              rotation=scanner.rotation.copy(), tvec=scanner.tvec.copy(),
                              correction_epoch=scanner.pose_snapshot()['correction_epoch'],
                              response_axes=deepcopy(self._axes),quality=scanner.quality,
                              tracking_ok=True,geometry_tracking_ok=True,
                              projected=deepcopy(scanner.last_projected),
                              targets=deepcopy(scanner.last_targets),error=None)

    @staticmethod
    def _result_state(result, *, copy_values=True):
        # Internally the full result has already been frozen by one deep copy.
        # Its control projection may share those immutable values: snapshot()
        # always gives consumers their own copy. External callers retain the
        # original independent-value contract.
        value = deepcopy if copy_values else lambda item: item
        cells=[]
        for source in result['cells']:
            cell={key:value(source[key]) for key in
                  ('face','row','col','occupant','occupant_status','node_status','icon_id',
                   'occupant_evidence_counts','confidence') if key in source}
            cell['evidence']=[{key:value(entry[key]) for key in
                              ('frame_id','group','cosine','quad','occupant','confidence','quality',
                               'target_box','target_point','anchor_type','association_evidence') if key in entry}
                              for entry in source.get('evidence',[])[:6]]
            cells.append(cell)
        return dict(cells=cells,layout_complete=result['layout_complete'],
                    known_cells=result['known_cells'],faces_observed=result['faces_observed'],
                    semantic_map_revision=result.get('map_revision'),
                    **{key:value(result[key]) for key in
                       ('schema','coordinate_frame','player_cell','singularity_cell',
                        'inspiration_cells','diagnostics','target_clues',
                        'target_candidate_associations') if key in result})

    def snapshot(self):
        with self._lock:
            state=deepcopy(self._snapshot)
            state.update(deepcopy(self._semantic_state))
            state['semantic_revision']=self._semantic_revision
            age=time.monotonic()-self._scene_time
            state.update(scene_age_sec=age,scene=self._scene)
            state['scene_observation'] = deepcopy(self._scene_observation)
            state['scene_metadata'] = deepcopy(self._scene_metadata)
            state['semantic_metadata'] = deepcopy(self._semantic_metadata)
            anchor_at = state.get('glyph_anchor_at')
            state['glyph_anchor_age_sec'] = time.monotonic()-anchor_at if anchor_at else None
            state['fusion_paused'] = bool(state.get('fusion_paused') or
                (anchor_at and state['glyph_anchor_age_sec'] > 2.))
            if state.get('glyph_anchor_map_revision') != state.get('map_revision'):
                state['glyph_anchor_rotation'] = None
            state['evidence_kind'] = self._evidence_kind
            state['geometry_tracking_ok'] = bool(state.get('geometry_tracking_ok', state.get('tracking_ok')))
            metadata = self._scene_metadata
            same_frame_source = bool(metadata and metadata.get('session_id') == state.get('session_id')
                                     and metadata.get('map_revision') == state.get('map_revision'))
            state['scene_tracking_ok'] = bool(self._scene_valid and age <= .5 and same_frame_source)
            if not self._scene_valid or age>.5:
                state['tracking_ok']=False
                state['pause_reason']='scene_unknown' if not self._scene_valid else 'scene_feedback_stale'
            elif not same_frame_source:
                state['tracking_ok']=False
                state['pause_reason']='scene_source_mismatch'
            return state

    @property
    def error(self):
        with self._lock:
            return self._error

    def seal_result_if(self, predicate):
        """Atomically finish at the actual semantic frame that meets the goal.

        Draining a packet already in flight cannot replace this accepted source
        with geometry or entity boxes from another frame. The caller still owns
        the final fresh page/session/unchanged-gameplay checks after stop().
        """
        with self._lock:
            now = time.monotonic()
            source = self._semantic_result.get('semantic_source') or {}
            if (self._error or self._semantic_state.get('fusion_paused') or not self._scene_valid
                    or now-self._scene_time > .5 or now-self._snapshot['frame_time'] > .35
                    or not self._snapshot.get('geometry_tracking_ok')
                    or not source or now-source['frame_time'] > .8
                    or source['session_id'] != self._snapshot.get('session_id')
                    or source['map_revision'] != self._snapshot.get('map_revision')):
                return False
            if not predicate(self._semantic_result):
                return False
            if self._sealed_result is None:
                self._sealed_result = deepcopy(self._semantic_result)
            return True

    @property
    def stats(self):
        if not self._closed:
            raise RuntimeError('stream_stats_require_stop')
        return deepcopy(self._stats)

    def _fault(self,reason):
        with self._condition:
            if self._error is None:self._error=str(reason)
            self._snapshot.update(error=self._error,tracking_ok=False,published_at=time.monotonic())
            self._stop.set()
            self._condition.notify_all()

    def start(self):
        if self._thread is not None or self._closed:
            raise RuntimeError('stream_already_started_or_closed')
        self._scanner.pending_drag=None
        self._scanner.drag_in_progress=False
        self._stream_started=time.monotonic()
        self._writer=threading.Thread(target=self._write_loop,name='cube-scan-png',daemon=True)
        self._semantic_thread=threading.Thread(target=self._semantic_loop,name='cube-scan-semantics',daemon=True)
        self._scene_thread=threading.Thread(target=self._scene_loop,name='cube-scan-page',daemon=True)
        self._thread=threading.Thread(target=self._run,name='cube-scan-tracking',daemon=True)
        try:
            self._writer.start()
            self._semantic_thread.start()
            self._scene_thread.start()
            self._thread.start()
        except Exception:
            self.stop()
            raise
        return self

    def _write_loop(self):
        while True:
            item=self._writes.get()
            try:
                if item is None:return
                raw,overlay,image,annotation=item
                if not cv2.imwrite(str(raw),cv2.cvtColor(image,cv2.COLOR_RGB2BGR)):
                    raise OSError(f'raw_png_write_failed:{raw.name}')
                if not cv2.imwrite(str(overlay),cv2.cvtColor(annotation,cv2.COLOR_RGB2BGR)):
                    raise OSError(f'overlay_png_write_failed:{overlay.name}')
                self._stats['written_frames']+=1
            except Exception as exc:
                self._fault(f'stream_writer:{type(exc).__name__}:{exc}')
            finally:
                self._writes.task_done()

    def _save(self,packet,annotation,observation,scene,semantic):
        frame_id=packet['frame_id']
        raw=self._directory/'frames'/f'{frame_id:04d}.png'
        overlay=raw.with_name(f'{frame_id:04d}_overlay.png')
        row=dict(frame_id=frame_id,path=f'frames/{raw.name}',overlay_path=f'frames/{overlay.name}',
                 during_drag=True,semantic_fused=semantic,scene=scene,generation=packet['generation'],
                 session_id=packet['session_id'],frame_time=packet['frame_time'],
                 capture_backend=packet.get('capture_backend'),
                 elapsed_sec=round(packet['frame_time']-self._started,3),observation=_json_value(observation),
                 pose=_json_value(packet.get('pose')),fused_pose=_json_value(packet.get('fused_pose')),
                 geometry_body_basis=_json_value(packet.get('geometry_body_basis')))
        try:self._writes.put_nowait((raw,overlay,packet['image'],annotation))
        except Full:raise RuntimeError('stream_writer_queue_full')
        with self._lock:
            self._frames.append(row)
            self._stats['writer_queue_peak']=max(self._stats['writer_queue_peak'],self._writes.qsize())

    def _run(self):
        generation,session,seq=-1,None,0
        try:
            while not self._stop.is_set():
                packet=self._adapter.capture_stream_frame(after_generation=generation,expected_session_id=session)
                if packet is None:
                    self._stop.wait(.004)
                    continue
                current_session=packet['session_id']
                if session is not None and current_session!=session:raise RuntimeError('capture_session_changed')
                session=current_session
                next_generation=int(packet['generation'])
                if next_generation<=generation:raise RuntimeError('capture_generation_not_increasing')
                self._stats['dropped_frames']+=max(0,next_generation-generation-1) if generation>=0 else 0
                generation=next_generation
                capture=packet['capture'];image=getattr(capture,'image',None)
                if not capture.success or image is None:raise RuntimeError('stream_capture_failed')
                if image.shape!=(720,1280,3) or image.dtype!=np.uint8:
                    raise RuntimeError('stream_invalid_or_blank_frame')
                if float(image[::8,::8].std())<2:
                    failed=dict(frame_id=self._next_frame,image=image.copy(),generation=generation,
                                session_id=session,frame_time=float(packet['arrived_at_monotonic']))
                    self._next_frame+=1
                    self._save(failed,image.copy(),dict(tracking_ok=False,reason='blank_frame'),'blank_frame',False)
                    raise RuntimeError('stream_invalid_or_blank_frame')
                image=image.copy()
                frame_id=self._next_frame;self._next_frame+=1
                frame_time=float(packet['arrived_at_monotonic'])
                begun=time.monotonic()
                with self._lock:
                    cache=deepcopy(self._mask_cache)
                    correction,self._correction_slot=self._correction_slot,None
                masks=dict(mask_observation=cache['mask_observation'],age_sec=max(0.,frame_time-cache['frame_time']))
                correction_event=None
                if correction is not None:
                    epoch=self._scanner.pose_snapshot()['correction_epoch']
                    correction_event=dict(source_frame_id=correction.get('source_frame_id'),
                                          source_epoch=correction.get('source_epoch'),
                                          current_epoch=epoch,frame_id=frame_id,
                                          angle_deg=correction.get('angle_deg'))
                    if correction.get('source_epoch')!=epoch:
                        with self._lock:
                            self._stats['pose_corrections_stale']+=1
                            self._stats['pose_correction_events'].append(dict(correction_event,status='stale'))
                        correction=None
                observation=self._scanner.track_frame(image,frame_id,mask_targets=masks,pose_correction=correction)
                if correction is not None:
                    applied=bool(observation.get('correction_applied'))
                    self._geometry_basis = _geometry_body_basis(self._geometry_basis, correction, applied)
                    with self._lock:
                        self._stats['pose_corrections_applied' if applied else 'pose_corrections_rejected']+=1
                        self._stats['pose_correction_events'].append(dict(correction_event,
                            status='applied' if applied else 'rejected',
                            result_epoch=self._scanner.pose_snapshot()['correction_epoch']))
                elapsed=time.monotonic()-begun;seq+=1
                pose=deepcopy(observation.get('pose') or self._scanner.pose_snapshot())
                frozen=dict(frame_id=frame_id,image=image,generation=generation,session_id=session,
                            frame_time=frame_time,pose=pose,observation=deepcopy(observation),
                            capture_backend=getattr(capture,'backend',None),
                            geometry_body_basis=self._geometry_basis.copy(),
                            map_revision=self._scanner.identity_corrections)
                state=dict(seq=seq,frame_id=frame_id,frame_time=frame_time,published_at=time.monotonic(),
                           generation=generation,session_id=session,rotation=self._scanner.rotation.copy(),
                           tvec=self._scanner.tvec.copy(),map_revision=self._scanner.identity_corrections,
                           correction_epoch=pose.get('correction_epoch',0),
                           geometry_body_basis=self._geometry_basis.copy(),
                           elapsed=frame_time-self._started,response_axes=deepcopy(self._axes),
                           quality=self._scanner.quality,tracking_ok=bool(observation.get('tracking_ok')),
                           geometry_tracking_ok=bool(observation.get('tracking_ok')),
                           geometry_reason=observation.get('reason'),
                           projected=deepcopy(self._scanner.last_projected),targets=deepcopy(self._scanner.last_targets),
                           processing_sec=elapsed,dropped_frames=self._stats['dropped_frames'],semantic_fused=False,error=None)
                self._stats['processed_frames']+=1
                self._stats['tracking_total_sec']+=elapsed
                self._stats.update(generation=generation,session_id=session)
                with self._condition:
                    self._latest_packet=frozen
                    if self._error:state.update(error=self._error,tracking_ok=False)
                    self._snapshot=state
                    if self._scene_slot is not None:self._stats['scene_replaced']+=1
                    self._scene_slot=frozen
                    self._condition.notify_all()
                if not observation.get('tracking_ok'):
                    # The failed geometry frame may be an overlay. Let the
                    # page owner classify that exact packet before join.
                    self._save(frozen,self._scanner.annotate(image),observation,'tracking_failed',False)
                    self._fault(observation.get('reason','stream_tracking_lost'))
                self._stop.wait(max(0.,1./30.-(time.monotonic()-begun)))
        except Exception as exc:
            self._fault(f'stream_geometry:{type(exc).__name__}:{exc}')

    def _scene_loop(self):
        """Check pages independently; only its verified packets may be fused."""
        unknown=0
        submitted_frame_id=-1
        try:
            while True:
                with self._condition:
                    while self._scene_slot is None and not self._stop.is_set():self._condition.wait(.05)
                    if self._scene_slot is None and self._stop.is_set():return
                    packet,self._scene_slot=self._scene_slot,None
                begun=time.monotonic()
                scene=self._observe(packet['image'])
                ended=time.monotonic()
                is_board=bool(scene.get('valid') and scene.get('scene')=='board' and scene.get('player_turn'))
                unknown=unknown+1 if scene.get('scene')=='unknown' else 0
                checked=dict(packet,scene_observation=scene)
                with self._condition:
                    self._scene=scene.get('scene','invalid');self._scene_valid=is_board
                    self._scene_time=packet['frame_time']
                    self._scene_observation=deepcopy(scene)
                    self._scene_metadata={key:packet[key] for key in
                        ('frame_id','generation','session_id','frame_time','map_revision')}
                    self._scene_metadata.update(started_at=begun,ended_at=ended,processing_sec=ended-begun,
                        latency_sec=ended-packet['frame_time'])
                    self._stats['scene_frames']+=1
                    self._stats['scene_total_sec']+=ended-begun
                    self._stats['scene_latency_total_sec']+=ended-packet['frame_time']
                    # The semantic owner already limits its execution rate. Keep
                    # its capacity-one inbox current even for stationary views;
                    # a skipped page-checked frame only makes the next source
                    # older. Independent positive views are still proved by the
                    # mapper, rather than by this scheduling decision.
                    submit=packet['frame_id']>submitted_frame_id
                    if (is_board and packet['observation'].get('tracking_ok') and submit
                            and not self._stop.is_set()):
                        if self._slot is not None:self._stats['semantic_replaced']+=1
                        self._slot=checked
                        self._stats['semantic_queue_peak']=1
                        submitted_frame_id=packet['frame_id']
                    self._condition.notify_all()
                if not is_board:
                    self._save(packet,packet['image'].copy(),dict(tracking_ok=False,reason='scene_not_board'),
                               scene.get('scene'),False)
                    if scene.get('scene')!='unknown' or unknown>=3:
                        self._fault('scene_not_board')
                        return
        except Exception as exc:
            self._fault(f'stream_scene:{type(exc).__name__}:{exc}')

    @staticmethod
    def _semantic_packet_source(packet):
        """Validate a whole tracked packet; never normalize another frame into it."""
        if not isinstance(packet, dict):return None
        try:
            source={key:packet[key] for key in ('frame_id','generation','session_id','frame_time','map_revision')}
            if any(type(source[key]) is not int or source[key]<0 for key in ('frame_id','generation','map_revision')):
                return None
            session=source['session_id'];stamp=source['frame_time']
            if (not isinstance(session,(str,int)) or isinstance(session,bool) or session==''
                    or type(stamp) not in (int,float) or not math.isfinite(stamp)
                    or stamp>time.monotonic() or packet.get('capture_backend')!='wgc'
                    or packet.get('observation',{}).get('tracking_ok') is not True):return None
            image=packet['image'];pose=packet['pose']
            if (not isinstance(image,np.ndarray) or image.shape!=(720,1280,3) or image.dtype!=np.uint8
                    or pose.get('map_revision')!=source['map_revision']
                    or type(pose.get('correction_epoch')) is not int or pose['correction_epoch']<0
                    or not math.isfinite(float(pose['quality']))):return None
            for key in ('rvec','tvec'):
                vector=np.asarray(pose[key],float)
                if vector.size!=3 or not np.isfinite(vector).all():return None
            for value in (pose['rotation'],packet['geometry_body_basis']):
                matrix=np.asarray(value,float)
                if (matrix.shape!=(3,3) or not np.isfinite(matrix).all()
                        or not np.allclose(matrix.T@matrix,np.eye(3),rtol=0.,atol=1e-5)
                        or abs(float(np.linalg.det(matrix))-1.)>1e-5):return None
            return source
        except (KeyError,TypeError,ValueError,AttributeError,OverflowError):return None

    def _semantic_source_duplicate(self, source):
        previous=self._last_consumed_semantic_source
        return bool(previous is not None and
            (any(source[key]!=previous[key] for key in ('session_id','map_revision')) or
             any(source[key]<=previous[key] for key in ('frame_id','generation','frame_time'))))

    def _fresh_semantic_packet(self, checked):
        """Inline checks grant only semantic ingress; page/input ownership stays separate."""
        diagnostic=dict(status='disabled',inline_scene_sec=0.)
        if not self._refresh_semantic_source:return checked,diagnostic
        with self._lock:
            if self._stop.is_set() or self._error:return None,dict(diagnostic,status='stopped')
            latest=self._latest_packet
            context=(self._snapshot.get('session_id'),self._snapshot.get('map_revision'))
        old=self._semantic_packet_source(checked)
        if old is None:return None,dict(diagnostic,status='invalid_checked_source')
        new=self._semantic_packet_source(latest)
        selected=checked;source=old
        refreshed=bool(new is not None and
            all(new[key]==old[key] for key in ('session_id','map_revision')) and
            all(new[key]>old[key] for key in ('frame_id','generation','frame_time')))
        if refreshed:selected=latest;source=new
        diagnostic.update(status='refreshed' if refreshed else 'checked_source',
            old_source=old,selected_source=source)
        if ((None not in context and (source['session_id'],source['map_revision'])!=context)
                or self._semantic_source_duplicate(source)):
            return None,dict(diagnostic,status='source_context_or_duplicate_rejected')
        if refreshed:
            began=time.perf_counter();scene=self._observe(selected['image'])
            diagnostic['inline_scene_sec']=time.perf_counter()-began
            diagnostic['selected_rgb_sha256']=hashlib.sha256(selected['image'].tobytes()).hexdigest()
            with self._lock:
                if self._stop.is_set() or self._error:return None,dict(diagnostic,status='stopped')
                current_context=(self._snapshot.get('session_id'),self._snapshot.get('map_revision'))
                if None not in current_context and current_context!=(source['session_id'],source['map_revision']):
                    return None,dict(diagnostic,status='source_context_changed_during_inline')
            is_board=bool(scene.get('valid') is True and scene.get('scene')=='board'
                and scene.get('player_turn') is True and not scene.get('enemy_turn')
                and not scene.get('unknown_modal_evidence'))
            if not is_board:
                diagnostic['status']='inline_scene_rejected'
                self._save(selected,selected['image'].copy(),
                    dict(tracking_ok=False,reason='semantic_inline_scene_not_board',
                         semantic_source_refresh=diagnostic),scene.get('scene'),False)
                return None,diagnostic
            selected=dict(selected,scene_observation=scene)
        return selected,diagnostic

    def _semantic_loop(self):
        next_allowed=0.
        try:
            while True:
                with self._condition:
                    while self._slot is None and not self._stop.is_set():self._condition.wait(.05)
                    if self._slot is None and self._stop.is_set():return
                delay=next_allowed-time.monotonic()
                if delay>0:self._stop.wait(delay)
                with self._condition:packet,self._slot=self._slot,None
                if packet is None:continue
                begun=time.monotonic()
                packet,source_refresh=self._fresh_semantic_packet(packet)
                if packet is None:
                    if self._stop.is_set() or self._error:return
                    continue
                scene=packet['scene_observation']
                cost_started=time.perf_counter()
                hud_prepared=self._target_explainer.prepare(packet)
                hud_finished=time.perf_counter()
                if self._refresh_semantic_source:
                    with self._lock:
                        if self._stop.is_set() or self._error:return
                        source=self._semantic_packet_source(packet)
                        context=(self._snapshot.get('session_id'),self._snapshot.get('map_revision'))
                        if (source is None or self._semantic_source_duplicate(source) or
                                None not in context and context!=(source['session_id'],source['map_revision'])):continue
                        # This is the admission point. stop() may drain only
                        # work already admitted; it cannot admit another model.
                        self._last_consumed_semantic_source=source
                revision=self._semantic.identity_corrections
                observation=self._semantic.semantic_view(packet['image'],packet['frame_id'],packet['pose'],
                    source_frame_time=packet['frame_time'])
                semantic_finished=time.perf_counter()
                fused_at=time.monotonic()
                if self._semantic.identity_corrections!=revision:
                    observation=dict(tracking_ok=False,reason='semantic_identity_revision_requires_rescan')
                artifact=dict(packet,fused_pose=self._semantic.pose_snapshot())
                elapsed=fused_at-begun
                self._stats['semantic_frames']+=1;self._stats['semantic_total_sec']+=elapsed
                metadata={key:packet[key] for key in
                    ('frame_id','generation','session_id','frame_time','map_revision')}
                metadata['capture_backend']=packet.get('capture_backend')
                metadata.update(started_at=begun,ended_at=fused_at,
                        fusion_sec=fused_at-begun,artifact_sec=None,
                        source_epoch=packet['pose'].get('correction_epoch'),
                        processing_sec=elapsed)
                # Freeze the owner's result outside the shared feedback lock.
                # The geometry/page owners can publish newer captures while
                # this private copy is prepared; none of it is visible until
                # the atomic swap below.
                frozen_result=deepcopy(self._semantic.last_fused_result or self._semantic.result())
                metadata['pose']=_json_value(self._semantic.pose_snapshot())
                frozen_result['semantic_source']=dict(metadata)
                copy_finished=time.perf_counter()
                explained=self._target_explainer.apply(frozen_result, observation, packet)
                explanation_finished=time.perf_counter()
                if explained['status']=='applied':
                    frozen_result=explained['layout']
                explanation_diagnostic=dict(status=explained['status'],
                    **explained['diagnostics'])
                observation=dict(observation,
                    semantic_hud_guard=dict(ready=hud_prepared.get('ready'),
                        reason=hud_prepared.get('reason'),source=hud_prepared.get('source')),
                    current_target_explanation=explanation_diagnostic)
                if self._refresh_semantic_source:
                    observation['semantic_source_refresh']=source_refresh
                    metadata['source_refresh']=source_refresh
                frozen_result['current_target_explanation']=deepcopy(explanation_diagnostic)
                semantic_state=self._result_state(frozen_result, copy_values=False)
                semantic_state.update(fusion_paused=bool(observation.get('fusion_paused')),
                        glyph_anchor_age_sec=observation.get('glyph_anchor_age_sec'),
                        refine_diagnostic=deepcopy(observation.get('refine_diagnostic')),
                        target_coverage=deepcopy(observation.get('target_coverage') or {}))
                semantic_state['accepted_face_observation']=_accepted_face_observation(
                    self._semantic, packet['frame_id'], observation)
                semantic_state['accepted_anchor_observation']=_accepted_anchor_observation(
                    self._semantic, packet, observation)
                for key in ('glyph_anchor_at','glyph_anchor_rotation','glyph_anchor_frame_id',
                            'glyph_anchor_map_revision','glyph_anchor_faces','glyph_anchor_reason'):
                    if key in observation:semantic_state[key]=deepcopy(observation[key])
                stream_cost=dict(hud_prepare=hud_finished-cost_started,
                    semantic_view=semantic_finished-hud_finished,
                    freeze_copy=copy_finished-semantic_finished,
                    target_explanation=explanation_finished-copy_finished,
                    feedback_projection=time.perf_counter()-explanation_finished)
                if self._refresh_semantic_source:
                    stream_cost['inline_scene']=source_refresh['inline_scene_sec']
                # Profiling only: none of these values grants freshness,
                # supports a target vote, or changes a controller gate.
                observation['semantic_stream_cost_sec']=stream_cost
                metadata['cost_sec']=stream_cost
                with self._lock:
                    published_at=time.monotonic()
                    metadata.update(published_at=published_at,
                        publish_prepare_sec=published_at-fused_at,
                        latency_sec=published_at-packet['frame_time'])
                    frozen_result['semantic_source'].update(metadata)
                    self._stats['semantic_latency_total_sec']+=metadata['latency_sec']
                    self._semantic_metadata=metadata
                    self._semantic_result=frozen_result
                    self._semantic_state=semantic_state
                    if observation.get('tracking_ok'):
                        self._semantic_revision+=1
                        correction=observation.get('pose_correction')
                        if correction is not None:
                            self._stats['pose_corrections_proposed']+=1
                            current_epoch=self._snapshot.get('correction_epoch',0)
                            if correction.get('source_epoch')!=current_epoch:
                                self._stats['pose_corrections_stale']+=1
                                self._stats['pose_correction_events'].append(dict(
                                    source_frame_id=correction.get('source_frame_id'),
                                    source_epoch=correction.get('source_epoch'),current_epoch=current_epoch,
                                    status='stale_after_semantic'))
                            else:
                                if self._correction_slot is not None:self._stats['pose_corrections_replaced']+=1
                                self._correction_slot=deepcopy(correction)
                    self._mask_cache=dict(mask_observation=deepcopy(observation.get('mask_observation',[])),
                                          frame_time=packet['frame_time'])
                # Publish control feedback before rendering evidence. The PNG
                # owner still receives the exact source image and fused pose.
                if self._semantic.last_fused_result is None:
                    annotation=self._semantic.annotate(packet['image'])
                else:
                    annotation=self._semantic.annotate(packet['image'],
                        fused_result=self._semantic.last_fused_result)
                self._save(artifact,annotation,observation,scene.get('scene'),True)
                with self._lock:
                    self._semantic_metadata['artifact_sec']=time.monotonic()-fused_at
                if not observation.get('tracking_ok'):
                    self._fault(observation.get('reason','semantic_failed'))
                    return
                next_allowed=begun+.2
        except Exception as exc:
            self._fault(f'stream_semantic:{type(exc).__name__}:{exc}')

    def stop(self):
        """Join scanner owners, drain artifacts, then merge semantic evidence."""
        if self._closed:return
        stopped_at = time.perf_counter()
        stop_timing = {}
        self._stop.set()
        with self._condition:self._condition.notify_all()
        for name, thread in (('geometry', self._thread), ('scene', self._scene_thread),
                             ('semantic', self._semantic_thread)):
            joined_at = time.perf_counter()
            if thread is not None and thread.ident is not None:thread.join()
            stop_timing[name+'_join_sec'] = time.perf_counter()-joined_at
        merge_started = time.perf_counter()
        if self._correction_slot is not None:
            correction=self._correction_slot
            self._stats['pose_corrections_rejected']+=1
            self._stats['pose_correction_events'].append(dict(
                source_frame_id=correction.get('source_frame_id'),source_epoch=correction.get('source_epoch'),
                status='not_applied_after_stop'))
            self._correction_slot=None
        for field in('evidence','view_rotations','group','group_rotation','icon_anchors',
                     'best_known','stagnant_frames','identity_corrections',
                     'glyph_anchor_at','glyph_anchor_reason','glyph_anchor_rotation',
                     'glyph_anchor_frame_id','glyph_anchor_map_revision','glyph_anchor_faces'):
            setattr(self._scanner,field,deepcopy(getattr(self._semantic,field)))
        # Retain the semantic owner's frozen source/pose/target associations.
        # The geometry owner's current pose may be newer and cannot be mixed
        # with the older entity boxes when exporting target readiness.
        self._scanner.last_fused_result = deepcopy(self._sealed_result or self._semantic.last_fused_result)
        if self._latest_packet is not None:
            packet=self._latest_packet
            if not any(row['frame_id']==packet['frame_id'] for row in self._frames):
                try:self._save(packet,self._scanner.annotate(packet['image']),packet['observation'],'final',False)
                except Exception as exc:self._fault(f'final_frame:{exc}')
        if self._writer is not None and self._writer.ident is not None:
            stop_timing['merge_and_final_frame_sec'] = time.perf_counter()-merge_started
            writer_started = time.perf_counter()
            self._writes.put(None);self._writer.join()
            stop_timing['writer_join_sec'] = time.perf_counter()-writer_started
        else:
            stop_timing['merge_and_final_frame_sec'] = time.perf_counter()-merge_started
            stop_timing['writer_join_sec'] = 0.
        self._frames.sort(key=lambda row:row['frame_id'])
        duration=max(.001,time.monotonic()-(self._stream_started or time.monotonic()))
        self._stats.update(error=self.error,duration_sec=duration,
                           stop_timing=stop_timing,
                           track_fps=self._stats['processed_frames']/duration,
                           semantic_fps=self._stats['semantic_frames']/duration,
                           scene_fps=self._stats['scene_frames']/duration,
                           scene_mean_latency_sec=self._stats['scene_latency_total_sec']/max(1,self._stats['scene_frames']),
                           semantic_mean_latency_sec=self._stats['semantic_latency_total_sec']/max(1,self._stats['semantic_frames']))
        self._closed=True
        stop_timing['total_stop_sec'] = time.perf_counter()-stopped_at


__all__=['ScanVisionStream']
