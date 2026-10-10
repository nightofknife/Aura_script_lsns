"""Camera pages are finite navigation tasks, never recognition votes."""
from copy import deepcopy
import json
import math
from pathlib import Path

import numpy as np
import pytest

from plans.resonance_pc.src.actions._deep_dive_scan_policy import CellScanPolicy, MixedFaceScanPolicy, _matrix, _pose_angle
from plans.resonance_pc.src.actions._deep_dive_target_first_scan_policy import TargetFirstCellScanPolicy
from plans.resonance_pc.src.actions._deep_dive_target_face_scan_policy import TargetFacePageScanPolicy
from test_deep_dive_target_navigation_opportunities import actual_packet
from test_deep_dive_mixed_scan_policy import AXES, cells


def feedback(rows, rotation, frame=9):
    value = actual_packet(rows)
    stamp = float(frame + 1)
    source = value['accepted_anchor_observation']
    source.update(frame_id=frame, frame_time=stamp, rotation=rotation.tolist(),
                  accepted_faces={'F':5, 'L':3})
    value.update(frame_id=frame+1, generation=frame+101, frame_time=stamp+.1,
        glyph_anchor_frame_id=frame, glyph_anchor_at=stamp,
        glyph_anchor_rotation=rotation.tolist(), semantic_revision=frame)
    value['refine_diagnostic'].update(source_frame_id=frame, source_frame_time=stamp,
        accepted_faces={'F':5, 'L':3})
    value['semantic_metadata'].update(frame_id=frame, generation=frame+100, frame_time=stamp)
    value['semantic_metadata']['pose']['rotation'] = rotation.tolist()
    value['target_coverage'].update(source_frame_id=frame, source_frame_time=stamp)
    return value


def subject():
    rows = cells()
    rotation = _matrix(np.array([-.18, .38, 0.]))
    second = _matrix(np.array([0., 0., math.radians(16.)])) @ rotation
    value = feedback(rows, rotation)
    policy = TargetFacePageScanPolicy(expected_inspirations=2)
    policy._sync_page_basis(value)
    policy._page_face = 'F'
    policy._page_pair = [rotation, second]
    policy._page_order = list('FDL BUR'.replace(' ', ''))
    policy._page_source_floor = (7, 0, 8, 108, 9.)
    policy._page_attempts = 1
    policy._page_pending_cursor = 0
    policy._page_navigation_route = True
    return policy, rows, value, rotation, second


def record(policy, rows, value):
    policy._remember_target_source(value)
    policy._mixed_feedback = value
    policy._record_opportunities(rows, value)


def test_two_actual_sources_advance_navigation_only_without_assigning_unknowns():
    policy, rows, value, _, second = subject()
    before = deepcopy(rows)
    record(policy, rows, value)
    assert len(policy._page_views) == 1 and policy._page_cursor == 1
    assert policy._page_source_floor == policy._source_key(value)
    record(policy, rows, value)  # Same source cannot be reused on the next page.
    assert len(policy._page_views) == 1
    later = feedback(rows, second, frame=11)
    record(policy, rows, later)
    assert policy._page_results['F']['complete'] is True
    assert [v['frame_id'] for v in policy._page_results['F']['views']] == [9, 11]
    assert policy._page_face is None and rows == before


@pytest.mark.parametrize('defect', ['model', 'coverage', 'stale', 'pose', 'basis',
    'source_map', 'metadata_session', 'thin', 'old_frame', 'old_generation', 'old_stamp'])
def test_invalid_or_before_dispatch_source_cannot_advance_page(defect):
    policy, rows, value, _, _ = subject()
    if defect == 'model': value['target_coverage']['model_executed'] = False
    elif defect == 'coverage': value['target_coverage']['coverage_valid'] = False
    elif defect == 'stale': value['glyph_anchor_age_sec'] = .8
    elif defect == 'pose': value['semantic_metadata']['pose']['rotation'] = np.eye(3).tolist()
    elif defect == 'basis': value['accepted_anchor_observation']['body_basis'][0][0] = 2.
    elif defect == 'source_map': value['target_coverage']['source_map_revision'] = 1
    elif defect == 'metadata_session': value['semantic_metadata']['session_id'] = 8
    elif defect == 'thin':
        value['accepted_anchor_observation']['accepted_faces'] = {'F':7}
        value['refine_diagnostic']['accepted_faces'] = {'F':7}
    elif defect == 'old_frame': policy._page_source_floor = (7, 0, 9, 108, 9.)
    elif defect == 'old_generation': policy._page_source_floor = (7, 0, 8, 109, 9.)
    elif defect == 'old_stamp': policy._page_source_floor = (7, 0, 8, 108, 10.)
    record(policy, rows, value)
    assert policy._page_views == [] and policy._page_cursor == 0


