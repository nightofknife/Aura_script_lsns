"""Initial HUD proofs from independent saved fake pixels; no runtime or input."""
import asyncio
from copy import deepcopy
import hashlib
import json

import cv2
import numpy as np
import pytest

from tools import deep_dive_live_acceptance as live


VALUES = dict(zip(live.HUD_FIELDS, (1, 6, 0, 1, 1, 1, 0, 2)))
NATIVE = dict(plane_index='native_plane_icon', rounds_remaining='native_round_glyph_templates',
              **{field:'native_font_pair_templates' for field in live.HUD_FIELDS[2:]})


@pytest.fixture
def case(tmp_path, monkeypatch):
    clock = [100.]
    monkeypatch.setattr(live.time, 'monotonic', lambda: clock[0])
    harness = live.ScanHarness(None, {}, tmp_path)
    harness.round = dict(hud_samples=[])
    calls = []

    def install(changes, mutate=None, tamper=None):
        async def sample(app, path, previous, *, hard_deadline, native_only):
            assert native_only is True and hard_deadline == 190.
            index = len(calls)
            calls.append(dict(path=path, previous=deepcopy(previous)))
            clock[0] += .05
            values = dict(VALUES, **changes[index])
            rgb = np.zeros((720, 1280, 3), np.uint8)
            rgb[100, 100] = (index+1, 80, 200)
            cv2.imwrite(str(path), cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
            result = dict(source=dict(backend='wgc', session_id='actual', generation=index+1,
                frame_time=clock[0]-.01, path=path.relative_to(tmp_path).as_posix(), sha256=live.sha(path)),
                source_age_sec=.01, rgb_sha256=hashlib.sha256(rgb.tobytes()).hexdigest(),
                observation=dict(valid=True, scene='board', player_turn=True),
                hud=dict(values, scene='board', status='partial' if None in values.values() else 'complete',
                    evidence_source={k:v for k,v in NATIVE.items() if values[k] is not None}))
            if mutate:
                mutate(result, index)
            live.write_json(path.with_suffix('.json'), result)
            if tamper:
                tamper(result, path, index)
            return result
        monkeypatch.setattr(harness, 'hud_sample', sample)
    return harness, clock, calls, install, tmp_path


def run(case):
    harness, _, _, _, folder = case
    return asyncio.run(harness.initial_hud(None, folder, 190.))


def test_two_native_complete_actual_sources_become_baselines(case):
    harness, _, calls, install, folder = case
    install([{}, {}])
    result = run(case)
    assert len(calls) == 2 and calls[0]['previous'] is None
    assert calls[1]['previous']['source']['generation'] == 1
    assert result == harness.round['hud_samples']
    assert [s['source']['generation'] for s in result] == [1, 2]
    assert harness.round['initial_hud_reason'] == 'two_initial_native_hud_sources_stable'
    assert harness.round['initial_hud_elapsed_sec'] > 0
    assert all((folder/f'hud_pre_{index}.{ext}').exists() for index in (1, 2) for ext in ('png', 'json'))


@pytest.mark.parametrize('first', ['unknown', 'stale'])
def test_failed_proof_kept_but_never_becomes_post_baseline(case, first):
    harness, _, calls, install, _ = case
    def mutate(sample, index):
        if first == 'stale' and index == 0:
            sample['source_age_sec'] = .7
            sample['source']['frame_time'] -= .7
    install([{'rounds_remaining':None} if first == 'unknown' else {}, {}, {}], mutate)
    result = run(case)
    assert len(calls) == 3 and len(harness.round['initial_hud_attempts']) == 3
    assert [s['source']['generation'] for s in result] == [2, 3]
    assert result == harness.round['hud_samples']
    assert harness.round['initial_hud_attempts'][0]['sample']['source']['generation'] == 1


@pytest.mark.parametrize('second_partial', [False, True])
def test_known_change_even_on_partial_attempt_stops_without_restoration(case, second_partial):
    harness, _, calls, install, _ = case
    changed = {'moves_used':1}
    if second_partial:
        changed['plane_index'] = None
    install([{'rounds_remaining':None}, changed, {}, {}])
    with pytest.raises(RuntimeError, match='hud_state_changed'):
        run(case)
    assert len(calls) == 2 and harness.round['hud_samples'] == []
    assert harness.round['initial_hud_attempts'][1]['sample']['hud']['moves_used'] == 1


def test_values_from_partial_attempts_are_never_joined(case):
    harness, _, calls, install, _ = case
    install([{'rounds_remaining':None}, {'plane_index':None}, {}, {'moves_used':None}])
    with pytest.raises(RuntimeError, match='initial_full_hud_not_complete_stable'):
        run(case)
    assert len(calls) == 4 and harness.round['hud_samples'] == []
    assert len(harness.round['initial_hud_attempts']) == 4


def test_incomplete_between_ready_samples_resets_consecutive_requirement(case):
    harness, _, calls, install, _ = case
    install([{}, {'rounds_remaining':None}, {}, {}])
    result = run(case)
    assert len(calls) == 4
    assert [s['source']['generation'] for s in result] == [3, 4]
    assert len(harness.round['initial_hud_attempts']) == 4


def test_deadline_expired_before_capture_does_not_fetch(case):
    harness, clock, calls, install, _ = case
    install([{}, {}])
    clock[0] = 189.5
    with pytest.raises(RuntimeError, match='initial_hud_dispatch_budget_exhausted'):
        run(case)
    assert calls == [] and harness.round['hud_samples'] == []


def test_native_processing_and_artifact_time_count_inside_deadline(case):
    harness, clock, calls, install, _ = case
    def late(sample, index):
        if index == 1:
            clock[0] = 190.01
    install([{}, {}], late)
    with pytest.raises(RuntimeError, match='initial_hud_dispatch_budget_exhausted'):
        run(case)
    assert len(calls) == 2 and harness.round['hud_samples'] == []
    assert len(harness.round['initial_hud_attempts']) == 2


@pytest.mark.parametrize('bad,reason', [
    ('native', 'hud_field_not_native'), ('source', 'hud_source_did_not_advance'),
    ('generation', 'hud_source_did_not_advance'), ('backend', 'hud_source_identity_invalid'),
    ('page', 'hud_source_not_player_board'), ('bool', 'hud_field_not_native'),
])
def test_source_page_or_native_failure_never_retries(case, bad, reason):
    harness, _, calls, install, _ = case
    def mutate(sample, index):
        if index != 1:
            return
        if bad == 'native':sample['hud']['evidence_source']['moves_used'] = 'ocr_pair'
        elif bad == 'source':sample['source']['session_id'] = 'other'
        elif bad == 'generation':sample['source']['generation'] = 1
        elif bad == 'backend':sample['source']['backend'] = 'printwindow'
        elif bad == 'page':sample['observation']['player_turn'] = False
        elif bad == 'bool':sample['hud']['moves_used'] = False
    install([{}, {}, {}], mutate)
    with pytest.raises(RuntimeError, match=reason):
        run(case)
    assert len(calls) == 2 and harness.round['hud_samples'] == []


@pytest.mark.parametrize('tamper_field', ['json', 'png', 'rgb_digest'])
def test_raw_source_mismatch_is_not_treated_as_transient_unknown(case, tamper_field):
    harness, _, calls, install, _ = case
    def tamper(sample, path, index):
        if tamper_field == 'json':
            saved = json.loads(path.with_suffix('.json').read_text(encoding='utf8'))
            saved['source']['generation'] += 1
            live.write_json(path.with_suffix('.json'), saved)
        elif tamper_field == 'png':
            path.write_bytes(b'corrupt')
        else:
            sample['rgb_sha256'] = 'wrong'
            live.write_json(path.with_suffix('.json'), sample)
    install([{}, {}], tamper=tamper)
    with pytest.raises(RuntimeError, match='hud_raw_source_mismatch'):
        run(case)
    assert len(calls) == 1 and harness.round['hud_samples'] == []


def test_json_native_tuple_alternatives_do_not_cause_representation_mismatch(case):
    _, _, _, install, _ = case
    def mutate(sample, index):
        sample['hud']['native_evidence'] = {'alternatives':[('0', .9182), ('8', .7656)]}
    install([{}, {}], mutate)
    assert len(run(case)) == 2
