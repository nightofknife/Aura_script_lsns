"""Whole-face route locks and completion require actual independent reads."""
from copy import deepcopy
import math

import numpy as np

from plans.resonance_pc.src.actions._deep_dive_scan_policy import (
    CellScanPolicy, FaceFirstScanPolicy, _matrix, _pose_angle,
)


AXES = {0: np.array([0., .003, 0.]), 1: np.array([.003, 0., 0.])}


def cells():
    return [dict(face=face, row=row, col=col, occupant='none',
        occupant_status='confirmed', node_status='known', confidence=.9,
        occupant_evidence_counts={'none': 3},
        evidence=[dict(group=g, frame_id=g, occupant='none') for g in (1, 2, 3)])
        for face in 'URFDLB' for row in range(3) for col in range(3)]


def policy():
    result = FaceFirstScanPolicy(expected_inspirations=2)
    result._last_gain = np.zeros(54)
    result._last_attempt = np.full(54, -6.)
    return result


def views():
    return [_matrix(np.array([0., angle, 0.]))
            for angle in np.radians((-10., 0., 10.))]


def feedback_packet(frame=3, pose=None, renewed=True):
    return dict(refine_diagnostic=dict(source_frame_id=frame, renewed=renewed),
        accepted_face_observation=dict(frame_id=frame,
            rotation=(np.eye(3) if pose is None else pose).tolist(),
            cell_indices=list(range(18, 27))))


def test_confirmed_map_does_not_fabricate_three_face_views():
    controller = policy()
    rows = cells()
    before = deepcopy(rows)
    assert not controller._face_proven('F', rows)
    assert not controller._objective(rows)[0].any()
    assert rows == before


def test_three_views_and_nine_real_occupancies_are_required():
    controller = policy()
    controller.face_views['F'] = views()
    rows = cells()
    assert controller._face_proven('F', rows)
    rows[18]['occupant_status'] = 'unknown'
    assert not controller._face_proven('F', rows)
    rows = cells()
    rows[18]['evidence'][2]['group'] = 2
    assert not controller._face_proven('F', rows)


def test_positive_two_groups_and_targets_unknown_nodes_are_allowed():
    controller = policy()
    controller.face_views['F'] = views()
    rows = cells()
    for row in rows[18:27]:
        row['node_status'] = 'unknown'
    rows[22].update(occupant='player', evidence=[
        dict(group=g, frame_id=g, occupant='player') for g in (1, 2)])
    assert controller._face_proven('F', rows)
    rows[22]['evidence'][1]['group'] = 1
    assert not controller._face_proven('F', rows)
    controller.recognition_goal = 'full'
    assert not controller._face_proven('F', cells()[:18]+rows[18:])


def test_actual_source_acceptance_and_eight_degree_separation():
    controller = policy()
    controller._locked_face = 'F'
    rows = cells()
    feedback = feedback_packet()
    controller._record_face_view(rows, np.eye(3), feedback, 1.)
    assert len(controller.face_views['F']) == 1
    # New frames alone do not make a second independent view.
    for row in rows:
        row['evidence'].append(dict(group=4, frame_id=4, occupant='none'))
    feedback['refine_diagnostic']['source_frame_id'] = 4
    feedback['accepted_face_observation'].update(frame_id=4,
        rotation=_matrix(np.array([0., .04, 0.])).tolist())
    controller._record_face_view(rows, _matrix(np.array([0., .04, 0.])), feedback, 1.)
    assert len(controller.face_views['F']) == 1
    feedback['accepted_face_observation']['rotation'] = views()[2].tolist()
    controller._record_face_view(rows, views()[2], feedback, 1.)
    assert len(controller.face_views['F']) == 2
    feedback['refine_diagnostic']['source_frame_id'] = 99
    controller._record_face_view(rows, views()[0], feedback, 1.)
    assert len(controller.face_views['F']) == 2


def test_extrapolated_and_oblique_views_cannot_complete_face():
    controller = policy()
    controller._locked_face = 'F'
    feedback = feedback_packet(renewed=False)
    controller._record_face_view(cells(), np.eye(3), feedback, 1.)
    feedback['refine_diagnostic']['renewed'] = True
    feedback['accepted_face_observation']['rotation'] = _matrix(np.array([0., .6, 0.])).tolist()
    controller._record_face_view(cells(), _matrix(np.array([0., .6, 0.])), feedback, 1.)
    assert controller.face_views['F'] == []


