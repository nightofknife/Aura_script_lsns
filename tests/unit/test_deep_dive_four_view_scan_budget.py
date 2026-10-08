"""Four-view route protection and public task forwarding; no game resources."""
import asyncio
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from packages.aura_core.scheduler.validation import InputValidator
from plans.resonance_pc.src.actions import consciousness_deep_dive_planned_run_pc_actions as flow
from plans.resonance_pc.src.actions._deep_dive_scan_budget import (
    SCAN_ROUTES, resolve_scan_budget, post_scan_recognition_budget,
)
from plans.resonance_pc.src.actions._deep_dive_planner_rules import coord_dict


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('route', SCAN_ROUTES[:-1])
def test_legacy_routes_keep_90_second_cap(route):
    assert resolve_scan_budget(route) == 60.
    assert resolve_scan_budget(route, 300) == 90.
    assert resolve_scan_budget(route, 30) == 30.
    assert post_scan_recognition_budget(dict(scan_route=route, scan_time_budget_sec=300,
                                            recognition_elapsed_sec=91)) == (90., 91)


def test_four_views_keeps_scan_and_post_scan_allowances_separate():
    assert resolve_scan_budget('four_views') == 300.
    assert resolve_scan_budget('four_views', 900) == 900.
    assert post_scan_recognition_budget(dict(scan_route='four_views', scan_time_budget_sec=300,
        recognition_elapsed_sec=102.6, post_scan_recognition_elapsed_sec=2.)) == (90., 2.)


@pytest.mark.parametrize('value', [True, '300', float('nan'), float('inf'), 4, 901])
def test_invalid_budgets_are_rejected(value):
    with pytest.raises(ValueError):
        resolve_scan_budget('four_views', value)


def test_missing_route_preserves_legacy_session_budget():
    assert post_scan_recognition_budget(dict(scan_time_budget_sec=60, recognition_elapsed_sec=59)) == (60., 59)
    with pytest.raises(ValueError):
        resolve_scan_budget('unexpected', 300)


@pytest.mark.parametrize('task_name,budget_name,node', [
    ('consciousness_deep_dive_scan_pc', 'time_budget_sec', 'scan'),
    ('consciousness_deep_dive_planned_run_pc', 'scan_time_budget_sec', 'initialize'),
])
def test_public_tasks_allow_four_views_and_forward_route(task_name, budget_name, node):
    task = yaml.safe_load((ROOT / 'plans/resonance_pc/tasks' / f'{task_name}.yaml').read_text('utf-8'))[task_name]
    schema = task['meta']['inputs']
    inputs = {row['name']: row for row in schema}
    assert inputs['scan_route']['enum'] == list(SCAN_ROUTES)
    assert inputs[budget_name]['default'] == 300 and inputs[budget_name]['max'] == 900
    assert task['steps'][node]['params']['scan_route'] == '{{ inputs.scan_route }}'
    valid, normalized = InputValidator(None).validate_inputs_against_meta(schema,
        {'scan_route': 'four_views', budget_name: 900})
    assert valid and normalized['scan_route'] == 'four_views' and normalized[budget_name] == 900
    valid, _ = InputValidator(None).validate_inputs_against_meta(schema, {'scan_route': 'unknown'})
    assert not valid


async def _nothing(*_args, **_kwargs):
    pass


class WindowSizeOnly:
    def get_window_size(self):
        return 1280, 720


