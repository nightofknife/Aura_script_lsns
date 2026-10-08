"""Target-list closure and partial maps must not fabricate ordinary content."""
from copy import deepcopy

import pytest

from plans.resonance_pc.src.actions import _deep_dive_movement_planner as planner
from plans.resonance_pc.src.actions import _deep_dive_directed_operation_flow as directed
from plans.resonance_pc.src.actions import _deep_dive_operation_frame as operation
from plans.resonance_pc.src.actions._deep_dive_planner_rules import coord_dict, move_dict
from plans.resonance_pc.src.actions._deep_dive_target_readiness import (
    check_required_cells, required_cells, targets_readiness,
)


def layout(*, bound=2, all_occupancies=False):
    cells=[]
    for slot in range(54):
        proof=[dict(frame_id=100+slot*3+g,group=g,occupant='none',confidence=.8)for g in (1,2,3)]
        row=dict(coord_dict(slot),occupant='none'if all_occupancies else 'unknown',
            occupant_status='confirmed'if all_occupancies else 'unknown',node_status='unknown',
            icon_id=None,node_kind=None,confidence=.8 if all_occupancies else 0.,
            occupant_evidence_counts={'none':3}if all_occupancies else {},evidence=proof if all_occupancies else [])
        if slot%9==0 and not all_occupancies:
            row.update(occupant_evidence_counts={'none':1},evidence=proof[:1])
        cells.append(row)
    for slot,kind in ((4,'player'),(31,'singularity'),(3,'inspiration'),(35,'inspiration')):
        cells[slot].update(occupant=kind,occupant_status='confirmed',node_status='not_required_target',
            confidence=.9,occupant_evidence_counts={kind:2},
            evidence=[dict(frame_id=300+slot*3+g,group=g,occupant=kind,confidence=.9)for g in (1,2)])
    return dict(schema='resonance_pc.deep_dive_layout.v1',coordinate_frame='scan_local',
        recognition_goal='targets',targets_ready=True,success=True,status='targets_ready',layout_complete=False,
        cells=cells,player_cell=coord_dict(4),singularity_cell=coord_dict(31),
        inspiration_cells=[coord_dict(3),coord_dict(35)],expected_inspirations=bound,faces_observed=6,
        diagnostics=dict(glyph_anchor_age_sec=.2,glyph_anchor_reason='known_multi_face_joint_fit'),
        target_clues=[],target_candidate_associations=[])


def snapshot(value):
    return dict(layout=value,plane_epoch=1,scan_epoch=2,map_revision=0,pose_epoch=1)


def test_hud_upper_bound_closes_targets_and_unknown_cells_are_unchanged():
    value=layout();before=deepcopy(value)
    result=targets_readiness(value)
    assert result['ready'] and not result['occupancy_complete']
    assert result['target_list_complete_by']=='hud_upper_bound_reached'
    assert result['inspiration_slots']==[3,35]
    assert value==before and value['cells'][1]['occupant']=='unknown'


def test_boss_consumed_inspiration_requires_all_occupancy_not_ordinary_pages():
    value=layout(bound=3)
    assert targets_readiness(value)['reason']=='inspiration_below_upper_bound_requires_complete_occupancy'
    complete=layout(bound=3,all_occupancies=True)
    result=targets_readiness(complete)
    assert result['ready'] and result['occupancy_complete']
    assert all(row['icon_id']is None for row in complete['cells'])
    complete['cells'][0]['evidence']=complete['cells'][0]['evidence'][:2]
    assert not targets_readiness(complete)['ready']


@pytest.mark.parametrize('malformed', [True, float('nan'), float('inf')])
def test_invalid_negative_confidence_cannot_prove_boss_consumption(malformed):
    value=layout(bound=3,all_occupancies=True)
    value['cells'][0]['confidence']=malformed
    assert targets_readiness(value)['reason']=='inspiration_below_upper_bound_requires_complete_occupancy'


def test_malformed_target_proof_is_rejected_instead_of_becoming_empty_evidence():
    value=layout();value['cells'][4]['occupant_evidence_counts']=None
    assert targets_readiness(value)['reason']=='invalid_target_evidence'
    value=layout();value['target_candidate_associations']={}
    assert targets_readiness(value)['reason']=='invalid_target_candidate_evidence'


@pytest.mark.parametrize('mutate,reason',[
    (lambda value:value.pop('expected_inspirations'),'inspiration_hud_unresolved'),
    (lambda value:value.update(expected_inspirations=1),'inspiration_count_exceeds_hud'),
    (lambda value:value.update(prediction_only=True),'prediction_only_layout'),
    (lambda value:value['diagnostics'].update(glyph_anchor_age_sec=1.251),'current_glyph_anchor_required'),
    (lambda value:value.update(faces_observed=5),'six_faces_not_observed'),
    (lambda value:value['cells'][45].update(evidence=[]),'six_faces_not_observed'),
    (lambda value:value['cells'][4]['evidence'][1].update(group=1),'independent_positive_target_evidence_required'),
    (lambda value:value['cells'][4].update(confidence=.69),'independent_positive_target_evidence_required'),
    (lambda value:value['cells'][1].update(occupant_status='conflict'),'target_occupancy_conflict'),
    (lambda value:value['cells'][1].update(occupant_evidence_counts={'inspiration':1}),'unconfirmed_target_evidence'),
    (lambda value:value.update(player_cell=coord_dict(1)),'player_cell_contradicts_target_evidence'),
])
def test_reject_incomplete_or_conflicting_target_proof(mutate,reason):
    value=layout();mutate(value)
    assert targets_readiness(value)['reason']==reason


