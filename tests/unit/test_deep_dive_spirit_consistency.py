"""Real ring/hex evidence and HUD limits must agree before either strategy acts."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions._deep_dive_layout_semantics import detect_targets
from plans.resonance_pc.src.actions import consciousness_deep_dive_planned_run_pc_actions as flow


FIXTURES = Path(__file__).parents[1] / 'fixtures' / 'deep_dive_spirit'
SAMPLES = json.loads((FIXTURES / 'samples.json').read_text(encoding='utf8'))['samples']


@pytest.mark.parametrize('sample', SAMPLES, ids=lambda sample: sample['name'])
def test_saved_ring_and_competing_hex_views(sample):
    patch = cv2.cvtColor(cv2.imread(str(FIXTURES / (sample['name'] + '.png'))), cv2.COLOR_BGR2RGB)
    image = np.zeros((720, 1280, 3), np.uint8)
    image[210:390, 550:730] = patch
    candidates = [row for row in detect_targets(image) if row['kind'] == 'inspiration'
                  and np.linalg.norm(np.asarray(row['point']) - [640, 300]) < 35]
    strong = [row for row in candidates if row.get('confirmable', True) and row['confidence'] >= .6]
    assert bool(strong) is sample['strong_inspiration']
    if not sample['strong_inspiration']:
        assert all(row['confirmable'] is False for row in candidates)


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
