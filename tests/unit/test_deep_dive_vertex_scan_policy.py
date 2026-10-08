"""Opt-in vertex scheduling is not semantic proof or scan completion."""
from copy import deepcopy
import math

import numpy as np
import pytest

from plans.resonance_pc.src.actions._deep_dive_scan_policy import (
    CellScanPolicy, MixedFaceScanPolicy, _matrix, _pose_angle,
)
from plans.resonance_pc.src.actions._deep_dive_layout_vision import SEED_T
from plans.resonance_pc.src.actions._deep_dive_vertex_scan_policy import (
    VertexScanPolicy, _between, _samples, _view,
)


AXES = {0: np.array([0., .003, 0.]), 1: np.array([.003, 0., 0.])}
V0 = np.array([1., -1., -1.])/math.sqrt(3.)


def pose(direction=V0):
    camera = -SEED_T.reshape(3)/np.linalg.norm(SEED_T)
    return _between(direction, camera)


def cells():
    result = [dict(face=f, row=r, col=c, occupant='none', occupant_status='confirmed',
        node_status='known', confidence=.9, occupant_evidence_counts={'none': 3},
        evidence=[dict(frame_id=i, group=i, occupant='none', confidence=.9) for i in (1, 2, 3)])
        for f in 'URFDLB' for r in range(3) for c in range(3)]
    # Leave a real planning deficit; an all-empty completed inventory is not a
    # feasible game fixture and the base controller correctly waits for readiness.
    result[-1].update(occupant='unknown', occupant_status='unknown', node_status='unknown',
                      confidence=0., occupant_evidence_counts={}, evidence=[])
    return result


def packet(fid=1, stamp=100., rotation=None, faces=None, basis=None, age=.2, renewed=True):
    rotation = pose() if rotation is None else rotation
    basis = np.eye(3) if basis is None else basis
    faces = {'U': 4, 'R': 4, 'F': 4} if faces is None else faces
    result = dict(session_id=999, map_revision=0, semantic_revision=fid,
        geometry_body_basis=basis.tolist(), glyph_anchor_at=stamp, glyph_anchor_age_sec=age,
        glyph_anchor_frame_id=fid, glyph_anchor_map_revision=0, glyph_anchor_rotation=rotation.tolist(),
        refine_diagnostic=dict(source_frame_id=fid, source_frame_time=stamp, source_map_revision=0,
            renewed=renewed, reason='known_multi_face_joint_fit', accepted_faces=faces))
    if renewed:
        result['accepted_anchor_observation'] = dict(frame_id=fid, frame_time=stamp,
            session_id=999, map_revision=0, rotation=rotation.tolist(), body_basis=basis.tolist(),
            accepted_faces=faces)
    return result


def bind(policy, source, rows=None):
    rows = cells() if rows is None else rows
    if policy._vertex_context is None:
        policy._vertex_context = (999, 0)
    policy._mixed_feedback = source
    policy._support_feedback(source, rows, 0.)
    return rows


def test_four_vertices_are_adjacent_and_cover_six_faces_with_two_shared_planes():
    policy = VertexScanPolicy()
    policy._initialize(pose(), np.eye(3))
    assert policy._vertex_faces == [('U', 'R', 'F'), ('R', 'F', 'D'), ('F', 'D', 'L'), ('D', 'L', 'B')]
    assert set().union(*map(set, policy._vertex_faces)) == set('URFDLB')
    for a, b, fa, fb in zip(policy._vertices, policy._vertices[1:],
                            policy._vertex_faces, policy._vertex_faces[1:]):
        assert np.dot(a, b) == pytest.approx(1/3)
        assert len(set(fa) & set(fb)) == 2
        midpoint = (a+b)/np.linalg.norm(a+b)
        assert np.count_nonzero(np.abs(midpoint) > .5) == 2
    assert _view(pose(), policy.tvec) == pytest.approx(V0)


def test_arbitrary_initial_direction_picks_connected_vertices_not_fixed_drag_pixels():
    direction = np.array([-.4, .7, .6]); direction /= np.linalg.norm(direction)
    policy = VertexScanPolicy()
    policy._initialize(pose(direction), np.eye(3))
    assert policy._vertices[0] == pytest.approx(np.array([-1, 1, 1])/math.sqrt(3))
    assert set().union(*map(set, policy._vertex_faces)) == set('URFDLB')


