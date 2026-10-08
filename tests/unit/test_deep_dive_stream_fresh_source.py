"""Inline source checks cannot freshen old pixels or authorize page/input gates."""
from copy import deepcopy
import hashlib

import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_scan_stream as streams
from plans.resonance_pc.src.actions._deep_dive_layout_vision import LayoutScanner


def board():
    return dict(valid=True,scene='board',player_turn=True,enemy_turn=False,
                unknown_modal_evidence=False,plane_evidence=dict(plane_index=1))


@pytest.fixture
def stream(tmp_path,monkeypatch):
    scanner=LayoutScanner();scanner.ready=True;scanner.quality=1.
    obj=streams.ScanVisionStream(scanner,None,tmp_path,[],100.,
        observe_fn=streams.scan_scene.observe,refresh_semantic_source=True)
    monkeypatch.setattr(streams.time,'monotonic',lambda:100.4)
    monkeypatch.setattr(streams.scan_scene,'_player_board',lambda rgb:board())
    obj._snapshot.update(session_id=7,map_revision=0)
    return obj


def packet(stream,frame,*,checked=False):
    source=dict(frame_id=frame,generation=frame,session_id=7,frame_time=100.+frame*.01,
        map_revision=0,capture_backend='wgc',image=np.full((720,1280,3),frame,np.uint8),
        pose=stream._scanner.pose_snapshot(),geometry_body_basis=np.eye(3),
        observation=dict(tracking_ok=True))
    if checked:source['scene_observation']=dict(board(),old_scene_marker=frame)
    return source


def test_latest_packet_replaces_entire_source_and_checks_its_actual_rgb(stream,monkeypatch):
    old=packet(stream,1,checked=True);latest=packet(stream,2);seen=[]
    stream._latest_packet=latest
    monkeypatch.setattr(streams.scan_scene,'_player_board',lambda rgb:seen.append(rgb is latest['image']) or board())
    chosen,diag=stream._fresh_semantic_packet(old)
    assert seen==[True] and diag['status']=='refreshed'
    for key in ('image','pose','geometry_body_basis','observation'):assert chosen[key] is latest[key]
    for key in ('frame_id','generation','session_id','frame_time','map_revision','capture_backend'):assert chosen[key]==latest[key]
    assert 'old_scene_marker' not in chosen['scene_observation']
    assert diag['selected_rgb_sha256']==hashlib.sha256(latest['image'].tobytes()).hexdigest()
    assert stream._scene_valid is False and stream._scene_observation is None
    assert stream._semantic_revision==0 and stream._last_consumed_semantic_source is None


@pytest.mark.parametrize('enabled,observer',[(False,streams.scan_scene.observe),(True,lambda rgb:board())])
def test_default_or_custom_observer_keeps_original_checked_packet(tmp_path,enabled,observer):
    scanner=LayoutScanner();scanner.ready=True
    obj=streams.ScanVisionStream(scanner,None,tmp_path,[],100.,observe_fn=observer,refresh_semantic_source=enabled)
    old=packet(obj,1,checked=True);obj._latest_packet=packet(obj,2)
    chosen,diag=obj._fresh_semantic_packet(old)
    assert chosen is old and diag['status']=='disabled'


@pytest.mark.parametrize('fault',('session','map','generation','frame','time','backend','tracking','rotation','basis','translation','epoch','rgb'))
def test_invalid_or_nonnew_latest_never_substitutes_fields_into_checked_packet(stream,fault):
    old=packet(stream,1,checked=True);latest=packet(stream,2)
    if fault=='session':latest['session_id']=8
    elif fault=='map':latest['map_revision']=1
    elif fault=='generation':latest['generation']=1
    elif fault=='frame':latest['frame_id']=True
    elif fault=='time':latest['frame_time']=float('nan')
    elif fault=='backend':latest['capture_backend']='printwindow'
    elif fault=='tracking':latest['observation']['tracking_ok']=False
    elif fault=='rotation':latest['pose']['rotation']=np.zeros((3,3))
    elif fault=='basis':latest['geometry_body_basis']=np.zeros((3,3))
    elif fault=='translation':latest['pose']['tvec']=np.array([0.,0.,float('inf')])
    elif fault=='epoch':latest['pose']['correction_epoch']=False
    elif fault=='rgb':latest['image']=latest['image'][:1]
    stream._latest_packet=latest
    chosen,diag=stream._fresh_semantic_packet(old)
    assert chosen is old and diag['status']=='checked_source'


@pytest.mark.parametrize('field,value',[('scene','enemy_turn'),('valid',False),('player_turn',False),('enemy_turn',True),('unknown_modal_evidence',True)])
def test_inline_nonboard_saves_only_real_new_source_and_never_fuses(stream,monkeypatch,field,value):
    old=packet(stream,1,checked=True);latest=packet(stream,2);stream._latest_packet=latest;saved=[]
    monkeypatch.setattr(streams.scan_scene,'_player_board',lambda rgb:dict(board(),**{field:value}))
    monkeypatch.setattr(stream,'_save',lambda *args:saved.append(args))
    chosen,diag=stream._fresh_semantic_packet(old)
    assert chosen is None and diag['status']=='inline_scene_rejected'
    assert len(saved)==1 and saved[0][0] is latest and saved[0][-1] is False
    assert stream._semantic_revision==0 and stream._last_consumed_semantic_source is None
    assert stream._scene_valid is False