def test_face_remains_locked_when_other_face_has_more_missing_cells():
    controller = policy()
    rows = cells()
    controller._plan(np.eye(3), rows, AXES, 0.)
    assert controller._locked_face == 'F'
    for row in rows[27:36]:
        row.update(occupant='unknown', occupant_status='unknown', evidence=[])
    controller._plan(np.eye(3), rows, AXES, 1.)
    assert controller._locked_face == 'F'
    assert controller._target_indices == list(range(18, 27))
    assert -float((controller._final_rotation @ np.array([0., 0., -1.]))[2]) >= .90


def test_face_route_retains_exact_final_pose_and_thirty_degree_checkpoints():
    controller = policy()
    start = _matrix(np.array([-.8, .6, 0.]))
    controller._plan(start, cells(), AXES, 0.)
    previous = start
    for pose in controller._route:
        assert _pose_angle(pose, previous) <= math.radians(30.)+1e-8
        previous = pose
    np.testing.assert_allclose(controller._route[-1], controller._final_rotation)


def test_completed_face_not_revisited_and_adjacent_face_precedes_opposite():
    controller = policy()
    rows = cells()
    controller._locked_face = 'F'
    controller.face_views['F'] = views()
    controller._leave_face(rows, 'face_complete')
    assert controller.completed_faces == {'F'}
    controller._plan(np.eye(3), rows, AXES, 1.)
    assert controller._locked_face in ('U', 'R', 'D', 'L')
    assert controller.visits['F'] == 0


def test_failed_face_is_explicit_and_never_silently_completed():
    controller = policy()
    rows = cells()
    controller._plan(np.eye(3), rows, AXES, 0.)
    controller._face_attempts = 7
    controller._plan(np.eye(3), rows, AXES, 1.)
    assert controller.failed_faces['F'] == 'face_evidence_budget_exhausted'
    assert 'F' not in controller.completed_faces
    assert controller._locked_face != 'F'
    controller.failed_faces = {face: 'failed' for face in 'URFDLB'}
    controller._locked_face = None
    controller._plan(np.eye(3), rows, AXES, 2.)
    result = controller.choose(np.eye(3), rows, AXES, elapsed=2.)
    assert result['direction'] is None and result['reason'] == 'face_scan_blocked'


def test_existing_cell_policy_is_unchanged():
    rows = cells()
    result = CellScanPolicy().choose(np.eye(3), rows, AXES, elapsed=0.)
    assert result['reason'] == 'all_cells_confirmed'


def test_current_anchor_gate_is_inherited_even_when_face_is_locked():
    controller = policy()
    feedback = dict(glyph_anchor_age_sec=2.1)
    result = controller.choose(np.eye(3), cells(), AXES, elapsed=0., anchor_feedback=feedback)
    assert result['direction'] is None
    assert result['reason'] == 'await_first_current_glyph_evidence'


def test_all_faces_finished_can_observe_pending_inventory_without_new_route():
    controller = policy()
    controller.completed_faces = set('URFDLB')
    result = controller.choose(np.eye(3), cells(), AXES, elapsed=10.)
    assert result['direction'] is None and result['phase'] == 'observe'
    assert controller.all_faces_complete
    assert controller.summary()['reason'] == 'all_faces_proven'
    assert controller._plan_serial == 0


def test_expired_glyph_anchor_cannot_supply_face_view_proof():
    controller = policy()
    controller._locked_face = 'F'
    feedback = feedback_packet()
    feedback['glyph_anchor_age_sec'] = 1.1
    controller._record_face_view(cells(), np.eye(3), feedback, 1.)
    assert controller.face_views['F'] == []


