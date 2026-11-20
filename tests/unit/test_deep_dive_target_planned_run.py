import asyncio
from copy import deepcopy
from types import SimpleNamespace

import numpy as np
import pytest

from plans.resonance_pc.src.actions import consciousness_deep_dive_planned_run_pc_actions as flow
from plans.resonance_pc.src.actions._deep_dive_inspiration_planner import plan_inspiration
from plans.resonance_pc.src.actions._deep_dive_native_layout import controls_from_board
from plans.resonance_pc.src.actions._deep_dive_planner_rules import coord_dict, move_dict
from plans.resonance_pc.src.actions import _deep_dive_planned_run_vision as vision
from plans.aura_base.src.platform.contracts import TargetRuntimeError


@pytest.mark.parametrize('formation_level', [20101955, 20101977])
def test_vortex_battle_handoff_checks_the_native_formation_level(monkeypatch, formation_level):
    expected_level = 20101955
    pending = dict(kind='option_submit', battle_level=expected_level, native={'id': 86100252})
    context = dict(event_type='vortex', source_action_id='move', flow={'vortex_pending': pending})
    state = {'event_stack': [context]}
    scene = dict(scene='battle_formation', native={'subject_id': formation_level})
    stops = []
    monkeypatch.setattr(flow, '_stable_scene', lambda *_: True)
    monkeypatch.setattr(flow, '_stop', lambda state, reason, image: stops.append(reason))
    monkeypatch.setattr(flow, '_log', lambda *args, **kwargs: None)

    asyncio.run(flow._events(state, None, None, scene))

    if formation_level == expected_level:
        assert state['event_stack'][1]['owner'] == 'vortex_sub_battle'
        assert 'vortex_pending' not in context['flow']
        assert not stops
    else:
        assert len(state['event_stack']) == 1
        assert context['flow']['vortex_pending'] is pending
        assert stops == ['vortex_battle_level_mismatch']


def test_native_rotation_uses_projected_control_and_cancels_mismatched_preview():
    layout = dict(coordinate_frame='native_board', success=True, layout_complete=True,
                  player_cell=coord_dict(4))
    snapshot = dict(layout=layout, plane_epoch='p', scan_epoch=1, map_revision=0)
    action = flow.directed.begin_layer_rotation(3, 4, snapshot)
    assert action['phase'] == 'open_mode'
    raw = dict(size=3, frame=10, width=1280, height=720, player=dict(face=1, row=2, col=2),
               turn={}, moves=[], rotations=[dict(name='back', next_face=2, point=[530, 241])])
    controls = controls_from_board(raw)
    scene = dict(valid=True, scene='choose_rotate')
    for _ in range(3):
        step = flow.directed.advance_operation(action, None, scene, snapshot, native_controls=controls)
    assert step['action'] == 'select_rotate' and step['point'] == [530, 241]
    assert action['binding']['source'] == 'bridge'

    scene = dict(valid=True, scene='rotate_preview', rotate_cancel_point=[480, 543],
                 rotate_confirm_point=[790, 543])
    wrong = controls_from_board(dict(raw, preview_face=4, rotations=[]))
    for _ in range(2):
        step = flow.directed.advance_operation(action, None, scene, snapshot, native_controls=wrong)
    assert step['action'] == 'cancel_rotation'
    assert action['preview_evidence']['observed_rotation_id'] == 2
    assert not action['submitted']


@pytest.mark.parametrize('selected_col,expected', [(1, 'entry_selected'), (3, 'blocked')])
def test_native_move_requires_the_actual_selected_destination(selected_col, expected):
    cells = [dict(coord_dict(slot), occupant='none', occupant_status='confirmed',
                  node_status='known', node_kind='empty', icon_id='white_diamond') for slot in range(54)]
    cells[7].update(node_kind='shop', icon_id='blue_scales')
    layout = dict(coordinate_frame='native_board', success=True, layout_complete=True,
                  player_cell=coord_dict(4), cells=cells)
    snapshot = dict(layout=layout, plane_epoch='p', scan_epoch=1, map_revision=0)
    action = flow.directed.begin_move(7, 4, snapshot)
    raw = dict(size=3, frame=20, width=1280, height=720, player=dict(face=1, row=2, col=2), turn={},
               rotations=[], moves=[dict(face=1, row=2, col=1, point=[531, 391])])
    scene = dict(valid=True, scene='choose_move')
    for _ in range(3):
        step = flow.directed.advance_operation(action, None, scene, snapshot, native_controls=controls_from_board(raw))
    assert step['action'] == 'select_move' and step['point'] == [531, 391]
    scene = dict(valid=True, scene='event_entry', event_type='shop')
    picked = controls_from_board(dict(raw, selected=dict(face=1, row=2, col=selected_col)))
    if selected_col == 1:
        unclear = dict(valid=True, scene='ambiguous_event_entry')
        assert flow.directed.advance_operation(
            action, None, unclear, snapshot, native_controls=picked)['status'] == 'waiting'
    for _ in range(2):
        step = flow.directed.advance_operation(action, None, scene, snapshot, native_controls=picked)
    assert step['status'] == expected