def test_scene_owner_submission_of_already_consumed_source_cannot_vote_again(stream):
    old=packet(stream,1,checked=True);latest=packet(stream,2);stream._latest_packet=latest
    chosen,_=stream._fresh_semantic_packet(old)
    stream._last_consumed_semantic_source=stream._semantic_packet_source(chosen)
    checked=dict(latest,scene_observation=board())
    assert stream._fresh_semantic_packet(checked)[0] is None
    assert stream._fresh_semantic_packet(old)[0] is None


@pytest.mark.parametrize('when',('before','inline','hud'))
def test_stop_prevents_admission_of_new_model(stream,monkeypatch,when):
    old=packet(stream,1,checked=True);stream._slot=old;stream._latest_packet=packet(stream,2)
    monkeypatch.setattr(stream._semantic,'semantic_view',lambda *args,**kwargs:pytest.fail('new model admitted after stop'))
    if when=='before':stream._stop.set()
    elif when=='inline':
        def observe(rgb):stream._stop.set();return board()
        monkeypatch.setattr(streams.scan_scene,'_player_board',observe)
    else:
        def prepare(pkt):stream._stop.set();return dict(ready=False)
        monkeypatch.setattr(stream._target_explainer,'prepare',prepare)
    stream._semantic_loop()
    assert stream._last_consumed_semantic_source is None
    assert stream._stats['semantic_frames']==0


def test_context_change_during_inline_rejects_before_hud_or_model(stream,monkeypatch):
    old=packet(stream,1,checked=True);stream._latest_packet=packet(stream,2)
    def observe(rgb):stream._snapshot['session_id']=8;return board()
    monkeypatch.setattr(streams.scan_scene,'_player_board',observe)
    chosen,diag=stream._fresh_semantic_packet(old)
    assert chosen is None and diag['status']=='source_context_changed_during_inline'


def test_substituted_public_observer_does_not_enable_inline(tmp_path,monkeypatch):
    custom=lambda rgb:board()
    monkeypatch.setattr(streams.scan_scene,'observe',custom)
    scanner=LayoutScanner();scanner.ready=True
    obj=streams.ScanVisionStream(scanner,None,tmp_path,[],100.,observe_fn=custom,refresh_semantic_source=True)
    assert obj._refresh_semantic_source is False


@pytest.mark.parametrize('coverage', [None, {'model_executed': False, 'coverage_valid': True},
                                    {'model_executed': True, 'coverage_valid': False},
                                    {'model_executed': True, 'coverage_valid': True,
                                     'frame_id': 2, 'frame_time': 100.02, 'map_revision': 0}])
def test_successful_pipeline_publishes_and_saves_the_new_source_only(stream,monkeypatch,coverage):
    old=packet(stream,1,checked=True);latest=packet(stream,2);stream._slot=old;stream._latest_packet=latest
    seen=[];saved=[]
    def prepare(pkt):
        assert pkt['image'] is latest['image'] and pkt['scene_observation']['scene']=='board'
        return dict(ready=False,reason='fixture_no_hud_vote')
    def semantic(rgb,frame_id,pose,*,source_frame_time):
        seen.append((rgb is latest['image'],frame_id,pose is latest['pose'],source_frame_time))
        stream._semantic.rvec=np.asarray(pose['rvec']).copy()
        stream._semantic.tvec=np.asarray(pose['tvec']).copy()
        stream._semantic.rotation=np.asarray(pose['rotation']).copy()
        stream._semantic.last_fused_result=stream._semantic.result()
        return dict(tracking_ok=True,frame_id=frame_id,target_coverage=coverage)
    def save(pkt,*args):
        saved.append(pkt);stream._stop.set()
    monkeypatch.setattr(stream._target_explainer,'prepare',prepare)
    monkeypatch.setattr(stream._semantic,'semantic_view',semantic)
    monkeypatch.setattr(stream._semantic,'annotate',lambda rgb,**kwargs:rgb.copy())
    monkeypatch.setattr(stream,'_save',save)
    stream._semantic_loop()
    assert seen==[(True,2,True,latest['frame_time'])]
    source=stream._semantic_result['semantic_source']
    for key in ('frame_id','generation','session_id','frame_time','map_revision','capture_backend'):assert source[key]==latest[key]
    assert source['source_refresh']['selected_rgb_sha256']==hashlib.sha256(latest['image'].tobytes()).hexdigest()
    assert source['cost_sec']['inline_scene']>=0.
    assert stream.snapshot()['semantic_metadata']['pose'] == source['pose']
    assert source['pose']['tvec'] == latest['pose']['tvec'].tolist()
    copied = stream.snapshot()
    copied['semantic_metadata']['pose']['tvec'][0] = 999.
    assert stream.snapshot()['semantic_metadata']['pose'] == source['pose']
    assert len(saved)==1 and saved[0]['image'] is latest['image']
    assert np.array_equal(saved[0]['geometry_body_basis'],latest['geometry_body_basis'])
    assert stream._last_consumed_semantic_source['frame_id']==2
    assert stream._scene_valid is False # Inline proof did not authorize page/input.
    assert stream.snapshot()['target_coverage'] == (coverage or {})
    if coverage:
        # Caller mutation cannot turn a stored failed model opportunity into
        # successful coverage or change its real source identity.
        before = deepcopy(coverage)
        coverage.clear()
        assert stream.snapshot()['target_coverage'] == before
        public = stream.snapshot()
        public['target_coverage'].clear()
        assert stream.snapshot()['target_coverage'] == before
