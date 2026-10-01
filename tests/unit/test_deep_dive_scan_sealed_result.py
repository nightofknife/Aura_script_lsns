from copy import deepcopy
import threading
import time

import pytest

from plans.resonance_pc.src.actions._deep_dive_scan_stream import ScanVisionStream


def stream():
    owner = object.__new__(ScanVisionStream)
    now = time.monotonic()
    owner._lock = threading.Lock()
    owner._error = None
    owner._semantic_state = {'fusion_paused': False}
    owner._scene_valid = True
    owner._scene_time = now
    owner._snapshot = dict(frame_time=now, geometry_tracking_ok=True, session_id=7, map_revision=2)
    owner._semantic_result = dict(cells=[{'occupant': 'player'}], semantic_source=dict(
        frame_id=40, frame_time=now, session_id=7, map_revision=2, pose={'rotation': 'frozen_pose'}))
    owner._sealed_result = None
    return owner


def test_ready_source_is_deeply_frozen_before_later_packet_drain():
    owner = stream()
    expected = deepcopy(owner._semantic_result)
    assert owner.seal_result_if(lambda result: result['cells'][0]['occupant'] == 'player')
    owner._semantic_result['cells'][0]['occupant'] = 'unknown'
    owner._semantic_result['semantic_source']['pose']['rotation'] = 'newer_pose'
    assert owner._sealed_result == expected
    assert owner.seal_result_if(lambda _: True)
    assert owner._sealed_result == expected


@pytest.mark.parametrize('cause', ['error', 'scene_stale', 'pose_stale', 'semantic_stale', 'session_changed', 'map_changed', 'fusion_paused'])
def test_seal_never_accepts_stale_or_cross_session_evidence(cause):
    owner = stream()
    if cause == 'error': owner._error = 'tracking_failed'
    if cause == 'scene_stale': owner._scene_time -= 1
    if cause == 'pose_stale': owner._snapshot['frame_time'] -= 1
    if cause == 'semantic_stale': owner._semantic_result['semantic_source']['frame_time'] -= 1
    if cause == 'session_changed': owner._snapshot['session_id'] = 8
    if cause == 'map_changed': owner._snapshot['map_revision'] = 3
    if cause == 'fusion_paused': owner._semantic_state['fusion_paused'] = True
    assert not owner.seal_result_if(lambda _: True)
    assert owner._sealed_result is None