def test_new_source_inside_eight_degrees_cannot_complete_page():
    policy, rows, value, rotation, _ = subject()
    record(policy, rows, value)
    near = _matrix(np.array([0., 0., math.radians(7.9)])) @ rotation
    policy._page_pair[1] = near
    record(policy, rows, feedback(rows, near, frame=11))
    assert len(policy._page_views) == 1 and not policy._page_results


def test_page_goal_and_progress_rebase_with_noncommuting_body_correction():
    policy, _, value, rotation, _ = subject()
    policy._route = [rotation.copy()]
    policy._final_rotation = policy._progress_goal = rotation.copy()
    basis = _matrix(np.array([.03, -.04, .02]))
    changed = deepcopy(value)
    changed['geometry_body_basis'] = basis.tolist()
    policy._sync_page_basis(changed)
    assert np.allclose(policy._route[0], rotation @ basis)
    assert np.allclose(policy._progress_goal, rotation @ basis)
    assert np.allclose(policy._page_pair[0], rotation)  # Stored in the fixed base.


def test_context_change_with_invalid_basis_clears_pages_and_sources():
    policy, rows, value, _, _ = subject()
    record(policy, rows, value)
    changed = deepcopy(value)
    changed['map_revision'] = 1
    changed['geometry_body_basis'] = None
    policy._sync_page_basis(changed)
    assert not policy._page_views and policy._page_face is None
    assert not policy._page_results and not policy._page_used_sources


def test_cell_choose_prearrival_revision_does_not_consume_page_endpoint(monkeypatch):
    policy, rows, value, rotation, second = subject()
    rows[0].update(occupant='unknown', occupant_status='unknown', evidence=[])
    policy.expected_inspirations = 3  # Original readiness has not closed.
    policy._mixed_feedback = value
    policy._route = [rotation.copy()]
    policy._final_rotation = rotation.copy()
    policy._target_indices = list(range(18,27))
    policy.target_face = 'F'
    policy._started = 10.
    policy._at_goal_since = 10.
    policy._arrival_revision = 0
    # This actual metadata predates the geometry dispatch floor.
    policy._page_source_floor = policy._source_key(value)
    calls = []
    monkeypatch.setattr(policy,'_plan',lambda *args: calls.append(True))
    before = deepcopy(rows)
    result = policy.choose(rotation, rows, AXES, elapsed=10.4,
        semantic_revision=1, anchor_feedback=value)
    assert policy._page_cursor == 0 and policy._page_attempts == 1, result
    assert np.allclose(policy._final_rotation, rotation)
    assert result['reason'] == 'await_semantic_frame'
    assert not calls
    assert rows == before and result is not None
    assert not policy._page_results  # Known ordinary cells didn't mark a page complete.


def test_no_supported_pair_finishes_six_partial_pages_and_returns_cell(monkeypatch):
    policy, rows, value, rotation, _ = subject()
    policy._mixed_feedback = value
    policy._page_face = policy._page_pair = None
    monkeypatch.setattr(policy, '_select_page_pair', lambda *args: None)
    called = []
    monkeypatch.setattr(CellScanPolicy, '_plan', lambda *args: called.append(True))
    before = deepcopy(rows)
    policy._plan(rotation, rows, AXES, 12.)
    assert len(policy._page_results) == 6 and called == [True]
    assert all(not x['complete'] for x in policy._page_results.values())
    assert policy.summary()['face_pages']['fallback'] and rows == before


