import asyncio
from copy import deepcopy
import time
from unittest.mock import AsyncMock

import pytest

from plans.resonance_pc.src.actions import consciousness_deep_dive_loop_pc_actions as loop
from plans.resonance_pc.src.actions import consciousness_deep_dive_planned_run_pc_actions as runtime
from plans.resonance_pc.src.actions._deep_dive_native_choices import observe_choice
from plans.resonance_pc.src.actions._deep_dive_runtime_events import advance_context, create_context


def panel(kind, *, level=20102161, role=None, revision=1):
    return observe_choice({}, dict(
        kind=kind, status='ready', revision=revision, subject_id=level,
        rows=[dict(id=role or level, index=1, disabled=False, selected=False)],
    ), 'battle')


def run_state():
    context = create_context('battle', 'player_move_node', time.monotonic())
    return dict(status='running', phase='event_dispatch', event_stack=[context])


def test_fills_each_vacancy_before_starting_battle(monkeypatch):
    execute = AsyncMock(side_effect=['changed', 'applied', 'applied', 'submitted'])
    monkeypatch.setattr(runtime, '_execute_native_choice', execute)
    state = run_state()
    first = panel('formation_fill', role=10000072)
    second = panel('formation_fill', role=10000298)
    start = panel('formation_start', revision=2)
    busy = observe_choice({'scene': 'battle_formation', 'click': [20, 50]},
                          dict(kind='formation_start', status='submitted'), 'battle')

    async def run():
        for observation in (first, first, first, second, second, start, start, busy, busy):
            await runtime._advance_state(state, None, None, None, None, observation, {})

    asyncio.run(run())
    assert [call.args[1] for call in execute.await_args_list] == [
        dict(observation['native'], command='confirm') for observation in (first, first, second, start)
    ]
    context = state['event_stack'][0]
    assert context['phase'] == 'battle_wait'
    assert context['pending']['action'] == 'battle_start'
    assert [row['action'] for row in context['trace']] == ['formation_fill', 'formation_fill', 'battle_start']


def test_native_battle_waits_three_minutes_for_unclassified_combat():
    context = create_context('boss_battle', 'enemy_move_encounter', 0)
    formation = panel('formation_start')
    advance_context(context, formation, 0)
    assert advance_context(context, formation, 1)['action'] == 'battle_start'

    combat = dict(scene='unknown', valid=True)
    for now in (2, 46, 180):
        assert advance_context(context, combat, now)['status'] == 'waiting'
    assert context['pending']['tries'] == 1

    continuing = deepcopy(context)
    victory = panel('battle_victory', revision=2)
    advance_context(continuing, victory, 180)
    assert advance_context(continuing, victory, 181)['action'] == 'battle_next'
    assert advance_context(context, combat, 181)['reason'] == 'battle_start_transition_timeout'


def test_shortage_leaves_formation_and_submits_settlement_once(monkeypatch):
    execute = AsyncMock(return_value='submitted')
    monkeypatch.setattr(runtime, '_execute_native_choice', execute)
    state = run_state()
    leave = panel('formation_leave')
    quit_panel = panel('formation_quit', revision=2)
    finish = panel('formation_finish', revision=3)
    settlement = dict(scene='settlement', valid=True, outcome='failure')

    async def run():
        for observation in (finish, leave, leave, leave, finish, quit_panel, quit_panel,
                            quit_panel, finish, finish, finish, settlement, settlement):
            await runtime._advance_state(state, None, None, None, None, observation, {})

    asyncio.run(run())
    assert [call.args[1] for call in execute.await_args_list] == [
        dict(observation['native'], command='confirm') for observation in (leave, quit_panel, finish)
    ]
    assert state['status'] == 'completed'
    assert state['reason'] == 'formation_shortage_settled'


def test_shortage_settlement_rejects_a_changed_level(monkeypatch):
    execute = AsyncMock(return_value='submitted')
    monkeypatch.setattr(runtime, '_execute_native_choice', execute)
    state = run_state()

    async def run():
        for observation in (panel('formation_leave'), panel('formation_leave'),
                            panel('formation_quit', level=20102202), panel('formation_quit', level=20102202)):
            await runtime._advance_state(state, None, None, None, None, observation, {})

    asyncio.run(run())
    execute.assert_awaited_once()
    assert state['status'] == 'blocked'
    assert state['reason'] == 'formation_changed_before_settlement'


@pytest.mark.parametrize('home_verified', [True, False])
def test_restart_requires_activity_home_and_does_not_use_run_quota(home_verified):
    store = AsyncMock()
    state = dict(schema=loop.SCHEMA, session_key='test', sequence=0, loop_count=1,
                 completed_runs=0, run_index=1, stage='single', status='running',
                 last_frame='', last_result={})
    store.get.side_effect = lambda _: deepcopy(state)
    store.set.side_effect = lambda _, value: state.update(value)

    async def run():
        await loop.checkpoint_loop('test', 'single', {'deep_dive_planned_run': dict(
            status='completed', success=True, terminal_reached=True, terminal='settlement',
            reason='formation_shortage_settled',
        )}, state_store=store)
        await loop.checkpoint_loop('test', 'cleanup', {'deep_dive_cleanup': dict(
            status='completed', success=True,
            page_state='deep_dive_activity_home' if home_verified else 'deep_dive_board',
        )}, state_store=store)
        assert state['completed_runs'] == 0
        retry = await loop.begin_loop_run('test', state_store=store)
        assert retry['ok'] is home_verified
        if home_verified:
            assert retry['run_index'] == 1
            for stage, field, data in (
                ('entry', 'deep_dive', dict(page_state='deep_dive_board')),
                ('single', 'deep_dive_planned_run', dict(terminal_reached=True, terminal='settlement',
                                                       reason='settlement_observed')),
                ('cleanup', 'deep_dive_cleanup', dict(page_state='deep_dive_activity_home')),
            ):
                await loop.checkpoint_loop('test', stage, {field: dict(status='completed', success=True, **data)},
                                           state_store=store)
            assert state['status'] == 'completed'
            assert state['completed_runs'] == 1

    asyncio.run(run())
