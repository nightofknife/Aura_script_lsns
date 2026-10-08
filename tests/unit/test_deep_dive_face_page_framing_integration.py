"""Framing consumes only frozen sources and existing finite navigation budgets."""
from copy import deepcopy
import json
from pathlib import Path
import numpy as np
import pytest

from plans.resonance_pc.src.actions._deep_dive_scan_policy import MixedFaceScanPolicy, _matrix
from plans.resonance_pc.src.actions._deep_dive_target_face_scan_policy import TargetFacePageScanPolicy
from plans.resonance_pc.src.actions import _deep_dive_target_face_scan_policy as page_module
from test_deep_dive_target_framing import packet
from test_deep_dive_mixed_scan_policy import cells, AXES


def subject():
    _,feedback,candidate,_ = packet(index=26)
    feedback.update(geometry_body_basis=np.eye(3).tolist(),glyph_anchor_rotation=np.eye(3).tolist(),
        frame_id=964,generation=40,frame_time=100.1)
    rows = cells()
    rows[26].update(occupant='unknown',occupant_status='unknown',evidence=[],occupant_evidence_counts={})
    policy = TargetFacePageScanPolicy()
    policy._sync_page_basis(feedback)
    policy._remember_target_source(feedback)
    policy._mixed_feedback = feedback
    policy._interest_now = 101.
    return policy,rows,feedback,candidate


def test_first_hint_freezes_exact_original_projection_not_current_translation():
    policy,rows,feedback,candidate = subject()
    before = deepcopy(rows)
    policy.tvec = np.array([4.,8.,60.])
    assert policy._remember_assigned_interests(rows,feedback,101.)
    record = policy._page_framing[26]
    assert record['status'] == 'valid'
    assert record['source_projection']['tvec'] == [0.,.5,42.]
    source = record['source_projection']
    camera = policy._quads[26]+np.array([0.,.5,42.])
    quad = camera[:,:2]/camera[:,2,None]*policy._K[0,0]+policy._K[:2,2]
    assert np.array_equal(np.array(source['quad']),quad)
    feedback['semantic_metadata']['pose']['tvec'][1][0] = 99.
    candidate['box'][0] = 0
    assert source['tvec'] == [0.,.5,42.]
    assert record['candidate']['box'][0] == 599
    assert rows == before


def test_missing_creation_proof_cannot_be_reconstructed_from_later_good_packet():
    policy,rows,feedback,_ = subject()
    feedback['semantic_metadata']['pose']['tvec'][1][0] += 1.
    assert policy._remember_assigned_interests(rows,feedback,101.)
    assert 26 not in policy._page_framing
    rejected = policy.summary()['face_pages']
    assert rejected['framing_rejections'] == 1
    assert rejected['framing_rejected'] == [dict(index=26,source_frame_id=963,reason='original_association_proof_missing')]
    feedback['semantic_metadata']['pose']['tvec'][1][0] -= 1.
    assert not policy._remember_assigned_interests(rows,feedback,101.1)
    assert 26 not in policy._page_framing
    assert policy._page_framing_rejections == 1


def test_new_hint_is_queued_without_changing_active_route_waterline_or_attempts():
    policy,rows,feedback,_ = subject()
    policy._page_face = 'F'
    policy._page_pair = [np.eye(3),_matrix(np.array([.2,0.,0.]))]
    policy._route = [_matrix(np.array([.1,0.,0.]))]
    route = deepcopy(policy._route)
    floor = policy._page_source_floor = (123,0,961,37,99.)
    policy._page_attempts = 1
    assert policy._remember_assigned_interests(rows,feedback,101.)
    assert np.array_equal(policy._route[0],route[0])
    assert policy._page_source_floor == floor and policy._page_attempts == 1
    assert policy._assigned_interests[26]['attempts'] == 0


