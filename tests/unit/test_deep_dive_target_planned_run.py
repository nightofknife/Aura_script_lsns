import asyncio
from copy import deepcopy

from plans.resonance_pc.src.actions import consciousness_deep_dive_planned_run_pc_actions as flow
from plans.resonance_pc.src.actions._deep_dive_planner_rules import coord_dict, move_dict


def test_missing_destination_enters_requested_read_instead_of_input(tmp_path, monkeypatch):
    layout = dict(cells=[dict(coord_dict(i), occupant='unknown', occupant_status='unknown',
                             node_status='unknown', icon_id=None) for i in range(54)],
                  player_cell=coord_dict(4), singularity_cell=coord_dict(31), inspiration_cells=[])
    hud = dict(rounds_remaining=6, moves_used=0, rotations_used=0, collected_count=0, inspiration_total=0)
    state = dict(status='running', phase='planning', strategy='chase', layout=layout,
                 hud=hud, collected_count=0, planning_time_budget_sec=1., sequence=0,
                 wide_reference={'status': 'ready'}, plane_epoch='p', scan_epoch=1, map_revision=0,
                 pose_epoch=0, output_dir=str(tmp_path))
    move = move_dict(4, 1)
    async def save(*_args): pass
    monkeypatch.setattr(flow, '_save', save)
    monkeypatch.setattr(flow, 'plan_current_state', lambda *_args, **_kwargs:
                        dict(success=True, status='solved', next_action=move))
    monkeypatch.setattr(flow.directed, 'begin_move', lambda *_: (_ for _ in ()).throw(
        AssertionError('unread destination reached input binding')))
    asyncio.run(flow._plan(state, None, None))
    assert state['phase'] == 'supplement_cells'
    assert state['required_cells'] == [1]
    assert state['layout']['cells'][1]['node_status'] == 'unknown'


def test_invalidation_discards_requested_node_and_saved_action():
    state = dict(map_revision=3, pose_epoch=2, resume_planned_action={'kind': 'move'},
                 required_cells=[1], required_cell_votes={'1': {'sources': [4, 5]}})
    flow._invalidate(state, 'enemy_stage')
    assert not any(key in state for key in ('resume_planned_action', 'required_cells', 'required_cell_votes'))
    assert state['layout'] is None


def test_compact_layout_keeps_real_target_evidence_and_no_null_count_container():
    layout = dict(cells=[dict(coord_dict(i), occupant='unknown', occupant_status='unknown')
                         for i in range(54)], recognition_goal='targets', targets_ready=True,
                  expected_inspirations=2, semantic_source={'frame_id': 8})
    layout['cells'][4].update(evidence=[{'frame_id': 8, 'group': 2, 'occupant': 'player'}],
                              occupant_evidence_counts={'player': 2})
    compact = flow._compact_layout(layout, {'scan_epoch': 1, 'plane_epoch': 'p'})
    assert compact['cells'][4]['evidence'] == layout['cells'][4]['evidence']
    assert 'occupant_evidence_counts' not in compact['cells'][0]
    assert compact['expected_inspirations'] == 2


def test_compact_four_views_preserves_proof_without_overriding_legacy_fallbacks():
    state=dict(scan_epoch=3,plane_epoch='p')
    legacy=flow._compact_layout(dict(cells=[],scan_epoch='legacy'),state)
    assert 'unassociated_target_candidates' not in legacy
    assert 'glyph_anchor_age_sec' not in legacy
    layout=dict(cells=[],scan_epoch='actual_wgc_epoch',scan_route='four_views',
        frame_convention_version='four_views_reset_player_up_v1',reset_player_face='U',
        glyph_anchor_age_sec=.3,unassociated_target_candidates=[],
        raw_target_candidate_associations=[{'cell_index':None}],
        explained_target_candidates=[{'adds_ownership_group':False}])
    compact=flow._compact_layout(layout,state)
    assert compact['source_scan_epoch']=='actual_wgc_epoch' and compact['scan_epoch']==3
    assert compact['unassociated_target_candidates']==[]
    assert compact['raw_target_candidate_associations']==layout['raw_target_candidate_associations']
    assert compact['explained_target_candidates']==layout['explained_target_candidates']
    assert compact['frame_convention_version']==layout['frame_convention_version']