@pytest.mark.parametrize('player,boss,inspirations,rounds,rotations,expected_yield', [
    (21, 23, (18, 20), 3, 1, 0),  # Taking the nearby inspiration reduces the deadline probability.
    (4, 0, (1,), 1, 0, 1),  # Both moves next to the boss meet on time, and one picks up inspiration.
])
def test_combined_solver_prefers_deadline_probability_then_inspiration(
        player, boss, inspirations, rounds, rotations, expected_yield):
    result = plan_inspiration(player, boss, inspirations, rounds, rotations_left=rotations,
                              encounter_first=True, time_budget_sec=60.)
    assert result['objective'] == 'max_deadline_encounter_probability_then_inspiration'
    assert result['metrics']['deadline_encounter_probability'] == pytest.approx(1.)
    assert result['metrics']['expected_successful_new_inspirations'] == pytest.approx(expected_yield)
    if expected_yield:
        assert result['next_action'] == move_dict(player, inspirations[0])


@pytest.mark.parametrize('unavailable', [False, True])
def test_planned_hud_gets_plane_and_turn_from_bridge(monkeypatch, unavailable):
    def read_turn():
        if unavailable:
            raise TargetRuntimeError('deep_dive_turn_unavailable', 'counter missing')
        return dict(plane_index=2, rounds_remaining=3, moves_used=1, moves_total=1,
                    rotations_used=0, rotations_total=1, frame=16)

    def read_pair(image, name):
        assert name == 'inspirations'
        return (1, 2), {}

    monkeypatch.setattr(vision.hud_templates, '_read_pair', read_pair)
    monkeypatch.setattr(vision, 'read_rounds_template', lambda image: pytest.fail('visual round lookup'))
    monkeypatch.setattr(vision, '_ocr_text', lambda *_args: pytest.fail('visual plane lookup'))
    app = SimpleNamespace(controller=SimpleNamespace(bridge=SimpleNamespace(read_deep_dive_turn=read_turn)))
    scene = dict(valid=True, scene='board', plane_evidence={'plane_index': 1})
    hud = asyncio.run(flow._read_runtime_hud(app, np.zeros((720, 1280, 3), dtype=np.uint8), object(), scene))
    assert hud['collected_count'] == 1
    if unavailable:
        assert all(hud[field] is None for field in vision.TURN_FIELDS)
    else:
        assert hud['plane_index'] == 2 and hud['rounds_remaining'] == 3 and hud['moves_used'] == 1
        assert all(hud['evidence_source'][field] == 'bridge_turn' for field in vision.TURN_FIELDS)


def test_missing_destination_enters_requested_read_instead_of_input(tmp_path, monkeypatch):
    layout = dict(cells=[dict(coord_dict(i), occupant='unknown', occupant_status='unknown',
                             node_status='unknown', icon_id=None) for i in range(54)],
                  player_cell=coord_dict(4), singularity_cell=coord_dict(31), inspiration_cells=[])
    hud = dict(rounds_remaining=6, moves_used=0, moves_total=1, rotations_used=0, rotations_total=1,
               collected_count=0, inspiration_total=0)
    state = dict(status='running', phase='planning', strategy='chase', layout=layout,
                 hud=hud, collected_count=0, planning_time_budget_sec=1., sequence=0,
                 wide_reference={'status': 'ready'}, plane_epoch='p', scan_epoch=1, map_revision=0,
                 pose_epoch=0, output_dir=str(tmp_path))
    move = move_dict(4, 1)
    async def save(*_args): pass
    monkeypatch.setattr(flow, '_save', save)
    def solve(_layout, *, strategy, rounds_remaining, moves_left, rotations_left, **_kwargs):
        assert strategy == flow.ENCOUNTER_THEN_INSPIRATION
        assert (rounds_remaining, moves_left, rotations_left) == (6, 1, 1)
        return dict(success=True, status='solved', next_action=move)
    monkeypatch.setattr(flow, 'plan_current_state', solve)
    monkeypatch.setattr(flow.directed, 'begin_move', lambda *_: (_ for _ in ()).throw(
        AssertionError('unread destination reached input binding')))
    asyncio.run(flow._plan(state, None, None))
    assert state['phase'] == 'supplement_cells'
    assert state['strategy'] == flow.ENCOUNTER_THEN_INSPIRATION
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