def test_two_axis_paths_reach_direction_without_imposing_unreachable_roll():
    policy = VertexScanPolicy()
    target = np.array([1., 1., -1.])/math.sqrt(3.)
    paths = policy._paths_to_vertex(pose(), AXES, target)
    assert paths
    for path, cost in paths:
        assert cost <= 2*math.radians(110.)
        assert np.dot(_view(path[-1], policy.tvec), target) >= math.cos(math.radians(6.))
        assert np.allclose(path[-1].T @ path[-1], np.eye(3))


def test_prediction_single_face_midpoint_is_rejected_even_with_complete_atlas():
    policy = VertexScanPolicy()
    end = pose(np.array([1., 1., -1.])/math.sqrt(3.))
    rows = cells()
    assert policy._hard_route(pose(), [end], rows)
    assert not policy._hard_route(pose(), [np.eye(3), end], rows)
    samples = _samples(pose(), [end])
    assert all(_pose_angle(a, b) <= math.radians(10.)+1e-9 for a, b in zip(samples, samples[1:]))


@pytest.mark.parametrize('defect', ['missing', 'old', 'frame', 'single', 'weak_face', 'new_not_known'])
def test_only_bound_actual_known_multiface_source_can_be_ingested(defect):
    policy = VertexScanPolicy()
    policy._initialize(pose(), np.eye(3))
    source = packet()
    if defect == 'missing':
        source.pop('accepted_anchor_observation')
    elif defect == 'old':
        source['glyph_anchor_age_sec'] = .9
    elif defect == 'frame':
        source['accepted_anchor_observation']['frame_id'] = 2
    elif defect == 'single':
        source = packet(faces={'F': 9})
    elif defect == 'weak_face':
        source = packet(faces={'U': 1, 'R': 2, 'F': 2})
    else:
        source['refine_diagnostic'].update(reason='independent_grid',
            confirmed_faces={'U': 0, 'R': 0, 'F': 0}, candidate_faces={'U': 4, 'R': 4, 'F': 4})
    bind(policy, source)
    policy._ingest(pose(), np.eye(3))
    assert policy._vertex_views[0] == [] and policy._vertex_index == 0


def test_new_tracked_pose_does_not_acknowledge_older_semantic_arrival():
    policy = VertexScanPolicy()
    policy._initialize(pose(), np.eye(3)); bind(policy, packet())
    policy._ingest(_matrix(np.array([0., .2, 0.])) @ pose(), np.eye(3))
    assert not policy._vertex_views[0]


def test_endpoint_pair_needs_new_source_and_actual_eight_degree_separation():
    policy = VertexScanPolicy()
    policy._initialize(pose(), np.eye(3)); bind(policy, packet())
    policy._ingest(pose(), np.eye(3))
    assert len(policy._vertex_views[0]) == 1 and policy._vertex_index == 0
    small = _matrix(np.array([0., math.radians(7.), 0.])) @ pose()
    policy._pair_goal_base = small
    bind(policy, packet(2, 100.1, rotation=small)); policy._ingest(small, np.eye(3))
    assert policy._vertex_index == 0
    actual = _matrix(np.array([0., math.radians(14.), 0.])) @ pose()
    policy._pair_goal_base = actual
    bind(policy, packet(1, 100., rotation=actual)); policy._ingest(actual, np.eye(3))
    assert policy._vertex_index == 0
    bind(policy, packet(3, 100.2, rotation=actual)); policy._ingest(actual, np.eye(3))
    assert policy._vertex_index == 1 and len(policy._vertex_views[0]) == 2


def test_incoming_face_without_actual_known_glyphs_cannot_handoff():
    policy = VertexScanPolicy()
    policy._initialize(pose(), np.eye(3)); policy._vertex_index = 1
    actual = pose(np.array([1., 1., -1.])/math.sqrt(3.))
    bind(policy, packet(rotation=actual, faces={'R': 4, 'F': 4}))
    policy._ingest(actual, np.eye(3))
    assert policy._vertex_index == 1
    bind(policy, packet(2, 100.2, rotation=actual, faces={'D': 2, 'R': 4, 'F': 4}))
    policy._ingest(actual, np.eye(3))
    assert policy._vertex_index == 1 and len(policy._vertex_views[1]) == 1
    other = _matrix(np.array([0., math.radians(14.), 0.])) @ actual
    policy._pair_goal_base = other
    bind(policy, packet(3, 100.4, rotation=other, faces={'D': 2, 'R': 4, 'F': 4}))
    policy._ingest(other, np.eye(3))
    assert policy._vertex_index == 2


