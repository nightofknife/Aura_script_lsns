import math
import time
from threading import Event

import numpy as np

from plans.resonance_pc.src.actions._deep_dive_scan_policy import CellScanPolicy, _matrix
from test_deep_dive_scan_stream_pages import make_stream


def cells():
    return [dict(face=face, row=row, col=col, occupant='none',
                 occupant_status='confirmed', node_status='unknown', confidence=.9)
            for face in 'URFDLB' for row in range(3) for col in range(3)]


AXES = {0: np.array([0., .003, 0.]), 1: np.array([.003, 0., 0.])}


def test_loss_returns_to_verified_pose_and_old_pose_cannot_finish_recovery():
    policy = CellScanPolicy()
    rows = cells()
    prior = np.eye(3)
    observed = _matrix(np.array([0., math.radians(25.), 0.]))
    feedback = dict(glyph_anchor_at=100., glyph_anchor_age_sec=1.4,
                    glyph_anchor_rotation=prior.tolist(),
                    refine_diagnostic=dict(source_frame_id=1,renewed=False))
    policy.choose(observed, rows, AXES, elapsed=1.8, anchor_feedback=feedback)
    feedback['refine_diagnostic']['source_frame_id']=2
    choice = policy.choose(observed, rows, AXES, elapsed=2., anchor_feedback=feedback)
    assert choice['phase'] == 'anchor_recovery'
    assert choice['direction'][0] < 0
    choice = policy.choose(prior, rows, AXES, elapsed=2.2, anchor_feedback=feedback)
    assert choice['reason'] == 'await_current_anchor_evidence'
    assert policy._anchor_recovery is not None
    feedback.update(glyph_anchor_at=101., glyph_anchor_age_sec=.2)
    policy.choose(prior, rows, AXES, elapsed=2.3, anchor_feedback=feedback)
    assert policy._anchor_recovery is None


def test_recovery_stops_when_new_images_cannot_renew_support():
    policy = CellScanPolicy()
    rows = cells()
    observed = _matrix(np.array([0., .4, 0.]))
    feedback = dict(glyph_anchor_at=100., glyph_anchor_age_sec=1.4,
                    glyph_anchor_rotation=np.eye(3).tolist(),
                    refine_diagnostic=dict(source_frame_id=1,renewed=False))
    policy.choose(observed, rows, AXES, elapsed=1.8, anchor_feedback=feedback)
    feedback['refine_diagnostic']['source_frame_id']=2
    policy.choose(observed, rows, AXES, elapsed=2., anchor_feedback=feedback)
    choice = policy.choose(observed, rows, AXES, elapsed=6.1, anchor_feedback=feedback)
    assert choice['direction'] is None
    assert choice['reason'] == 'anchor_recovery_failed'


def test_successful_current_fit_is_not_mistaken_for_failed_support():
    policy=CellScanPolicy()
    feedback=dict(glyph_anchor_at=100.,glyph_anchor_age_sec=1.4,
                  glyph_anchor_rotation=np.eye(3).tolist(),
                  refine_diagnostic=dict(source_frame_id=1,renewed=True))
    for frame in range(1,4):
        feedback['refine_diagnostic']['source_frame_id']=frame
        policy.choose(np.eye(3),cells(),AXES,elapsed=2.+frame*.1,anchor_feedback=feedback)
        assert policy._anchor_recovery is None


def test_anchor_age_advances_even_when_semantic_thread_has_not_finished(tmp_path):
    stream, entered, release = make_stream(tmp_path, lambda image: dict(valid=True, scene='board'))
    with stream._lock:
        stream._semantic_state.update(glyph_anchor_at=time.monotonic()-1.9,
                                      glyph_anchor_map_revision=0)
        stream._snapshot['map_revision'] = 0
    first = stream.snapshot()['glyph_anchor_age_sec']
    time.sleep(.15)
    second = stream.snapshot()
    assert second['glyph_anchor_age_sec'] >= first+.13
    assert second['fusion_paused']


def test_old_map_anchor_is_not_a_recovery_destination(tmp_path):
    stream, _, _ = make_stream(tmp_path, lambda image: dict(valid=True, scene='board'))
    with stream._lock:
        stream._snapshot['map_revision'] = 1
        stream._semantic_state['glyph_anchor_map_revision'] = 0
        stream._semantic_state['glyph_anchor_rotation'] = np.eye(3).tolist()
    assert stream.snapshot()['glyph_anchor_rotation'] is None
