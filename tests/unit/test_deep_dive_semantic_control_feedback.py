"""A semantic publication must be reconsidered without another camera frame."""
import asyncio
import json
from types import SimpleNamespace
import time

import numpy as np
import pytest

from plans.resonance_pc.src.actions import consciousness_deep_dive_scan_pc_actions as scan


def test_fresh_semantic_revision_on_same_geometry_reaches_policy(tmp_path, monkeypatch):
    revisions = []
    class Policy:
        stats = {}
        def choose(self, *_args, **kwargs):
            revisions.append(kwargs['semantic_revision'])
            return dict(direction=None, phase='observe')
    policy = Policy()
    scene = dict(valid=True, scene='board', player_turn=True)
    snapshots = [dict(seq=1, semantic_revision=i, frame_time=time.monotonic(),
        published_at=time.monotonic(), frame_id=1, scene_observation=scene,
        geometry_tracking_ok=True, tracking_ok=True, rotation=np.eye(3),
        response_axes={0:np.array([0., .003, 0.]), 1:np.array([.003, 0., 0.])},
        quality=1., tvec=np.array([0., 0., 40.]), cells=[], known_cells=0)
        for i in (0, 1, 1)]
    ended = dict(snapshots[-1], error='end_of_test')
    class Stream:
        stats = {}
        def start(self): pass
        def stop(self): pass
        def snapshot(self): return snapshots.pop(0) if snapshots else ended
    monkeypatch.setattr(scan, 'ScanVisionStream', lambda *_args, **_kwargs: Stream())
    monkeypatch.setattr(scan, 'CellScanPolicy', lambda **_kwargs: policy)
    monkeypatch.setattr(scan, '_completion', lambda *_: (False, 'not_ready'))
    monkeypatch.setattr(scan, '_progress', lambda **_kwargs: None)
    app = SimpleNamespace(target_runtime=SimpleNamespace(_get_or_create_session=lambda: None))
    started = time.monotonic()
    status, reason, *_ = asyncio.run(scan._continuous_scan(app, None,
        SimpleNamespace(base_step_px=300, max_step_px=450), tmp_path, [], [],
        started, started+2., 10))
    assert revisions == [0, 1]
    assert (status, reason) == ('blocked', 'end_of_test')


def test_semantic_updates_cannot_renew_input_allowance_on_same_pose(tmp_path, monkeypatch):
    class Policy:
        stats = {}
        def choose(self, *_args, **_kwargs):
            return dict(direction=[1., 0.], distance_px=50.)
    class Controller:
        releases = 0
        async def mouse_down_async(self, *_args): pass
        async def mouse_up_async(self, *_args): self.releases += 1
    class App:
        controller = Controller()
        target_runtime = SimpleNamespace(_get_or_create_session=lambda: None)
        async def move_to_async(self, *_args, **_kwargs): pass
    began = time.monotonic()
    def snapshot(revision):
        return dict(seq=1, semantic_revision=revision, frame_time=began,
            published_at=began, frame_id=1,
            generation=1, session_id=7, map_revision=0,
            geometry_body_basis=np.eye(3),
            scene_observation=dict(valid=True, scene='board', player_turn=True),
            geometry_tracking_ok=True, tracking_ok=True, rotation=np.eye(3),
            response_axes={0:np.array([0., .003, 0.]), 1:np.array([.003, 0., 0.])},
            quality=1., tvec=np.array([0., 0., 40.]), cells=[], projected=[],
            targets=[], known_cells=0)
    packets = [snapshot(i) for i in range(24)]
    ended = dict(snapshot(24), error='end_of_test')
    class Stream:
        stats = {}
        def start(self): pass
        def stop(self): pass
        def snapshot(self): return packets.pop(0) if packets else ended
    monkeypatch.setattr(scan, 'ScanVisionStream', lambda *_args, **_kwargs: Stream())
    monkeypatch.setattr(scan, 'CellScanPolicy', lambda **_kwargs: Policy())
    monkeypatch.setattr(scan, '_completion', lambda *_: (False, 'not_ready'))
    monkeypatch.setattr(scan, '_progress', lambda **_kwargs: None)
    monkeypatch.setattr(scan, '_gesture_path', lambda *_: ((640, 330), (900, 330)))
    actions = []
    app = App()
    asyncio.run(scan._continuous_scan(app, None,
        SimpleNamespace(base_step_px=300, max_step_px=450), tmp_path, [], actions,
        began, began+2., 10))
    assert len(actions) == 1
    assert 0 < actions[0]['executed_distance_px'] <= 50.
    assert app.controller.releases == 1
    diagnostic = json.loads((tmp_path/'feedback_diagnostic.json').read_text('utf8'))
    journal = diagnostic['input_journal']
    inputs = journal['records']['input']
    assert journal['diagnostics_only'] and not any(journal['truncated'].values())
    assert len(inputs) == actions[0]['input_ticks'] > 0
    assert all(row['completed_at'] >= row['before_at'] and row['displacement'][1] == 0 for row in inputs)
    assert sum(row['displacement'][0] for row in inputs) == inputs[-1]['cumulative'][0]
    assert inputs[-1]['cumulative'][0] == actions[0]['executed_distance_px']
    assert len(journal['records']['geometry']) == 1  # Same geometry, multiple semantic revisions.
    assert journal['records']['geometry'][0]['base_rotation'] == np.eye(3).tolist()
    drained = json.loads((tmp_path/'stream_statistics_drained.json').read_text('utf8'))
    assert drained['feedback_diagnostic']['input_journal'] == journal
    assert diagnostic['scan_started_at'] == began


def test_cancel_check_still_persists_costs_after_stream_owner_drains(tmp_path, monkeypatch):
    stopped = []
    class Stream:
        def start(self): pass
        def stop(self): stopped.append(True)
        @property
        def stats(self):
            assert stopped
            return {'semantic_frames': 5, 'semantic_total_sec': .75}
    monkeypatch.setattr(scan, 'ScanVisionStream', lambda *_args, **_kwargs: Stream())
    monkeypatch.setattr(scan, 'CellScanPolicy', lambda **_kwargs: SimpleNamespace(stats={}))
    def cancel():
        raise asyncio.CancelledError()
    monkeypatch.setattr(scan, '_cancel_check', cancel)
    app = SimpleNamespace(target_runtime=SimpleNamespace(_get_or_create_session=lambda:None))
    began = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scan._continuous_scan(app, None,
            SimpleNamespace(base_step_px=300, max_step_px=450), tmp_path, [], [],
            began, began+1., 10))
    recorded = json.loads((tmp_path/'stream_statistics_drained.json').read_text('utf8'))
    assert recorded['semantic_frames'] == 5 and recorded['semantic_total_sec'] == .75
    assert recorded['feedback_diagnostic']['input_journal']['counts'] == {'input':0, 'geometry':0}
