"""Controller regressions for long routes at bounded semantic overlap speed."""
import math

import numpy as np

from plans.resonance_pc.src.actions._deep_dive_scan_policy import (
    CellScanPolicy, _matrix, _pose_angle,
)


AXES = {0: np.array([0., .003, 0.]), 1: np.array([.003, 0., 0.])}


def rows():
    return [dict(face=face, row=row, col=col, occupant='none',
                 occupant_status='confirmed', node_status='unknown', confidence=.9)
            for face in 'URFDLB' for row in range(3) for col in range(3)]


def route_policy():
    policy = CellScanPolicy()
    destination = _matrix(np.array([math.radians(110.), 0., 0.]))
    policy._route = policy._short_waypoints(np.eye(3), [destination])
    policy._final_rotation = destination
    policy._target_indices = [0]
    policy.target_face = 'U'
    policy._started = 0.
    return policy


def test_short_checkpoints_preserve_both_noncommuting_axis_legs():
    start = _matrix(np.array([.2, -.3, .1]))
    intermediate = _matrix(AXES[0]/.003*math.radians(110.)) @ start
    final = _matrix(AXES[1]/.003*math.radians(-75.)) @ intermediate
    route = CellScanPolicy._short_waypoints(start, [intermediate, final])
    assert any(np.array_equal(point, intermediate) for point in route)
    assert np.array_equal(route[-1], final)
    previous = start
    for point in route:
        assert _pose_angle(point, previous) <= math.radians(30.)+1e-8
        previous = point


def test_fresh_checkpoint_arrival_preserves_progress_past_original_deadline():
    policy = route_policy()
    first = policy._route[0].copy()
    policy.choose(first, rows(), AXES, observed_rotation=first, elapsed=6.8)
    assert policy._started == 6.8
    assert len(policy._route) == 3
    progressing = _matrix(np.array([math.radians(35.), 0., 0.]))
    policy.choose(progressing, rows(), AXES, observed_rotation=progressing, elapsed=7.2)
    assert policy._plan_serial == 0
    assert len(policy._route) == 3
    assert not policy.insufficient


def test_extrapolated_checkpoint_cannot_renew_approach_deadline():
    policy = route_policy()
    predicted = policy._route[0].copy()
    policy.choose(predicted, rows(), AXES, observed_rotation=np.eye(3), elapsed=6.8)
    assert policy._started == 0.
    assert len(policy._route) == 4


def test_unreached_checkpoint_retains_seven_second_watchdog():
    policy = route_policy()
    policy.choose(np.eye(3), rows(), AXES, observed_rotation=np.eye(3), elapsed=7.1)
    assert any(item['reason'] == 'pose_approach_timeout' for item in policy.insufficient)
    assert policy._plan_serial == 1


def test_batched_overlap_projection_matches_every_original_path_score():
    policy = CellScanPolicy()
    known = rows()
    for cell in known:
        cell['node_status'] = 'known'
    start = _matrix(np.array([-.4, .5, .1]))
    paths = []
    for angle in (-110., -75., -45., -22., 22., 45., 75., 110.):
        intermediate = _matrix(np.array([math.radians(angle), 0., 0.])) @ start
        for second in (-75., 0., 45.):
            final = _matrix(np.array([0., math.radians(second), 0.])) @ intermediate
            paths.append([intermediate, final] if second else [intermediate])
    original = np.array([min(1., max(.12, float(policy._anchor_support(
        np.array(path), known)[0].min())/4.)) for path in paths])
    assert np.array_equal(policy._route_anchor_support(paths, known), original)