def test_finite_three_endpoints_do_not_repeat_same_failed_goal(monkeypatch):
    policy, rows, value, rotation, second = subject()
    policy._mixed_feedback = value
    third = _matrix(np.array([0., 0., math.radians(32.)])) @ rotation
    policy._page_pair.append(third)
    policy._page_new_pair = False
    policy._route = []
    policy._plan(rotation, rows, AXES, 11.)
    assert policy._page_cursor == 1 and policy._page_attempts == 2
    policy._route = []
    policy._plan(second, rows, AXES, 12.)
    assert policy._page_cursor == 2 and policy._page_attempts == 3
    assert np.allclose(policy._final_rotation, third)
    monkeypatch.setattr(policy, '_select_page_pair', lambda *args: None)
    monkeypatch.setattr(CellScanPolicy, '_plan', lambda *args: None)
    policy._route = []
    policy._plan(third, rows, AXES, 13.)
    assert policy._page_results['F']['attempts'] == 3
    assert not policy._page_results['F']['complete']


def test_abort_before_shared_orbit_resolves_from_actual_pose_without_resetting_budget(monkeypatch):
    policy, rows, value, rotation, _ = subject()
    policy._mixed_feedback = value
    policy._route = []
    actual = _matrix(np.array([.7, -.1, .2])) @ rotation
    first = _matrix(np.array([.12, 0., 0.])) @ actual
    second = _matrix(np.array([.4, 0., 0.])) @ actual
    calls = []
    def select(observed, *args):
        calls.append(observed.copy())
        return [first, second], [first]
    monkeypatch.setattr(policy, '_select_page_pair', select)
    policy._plan(actual, rows, AXES, 11.)
    assert len(calls) == 1 and np.array_equal(calls[0], actual)
    assert policy._page_attempts == 2 and policy._page_cursor == 0
    assert np.allclose(policy._final_rotation, first)
    assert policy._page_source_floor == policy._source_key(value)


@pytest.mark.parametrize('page_mode', [False, True])
def test_new_navigation_hint_interrupts_old_policy_but_not_unfinished_pages(monkeypatch, page_mode):
    policy = TargetFacePageScanPolicy() if page_mode else TargetFirstCellScanPolicy()
    value = feedback(cells(), np.eye(3))
    if page_mode:
        policy._sync_page_basis(value)
    route = [_matrix(np.array([.4, .2, 0.]))]
    policy._route = deepcopy(route)
    policy._page_attempts = 1
    monkeypatch.setattr(policy, '_remember_target_source', lambda *args: None)
    monkeypatch.setattr(policy, '_record_opportunities', lambda *args: None)
    monkeypatch.setattr(policy, '_remember_assigned_interests', lambda *args: True)
    # Isolate the parent hook: the actual Cell.choose dwell chain is tested
    # separately above. A new hint must not consume a page endpoint here.
    monkeypatch.setattr(MixedFaceScanPolicy, 'choose', lambda *args, **kwargs: dict(reason='test'))
    policy.choose(np.eye(3), cells(), AXES, elapsed=10., anchor_feedback=value)
    assert bool(policy._route) is page_mode
    assert policy._page_attempts == 1
    if page_mode:
        assert np.array_equal(policy._route[0], route[0])
        policy._page_results = dict.fromkeys('URFDLB', {})
        assert policy._assigned_interest_interrupts_route()  # Fallback is unchanged.


def test_actual_old_single_face_source_does_not_prove_a_camera_page():
    saved = json.loads((Path(__file__).parent/'fixtures/deep_dive_navigation_sources_13.json').read_text(encoding='utf8'))
    value = deepcopy(next(x['feedback'] for x in saved['sources'] if x['frame_id'] == 845))
    policy = TargetFacePageScanPolicy()
    policy._sync_page_basis(value)
    policy._page_face = 'B'
    rotation = np.array(value['semantic_metadata']['pose']['rotation'])
    basis = np.array(value['accepted_anchor_observation']['body_basis'])
    policy._page_pair = [rotation @ basis.T]
    key = policy._source_key(value['semantic_metadata'])
    policy._page_source_floor = (*key[:2], key[2]-1, key[3]-1, key[4]-1.)
    record(policy, cells(), value)
    assert policy._page_views == []  # Actual B7 single-face can renew the mapper,
    assert not policy._page_results  # but cannot prove the new overlapping page.


