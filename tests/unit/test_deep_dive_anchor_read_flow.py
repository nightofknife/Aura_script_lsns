"""Flow contracts around a real saved board, without claiming live WGC reads.

The reader's pixel/fresh-source proofs are tested separately. Worker results here
are controlled doubles: this suite tests whether those results can reach actions.
"""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from plans.resonance_pc.src.actions import consciousness_deep_dive_planned_run_pc_actions as flow
from plans.resonance_pc.src.actions._deep_dive_planner_rules import cell_to_slot


@pytest.fixture
def case(tmp_path, monkeypatch):
    fixture = Path(__file__).parents[1] / 'fixtures/deep_dive_anchor/reading_only_binding04.json'
    saved = json.loads(fixture.read_text(encoding='utf-8'))
    hud = dict(plane_index=1, rounds_remaining=6, moves_used=0, moves_total=1,
               rotations_used=0, rotations_total=1, collected_count=0, inspiration_total=2)
    state = dict(status='running', phase='read_registration_anchors', layout=deepcopy(saved['layout']),
                 scan_start_hud=deepcopy(hud), hud=deepcopy(hud), hud_stable=2,
                 hud_signature=[hud[key] for key in flow.HUD_KEYS], event_stack=[], sequence=0,
                 scan_epoch=1, map_revision=4, pose_epoch=2, output_dir=str(tmp_path),
                 wide_reference=None, registration_anchor_votes={'prior': 'read-only vote'},
                 registration_anchor_proposal=deepcopy(saved['initial_proposal']),
                 registration_anchor_session=7)
    observation = dict(valid=True, scene='board', player_turn=True,
                       _capture_source=dict(session_id=7, generation=12, frame_time=10., capture_backend='wgc'))
    monkeypatch.setattr(flow, '_write_json', lambda *_: None)
    monkeypatch.setattr(flow, '_frame', lambda *_: None)
    monkeypatch.setattr(flow, '_log', lambda *_, **__: None)
    monkeypatch.setattr(flow, '_begin_planned_action', lambda *_: pytest.fail('reading authorized an action'))
    detector = SimpleNamespace(detect_packet=lambda _: deepcopy(saved['model_packet']))
    token = flow._RUN_DETECTOR.set(detector)
    try:
        yield state, hud, observation, np.zeros((720, 1280, 3), np.uint8), saved
    finally:
        flow._RUN_DETECTOR.reset(token)


def ready_reading(state):
    rows = []
    for slot in (9, 10):
        row = deepcopy(next(row for row in state['layout']['cells'] if cell_to_slot(row) == slot))
        row.update(icon_id='yellow_hex', node_status='known', occupant='none', occupant_status='confirmed',
                   node_read_evidence={'source': 'reading_only_registration_anchor',
                                       'requires_strict_public_rebuild': True})
        rows.append(row)
    return dict(status='reading_only', ready=True, cells=rows, reason='registration_anchors_read')


def advance(case, reading, raw=None):
    state, hud, observation, image, _ = case
    token = flow._RUN_ANCHOR_READ.set(reading)
    try:
        asyncio.run(flow._advance_state(state, None, None, None, image, observation, raw or hud))
    finally:
        flow._RUN_ANCHOR_READ.reset(token)


@pytest.mark.parametrize('key,value', [('plane_index', 2), ('rounds_remaining', 5),
                                      ('moves_used', 1), ('rotations_used', 1),
                                      ('collected_count', 1), ('inspiration_total', 3)])
def test_changed_hud_never_commits_even_completed_parallel_read(case, key, value):
    state, hud, _, _, _ = case
    before = deepcopy(state['layout'])
    reading = ready_reading(state)
    changed = dict(hud, **{key: value})
    # First changed HUD lacks consensus; the second confirms the change and stops.
    advance(case, reading, changed)
    assert state['layout'] == before and state['map_revision'] == 4
    advance(case, reading, changed)
    assert state['status'] == 'blocked'
    assert state['reason'] == 'game_state_changed_during_registration_anchor_read'
    assert state['layout'] == before and state['map_revision'] == 4
    assert state['wide_reference'] is None


def test_unknown_hud_never_commits_ready_parallel_read(case):
    state, hud, _, _, _ = case
    before = deepcopy(state['layout'])
    advance(case, ready_reading(state), dict(hud, rounds_remaining=None))
    assert state['layout'] == before and state['map_revision'] == 4
    assert state['phase'] == 'read_registration_anchors'


