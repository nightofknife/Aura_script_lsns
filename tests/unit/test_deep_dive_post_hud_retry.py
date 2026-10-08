"""Bounded post-HUD construction tests: saved fake pixels, no game runtime."""
import asyncio
from copy import deepcopy
import hashlib
import json
import sys
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from tools import deep_dive_live_acceptance as live


VALUES = dict(zip(live.HUD_FIELDS, (1, 6, 0, 1, 1, 1, 0, 2)))
NATIVE = dict(plane_index='native_plane_icon', rounds_remaining='native_round_glyph_templates',
              **{field:'native_font_pair_templates' for field in live.HUD_FIELDS[2:]})

# Real candidate15 hud_post.png native moves glyph 0 evidence. The native
# matcher returns sorted(dict.items()) pairs as tuples, not JSON lists.
REAL_NATIVE_GLYPH = dict(character='0', confidence=.9182, margin=.1526,
    alternatives=[('0', .9181817785486023), ('8', .7656249889423926),
                  ('5', .6805921169364876)],
    font='harmony', template='harmony_22_0_phase1.png')


def add_native_tuple_evidence(sample, index):
    sample['hud']['numeric_pair_template_evidence'] = {
        'moves': {'glyphs': [{'box': [1194, 399, 11, 17],
                            'match': deepcopy(REAL_NATIVE_GLYPH)}]}}