def test_physical_vertices_survive_basis_correction_but_reset_for_new_session():
    policy = VertexScanPolicy()
    policy._initialize(pose(), np.eye(3)); bind(policy, packet())
    original = np.asarray(policy._vertices).copy()
    basis = _matrix(np.array([.04, 0., 0.]))
    bind(policy, packet(2, 100.2, rotation=pose() @ basis, basis=basis))
    policy._ingest(pose() @ basis, basis)
    np.testing.assert_allclose(policy._vertices, original)
    policy._local_task = {'old': True}
    policy._reset_vertex((1000, 0))
    assert not policy._vertices and policy._vertex_index == 0 and policy._local_task is None


def test_unknown_atlas_or_invalid_predicted_path_holds_without_fabricating_labels(monkeypatch):
    policy = VertexScanPolicy()
    rows = bind(policy, packet()); before = deepcopy(rows)
    policy._initialize(pose(), np.eye(3))
    monkeypatch.setattr(policy, '_hard_route', lambda *args: False)
    policy._plan(pose(), rows, AXES, 0.)
    assert policy._vertex_block == 'vertex_no_known_multiface_axis_route'
    assert policy._target_indices == [] and policy._vertex_index == 0 and rows == before


def test_endpoint_local_goal_is_twelve_to_sixteen_and_does_not_grant_pair(monkeypatch):
    policy = VertexScanPolicy(); rows = bind(policy, packet())
    policy._initialize(pose(), np.eye(3))
    policy._ingest(pose(), np.eye(3))
    monkeypatch.setattr(policy, '_hard_route', lambda *args: True)
    monkeypatch.setattr(policy, '_readability', lambda rotations, *args, **kwargs: np.ones((len(rotations), 54)))
    policy._plan(pose(), rows, AXES, 0.)
    angle = math.degrees(_pose_angle(policy._final_rotation, pose()))
    assert 11.999 <= angle <= 16.001
    assert policy._vertex_index == 0 and len(policy._vertex_views[0]) == 1


def test_itinerary_exhaustion_is_never_completion_or_readiness():
    policy = VertexScanPolicy(); rows = bind(policy, packet()); before = deepcopy(rows)
    policy._initialize(pose(), np.eye(3)); policy._vertex_index = 4
    policy._plan(pose(), rows, AXES, 0.)
    assert policy._vertex_block == 'vertex_itinerary_exhausted_await_public_readiness'
    assert policy.summary()['vertex']['completion_authority'] == 'public_targets_readiness_only'
    assert rows == before


def test_inherited_current_anchor_recovery_preserves_unconsumed_itinerary():
    policy = VertexScanPolicy(); rows = bind(policy, packet())
    policy._initialize(pose(), np.eye(3)); policy._vertex_index = 1
    policy.choose(pose(), rows, AXES, elapsed=0., anchor_feedback=packet())
    for fid, elapsed in ((2, 1.7), (3, 1.8)):
        failed = packet(fid, 101., renewed=False, age=1.4)
        failed.update(glyph_anchor_at=100., glyph_anchor_frame_id=1, glyph_anchor_rotation=pose().tolist())
        result = policy.choose(pose(), rows, AXES, elapsed=elapsed, anchor_feedback=failed)
    assert policy._anchor_recovery is not None and result['phase'] == 'observe'
    assert result['reason'] == 'await_current_anchor_evidence'
    assert policy._vertex_index == 1
    assert policy._anchor_recovery['started'] == 1.8  # Same inherited clock/budget.


def new_face_packet(fid, stamp, rotation, known_d=0):
    source = packet(fid, stamp, rotation=rotation, faces={'D': 2, 'R': 4, 'F': 4})
    source['refine_diagnostic'].update(reason='seven_centres',
        candidate_faces={'D': 2, 'R': 4, 'F': 4},
        confirmed_faces={'D': known_d, 'R': 4, 'F': 4})
    return source