def test_low_score_masks_do_not_claim_targets_but_strong_unassociated_candidates_block():
    value=layout();weak=dict(kind='inspiration',confidence=.1,confirmable=False)
    value.update(target_clues=[weak],target_candidate_associations=[weak])
    assert targets_readiness(value)['ready']
    weak['confidence']=.25
    assert targets_readiness(value)['reason']=='unassociated_target_candidates'
    weak.update(cell_index=3)
    assert targets_readiness(value)['ready']
    value['target_candidate_associations']=[]
    assert targets_readiness(value)['reason']=='target_candidate_associations_incomplete'


@pytest.mark.parametrize('strategy',['chase','inspiration'])
def test_both_algorithms_receive_proven_targets_without_filling_unknown_pages(monkeypatch,strategy):
    value=layout();before=deepcopy(value);calls=[];move=move_dict(4,1)
    def solve(player,boss,*args,**kwargs):
        calls.append((player,boss,args))
        return dict(status='solved',next_action=move,player_turn_actions=[move],
            predicted_boss_branches=[],boss_branches=[],deadline_probability=1.,
            metrics=dict(deadline_encounter_probability=1.))
    monkeypatch.setattr(planner,'plan_chase'if strategy=='chase'else'plan_inspiration',solve)
    result=planner.plan_current_state(value,strategy,rounds_remaining=1,moves_left=1,rotations_left=0)
    assert result['success'] and result['required_cells']==[1]
    assert calls[0][:2]==(4,31)
    if strategy=='inspiration':assert calls[0][2][0]==(3,35)
    assert value==before


def test_readiness_flag_cannot_replace_actual_proof_or_opt_into_legacy_partial():
    value=layout();value['diagnostics']['glyph_anchor_age_sec']=2.
    with pytest.raises(ValueError,match='current_glyph_anchor_required'):
        planner.plan_current_state(value,rounds_remaining=0)
    value=layout();value.pop('recognition_goal')
    with pytest.raises(ValueError,match='complete successful scan'):
        planner.plan_current_state(value,rounds_remaining=0)


@pytest.mark.parametrize('strategy',['chase','inspiration'])
def test_actual_solver_budget_remains_safe_with_partial_map(strategy):
    value=layout();before=deepcopy(value)
    result=planner.plan_current_state(value,strategy,rounds_remaining=1,
        moves_left=1,rotations_left=0,time_budget_sec=.05)
    assert result['status'] in ('solved','blocked','search_budget_exhausted')
    assert result['recognition_goal']=='targets'
    assert value==before
    if result['success']:
        assert result['next_action']['kind']=='move'
    else:
        assert result['next_action']is None and result['player_turn_actions']==[]


def test_complete_legacy_layout_still_uses_original_node_guard():
    value=layout(all_occupancies=True);value.update(status='completed',layout_complete=True)
    value.pop('recognition_goal');value.pop('targets_ready')
    with pytest.raises(ValueError,match='Unknown or conflicting node content'):
        planner.plan_current_state(value,rounds_remaining=0)
    for row in value['cells']:
        if row['occupant']=='none':row.update(node_status='known',icon_id='red_single_eye')
    result=planner.plan_current_state(value,rounds_remaining=0)
    assert result['status']=='deadline_exhausted' and result['next_action']is None


def test_move_requires_actual_destination_node_but_rotation_and_target_tiles_do_not():
    value=layout()
    assert required_cells(value,move_dict(4,1))==[1]
    assert check_required_cells(value,move_dict(4,1))==dict(ready=False,reason='required_cells_unknown',required_cells=[1])
    assert required_cells(value,move_dict(4,3))==[]
    assert required_cells(value,dict(kind='rotate_layer',rotation_id=0))==[]
    action=directed.prepare_operation('move',snapshot(value))
    assert action['expected_actor_slot']==4 and action['phase']=='wide_reference'
    with pytest.raises(ValueError,match='required_cells_unknown'):
        directed.begin_move(1,4,snapshot(value))
    row=value['cells'][1];row.update(occupant='none',occupant_status='confirmed',node_status='known',icon_id='blue_scales')
    assert check_required_cells(value,move_dict(4,1))['ready']
    begun=directed.begin_move(1,4,snapshot(value))
    assert begun['target_occupant']=='none' and begun['target_icon_id']=='blue_scales'
    row.update(node_status='unknown')
    assert directed.advance_operation(begun,None,dict(valid=True),snapshot(value))['reason']=='required_cells_unknown'


def test_prediction_only_map_cannot_prepare_operation_even_with_copied_success_flags():
    value=layout();value['prediction_only']=True
    with pytest.raises(ValueError,match='prediction_only_layout'):
        directed.prepare_operation('move',snapshot(value))


def test_partial_registration_uses_known_subset_with_unchanged_three_face_threshold(monkeypatch):
    value=layout();before=deepcopy(value);cells,actor=operation._layout(value)
    slots=[0,1,2,9,10,18,19]
    for slot in slots:
        cells[slot].update(occupant='none',occupant_status='confirmed',node_status='known',icon_id=f'icon{slot}')
    # Unknown occupancy is not an anchor even if stale page fields are present.
    cells[20].update(icon_id='fake',node_status='known')
    readings=[dict(operation_slot=slot,icon_id=f'icon{slot}',confidence=.9)for slot in slots]
    readings.append(dict(operation_slot=20,icon_id='fake',confidence=.9))
    monkeypatch.setattr(operation,'_q_candidates',lambda *args:iter([dict(symmetry_id=0,Q=[],op_to_logical=list(range(54)))]))
    fit=dict(actor=4,readings=readings,rmse=1.)
    registered=operation._registration(cells,fit,actor)
    assert registered and registered[0]['matched']==7 and registered[0]['face_anchors']==[3,2,2]
    fit['readings']=readings[:-2]
    assert not operation._registration(cells,fit,actor)
    assert before['cells'][20]['occupant']=='unknown'
