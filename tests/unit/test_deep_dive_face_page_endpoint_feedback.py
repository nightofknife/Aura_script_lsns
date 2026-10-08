"""Endpoint receipt uses actual post-arrival sources, never publication revision."""
from copy import deepcopy

import pytest

from plans.resonance_pc.src.actions._deep_dive_scan_policy import CellScanPolicy
from test_deep_dive_target_face_pages import subject, feedback, record
from test_deep_dive_mixed_scan_policy import AXES


def arrival():
    policy, rows, value, rotation, _ = subject()
    policy._mixed_feedback = value
    policy._at_goal_since = 10.
    policy._interest_now = 10.
    policy._arrival_revision = 0
    # The packet source is valid but was captured before this geometry arrival.
    policy._remember_target_source(value)
    policy._opportunity_current_valid = True
    ready, deadline = policy._waypoint_semantic_feedback(rows,rotation,10.,1)
    assert not ready and deadline == 10.9
    return policy,rows,value,rotation


def test_late_published_prearrival_rgb_cannot_advance_or_finish_dwell():
    policy,rows,value,rotation = arrival()
    before = deepcopy(rows)
    record(policy,rows,value)
    assert not policy._page_views and policy._page_cursor == 0
    assert policy._waypoint_semantic_feedback(rows,rotation,10.5,999) == (False,10.9)
    assert rows == before


def test_true_postarrival_actual_source_is_required_and_can_advance():
    policy,rows,value,rotation = arrival()
    newer = feedback(rows,rotation,frame=11)
    policy._mixed_feedback = newer
    policy._remember_target_source(newer)
    policy._opportunity_current_valid = True
    assert policy._waypoint_semantic_feedback(rows,rotation,10.5,2) == (True,10.9)
    record(policy,rows,newer)
    assert len(policy._page_views) == 1 and policy._page_cursor == 1
    assert policy._page_arrival_started is None


def test_good_postarrival_source_after_absolute_deadline_is_not_page_receipt():
    policy,rows,value,rotation = arrival()
    newer = feedback(rows,rotation,frame=11)
    policy._mixed_feedback = newer
    policy._interest_now = 10.95
    policy._remember_target_source(newer)
    policy._opportunity_current_valid = True
    assert policy._waypoint_semantic_feedback(rows,rotation,10.95,2) == (False,10.9)
    before = deepcopy(rows)
    record(policy,rows,newer)
    assert not policy._page_views and policy._page_cursor == 0
    assert rows == before


@pytest.mark.parametrize('defect',['stale','single_face','model','pose','frame','generation','stamp'])
def test_bad_postarrival_feedback_never_completes_endpoint(defect):
    policy,rows,value,rotation = arrival()
    newer = feedback(rows,rotation,frame=11)
    if defect == 'stale': newer['glyph_anchor_age_sec'] = .8
    elif defect == 'single_face':
        newer['accepted_anchor_observation']['accepted_faces'] = {'F':7}
        newer['refine_diagnostic']['accepted_faces'] = {'F':7}
    elif defect == 'model': newer['target_coverage']['model_executed'] = False
    elif defect == 'pose': newer['semantic_metadata']['pose']['rotation'][0][0] = 2.
    else:
        index = {'frame':2,'generation':3,'stamp':4}[defect]
        policy._page_arrival_key = tuple(policy._source_key(newer['semantic_metadata'])[i]
            if i == index else v for i,v in enumerate(policy._page_arrival_key))
    record(policy,rows,newer)
    assert not policy._page_views


def test_jitter_or_poll_does_not_restart_absolute_page_deadline():
    policy,rows,value,rotation = arrival()
    policy._at_goal_since = 10.7  # Cell re-entered its tolerance after jitter.
    assert policy._waypoint_semantic_feedback(rows,rotation,10.8,99) == (False,10.9)
    assert policy._waypoint_semantic_feedback(rows,rotation,10.95,100) == (False,10.9)


def test_legacy_revision_and_point_six_deadline_are_unchanged():
    policy = CellScanPolicy()
    policy._at_goal_since = 10.
    policy._arrival_revision = 4
    assert policy._waypoint_semantic_feedback([],None,10.3,4) == (False,10.6)
    assert policy._waypoint_semantic_feedback([],None,10.3,5) == (True,10.6)
    assert policy._waypoint_semantic_feedback([],None,10.3,None) == (True,10.6)


def test_real_choose_times_out_at_original_absolute_deadline_after_jitter():
    policy,rows,value,rotation = arrival()
    policy.expected_inspirations = 3
    rows[0].update(occupant='unknown',occupant_status='unknown',evidence=[])
    policy._route = [rotation.copy()]
    policy._final_rotation = rotation.copy()
    policy._target_indices = list(range(18,27))
    policy.target_face = 'F'
    policy._started = 10.
    policy._at_goal_since = 10.7
    policy.choose(rotation,rows,AXES,elapsed=10.95,semantic_revision=999,anchor_feedback=value)
    assert policy._page_cursor == 1 and policy._page_attempts == 2
    assert policy.insufficient[-1]['reason'] == 'semantic_dwell_timeout'
    assert policy._page_arrival_started is None  # Reset only by new dispatch.


def test_legacy_real_choose_preserves_point_six_timeout(monkeypatch):
    _,rows,value,rotation,second = subject()
    rows[0].update(occupant='unknown',occupant_status='unknown',evidence=[])
    policy = CellScanPolicy(recognition_goal='targets')
    policy._route = [rotation.copy()]
    policy._final_rotation = rotation.copy()
    policy._target_indices = list(range(18,27))
    policy.target_face = 'F'
    policy._started = policy._at_goal_since = 10.
    policy._arrival_revision = 0
    calls = []
    def plan(*args):
        calls.append(True)
        policy._route = [second.copy()]
        policy._final_rotation = second.copy()
        policy._at_goal_since = None
    monkeypatch.setattr(policy,'_plan',plan)
    first = policy.choose(rotation,rows,AXES,elapsed=10.5,semantic_revision=0,anchor_feedback=value)
    assert first['reason'] == 'await_semantic_frame' and not calls
    policy.choose(rotation,rows,AXES,elapsed=10.61,semantic_revision=0,anchor_feedback=value)
    assert calls == [True] and policy.insufficient[-1]['reason'] == 'semantic_dwell_timeout'