def test_semantic_pose_cannot_be_replaced_by_new_tracking_pose():
    controller = policy()
    controller._locked_face = 'F'
    rows = cells()
    packet = feedback_packet()
    controller._record_face_view(rows, views()[0], packet, 1.)
    np.testing.assert_allclose(controller.face_views['F'][0], np.eye(3))
    packet = feedback_packet(frame=4)
    controller._record_face_view(rows, views()[2], packet, 1.)
    assert len(controller.face_views['F']) == 1
    packet = feedback_packet(frame=5, pose=_matrix(np.array([0., .6, 0.])))
    controller._record_face_view(rows, np.eye(3), packet, 1.)
    assert len(controller.face_views['F']) == 1


def test_missing_or_wrong_source_packet_never_proves_a_face_view():
    controller = policy()
    controller._locked_face = 'F'
    packet = feedback_packet()
    packet['accepted_face_observation']['frame_id'] = 4
    controller._record_face_view(cells(), np.eye(3), packet, 1.)
    packet = feedback_packet()
    packet['accepted_face_observation']['cell_indices'] = list(range(9))
    controller._record_face_view(cells(), np.eye(3), packet, 1.)
    assert controller.face_views['F'] == []


def test_cached_sweep_has_arrival_margin_and_monotone_separated_endpoints():
    controller = policy()
    controller._plan(np.eye(3), cells(), AXES, 0.)
    assert len(controller._face_sweep) == 3
    normal = np.array([0., 0., -1.])
    for pose in controller._face_sweep:
        assert -(pose @ normal)[2] >= .94
    for left, right in zip(controller._face_sweep, controller._face_sweep[1:]):
        assert _pose_angle(left, right) >= math.radians(14.)-1e-8
    first_delta = controller._face_sweep[1] @ controller._face_sweep[0].T
    second_delta = controller._face_sweep[2] @ controller._face_sweep[1].T
    # A monotone single-axis sweep has no reversal between its small legs.
    assert np.sum((first_delta-first_delta.T)*(second_delta-second_delta.T)) > 0


def test_semantic_dwell_retries_cache_without_new_candidate_search(monkeypatch):
    controller = policy()
    controller._plan(np.eye(3), cells(), AXES, 0.)
    endpoint = controller._final_rotation.copy()
    assert controller._sweep_searches == 1
    monkeypatch.setattr(controller, '_readability', lambda *args, **kwargs:
                        (_ for _ in ()).throw(AssertionError('unexpected new search')))
    controller._plan(endpoint, cells(), AXES, .7)
    np.testing.assert_allclose(controller._final_rotation, endpoint)
    assert controller._sweep_index == 0 and controller._sweep_searches == 1


def test_only_exact_endpoint_semantic_pose_advances_cached_sweep():
    controller = policy()
    controller._locked_face = 'F'
    controller._face_sweep = [_matrix(np.array([0., angle, 0.]))
                              for angle in np.radians((-16., 0., 16.))]
    # Current tracking at the endpoint with an old central semantic image
    # cannot move the sweep forward.
    controller._record_face_view(cells(), controller._face_sweep[0], feedback_packet(), 1.)
    assert controller._sweep_index == 0
    first = feedback_packet(frame=4, pose=controller._face_sweep[0])
    controller._record_face_view(cells(), np.eye(3), first, 1.)
    assert controller._sweep_index == 1
    assert controller.face_view_sources['F'] == [4]
    controller._record_face_view(cells(), controller._face_sweep[1], first, 1.)
    assert controller._sweep_index == 1
    second = feedback_packet(frame=5, pose=controller._face_sweep[1])
    controller._record_face_view(cells(), controller._face_sweep[2], second, 1.)
    assert controller._sweep_index == 2
    assert controller.summary()['face_view_sources']['F'] == [4, 5]


def test_six_face_order_is_a_fixed_adjacent_closed_cycle():
    controller = policy()
    controller._plan(np.eye(3), cells(), AXES, 0.)
    order = controller._face_order
    assert len(order) == len(set(order)) == 6
    normals = dict(U=(0,-1,0), R=(1,0,0), F=(0,0,-1),
                   D=(0,1,0), L=(-1,0,0), B=(0,0,1))
    for left, right in zip(order, order[1:]+order[:1]):
        assert np.dot(normals[left], normals[right]) == 0
    controller._face_attempts = 7
    controller._plan(np.eye(3), cells(), AXES, 1.)
    assert controller._face_order == order
    assert controller._locked_face == order[1]