def test_natural_boundary_applies_constraint_once_with_existing_budget(monkeypatch):
    policy,rows,feedback,_ = subject()
    policy._page_face = 'F'
    policy._page_pair = [np.eye(3),_matrix(np.array([.2,0.,0.]))]
    policy._page_navigation_route = True
    policy._page_pending_cursor = 0
    policy._page_attempts = 1
    policy._route = []  # Cell already released the finite endpoint.
    assert policy._remember_assigned_interests(rows,feedback,101.)
    expires = policy._assigned_interests[26]['expires']
    actual = _matrix(np.array([.02,0.,0.]))
    first,second = _matrix(np.array([.12,0.,0.]))@actual,_matrix(np.array([.4,0.,0.]))@actual
    calls = []
    def select(observed,*args):
        calls.append(observed.copy())
        policy._page_pair_framing = (26,)
        return [first,second],[first]
    monkeypatch.setattr(policy,'_select_page_pair',select)
    policy._plan(actual,rows,AXES,101.2)
    assert len(calls) == 1 and np.array_equal(calls[0],actual)
    assert policy._page_attempts == 2 and policy._assigned_interests[26]['attempts'] == 1
    assert policy._assigned_interests[26]['expires'] == expires
    assert policy._page_source_floor == policy._source_key(feedback)
    # Existing route is not repeatedly planned merely by polling.
    assert policy._route and policy._page_cursor == 0


@pytest.mark.parametrize('interruption',['local','recovery'])
def test_parent_early_clear_of_interruption_still_resolves_actual_page_pose(monkeypatch,interruption):
    policy,rows,feedback,_ = subject()
    policy._page_face = 'F'
    policy._page_pair = [np.eye(3),_matrix(np.array([.2,0.,0.]))]
    policy._page_attempts = 1
    policy._page_navigation_route = False
    policy._local_started = 100.
    if interruption == 'local':
        policy._local_task = dict(signature=('positive','inspiration',26,963),kind='inspiration',index=26)
    else:
        policy._anchor_recovery = dict(started=100.)
    monkeypatch.setattr(policy,'_remember_target_source',lambda *args: np.eye(3))
    monkeypatch.setattr(policy,'_record_opportunities',lambda *args: None)
    monkeypatch.setattr(policy,'_remember_assigned_interests',lambda *args: False)
    actual = _matrix(np.array([.35,.12,0.]))
    first = _matrix(np.array([.1,0.,0.]))@actual
    calls = []
    def select(observed,*args):
        calls.append(observed.copy())
        return [first,_matrix(np.array([.4,0.,0.]))@actual],[first]
    monkeypatch.setattr(policy,'_select_page_pair',select)
    def parent_choose(self,*args,**kwargs):
        self._local_task = self._anchor_recovery = None
        self._route = []
        self._plan(actual,rows,AXES,101.)
        return dict(reason='synthetic_parent_transition')
    monkeypatch.setattr(MixedFaceScanPolicy,'choose',parent_choose)
    policy.choose(actual,rows,AXES,elapsed=101.,anchor_feedback=feedback)
    assert len(calls) == 1 and np.array_equal(calls[0],actual)
    assert np.allclose(policy._final_rotation,first)
    assert policy._page_attempts == 2 and not policy._page_resume_required


def test_private_planning_rows_keep_actual_bbox_without_writing_atlas():
    policy,rows,feedback,candidate = subject()
    policy._page_face = 'F'
    assert policy._remember_assigned_interests(rows,feedback,101.)
    before = deepcopy(rows)
    private = policy._page_planning_rows(rows,policy._active_page_framing(rows))
    assert private[26]['occupant'] == 'inspiration' and rows[26]['occupant'] == 'unknown'
    assert private[26]['evidence'][0]['target_box'] == candidate['box']
    assert rows == before


def test_context_change_missing_basis_clears_all_frozen_framing():
    policy,rows,feedback,_ = subject()
    assert policy._remember_assigned_interests(rows,feedback,101.)
    changed = deepcopy(feedback)
    changed.update(map_revision=1,geometry_body_basis=None)
    policy._sync_page_basis(changed)
    assert not policy._page_framing and not policy._page_pair_framing


