"""Navigation history is not a new image observation or an arrival ack."""
import math

import numpy as np

from plans.resonance_pc.src.actions._deep_dive_scan_policy import CellScanPolicy, _matrix


AXES = {0: np.array([0., .003, 0.]), 1: np.array([.003, 0., 0.])}


def atlas():
    return [dict(face=face, row=row, col=col, occupant='unknown', occupant_status='unknown',
                 node_status='unknown', confidence=0., evidence=[])
            for face in 'URFDLB' for row in range(3) for col in range(3)]


def source(frame, *, pose=None, basis=None, faces=None, confirmed=None, candidates=None):
    pose = np.eye(3) if pose is None else pose
    basis = np.eye(3) if basis is None else basis
    faces = {'U': 5, 'L': 3} if faces is None else faces
    confirmed = faces if confirmed is None else confirmed
    candidates = faces if candidates is None else candidates
    return dict(session_id=7, map_revision=0, semantic_revision=frame,
        geometry_body_basis=basis.tolist(), glyph_anchor_at=100.+frame,
        glyph_anchor_age_sec=.2, glyph_anchor_frame_id=frame, glyph_anchor_map_revision=0,
        glyph_anchor_rotation=(pose @ basis).tolist(),
        refine_diagnostic=dict(source_frame_id=frame, source_frame_time=100.+frame,
            source_map_revision=0, renewed=True, accepted_faces=faces,
            confirmed_faces=confirmed, candidate_faces=candidates),
        accepted_anchor_observation=dict(frame_id=frame, frame_time=100.+frame,
            session_id=7, map_revision=0, rotation=(pose @ basis).tolist(),
            body_basis=basis.tolist(), accepted_faces=faces))


def remember(policy, feedback):
    return policy._support_feedback(feedback, atlas(), float(feedback['semantic_revision']))[1]


def test_new_only_side_does_not_replace_known_multiface_pose():
    policy = CellScanPolicy(recognition_goal='targets')
    remember(policy, source(1))
    thin = source(2, pose=_matrix(np.array([0., .02, 0.])), faces={'L': 7, 'B': 2},
                  confirmed={'L': 6}, candidates={'L': 7, 'B': 2})
    remember(policy, thin)
    assert policy._multi_face_anchor['frame_id'] == 2
    assert policy._multi_face_anchor['known_faces'] == {'L': 6, 'B': 0}
    assert len(policy._support_bank) == 1
    assert policy._support_bank[0]['frame_id'] == 1
    assert policy._recovery_support(np.eye(3), np.eye(3))['frame_id'] == 1


def test_known_intersection_is_lower_bound_not_minimum_of_counts():
    policy = CellScanPolicy(recognition_goal='targets')
    entry = remember(policy, source(1, faces={'L': 4, 'B': 4},
        confirmed={'L': 6, 'B': 3}, candidates={'L': 7, 'B': 7}))
    assert entry['known_faces'] == {'L': 3, 'B': 0}
    assert policy._recovery_support(np.eye(3), np.eye(3)) is None


def test_known_only_fit_can_certify_known_support_without_candidate_counts():
    policy = CellScanPolicy(recognition_goal='targets')
    feedback = source(1)
    feedback['refine_diagnostic'].update(reason='known_multi_face_joint_fit')
    feedback['refine_diagnostic'].pop('confirmed_faces')
    feedback['refine_diagnostic'].pop('candidate_faces')
    remember(policy, feedback)
    assert policy._recovery_support(np.eye(3), np.eye(3))['known_faces'] == {'U': 5, 'L': 3}


def test_bank_is_bounded_and_retains_strong_history_during_thin_views():
    policy = CellScanPolicy(recognition_goal='targets')
    remember(policy, source(1))
    for frame in range(2, 32):
        remember(policy, source(frame, pose=_matrix(np.array([0., frame*.11, 0.])),
            faces={'L': 7, 'B': 2}, confirmed={'L': 6}, candidates={'L': 7, 'B': 2}))
    assert len(policy._support_bank) <= 12
    assert any(item['frame_id'] == 1 for item in policy._support_bank)


def test_bank_retains_recent_supported_neighbourhood_not_only_old_broad_views():
    policy = CellScanPolicy(recognition_goal='targets')
    remember(policy, source(1))
    for frame in range(2, 22):
        remember(policy, source(frame, pose=_matrix(np.array([0., frame*.13, 0.])),
                                faces={'U': 4, 'L': 2}))
    assert len(policy._support_bank) <= 12
    assert any(item['frame_id'] == 21 for item in policy._support_bank)