@pytest.mark.parametrize('route,expected', [('cells', 90.), ('four_views', 300.)])
def test_initialize_persists_normalized_route_and_budget(tmp_path, monkeypatch, route, expected):
    saved = []
    async def capture(*_args):
        return None, dict(scene='board', player_turn=True)
    async def save(state, *_args):
        saved.append(deepcopy(state))
    monkeypatch.setattr(flow, 'resolve_base_path', lambda: tmp_path)
    monkeypatch.setattr(flow, '_capture', capture)
    monkeypatch.setattr(flow, '_save', save)
    monkeypatch.setattr(flow, '_terminal', lambda *_args: False)
    monkeypatch.setattr(flow, '_cancel_check', lambda: None)
    monkeypatch.setattr(flow.asyncio, 'sleep', _nothing)
    outcome = asyncio.run(flow.initialize_deep_dive_planned_run(scan_route=route,
        app=WindowSizeOnly(), state_store=object()))
    assert outcome['status'] == 'running'
    assert saved[-1]['scan_route'] == route and saved[-1]['scan_time_budget_sec'] == expected
    assert saved[-1]['post_scan_recognition_elapsed_sec'] == 0.


@pytest.mark.parametrize('route,expected', [('cells', 90.), ('four_views', 300.)])
def test_scan_turn_forwards_route_without_bypassing_target_readiness(tmp_path, monkeypatch, route, expected):
    received = []
    async def scan(_app, **kwargs):
        received.append(kwargs)
        # Geometry can contain 54 slots while all target evidence is unresolved.
        return dict(success=True, status='complete', reason='fixture_targets_unresolved',
                    layout=dict(success=True, cells=[coord_dict(i) for i in range(54)]), summary={})
    state = dict(status='running', scan_route=route, scan_time_budget_sec=300., scan_epoch=0,
                 safety_round_limit=100, turns_completed=0, output_dir=str(tmp_path))
    hud = dict(moves_used=0, rotations_used=0, rounds_remaining=5, inspiration_total=2, collected_count=0)
    monkeypatch.setattr(flow, 'run_layout_scan', scan)
    monkeypatch.setattr(flow, '_adopt_plane', lambda *_args: None)
    monkeypatch.setattr(flow, '_progress_ledger', lambda *_args: None)
    monkeypatch.setattr(flow, '_save', _nothing)
    monkeypatch.setattr(flow, '_stop', lambda state, reason, *_args, **_kw:
                        state.update(status='blocked', reason=reason))
    asyncio.run(flow._scan_turn(state, object(), None, None, hud))
    assert received[0]['scan_route'] == route and received[0]['time_budget_sec'] == expected
    assert received[0]['expected_inspirations'] == 2
    assert state['status'] == 'blocked' and state['reason'].startswith('scan_not_complete:')
    assert state['layout'] is None and state['input_owner'] is None


@pytest.mark.parametrize('route,captures', [('cells', 0), ('four_views', 1)])
def test_post_scan_stage_remains_available_after_long_four_view_scan(monkeypatch, route, captures):
    seen = []
    state = dict(status='running', phase='reset_wide', phase_started=flow.time.monotonic(),
                 terminal_reached=False, recognition_elapsed_sec=102.6,
                 post_scan_recognition_elapsed_sec=0., scan_route=route, scan_time_budget_sec=300.)
    async def load(*_args):
        return state
    async def capture(*_args):
        seen.append(True)
        return None, dict(valid=False, scene='board')
    monkeypatch.setattr(flow, '_load', load)
    monkeypatch.setattr(flow, '_capture', capture)
    monkeypatch.setattr(flow, '_advance_state', _nothing)
    monkeypatch.setattr(flow, '_save', _nothing)
    monkeypatch.setattr(flow, '_release_owned_input', _nothing)
    monkeypatch.setattr(flow, '_report', lambda *_args: None)
    monkeypatch.setattr(flow, '_cancel_check', lambda: None)
    monkeypatch.setattr(flow, '_stop', lambda state, reason, *_args, **_kw:
                        state.update(status='blocked', reason=reason))
    monkeypatch.setattr(flow.asyncio, 'sleep', _nothing)
    asyncio.run(flow.advance_deep_dive_planned_run('fixture', app=WindowSizeOnly()))
    assert len(seen) == captures
    assert state['post_scan_recognition_elapsed_sec'] >= 0
    if route == 'cells':
        assert state['reason'] == 'recognition_time_budget_exhausted'
    else:
        assert state['status'] == 'running'