def test_new_incoming_accepted_pixels_start_dither_but_never_handoff(monkeypatch):
    policy = VertexScanPolicy(); policy._initialize(pose(), np.eye(3)); policy._vertex_index = 1
    actual = pose(np.array([1., 1., -1.])/math.sqrt(3))
    rows = bind(policy, new_face_packet(1, 100., actual)); before = deepcopy(rows)
    policy._ingest(actual, np.eye(3))
    assert len(policy._vertex_views[1]) == 1 and policy._vertex_index == 1
    seen_required = []
    other = _matrix(np.array([0., math.radians(14.), 0.])) @ actual
    monkeypatch.setattr(policy, '_pair_paths', lambda *args: (seen_required.append(args[-1]) or [([other], .2)]))
    policy._plan(actual, rows, AXES, 1.)
    assert set(seen_required[0]) == {'R', 'F'}  # Unknown D cannot prevent collection.
    bind(policy, new_face_packet(2, 100.2, other)); policy._ingest(other, np.eye(3))
    assert policy._vertex_index == 1 and len(policy._vertex_views[1]) == 1
    bind(policy, new_face_packet(3, 100.4, other, known_d=2)); policy._ingest(other, np.eye(3))
    assert policy._vertex_index == 2 and rows == before


def test_bootstrap_reuses_cells_control_without_new_clock_or_votes(monkeypatch):
    policy = VertexScanPolicy(); rows = cells(); before = deepcopy(rows); calls = []
    def cells_choose(self, *args):
        calls.append(args)
        return dict(direction=(1., 0.), phase='approach', reason='cells_control')
    monkeypatch.setattr(CellScanPolicy, 'choose', cells_choose)
    source = packet(faces={'U': 1, 'F': 3})
    result = policy.choose(pose(), rows, AXES, elapsed=83., anchor_feedback=source)
    assert result['reason'] == 'cells_control' and policy._bootstrap_active
    assert calls[0][6] == 83. and not policy._vertices and rows == before


def test_bootstrap_planning_calls_existing_global_cells_planner(monkeypatch):
    policy = VertexScanPolicy(); calls = []
    monkeypatch.setattr(CellScanPolicy, '_plan', lambda *args: calls.append(args))
    policy._plan(pose(), cells(), AXES, 44.)
    assert len(calls) == 1 and calls[0][-1] == 44.
    assert policy.summary()['vertex']['bootstrap_active']


@pytest.mark.parametrize('current', [True, False])
def test_bootstrap_switch_requires_same_current_pose_known_source(monkeypatch, current):
    policy = VertexScanPolicy(); calls = []
    def choose(*args):
        calls.append(args[0]._bootstrap_active)
        return dict(direction=None, phase='observe', reason='mock_controller')
    monkeypatch.setattr(CellScanPolicy, 'choose', choose)
    monkeypatch.setattr(MixedFaceScanPolicy, 'choose', choose)
    observed = pose() if current else _matrix(np.array([0., .2, 0.])) @ pose()
    policy.choose(observed, cells(), AXES, elapsed=30., anchor_feedback=packet())
    assert calls == [not current] and bool(policy._vertices) == current


def test_pair_collect_is_bounded_even_if_unknown_face_never_becomes_known(monkeypatch):
    policy = VertexScanPolicy(); policy._initialize(pose(), np.eye(3)); policy._vertex_index = 1
    actual = pose(np.array([1., 1., -1.])/math.sqrt(3))
    rows = bind(policy, new_face_packet(1, 100., actual)); policy._ingest(actual, np.eye(3))
    a = _matrix(np.array([0., .24, 0.])) @ actual
    b = _matrix(np.array([0., -.24, 0.])) @ actual
    monkeypatch.setattr(policy, '_pair_paths', lambda *args: [([a], .24), ([b], .24)])
    calls = []
    monkeypatch.setattr(CellScanPolicy, '_plan', lambda *args: calls.append(args[-1]))
    for now in (1., 2., 3., 4.):
        policy._plan(actual, rows, AXES, now)
    assert len(policy._pair_attempts[1]) == 2 and policy._vertex_index == 1
    assert policy._vertex_failure_reason == 'vertex_pair_evidence_unresolved'
    assert policy._vertex_block is None and calls == [3., 4.]