def test_nearest_reachable_known_source_wins_over_latest_thin_source():
    policy = CellScanPolicy(recognition_goal='targets')
    remember(policy, source(1))
    nearby = _matrix(np.array([0., math.radians(12.), 0.]))
    remember(policy, source(2, pose=nearby))
    remember(policy, source(3, pose=_matrix(np.array([0., math.radians(18.), 0.])),
        faces={'L': 7, 'B': 2}, confirmed={'L': 6}, candidates={'L': 7, 'B': 2}))
    observed = _matrix(np.array([0., math.radians(17.), 0.]))
    assert policy._recovery_support(observed, np.eye(3))['frame_id'] == 2
    assert policy._recovery_support(observed, np.eye(3), [nearby])['frame_id'] == 1
    far = _matrix(np.array([0., math.radians(60.), 0.]))
    assert policy._recovery_support(far, np.eye(3)) is None


def test_noncommuting_body_basis_changes_only_navigation_coordinates():
    policy = CellScanPolicy(recognition_goal='targets')
    physical = _matrix(np.array([.2, -.1, .3]))
    first = _matrix(np.array([.05, .01, 0.]))
    remember(policy, source(1, pose=physical, basis=first))
    newest = first @ _matrix(np.array([0., .02, -.03]))
    entry = policy._recovery_support(physical @ newest, newest)
    assert np.allclose(entry['base_rotation'] @ newest, physical @ newest)
    assert policy._support_current['frame_id'] == 1  # Navigation selection makes no new proof.


def test_context_change_discards_entire_bank():
    policy = CellScanPolicy(recognition_goal='targets')
    remember(policy, source(1))
    changed = source(2)
    changed['session_id'] = 99
    changed['accepted_anchor_observation']['session_id'] = 7
    remember(policy, changed)
    assert policy._support_bank == []


def test_strong_nearby_search_never_chooses_single_known_face(monkeypatch):
    policy = CellScanPolicy(recognition_goal='targets')
    readable = np.zeros((8, 54))
    readable[:, :8] = 1.
    monkeypatch.setattr(policy, '_readability', lambda *args, **kwargs: readable)
    assert policy._recovery_search_goal(np.eye(3), AXES, atlas(), [], require_multi_face=True) is None
    assert policy._recovery_search_goal(np.eye(3), AXES, atlas(), []) is not None


def test_alternate_source_navigation_still_requires_new_real_multiface_proof(monkeypatch):
    policy = CellScanPolicy(recognition_goal='targets')
    rows = atlas()
    calls = []
    def plan(observed, rows, axes, now):
        calls.append(now)
        policy._route = [_matrix(np.array([0., math.radians(20.), 0.])) @ observed]
        policy._final_rotation = policy._route[-1]
        policy._target_indices = [26]
        policy.target_face = 'F'
        policy._started = now
        policy._at_goal_since = None
        policy._plan_serial += 1
    monkeypatch.setattr(policy, '_plan', plan)
    monkeypatch.setattr(policy, '_recovery_search_goal', lambda *args, **kwargs: None)
    policy.choose(np.eye(3), rows, AXES, elapsed=0., anchor_feedback=source(1))
    near = _matrix(np.array([0., math.radians(10.), 0.]))
    policy.choose(near, rows, AXES, elapsed=.2, anchor_feedback=source(2, pose=near))
    failed = source(3, pose=near)
    failed['refine_diagnostic']['renewed'] = False
    failed.pop('accepted_anchor_observation')
    failed.update(glyph_anchor_at=102., glyph_anchor_frame_id=2, glyph_anchor_age_sec=1.4)
    policy.choose(near, rows, AXES, elapsed=1.7, anchor_feedback=failed)
    failed['semantic_revision'] = 4
    failed['refine_diagnostic']['source_frame_id'] = 4
    policy.choose(near, rows, AXES, elapsed=1.8, anchor_feedback=failed)
    assert policy._anchor_recovery['support_frame_id'] == 2
    begun = policy._anchor_recovery['started']
    single = source(5, pose=near, faces={'L': 8})
    policy.choose(near, rows, AXES, elapsed=2.6, anchor_feedback=single)
    assert policy._anchor_recovery is not None
    assert policy._anchor_recovery['started'] == begun
    assert policy._anchor_recovery['support_frame_id'] == 1
    assert np.allclose(policy._anchor_recovery['goal'], np.eye(3))
    assert policy.stats['recovery_support_switches'] == 1
    policy.choose(np.eye(3), rows, AXES, elapsed=2.7, anchor_feedback=source(6))
    assert policy._anchor_recovery is None
    assert policy.stats['multi_face_recoveries'] == 1
    assert rows[26]['occupant'] == 'unknown'