def test_real_seed_pair_candidates_keep_main_and_a_side_not_single_face_front():
    saved = json.loads((Path(__file__).parent/'fixtures/deep_dive_navigation_sources_13.json').read_text(encoding='utf8'))
    value = saved['sources'][0]['feedback']
    policy = TargetFacePageScanPolicy()
    policy._sync_page_basis(value)
    policy._page_face = 'F'
    policy.tvec = np.array(value['semantic_metadata']['pose']['tvec']).reshape(3)
    observed = np.array(value['semantic_metadata']['pose']['rotation'])
    rows = [dict(occupant='unknown', node_status='unknown', evidence=[]) for _ in range(54)]
    before = deepcopy(rows)
    result = policy._select_page_pair(observed, rows, AXES)
    assert result is not None
    diagnostic = policy.summary()['face_pages']['framing']
    assert diagnostic['status'] == 'selected' and diagnostic['target_indices'] == []
    assert 6 <= diagnostic['union_cells'] <= 9 and rows == before
    pair, path = result
    candidates = np.array([p @ policy._page_basis for p in pair[:2]])
    glyphs = policy._readability(candidates, rows, anchors=True, known_only=False)>0
    assert all(any(glyphs[i,j*9:(j+1)*9].sum()>=2 for j in (0,1,3,4,5)) for i in (0,1))
    assert math.degrees(_pose_angle(pair[0],pair[1]))>=14.-1e-7
    assert len(path)<=2


@pytest.mark.parametrize('invalid', [None, True, (.78,), (.94,.78), (0.,.94),
    (.78,1.01), (.78,float('nan')), (.78,float('inf')), (False,.94)])
def test_navigation_cosine_range_rejects_invalid_boundaries(invalid):
    with pytest.raises(ValueError):
        TargetFacePageScanPolicy(page_cosine_range=invalid)


@pytest.mark.parametrize('cosine, accepted', [(.80,True), (.77,False)])
def test_navigation_actual_source_uses_configured_lower_boundary_without_votes(cosine, accepted):
    policy, rows, value, _, _ = subject()
    index = policy._keys.index(('F',1,1))
    translation = np.asarray(value['semantic_metadata']['pose']['tvec']).reshape(3)
    low, high = 0., math.pi/2
    for _ in range(45):
        angle = (low+high)/2
        rotation = _matrix(np.array([0.,angle,0.]))
        camera = rotation@policy._points[index]+translation
        measured = -float((rotation@policy._normals[index])@camera)/np.linalg.norm(camera)
        if measured > cosine: low = angle
        else: high = angle
    value = feedback(rows,rotation)
    policy._page_pair[0] = rotation
    before = deepcopy(rows)
    record(policy,rows,value)
    assert bool(policy._page_views) is accepted
    assert rows == before and not policy._page_results
    assert policy.summary()['face_pages']['page_cosine_range'] == [.78,.94]


def test_saved_f_geometry_broader_navigation_range_finds_original_safe_pair():
    # Actual packet560/axes. Unknown rows are a conservative geometry proxy,
    # not the unsaved historical controller562 atlas or live proof.
    value = json.loads((Path(__file__).parent/'fixtures/deep_dive_page_geometry_22_560.json').read_text(encoding='utf8'))
    rows = [dict(occupant='unknown',node_status='unknown',evidence=[]) for _ in range(54)]
    before = deepcopy(rows)
    axes = {int(k):np.array(v) for k,v in value['axes'].items()}
    observed = np.array(value['pose']['rotation'])
    results = []
    for bounds in ((.86,.925),(.78,.94)):
        policy = TargetFacePageScanPolicy(page_cosine_range=bounds)
        policy._page_context = (value['session_id'],value['map_revision'])
        policy._page_basis = np.array(value['geometry_body_basis'])
        policy._page_face = 'F'
        policy.tvec = np.array(value['pose']['tvec'])
        results.append(policy._select_page_pair(observed,rows,axes))
    assert results[0] is None and results[1] is not None
    assert rows == before
