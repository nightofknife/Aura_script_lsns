"""Recovery proves the current pose, rather than arrival at a historical goal."""
import math

import numpy as np
import pytest

from test_deep_dive_multiface_recovery_policy import AXES, enter_recovery, feedback
from plans.resonance_pc.src.actions._deep_dive_scan_policy import _matrix


def test_new_current_source_recovers_before_historical_goal(monkeypatch):
    policy, atlas, calls = enter_recovery(monkeypatch)
    actual = _matrix(np.array([0., math.radians(9.), 0.]))
    proof = feedback(4, 102., faces={'D':4, 'L':2}, rotation=actual)
    policy.choose(actual, atlas, AXES, elapsed=2., anchor_feedback=proof)
    assert policy._anchor_recovery is None
    assert policy.stats['multi_face_recoveries'] == 1
    assert len(calls) == 2
    assert atlas[26]['occupant'] == 'unknown'


@pytest.mark.parametrize('defect', ['timestamp', 'old', 'frame', 'single', 'far', 'paused', 'stale'])
def test_navigation_goal_does_not_bypass_current_source_proof(monkeypatch, defect):
    policy, atlas, _ = enter_recovery(monkeypatch)
    actual = _matrix(np.array([0., math.radians(9.), 0.]))
    proof = feedback(4, 102., faces={'D':4, 'L':2}, rotation=actual)
    if defect == 'timestamp':
        proof['glyph_anchor_at'] = 103.
    elif defect == 'old':
        proof = feedback(rotation=actual)
    elif defect == 'frame':
        proof['accepted_anchor_observation']['frame_id'] = 99
    elif defect == 'single':
        proof = feedback(4, 102., faces={'D':7}, rotation=actual)
    elif defect == 'far':
        proof = feedback(4, 102., faces={'D':4, 'L':2}, rotation=np.eye(3))
    elif defect == 'paused':
        proof['fusion_paused'] = True
    elif defect == 'stale':
        proof['glyph_anchor_age_sec'] = .9
    begun = policy._anchor_recovery['started']
    policy.choose(actual, atlas, AXES, elapsed=2., anchor_feedback=proof)
    assert policy._anchor_recovery is not None
    assert policy._anchor_recovery['started'] == begun
    result = policy.choose(actual, atlas, AXES, elapsed=6., anchor_feedback=proof)
    assert result['reason'] == 'anchor_recovery_failed'
    assert policy._anchor_recovery['started'] == begun


def test_current_source_exit_transforms_noncommuting_body_basis(monkeypatch):
    policy, atlas, _ = enter_recovery(monkeypatch)
    physical = _matrix(np.array([0., math.radians(9.), 0.]))
    source_basis = _matrix(np.array([.02, -.01, .03]))
    current_basis = source_basis @ _matrix(np.array([-.015, .025, 0.]))
    proof = feedback(4, 102., faces={'D':4, 'L':2}, basis=current_basis,
                     source_basis=source_basis, rotation=physical @ source_basis)
    policy.choose(physical @ current_basis, atlas, AXES, elapsed=2., anchor_feedback=proof)
    assert policy._anchor_recovery is None
    assert policy.stats['multi_face_recoveries'] == 1
