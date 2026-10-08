"""Late genuine pose support stops search without acknowledging recovery."""
import math

import numpy as np
import pytest

from test_deep_dive_multiface_recovery_policy import AXES, enter_recovery, feedback
from plans.resonance_pc.src.actions._deep_dive_scan_policy import _matrix


def test_late_current_multiface_support_holds_until_fresh_source(monkeypatch):
    policy, atlas, _ = enter_recovery(monkeypatch)
    proof = feedback(4, 102., faces={'D':4, 'L':2}, age=1.)
    policy.choose(np.eye(3), atlas, AXES, elapsed=2., anchor_feedback=proof)
    recovery = policy._anchor_recovery
    begun, goal = recovery['started'], recovery['goal'].copy()
    result = policy.choose(np.eye(3), atlas, AXES, elapsed=2.7, anchor_feedback=proof)
    assert result['reason'] == 'await_fresh_current_anchor_evidence'
    assert result['direction'] is None
    assert policy._anchor_recovery is recovery
    assert recovery['started'] == begun and recovery['attempt'] == 0
    assert np.allclose(recovery['goal'], goal)
    fresh = feedback(5, 103., faces={'D':4, 'L':2}, age=.2)
    policy.choose(np.eye(3), atlas, AXES, elapsed=2.8, anchor_feedback=fresh)
    assert policy._anchor_recovery is None
    assert policy.stats['multi_face_recoveries'] == 1
    assert atlas[26]['occupant'] == 'unknown'


def test_wait_does_not_extend_total_recovery_deadline(monkeypatch):
    policy, atlas, _ = enter_recovery(monkeypatch)
    proof = feedback(4, 102., faces={'D':4, 'L':2}, age=1.)
    policy.choose(np.eye(3), atlas, AXES, elapsed=2., anchor_feedback=proof)
    begun = policy._anchor_recovery['started']
    for at in (2.7, 3.7, 5.7):
        result = policy.choose(np.eye(3), atlas, AXES, elapsed=at, anchor_feedback=proof)
        assert result['reason'] == 'await_fresh_current_anchor_evidence'
        assert policy._anchor_recovery['started'] == begun
    result = policy.choose(np.eye(3), atlas, AXES, elapsed=5.81, anchor_feedback=proof)
    assert result['reason'] == 'anchor_recovery_failed'
    assert policy._anchor_recovery['attempt'] == 0


@pytest.mark.parametrize('defect', ['single', 'pose', 'fit', 'paused', 'ancient', 'timestamp'])
def test_unsupported_or_wrong_pose_still_searches(monkeypatch, defect):
    policy, atlas, _ = enter_recovery(monkeypatch)
    searched = []
    def search(*args, **kwargs):
        searched.append(True)
        return _matrix(np.array([0., math.radians(12.), 0.]))
    monkeypatch.setattr(policy, '_recovery_search_goal', search)
    proof = feedback(4, 102., faces={'D':4, 'L':2}, age=1.)
    if defect == 'single':
        proof = feedback(4, 102., faces={'D':7}, age=1.)
    elif defect == 'pose':
        proof = feedback(4, 102., faces={'D':4, 'L':2}, age=1.,
                         rotation=_matrix(np.array([0., math.radians(5.), 0.])))
    elif defect == 'fit':
        proof['refine_diagnostic']['renewed'] = False
    elif defect == 'paused':
        proof['fusion_paused'] = True
    elif defect == 'ancient':
        proof['glyph_anchor_age_sec'] = 2.
    elif defect == 'timestamp':
        proof['glyph_anchor_at'] = 103.
    policy.choose(np.eye(3), atlas, AXES, elapsed=2., anchor_feedback=proof)
    result = policy.choose(np.eye(3), atlas, AXES, elapsed=2.7, anchor_feedback=proof)
    assert result.get('reason') != 'await_fresh_current_anchor_evidence'
    assert searched == [True]
    assert policy._anchor_recovery['attempt'] == 1
