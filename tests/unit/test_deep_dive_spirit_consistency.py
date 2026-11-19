"""Synthetic ring evidence and HUD limits must agree before either strategy acts."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plans.resonance_pc.src.actions import consciousness_deep_dive_planned_run_pc_actions as flow


HUD = dict(plane_index=1, rounds_remaining=6, moves_used=0, moves_total=1,
           rotations_used=0, rotations_total=1, collected_count=0, inspiration_total=2)


def layout(count):
    return dict(cells=[dict(face='U', row=0, col=i, occupant='inspiration') for i in range(count)],
                inspiration_cells=[dict(face='U', row=0, col=i) for i in range(count)])


def state(tmp_path, strategy):
    return dict(status='running', phase='accept_scan', strategy=strategy, scan_epoch=1,
                map_revision=0, pose_epoch=0, scan_candidate=layout(3), known_cells=54,
                output_dir=str(tmp_path), scan_start_hud=deepcopy(HUD), hud_stable=0,
                event_stack=[], terminal_stable=0)


@pytest.mark.parametrize('strategy', ['chase', 'inspiration'])
def test_excess_count_waits_for_stable_hud_then_rescans_once(tmp_path, strategy):
    run = state(tmp_path, strategy)
    board = dict(valid=True, scene='board', player_turn=True)
    asyncio.run(flow._advance_state(run, None, None, None, None, board, HUD))
    assert run['phase'] == 'accept_scan'  # One OCR sample cannot accept/reject a map.
    asyncio.run(flow._advance_state(run, None, None, None, None, board, HUD))
    assert run['phase'] == 'scan_turn'
    assert run['layout'] is None and run['scan_candidate'] is None
    assert run['inspiration_consistency']['upper_bound'] == 2
    assert run['inspiration_conflict_retry']['attempts'] == 1
    run.update(phase='accept_scan', scan_candidate=layout(3), scan_epoch=2)
    asyncio.run(flow._advance_state(run, None, None, None, None, board, HUD))
    asyncio.run(flow._advance_state(run, None, None, None, None, board, HUD))
    assert run['status'] == 'blocked'
    assert run['reason'] == 'inspiration_count_exceeds_hud'
    assert run['layout'] is None
    assert len(run['scan_candidate']['inspiration_cells']) == 3  # Never rank/drop excess cells.


@pytest.mark.parametrize('count', [0, 1, 2])
def test_boss_swallowing_does_not_require_count_equality(tmp_path, count):
    run = state(tmp_path, 'chase')
    run['scan_candidate'] = layout(count)
    assert flow._accept_scan_candidate(run, HUD)
    assert run['layout']['inspiration_consistency']['valid']


@pytest.mark.parametrize('item_first,consumed_quantity', [(False, 1), (True, 2)])
def test_singularity_overlap_continues_through_native_planning(tmp_path, monkeypatch, item_first, consumed_quantity):
    hud = dict(HUD, rounds_remaining=3, collected_count=1)
    singularity = dict(type='singularity', face=2, row=3, col=2)
    consumed = dict(singularity, type='inspiration', item_id=11400622, quantity=consumed_quantity)
    free = dict(consumed, face=1, row=1, quantity=1)
    overlap = [consumed, singularity] if item_first else [singularity, consumed]
    board = dict(schema='resonance_pc.deep_dive_board.v1', coordinate_base=1, size=3,
                 turn=dict(hud, move_distance=1),
                 faces=[dict(face=face, rows=[[dict(type='empty') for _ in range(3)]
                                             for _ in range(3)]) for face in range(1, 7)],
                 objects=[dict(type='player', face=2, row=1, col=2), *overlap, free])
    app = SimpleNamespace(controller=SimpleNamespace(bridge=SimpleNamespace(read_deep_dive_board=lambda: board)))
    run = dict(status='running', phase='scan_turn', plane_index=hud['plane_index'], plane_epoch='fixture',
               scan_epoch=0, map_revision=0, pose_epoch=0, hud_stable=0, terminal_stable=0,
               safety_round_limit=100, turns_completed=0, planning_time_budget_sec=10,
               reward_ledger={}, event_stack=[], output_dir=str(tmp_path))
    observation = dict(valid=True, scene='board', player_turn=True)
    monkeypatch.setattr(flow, '_save', AsyncMock())

    async def scan_and_plan():
        for _ in range(5):
            await flow._advance_state(run, app, None, None, None, observation, hud)

    asyncio.run(scan_and_plan())
    assert run['status'] == 'running' and run['phase'] == 'operation'
    assert run['pending_action']['kind'] in {'move', 'rotate'}
    layout = run['layout']
    assert layout['singularity_cell'] == dict(face='F', row=1, col=2)
    assert layout['inspiration_cells'] == [dict(face='U', row=1, col=0)]
    cell = next(cell for cell in layout['cells'] if cell['occupant'] == 'singularity')
    assert cell['objects'] == [singularity]
    assert consumed in layout['native_board']['objects']
    assert run['collected_count'] == 1 and run['reward_ledger']['1']['gained'] == 0


def test_uncollected_upper_bound_and_cached_list_cannot_bypass_gate():
    hud = dict(HUD, collected_count=1)
    assert flow._inspiration_consistency(layout(1), hud)['valid']
    assert not flow._inspiration_consistency(layout(2), hud)['valid']
    inconsistent = layout(2)
    inconsistent['inspiration_cells'] = []
    assert not flow._inspiration_consistency(inconsistent, hud)['valid']


@pytest.mark.parametrize('strategy', ['chase', 'inspiration'])
def test_planner_defensively_rejects_inconsistent_map(tmp_path, strategy):
    run = state(tmp_path, strategy)
    run.update(phase='planning', layout=layout(3), hud=HUD, wide_reference={'status': 'ready'})
    asyncio.run(flow._plan(run, None, None))
    assert run['status'] == 'blocked'
    assert run['reason'] == 'inspiration_count_exceeds_hud'
