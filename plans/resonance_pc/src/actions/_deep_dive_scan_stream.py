"""Independent geometry, latest-slot semantics, and bounded evidence writing."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from queue import Full, Queue
import threading
import time

import cv2
import numpy as np

from ._deep_dive_single_run_vision import observe as observe_scene


def _angle(left, right):
    return float(np.degrees(np.linalg.norm(cv2.Rodrigues(left @ right.T)[0])))


def _json_value(value):
    if isinstance(value,np.ndarray):return value.tolist()
    if isinstance(value,np.generic):return value.item()
    if isinstance(value,dict):return {key:_json_value(child) for key,child in value.items()}
    if isinstance(value,(tuple,list)):return [_json_value(child) for child in value]
    return value


class ScanVisionStream:
    """Tracker and semantic scanners each have exactly one owning thread."""
    def __init__(self, scanner, adapter, output_dir, frames, started):
        if not scanner.ready:
            raise ValueError('stream_scanner_not_initialized')
        self._scanner = scanner
        self._semantic = scanner.fork_semantic()
        self._adapter = adapter
        self._directory = Path(output_dir)
        (self._directory / 'frames').mkdir(parents=True, exist_ok=True)
        self._frames, self._started = frames, started
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._writes = Queue(maxsize=16)
        self._thread = self._semantic_thread = self._writer = None
        self._closed, self._error = False, None
        self._slot = self._latest_packet = None
        self._correction_slot = None
        self._axes = deepcopy(scanner.response_axes)
        self._semantic_state = self._result_state(self._semantic.result())
        self._scene, self._scene_valid = 'board', True
        self._scene_time = time.monotonic()
        self._mask_cache = dict(mask_observation=deepcopy(scanner.target_mask_observation()), frame_time=self._scene_time)
        self._semantic_revision = 0
        self._stats = dict(processed_frames=0, dropped_frames=0, written_frames=0,
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
                              rotation=scanner.rotation.copy(), tvec=scanner.tvec.copy(),
                              correction_epoch=scanner.pose_snapshot()['correction_epoch'],
                              response_axes=deepcopy(self._axes),quality=scanner.quality,
                              tracking_ok=True,projected=deepcopy(scanner.last_projected),
                              targets=deepcopy(scanner.last_targets),error=None,
                              **deepcopy(self._semantic_state))

    @staticmethod
    def _result_state(result):
        cells=[]
        for source in result['cells']:
            cell={key:deepcopy(source[key]) for key in
                  ('face','row','col','occupant','occupant_status','node_status','icon_id',
                   'occupant_evidence_counts','confidence') if key in source}
            cell['evidence']=[{key:deepcopy(entry[key]) for key in ('frame_id','group','cosine','quad') if key in entry}
                              for entry in source.get('evidence',[])[:3]]
            cells.append(cell)
        return dict(cells=cells,layout_complete=result['layout_complete'],
                    known_cells=result['known_cells'],faces_observed=result['faces_observed'])

    def snapshot(self):
        with self._lock:
            state=deepcopy(self._snapshot)
            state.update(deepcopy(self._semantic_state))
            state['semantic_revision']=self._semantic_revision
            age=time.monotonic()-self._scene_time
            state.update(scene_age_sec=age,scene=self._scene)
            if not self._scene_valid or age>.5:
                state['tracking_ok']=False
                state['pause_reason']='scene_unknown' if not self._scene_valid else 'scene_feedback_stale'
            return state

    @property
    def error(self):
        with self._lock:
            return self._error

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
        self._thread=threading.Thread(target=self._run,name='cube-scan-tracking',daemon=True)
        try:
            self._writer.start()
            self._semantic_thread.start()
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
                 elapsed_sec=round(packet['frame_time']-self._started,3),observation=_json_value(observation),
                 pose=_json_value(packet.get('pose')),fused_pose=_json_value(packet.get('fused_pose')))
        try:self._writes.put_nowait((raw,overlay,packet['image'],annotation))
        except Full:raise RuntimeError('stream_writer_queue_full')
        with self._lock:
            self._frames.append(row)
            self._stats['writer_queue_peak']=max(self._stats['writer_queue_peak'],self._writes.qsize())

    def _run(self):
        generation,session,seq=-1,None,0
        submitted_rotation,submitted_at=None,0.
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
                    with self._lock:
                        self._stats['pose_corrections_applied' if applied else 'pose_corrections_rejected']+=1
                        self._stats['pose_correction_events'].append(dict(correction_event,
                            status='applied' if applied else 'rejected',
                            result_epoch=self._scanner.pose_snapshot()['correction_epoch']))
                elapsed=time.monotonic()-begun;seq+=1
                pose=deepcopy(observation.get('pose') or self._scanner.pose_snapshot())
                frozen=dict(frame_id=frame_id,image=image,generation=generation,session_id=session,
                            frame_time=frame_time,pose=pose,observation=deepcopy(observation))
                state=dict(seq=seq,frame_id=frame_id,frame_time=frame_time,published_at=time.monotonic(),
                           generation=generation,session_id=session,rotation=self._scanner.rotation.copy(),
                           tvec=self._scanner.tvec.copy(),map_revision=self._scanner.identity_corrections,
                           correction_epoch=pose.get('correction_epoch',0),
                           elapsed=frame_time-self._started,response_axes=deepcopy(self._axes),
                           quality=self._scanner.quality,tracking_ok=bool(observation.get('tracking_ok')),
                           projected=deepcopy(self._scanner.last_projected),targets=deepcopy(self._scanner.last_targets),
                           processing_sec=elapsed,dropped_frames=self._stats['dropped_frames'],semantic_fused=False,error=None)
                self._stats['processed_frames']+=1
                self._stats['tracking_total_sec']+=elapsed
                self._stats.update(generation=generation,session_id=session)
                with self._condition:
                    self._latest_packet=frozen
                    state.update(deepcopy(self._semantic_state))
                    if self._error:state.update(error=self._error,tracking_ok=False)
                    self._snapshot=state
                    now=time.monotonic()
                    submit=(submitted_rotation is None or _angle(self._scanner.rotation,submitted_rotation)>=8 or now-submitted_at>=.25)
                    if submit and observation.get('tracking_ok'):
                        if self._slot is not None:self._stats['semantic_replaced']+=1
                        self._slot=frozen
                        self._stats['semantic_queue_peak']=1
                        submitted_rotation=self._scanner.rotation.copy();submitted_at=now
                        self._condition.notify_all()
                if not observation.get('tracking_ok'):
                    self._save(frozen,self._scanner.annotate(image),observation,'tracking_failed',False)
                    self._fault(observation.get('reason','stream_tracking_lost'))
                self._stop.wait(max(0.,1./30.-(time.monotonic()-begun)))
        except Exception as exc:
            self._fault(f'stream_geometry:{type(exc).__name__}:{exc}')

    def _semantic_loop(self):
        unknown=0;next_allowed=0.
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
                scene=observe_scene(packet['image'])
                is_board=bool(scene.get('valid') and scene.get('scene')=='board')
                unknown=unknown+1 if scene.get('scene')=='unknown' else 0
                with self._lock:
                    self._scene=scene.get('scene','invalid');self._scene_valid=is_board
                    self._scene_time=packet['frame_time']
                if not is_board:observation=dict(tracking_ok=False,reason='scene_not_board')
                else:
                    revision=self._semantic.identity_corrections
                    observation=self._semantic.semantic_view(packet['image'],packet['frame_id'],packet['pose'])
                    if self._semantic.identity_corrections!=revision:
                        observation=dict(tracking_ok=False,reason='semantic_identity_revision_requires_rescan')
                artifact=dict(packet,fused_pose=self._semantic.pose_snapshot() if is_board else None)
                self._save(artifact,self._semantic.annotate(packet['image']),observation,scene.get('scene'),is_board)
                elapsed=time.monotonic()-begun
                self._stats['semantic_frames']+=1;self._stats['semantic_total_sec']+=elapsed
                self._stats['semantic_latency_total_sec']+=time.monotonic()-packet['frame_time']
                with self._lock:
                    self._semantic_state=self._result_state(self._semantic.result())
                    if is_board and observation.get('tracking_ok'):
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
                    if is_board:
                        self._mask_cache=dict(mask_observation=deepcopy(observation.get('mask_observation',[])),
                                              frame_time=packet['frame_time'])
                if not observation.get('tracking_ok') and not(scene.get('scene')=='unknown' and unknown<3):
                    self._fault(observation.get('reason','semantic_failed'))
                    return
                next_allowed=begun+.2
        except Exception as exc:
            self._fault(f'stream_semantic:{type(exc).__name__}:{exc}')

    def stop(self):
        """Join scanner owners, drain artifacts, then merge semantic evidence."""
        if self._closed:return
        self._stop.set()
        with self._condition:self._condition.notify_all()
        for thread in(self._thread,self._semantic_thread):
            if thread is not None and thread.ident is not None:thread.join()
        if self._correction_slot is not None:
            correction=self._correction_slot
            self._stats['pose_corrections_rejected']+=1
            self._stats['pose_correction_events'].append(dict(
                source_frame_id=correction.get('source_frame_id'),source_epoch=correction.get('source_epoch'),
                status='not_applied_after_stop'))
            self._correction_slot=None
        for field in('evidence','view_rotations','group','group_rotation','icon_anchors',
                     'best_known','stagnant_frames','identity_corrections'):
            setattr(self._scanner,field,deepcopy(getattr(self._semantic,field)))
        if self._latest_packet is not None:
            packet=self._latest_packet
            if not any(row['frame_id']==packet['frame_id'] for row in self._frames):
                try:self._save(packet,self._scanner.annotate(packet['image']),packet['observation'],'final',False)
                except Exception as exc:self._fault(f'final_frame:{exc}')
        if self._writer is not None and self._writer.ident is not None:
            self._writes.put(None);self._writer.join()
        self._frames.sort(key=lambda row:row['frame_id'])
        duration=max(.001,time.monotonic()-(self._stream_started or time.monotonic()))
        self._stats.update(error=self.error,duration_sec=duration,
                           track_fps=self._stats['processed_frames']/duration,
                           semantic_fps=self._stats['semantic_frames']/duration,
                           semantic_mean_latency_sec=self._stats['semantic_latency_total_sec']/max(1,self._stats['semantic_frames']))
        self._closed=True


__all__=['ScanVisionStream']