def test_six_known_across_three_faces_cannot_replace_common_pair_six():
    policy = VertexScanPolicy(); policy._initialize(pose(), np.eye(3)); policy._vertex_index = 1
    actual = pose(np.array([1., 1., -1.])/math.sqrt(3))
    rows = bind(policy, packet(rotation=actual, faces={'D': 2, 'R': 2, 'F': 2}))
    policy._ingest(actual, np.eye(3))
    assert not policy._vertex_views[1]
    policy._plan(actual, rows, AXES, 10.)
    assert policy._vertex_block == 'vertex_current_shared_face_handoff_missing'


def test_initial_fully_known_three_faces_can_begin_pair_without_new_face_handoff():
    policy = VertexScanPolicy(); policy._initialize(pose(), np.eye(3))
    bind(policy, packet(faces={'U': 2, 'R': 2, 'F': 2}))
    policy._ingest(pose(), np.eye(3))
    assert len(policy._vertex_views[0]) == 1 and policy._vertex_index == 0
    assert set(policy._vertex_views[0][0]['common_faces']) == {'U', 'R', 'F'}


def test_failed_vertex_uses_cell_choose_and_plan_to_move_with_original_elapsed(monkeypatch):
    policy = VertexScanPolicy(); policy._initialize(pose(), np.eye(3)); rows = bind(policy, packet())
    policy._ingest(pose(), np.eye(3)); views = deepcopy(policy.summary()['vertex']['actual_views'])
    policy._local_task = {'signature': ('candidate', 'inspiration', None), 'kind': 'inspiration', 'index': None}
    policy._local_goal = pose(); policy._fallback_to_cells('vertex_pair_evidence_unresolved')
    calls = []
    def plan(self, observed, rows, axes, now):
        calls.append(now)
        self._install(observed, [_matrix(np.array([0., .24, 0.])) @ observed], rows, now)
    monkeypatch.setattr(CellScanPolicy, '_plan', plan)
    monkeypatch.setattr(MixedFaceScanPolicy, 'choose', lambda *args: pytest.fail('Mixed cannot control fallback'))
    before = deepcopy(rows)
    result = policy.choose(pose(), rows, AXES, elapsed=34., anchor_feedback=packet(2, 100.2))
    assert result['direction'] is not None and calls == [34.]
    assert policy._started == 34. and policy._local_task is None and policy._local_goal is None
    assert policy._vertex_index == 0 and policy.summary()['vertex']['actual_views'] == views
    assert policy.summary()['vertex']['failure_evidence']['actual_views'] == views and rows == before


def test_fallback_cannot_rebuild_itinerary_after_new_sources_or_context(monkeypatch):
    policy = VertexScanPolicy(); policy._initialize(pose(), np.eye(3)); bind(policy, packet())
    policy._ingest(pose(), np.eye(3)); policy._fallback_to_cells('vertex_pair_evidence_unresolved')
    failure = deepcopy(policy.summary()['vertex']['failure_evidence'])
    calls = []
    monkeypatch.setattr(CellScanPolicy, 'choose', lambda self, *args: (
        calls.append(args[6]) or dict(direction=None, phase='observe', reason='mock_cells')))
    monkeypatch.setattr(policy, '_initialize', lambda *args: pytest.fail('No rebuild after failure'))
    for fid in (2, 3):
        source = packet(fid, 100.+fid/10)
        if fid == 3:
            source.update(session_id=1000)
            source['accepted_anchor_observation']['session_id'] = 1000
        policy.choose(pose(), cells(), AXES, elapsed=80.+fid, anchor_feedback=source)
    assert calls == [82., 83.] and policy._vertex_failure_reason == 'vertex_pair_evidence_unresolved'
    assert not policy._bootstrap_active and policy.summary()['vertex']['failure_evidence'] == failure


def test_fallback_does_not_reset_actual_anchor_recovery_clock_or_mutate_atlas():
    policy = VertexScanPolicy(); policy._initialize(pose(), np.eye(3)); rows = bind(policy, packet())
    recovery = {'started': 40., 'attempt': 1, 'anchor_at': 100.}
    policy._anchor_recovery = recovery; before = deepcopy(rows)
    policy._fallback_to_cells('vertex_pair_evidence_unresolved')
    assert policy._anchor_recovery is recovery and recovery['started'] == 40.
    assert rows == before and policy._vertex_index == 0
