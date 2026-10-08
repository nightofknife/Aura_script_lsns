"""Face navigation uses the public model scan without replacing readiness."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
import time

import numpy as np
import pytest

from plans.resonance_pc.src.actions import consciousness_deep_dive_scan_pc_actions as scan
from tools.deep_dive_live_acceptance import validate_config


@pytest.mark.parametrize('route', ['target_faces', 'target_framed'])
def test_target_face_route_keeps_real_target_readiness(monkeypatch, route):
    calls = []
    def readiness(result, expected):
        calls.append((result, expected))
        return dict(ready=False, reason='missing_singularity')
    monkeypatch.setattr(scan, 'targets_readiness', readiness)
    source = dict(cells=[], marker='unresolved real scan')
    before = deepcopy(source)
    token = scan._SCAN_CONTROL.set(dict(scan_route=route, recognition_goal='targets',
                                      expected_inspirations=2,
                                      mixed_policy=SimpleNamespace(completed_faces=set('URFDLB'))))
    try:
        assert scan._completion(source) == (False, 'missing_singularity')
        assert calls[0][1] == 2
        assert calls[0][0]['cells'] == source['cells']
        assert source == before
    finally:
        scan._SCAN_CONTROL.reset(token)


@pytest.mark.parametrize('route', ['target_faces', 'target_framed'])
def test_target_face_route_requires_model_and_explicit_target_goal(route):
    with pytest.raises(ValueError, match='model target scan'):
        asyncio.run(scan.run_layout_scan(object(), scan_route=route, entity_detector=object()))
    with pytest.raises(ValueError, match='entity model'):
        asyncio.run(scan.run_layout_scan(object(), scan_route=route, recognition_goal='targets'))
    value = dict(opencv_threads=1,
        scan_inputs=dict(recognition_goal='targets', scan_route=route, time_budget_sec=90),
        entity_detector={'execution_provider': 'dml_worker'},
        runtime_assertions={'entity_provider': 'DmlExecutionProvider'})
    assert validate_config(value) == value


@pytest.mark.parametrize('route,module_name,class_name', [
    ('target_faces', '_deep_dive_target_face_scan_policy', 'TargetFacePageScanPolicy'),
    ('target_framed', '_deep_dive_target_framed_scan_policy', 'TargetFramedCellScanPolicy'),
])
def test_continuous_owner_selects_face_policy_and_drains_without_game_input(tmp_path, monkeypatch,
                                                                          route, module_name, class_name):
    import importlib
    page = importlib.import_module('plans.resonance_pc.src.actions.'+module_name)
    selected = []
    class Policy:
        stats = {}
        def choose(self, *args, **kwargs):
            selected.append(kwargs['semantic_revision'])
            return dict(direction=None, phase='observe')
    monkeypatch.setattr(page, class_name, lambda **kwargs: Policy())
    now = time.monotonic()
    packet = dict(seq=1, semantic_revision=7, frame_time=now, published_at=now,
        frame_id=1, scene_observation=dict(valid=True, scene='board', player_turn=True),
        geometry_tracking_ok=True, tracking_ok=True, rotation=np.eye(3),
        response_axes={0: np.array([0., .003, 0.]), 1: np.array([.003, 0., 0.])},
        quality=1., tvec=np.array([0., 0., 40.]), cells=[], known_cells=0)
    packets = [packet]
    stopped = []
    class Stream:
        stats = {}
        def start(self): pass
        def stop(self): stopped.append(True)
        def snapshot(self):
            return packets.pop(0) if packets else dict(packet, error='test_finished')
    monkeypatch.setattr(scan, 'ScanVisionStream', lambda *args, **kwargs: Stream())
    monkeypatch.setattr(scan, '_completion', lambda result: (False, 'not_ready'))
    monkeypatch.setattr(scan, '_progress', lambda **kwargs: None)
    app = SimpleNamespace(target_runtime=SimpleNamespace(_get_or_create_session=lambda: None))
    token = scan._SCAN_CONTROL.set(dict(scan_route=route, recognition_goal='targets',
                                      observe_fn=None))
    try:
        status, reason, *_ = asyncio.run(scan._continuous_scan(app, None,
            SimpleNamespace(base_step_px=300, max_step_px=450), tmp_path, [], [], now, now+2., 10))
    finally:
        scan._SCAN_CONTROL.reset(token)
    assert selected == [7]
    assert stopped == [True]
    assert (status, reason) == ('blocked', 'test_finished')