def test_ready_reading_only_updates_nodes_then_requires_strict_rebuild(case):
    state, _, _, _, _ = case
    state['wide_reference_stability'] = {'stable': 99}
    before = deepcopy(state['layout'])
    advance(case, ready_reading(state))
    assert state['phase'] == 'wide_reference' and state['status'] == 'running'
    assert state['map_revision'] == 5 and state['wide_reference'] is None
    for key in ('registration_anchor_votes', 'registration_anchor_proposal',
                'registration_anchor_session', 'wide_reference_stability'):
        assert key not in state
    for old, new in zip(before['cells'], state['layout']['cells']):
        if cell_to_slot(old) not in (9, 10):
            assert old == new
        else:
            assert new['node_status'] == 'known'
            assert new['node_read_evidence']['requires_strict_public_rebuild'] is True


def test_strict_failure_after_reading_does_not_plan(case, monkeypatch):
    state, _, _, image, _ = case
    advance(case, ready_reading(state))
    calls = []
    def strict(rgb, layout, **kwargs):
        calls.append((rgb is image, layout is state['layout'], kwargs['map_revision']))
        return dict(status='waiting', reason='wide_three_face_geometry_or_content_unconfirmed')
    monkeypatch.setattr(flow, 'build_wide_reference_frame', strict)
    monkeypatch.setattr(flow, 'targets_readiness', lambda _: {'ready': True})
    advance(case, None)
    assert calls == [(True, True, 5)]
    assert state['phase'] == 'read_registration_anchors' and state['wide_reference'] is None


def test_reading_only_returned_by_strict_builder_cannot_promote(case, monkeypatch):
    state, _, _, _, saved = case
    advance(case, ready_reading(state))
    monkeypatch.setattr(flow, 'build_wide_reference_frame', lambda *_, **__: deepcopy(saved['initial_proposal']))
    advance(case, None)
    assert state['phase'] == 'wide_reference' and state['wide_reference'] is None


def test_initial_orientation_consumes_no_vote_and_next_rgb_uses_actual_source(case, monkeypatch):
    state, _, observation, image, saved = case
    state.pop('registration_anchor_proposal')
    captured = []
    monkeypatch.setattr(flow, 'propose_read_only_wide_reference', lambda *_, **__: deepcopy(saved['initial_proposal']))
    monkeypatch.setattr(flow, 'read_missing_registration_anchors',
                        lambda *args, **kwargs: captured.append((args, kwargs)) or dict(ready=False, cells=[]))
    initial = flow._read_anchor_frame(image, state, observation)
    assert initial['ready'] is False and captured == []
    assert initial['reason'] == 'registration_anchor_wait_new_capture'
    advance(case, initial)
    assert state['registration_anchor_session'] == 7 and state['registration_anchor_votes'] == {}
    current = image.copy()
    current[0, 0, 0] = 1  # Synthetic changed RGB, no claim of a live capture.
    source = dict(observation, _capture_source=dict(session_id=7, generation=13, frame_time=10.2, capture_backend='wgc'))
    flow._read_anchor_frame(current, state, source)
    args, kwargs = captured[0]
    assert args[0] is current and args[1] is state['layout']
    assert kwargs['source_id'] == '7:13' and kwargs['source_time'] == 10.2
    assert kwargs['source_session'] == 7 and kwargs['source_generation'] == 13
    assert kwargs['source_backend'] == 'wgc'


def test_session_change_reproposes_without_reusing_votes(case, monkeypatch):
    state, _, observation, image, saved = case
    calls = []
    monkeypatch.setattr(flow, 'propose_read_only_wide_reference',
                        lambda *_, **kwargs: calls.append(kwargs) or deepcopy(saved['initial_proposal']))
    monkeypatch.setattr(flow, 'read_missing_registration_anchors', lambda *_, **__: pytest.fail('old session voted'))
    changed = dict(observation, _capture_source=dict(session_id=8, generation=1, frame_time=11., capture_backend='wgc'))
    initial = flow._read_anchor_frame(image, state, changed)
    assert calls[0]['source_session'] == 8 and initial['ready'] is False
    advance(case, initial)
    assert state['registration_anchor_session'] == 8 and state['registration_anchor_votes'] == {}


def test_invalidation_discards_reading_orientation_and_votes(case):
    state, _, _, _, _ = case
    flow._invalidate(state, 'new_player_round_observed')
    assert state['layout'] is None and state['wide_reference'] is None
    assert state['map_revision'] == 5 and state['pose_epoch'] == 3
    assert not any(key in state for key in ('registration_anchor_votes',
                                          'registration_anchor_proposal', 'registration_anchor_session'))