def test_default_page_without_hint_uses_full_union_rank_but_no_new_evidence():
    saved = json.loads((Path(__file__).parent/'fixtures/deep_dive_navigation_sources_13.json').read_text(encoding='utf8'))
    feedback = saved['sources'][0]['feedback']
    pose = feedback['semantic_metadata']['pose']
    policy = TargetFacePageScanPolicy()
    policy._sync_page_basis(feedback)
    policy._page_face = 'F'
    policy.tvec = np.asarray(pose['tvec'])
    rows = [dict(occupant='unknown',node_status='unknown',evidence=[]) for _ in range(54)]
    before = deepcopy(rows)
    assert policy._select_page_pair(np.asarray(pose['rotation']),rows,AXES) is not None
    diagnostic = policy.summary()['face_pages']['framing']
    assert diagnostic['status'] == 'selected' and diagnostic['target_indices'] == []
    assert 6 <= diagnostic['union_cells'] <= 9 and rows == before


def test_disabled_kernel_does_not_create_framing_bank_or_boundary_constraint():
    policy,rows,feedback,_ = subject()
    policy._target_framing_enabled = False
    assert policy._remember_assigned_interests(rows,feedback,101.)
    assert not policy._page_framing and policy._active_page_framing(rows) == []


def test_rejected_source_diagnostic_is_bounded_without_storing_packets():
    policy,_,_,_ = subject()
    for index in range(7):
        policy._reject_page_framing(dict(index=index,source_frame_id=900+index),'invalid_test_source')
    result = policy.summary()['face_pages']
    assert result['framing_rejections'] == 7 and len(result['framing_rejected']) == 5
    assert all(set(x) == {'index','source_frame_id','reason'} for x in result['framing_rejected'])


def test_optional_third_endpoint_cannot_rehide_the_original_required_target(monkeypatch):
    policy,rows,feedback,_ = subject()
    policy._page_face = 'F'
    assert policy._remember_assigned_interests(rows,feedback,101.)
    excluded = []
    def readable(rotations,*args,**kwargs):
        result = np.ones((len(rotations),54))
        if excluded:
            for i,rotation in enumerate(rotations):
                if np.array_equal(rotation,excluded[0]):
                    result[i,26] = 0.
        return result
    monkeypatch.setattr(policy,'_readability',readable)
    monkeypatch.setattr(policy,'_planning_anchor_support',lambda rotations,*args,**kwargs:np.ones(len(rotations)))
    monkeypatch.setattr(policy,'_route_anchor_support',lambda paths,*args,**kwargs:np.ones(len(paths)))
    selected = []
    def select(options,**kwargs):
        if not selected:
            selected.append(next(x for x in options if x['alternative'] is not None))
        option = next(x for x in options if (x['left'],x['right']) == (selected[0]['left'],selected[0]['right']))
        assert kwargs['readable'][option['left'],26] > 0 and kwargs['readable'][option['right'],26] > 0
        return dict(status='selected',option=option,target_indices=[26])
    monkeypatch.setattr(page_module,'select_target_framing_pair',select)
    saved = json.loads((Path(__file__).parent/'fixtures/deep_dive_navigation_sources_13.json').read_text(encoding='utf8'))
    observed = np.asarray(saved['sources'][0]['feedback']['semantic_metadata']['pose']['rotation'])
    first = policy._select_page_pair(observed,rows,AXES)
    assert len(first[0]) == 3
    excluded.append(first[0][2])
    second = policy._select_page_pair(observed,rows,AXES)
    assert second is not None
    assert all(not np.array_equal(rotation,excluded[0]) for rotation in second[0])


def test_disabled_framing_keeps_existing_pair_at_boundary(monkeypatch):
    policy,rows,feedback,_ = subject()
    policy._target_framing_enabled = False
    policy._page_face = 'F'
    policy._page_pair = [np.eye(3),_matrix(np.array([.2,0.,0.]))]
    policy._page_pair_framing = (26,)  # Disabled bank must not alter the kernel.
    policy._page_navigation_route = True
    policy._page_pending_cursor = 0
    policy._page_attempts = 1
    monkeypatch.setattr(policy,'_select_page_pair',lambda *args:pytest.fail('disabled framing reselected pair'))
    policy._plan(np.eye(3),rows,AXES,101.)
    assert policy._page_cursor == 1 and policy._page_attempts == 2