@pytest.fixture
def case(tmp_path, monkeypatch):
    clock = [100.]
    monkeypatch.setattr(live.time, 'monotonic', lambda: clock[0])
    harness = live.ScanHarness(None, {}, tmp_path)
    baselines = [dict(hud=dict(VALUES, status='complete'), source_age_sec=.1,
        source=dict(backend='wgc', session_id='current', generation=i, frame_time=97.+i),
        observation=dict(valid=True, scene='board', player_turn=True)) for i in (1, 2)]
    harness.round = dict(hud_samples=deepcopy(baselines))
    calls = []

    def install(changes, mutate=None):
        async def sample(app, path, previous, *, hard_deadline, native_only):
            assert native_only is True and hard_deadline == 190.
            index = len(calls)
            calls.append(dict(previous=deepcopy(previous), path=path))
            clock[0] += .05
            values = dict(VALUES, **changes[index])
            rgb = np.zeros((720, 1280, 3), np.uint8)
            rgb[100, 100] = (index+1, 90, 200)
            cv2.imwrite(str(path), cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
            result = dict(source=dict(backend='wgc', session_id='current', generation=index+3,
                frame_time=clock[0]-.01, path=path.relative_to(tmp_path).as_posix(), sha256=live.sha(path)),
                rgb_sha256=hashlib.sha256(rgb.tobytes()).hexdigest(), source_age_sec=.01,
                observation=dict(valid=True, scene='board', player_turn=True),
                hud=dict(values, scene='board', status='partial' if None in values.values() else 'complete',
                    evidence_source={k:v for k,v in NATIVE.items() if values[k] is not None}))
            if mutate:
                mutate(result, index)
            live.write_json(path.with_suffix('.json'), result)
            return result
        monkeypatch.setattr(harness, 'hud_sample', sample)
    return harness, baselines, clock, calls, install, tmp_path


def run(case):
    harness, baselines, _, _, _, folder = case
    return asyncio.run(harness.post_hud(None, folder, deepcopy(baselines), 190.))


def test_unknown_then_current_complete_uses_new_raw_and_preserves_attempts(case):
    harness, _, _, calls, install, folder = case
    install([{'rounds_remaining':None}, {}])
    assert run(case) == (True, 'eight_hud_fields_stable')
    assert len(calls) == 2 and calls[1]['previous']['hud']['rounds_remaining'] is None
    attempts = harness.round['post_hud_attempts']
    assert [r['reason'] for r in attempts] == ['incomplete_full_hud', 'eight_hud_fields_stable']
    assert attempts[0]['sample']['source']['generation'] == 3
    assert harness.round['hud_samples'][-1]['source']['generation'] == 4
    assert len(harness.round['hud_samples']) == 3
    assert all((folder/name).exists() for name in ['hud_post.png','hud_post.json','hud_post_retry_1.png','hud_post_retry_1.json'])
    assert harness.round['post_hud_elapsed_sec'] > 0


def test_real_native_tuple_evidence_matches_complete_saved_json(case):
    harness, _, _, calls, install, folder = case
    install([{}], add_native_tuple_evidence)
    assert run(case) == (True, 'eight_hud_fields_stable')
    assert len(calls) == 1
    sample = harness.round['hud_samples'][-1]
    glyph = sample['hud']['numeric_pair_template_evidence']['moves']['glyphs'][0]['match']
    assert type(glyph['alternatives'][0]) is tuple
    saved = json.loads((folder/'hud_post.json').read_text(encoding='utf8'))
    assert type(saved['hud']['numeric_pair_template_evidence']['moves']['glyphs'][0]['match']['alternatives'][0]) is list
    assert saved != sample
    assert saved == json.loads(json.dumps(sample, allow_nan=False))


@pytest.mark.parametrize('bad,reason', [
    ('value', 'hud_state_changed'), ('native', 'hud_field_not_native'),
    ('source', 'hud_source_did_not_advance'),
    ('nonfinite', 'hud_raw_source_mismatch'), ('unsupported', 'hud_raw_source_mismatch'),
])
def test_json_native_tuple_comparison_does_not_relax_evidence(case, bad, reason):
    _, _, _, calls, install, _ = case
    def mutate(sample, index):
        add_native_tuple_evidence(sample, index)
        if bad == 'value':sample['hud']['moves_used'] = 1
        elif bad == 'native':sample['hud']['evidence_source']['moves_used'] = 'cached'
        elif bad == 'source':sample['source']['session_id'] = 'other'
        elif bad == 'nonfinite':sample['hud']['numeric_pair_template_evidence']['score'] = float('nan')
        elif bad == 'unsupported':sample['hud']['numeric_pair_template_evidence']['score'] = object()
    install([{}, {}], mutate)
    assert run(case) == (False, reason)
    assert len(calls) == 1


@pytest.mark.parametrize('tamper', ['numeric_evidence', 'source'])
def test_saved_json_content_changes_still_rejected_with_native_tuples(case, tamper):
    harness, baselines, _, _, install, folder = case
    install([{}], add_native_tuple_evidence)
    assert run(case) == (True, 'eight_hud_fields_stable')
    path = folder/'hud_post.json'
    saved = json.loads(path.read_text(encoding='utf8'))
    if tamper == 'numeric_evidence':
        saved['hud']['numeric_pair_template_evidence']['moves']['glyphs'][0]['match']['alternatives'][0][1] += .01
    else:
        saved['source']['generation'] += 1
    live.write_json(path, saved)
    assert live.post_hud_attempt_reason(harness.round['hud_samples'][-1], baselines[-1],
        baselines[-1], folder/'hud_post.png', folder) == 'hud_raw_source_mismatch'


@pytest.mark.parametrize('partial', [False, True])
def test_any_known_changed_value_stops_even_with_another_unknown(case, partial):
    harness, _, _, calls, install, _ = case
    changed = {'moves_used':1}
    if partial:
        changed['rounds_remaining'] = None
    install([{'rounds_remaining':None}, changed, {}])
    assert run(case) == (False, 'hud_state_changed')
    assert len(calls) == 2 and len(harness.round['post_hud_attempts']) == 2
    assert harness.round['hud_samples'][-1]['hud']['moves_used'] == 1


def test_initial_partial_already_has_changed_field_never_retries(case):
    _, _, _, calls, install, _ = case
    install([{'rounds_remaining':None, 'collected_count':1}, {}])
    assert run(case) == (False, 'hud_state_changed')
    assert len(calls) == 1


def test_three_incomplete_reads_fail_without_fourth_capture(case):
    harness, _, _, calls, install, _ = case
    install([{'rounds_remaining':None}]*3+[{}])
    assert run(case) == (False, 'incomplete_full_hud')
    assert len(calls) == len(harness.round['post_hud_attempts']) == 3


@pytest.mark.parametrize('remaining', [0., -.1, .4, .5])
def test_expired_or_insufficient_dispatch_budget_does_not_capture(case, remaining):
    harness, baselines, clock, calls, install, folder = case
    install([{}])
    clock[0] = 190.-remaining
    assert asyncio.run(harness.post_hud(None, folder, baselines, 190.)) == (False, 'post_hud_dispatch_budget_exhausted')
    assert calls == [] and harness.round['post_hud_attempts'] == []


@pytest.mark.parametrize('bad,reason', [
    ('session','hud_source_did_not_advance'), ('generation','hud_source_did_not_advance'),
    ('stale','hud_source_not_fresh_wgc'), ('backend','hud_source_not_fresh_wgc'),
    ('page','hud_source_not_player_board'), ('native','hud_field_not_native'),
    ('file_hash','hud_raw_source_mismatch'), ('rgb_hash','hud_raw_source_mismatch'),
    ('path','hud_raw_source_mismatch'),
])
def test_unknown_source_or_native_mismatch_is_failure_not_retry(case, bad, reason):
    _, _, _, calls, install, _ = case
    def mutate(result, index):
        if bad == 'session':result['source']['session_id'] = 'other'
        elif bad == 'generation':result['source']['generation'] = 2
        elif bad == 'stale':result['source_age_sec'] = .6
        elif bad == 'backend':result['source']['backend'] = 'printwindow'
        elif bad == 'page':result['observation']['scene'] = 'enemy_turn'
        elif bad == 'native':result['hud']['evidence_source']['moves_used'] = 'cached'
        elif bad == 'file_hash':result['source']['sha256'] = '0'*64
        elif bad == 'rgb_hash':result['rgb_sha256'] = '0'*64
        elif bad == 'path':result['source']['path'] = 'other.png'
    install([{'rounds_remaining':None}, {}], mutate)
    assert run(case) == (False, reason)
    assert len(calls) == 1


def test_expiry_during_actual_read_save_is_failure_and_preserves_sample(case):
    harness, _, clock, calls, install, _ = case
    def mutate(result, index):clock[0] = 190.1
    install([{}], mutate)
    assert run(case) == (False, 'post_hud_dispatch_budget_exhausted')
    assert len(calls) == 1 and harness.round['post_hud_attempts'][0]['sample']['source']['generation'] == 3


def test_expiry_during_raw_audit_is_failure(case, monkeypatch):
    _, _, clock, calls, install, _ = case
    install([{}])
    original = live.post_hud_attempt_reason
    def delayed(*args):
        result = original(*args)
        clock[0] = 190.1
        return result
    monkeypatch.setattr(live, 'post_hud_attempt_reason', delayed)
    assert run(case) == (False, 'post_hud_dispatch_budget_exhausted')
    assert len(calls) == 1


@pytest.fixture
def actual_sample(case, monkeypatch):
    """Run the capture plumbing with only a fake WGC adapter/native reader."""
    harness, _, clock, calls, _, folder = case
    from plans.resonance_pc.src.actions import _deep_dive_planned_run_vision as vision
    harness.ocr = object()
    packets = []
    reads = []
    rgb = np.zeros((720, 1280, 3), np.uint8)
    rgb[100, 100] = (120, 40, 90)
    packet = dict(generation=3, session_id='current', arrived_at_monotonic=100.01,
                  capture=SimpleNamespace(success=True, image=rgb, backend='wgc'))
    class Adapter:
        def capture_stream_frame(self, generation, expected_session_id=None):
            packets.append((generation, expected_session_id))
            clock[0] = 100.02
            return packet
    app = SimpleNamespace(target_runtime=SimpleNamespace(_get_or_create_session=lambda:Adapter()))
    async def await_serial(awaitable):
        return await awaitable, False
    monkeypatch.setitem(sys.modules,
        'plans.resonance_pc.src.actions.consciousness_deep_dive_scan_pc_actions',
        SimpleNamespace(_await_serial=await_serial))
    monkeypatch.setattr(vision, 'observe', lambda image:dict(valid=True, scene='board', player_turn=True))
    def native(image, ocr, observation=None):
        reads.append((image.copy(), ocr, observation))
        return dict(VALUES, scene='board', status='complete', evidence_source=NATIVE.copy())
    monkeypatch.setattr(vision, 'read_hud', native)
    return harness, app, packet, packets, reads, folder


def test_actual_capture_uses_one_rgb_full_native_and_saved_digest(actual_sample):
    harness, app, packet, captures, reads, folder = actual_sample
    sample = asyncio.run(harness.hud_sample(app, folder/'hud_post.png',
        harness.round['hud_samples'][-1], hard_deadline=190., native_only=True))
    assert captures == [(2, 'current')]
    assert len(reads) == 1 and reads[0][1] is None
    assert np.array_equal(reads[0][0], packet['capture'].image)
    assert sample['source']['generation'] == 3 and sample['source']['frame_time'] == 100.01
    assert live.post_hud_attempt_reason(sample, harness.round['hud_samples'][-1],
        harness.round['hud_samples'][-1], folder/'hud_post.png', folder) == 'eight_hud_fields_stable'


@pytest.mark.parametrize('key,value', [('generation',True), ('generation','3'),
    ('session_id',True), ('session_id',''), ('arrived_at_monotonic','100.01'),
    ('arrived_at_monotonic',float('nan'))])
def test_capture_raw_identity_cannot_be_coerced_to_valid_proof(actual_sample, key, value):
    harness, app, packet, captures, reads, folder = actual_sample
    packet[key] = value
    with pytest.raises(RuntimeError, match='source_identity_invalid'):
        asyncio.run(harness.hud_sample(app, folder/'hud_post.png',
            harness.round['hud_samples'][-1], hard_deadline=190., native_only=True))
    assert reads == []


def test_actual_capture_with_insufficient_budget_does_not_fetch(actual_sample, case):
    harness, app, _, captures, reads, folder = actual_sample
    case[2][0] = 189.6
    with pytest.raises(RuntimeError, match='budget_exhausted'):
        asyncio.run(harness.hud_sample(app, folder/'hud_post.png',
            harness.round['hud_samples'][-1], hard_deadline=190., native_only=True))
    assert captures == reads == []


@pytest.mark.parametrize('value', [True, False])
def test_startup_fast_is_explicit_targets_boolean(value):
    config = dict(opencv_threads=1, scan_inputs=dict(recognition_goal='targets',scan_route='cells',
        time_budget_sec=90,startup_fast=value),entity_detector={'execution_provider':'dml_worker'},
        runtime_assertions={'entity_provider':'DmlExecutionProvider'})
    assert live.validate_config(config)['scan_inputs']['startup_fast'] is value


@pytest.mark.parametrize('value', ['true', 1, None, [], {}])
def test_startup_fast_cannot_be_truthy_non_boolean(value):
    config = dict(opencv_threads=1, scan_inputs=dict(recognition_goal='targets',scan_route='cells',
        time_budget_sec=90,startup_fast=value),entity_detector={'execution_provider':'dml_worker'},
        runtime_assertions={'entity_provider':'DmlExecutionProvider'})
    with pytest.raises(ValueError, match='boolean'):
        live.validate_config(config)
