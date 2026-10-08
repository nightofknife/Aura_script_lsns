"""Harness construction/provenance tests only; no runtime, capture or input."""
from copy import deepcopy
import json
import math
import threading
from types import SimpleNamespace

import pytest

from tools.deep_dive_acceptance import AcceptanceLedger, fingerprint_files
from tools.deep_dive_live_acceptance import (
    EntityConfig, HUD_FIELDS, PREFIX, campaign, declared_entities, final_invocation_elapsed,
    full_hud_unchanged, local_path, parser, positive_sources, runtime_errors, validate_config,
    shared_ocr, valid_so3, wait_wrapper_done,
)


def config():
    return dict(opencv_threads=1,
        scan_inputs=dict(recognition_goal='targets', scan_route='cells', time_budget_sec=90),
        entity_detector={'execution_provider':'dml_worker', 'worker.ort_version':'1.24.4'},
        runtime_assertions={'entity_provider':'DmlExecutionProvider', 'worker_ort_version':'1.24.4'})


def test_cli_requires_registered_truth_and_explicit_inputs():
    args = parser().parse_args(['--ledger','ledger.json','--output','output','--truth','reviewed',
                               '--count','20','--config','config.json'])
    assert args.truth == 'reviewed' and args.count == 20 and not args.keep_going
    with pytest.raises(SystemExit):
        parser().parse_args(['--ledger','x','--output','y','--config','z'])


@pytest.mark.parametrize('field,value', [
    ('opencv_threads',16), ('opencv_threads',True), ('scan_inputs',{'time_budget_sec':90}),
    ('entity_detector',{}), ('runtime_assertions',{}),
])
def test_configuration_never_implicitly_selects_provider_or_threads(field, value):
    candidate = config();candidate[field] = value
    with pytest.raises(ValueError):
        validate_config(candidate)


def test_config_retains_all_worker_suffixes_and_rejects_unsafe_task_inputs():
    candidate = config()
    candidate['entity_detector']['worker.runtime_site'] = '.pytest_tmp/isolated'
    candidate['entity_detector']['session.intra_op_num_threads'] = 4
    assert validate_config(candidate) == candidate
    candidate['scan_inputs']['output_dir'] = 'override'
    with pytest.raises(ValueError):
        validate_config(candidate)
    candidate = config();candidate['scan_inputs']['time_budget_sec'] = 91
    with pytest.raises(ValueError):
        validate_config(candidate)


def test_config_override_cannot_change_main_ocr_or_framework_keys():
    class Base:
        def get(self, key, default=None):
            return {PREFIX+'execution_provider':'auto', 'ocr.execution_provider':'cpu'}.get(key, default)
    value = EntityConfig(Base(), {'execution_provider':'dml_worker', 'worker.runtime_site':'local'})
    assert value.get(PREFIX+'execution_provider') == 'dml_worker'
    assert value.get(PREFIX+'worker.runtime_site') == 'local'
    assert value.get('ocr.execution_provider') == 'cpu'
    assert value.get('worker.runtime_site', 'unchanged') == 'unchanged'


def test_full_hud_resolves_the_existing_public_scan_ocr_dependency():
    singleton = object()
    class Registry:
        def get_service_instance(self, key):
            assert key == 'plans/aura_base/ocr'
            return singleton
    assert shared_ocr(Registry()) is singleton


def hud_samples():
    values = dict(zip(HUD_FIELDS, (1, 6, 0, 1, 1, 1, 0, 2)))
    return [dict(hud=dict(values, status='complete'), source_age_sec=.2,
        source=dict(backend='wgc',session_id='wgc',generation=i,frame_time=float(i)),
        observation=dict(valid=True,scene='board',player_turn=True)) for i in (1, 2, 3)]


def test_hud_requires_all_eight_complete_stable_values_from_three_actual_sources():
    assert full_hud_unchanged(hud_samples()) == (True, 'eight_hud_fields_stable')
    samples = hud_samples();samples[-1]['hud']['collected_count'] = 1
    assert full_hud_unchanged(samples) == (False, 'hud_state_changed')
    samples = hud_samples();samples[1]['hud']['moves_total'] = None
    assert full_hud_unchanged(samples) == (False, 'incomplete_full_hud')
    assert not full_hud_unchanged(hud_samples()[:2])[0]


@pytest.mark.parametrize('change', ['same_generation','wrong_session','stale','negative_age','not_board','not_wgc'])
def test_hud_rejects_unbound_stale_or_changed_source(change):
    samples = hud_samples()
    if change == 'same_generation':samples[2]['source']['generation'] = 2
    if change == 'wrong_session':samples[2]['source']['session_id'] = 'other'
    if change == 'stale':samples[2]['source_age_sec'] = .6
    if change == 'negative_age':samples[2]['source_age_sec'] = -.01
    if change == 'not_board':samples[2]['observation']['scene'] = 'enemy_turn'
    if change == 'not_wgc':samples[2]['source']['backend'] = 'printwindow'
    assert not full_hud_unchanged(samples)[0]


def rotation(degrees):
    angle = math.radians(degrees)
    return [[1.,0.,0.],[0.,math.cos(angle),-math.sin(angle)],
            [0.,math.sin(angle),math.cos(angle)]]


def proof_case(tmp_path):
    folder = tmp_path/'scan';(folder/'frames').mkdir(parents=True)
    cells, frames = [], []
    entities = [dict(kind='player',face='U',row=1,col=1),
                dict(kind='singularity',face='D',row=1,col=1),
                dict(kind='inspiration',face='B',row=1,col=2)]
    for index, entity in enumerate(entities):
        evidence = []
        for group in (1, 2):
            fid = index*2+group
            relative = f'frames/{fid:04d}.png'
            (folder/relative).write_bytes(f'unique unit source {fid}'.encode())
            frames.append(dict(frame_id=fid,path=relative,generation=fid,session_id=999,
                               frame_time=100.+fid,semantic_fused=True))
            evidence.append(dict(frame_id=fid,group=group,occupant=entity['kind'],confidence=.9,
                association_evidence=dict(source_frame_id=fid,source_frame_time=100.+fid,
                    source_map_revision=0,rotation=rotation(10.*(group-1)))))
        cells.append(dict(face=entity['face'],row=entity['row'],col=entity['col'],
                          occupant=entity['kind'],occupant_status='confirmed',evidence=evidence))
    layout = dict(map_revision=0,cells=cells,frames=frames,player_cell=entities[0],
                  singularity_cell=entities[1],inspiration_cells=[entities[2]])
    return folder, layout, entities


def test_positive_proofs_are_actual_declared_cells_and_chronological_unique_current_files(tmp_path):
    folder, layout, entities = proof_case(tmp_path)
    actual, sources, proofs, errors = positive_sources(layout, folder, tmp_path, 100.)
    assert not errors and actual == entities
    assert len(sources) == 6
    assert [s['frame_time'] for s in sources] == sorted(s['frame_time'] for s in sources)
    assert all(s['role'] == 'positive' and s['backend'] == 'wgc' for s in sources)
    assert all(p['independent_groups'] == [1, 2] for p in proofs)
    assert len({s['sha256'] for s in sources}) == 6


@pytest.mark.parametrize('defect', ['group','old_round','missing_identity','wrong_source_frame','wrong_revision'])
def test_positive_proofs_do_not_accept_two_fake_or_unbound_groups(tmp_path, defect):
    folder, layout, _ = proof_case(tmp_path)
    entry = layout['cells'][0]['evidence'][1]
    if defect == 'group':entry['group'] = 1
    if defect == 'old_round':layout['frames'][1]['frame_time'] = 99.
    if defect == 'missing_identity':layout['frames'][1].pop('generation')
    if defect == 'wrong_source_frame':entry['association_evidence']['source_frame_id'] = 999
    if defect == 'wrong_revision':entry['association_evidence']['source_map_revision'] = 1
    _, _, _, errors = positive_sources(layout, folder, tmp_path, 100.)
    assert any('fewer_than_two' in error for error in errors)


def test_identical_source_images_are_not_two_independent_evidence_files(tmp_path):
    folder, layout, _ = proof_case(tmp_path)
    (folder/'frames/0002.png').write_bytes((folder/'frames/0001.png').read_bytes())
    _, sources, _, errors = positive_sources(layout, folder, tmp_path, 100.)
    assert len(sources) == 5
    assert 'identical_image_for_distinct_positive_sources' in errors
    assert any('fewer_than_two' in error for error in errors)


def test_multiple_groups_on_one_capture_do_not_create_independence(tmp_path):
    folder, layout, _ = proof_case(tmp_path)
    first = layout['cells'][0]['evidence'][0]
    second = deepcopy(first);second['group'] = 2
    second['association_evidence']['rotation'] = rotation(20.)
    layout['cells'][0]['evidence'][1] = second
    _, _, proofs, errors = positive_sources(layout, folder, tmp_path, 100.)
    assert proofs[0]['independent_groups'] == [1, 2]
    assert not proofs[0]['independent_source_pairs']
    assert any('no_independent_actual_source_pair' in error for error in errors)


@pytest.mark.parametrize('angle,accepted', [(7.999999,False),(8.00161367674087,True),(10.,True)])
def test_actual_angle_is_not_rounded_or_replaced_by_group_number(tmp_path, angle, accepted):
    folder, layout, _ = proof_case(tmp_path)
    layout['cells'][0]['evidence'][1]['association_evidence']['rotation'] = rotation(angle)
    _, _, proofs, errors = positive_sources(layout, folder, tmp_path, 100.)
    assert bool(proofs[0]['independent_source_pairs']) is accepted
    assert (not errors) is accepted


@pytest.mark.parametrize('matrix', [
    [[1.,0.,0.],[0.,1.,0.],[0.,0.,-1.]],
    [[1.,0.,0.],[0.,1.,0.],[0.,0.,float('nan')]],
    [[1.,0.,0.],[0.,2.,0.],[0.,0.,1.]], [[1.,0.],[0.,1.]],
])
def test_positive_source_requires_finite_proper_rotation(tmp_path, matrix):
    assert not valid_so3(matrix)
    folder, layout, _ = proof_case(tmp_path)
    layout['cells'][0]['evidence'][1]['association_evidence']['rotation'] = matrix
    _, _, _, errors = positive_sources(layout, folder, tmp_path, 100.)
    assert 'invalid_positive_source_so3' in errors


def test_source_path_must_stay_inside_this_scans_directory(tmp_path):
    folder, layout, _ = proof_case(tmp_path)
    (tmp_path/'escape.png').write_bytes(b'other scan image')
    layout['frames'][1]['path'] = '../escape.png'
    _, _, _, errors = positive_sources(layout, folder, tmp_path, 100.)
    assert 'source_path_outside_scan_dir' in errors


def add_negative(folder, layout, fid=7):
    relative = f'frames/{fid:04d}.png'
    (folder/relative).write_bytes(b'real unit negative image')
    source = dict(source_frame_id=fid,source_frame_time=100.+fid,source_map_revision=0)
    frame = dict(frame_id=fid,path=relative,generation=fid,session_id=999,
        frame_time=100.+fid,semantic_fused=True,
        fused_pose=dict(map_revision=0,rotation=rotation(20.)),
        observation=dict(target_coverage=dict(source,model_executed=True,coverage_valid=True),
                         refine_diagnostic=dict(source,renewed=True)))
    evidence = dict(frame_id=fid,group=3,occupant='none',confidence=.9,icon_id='blue_scales')
    layout['frames'].append(frame)
    layout['cells'].append(dict(face='R',row=0,col=0,occupant='none',evidence=[evidence]))
    return frame, evidence


def test_actual_negative_and_geometry_sources_retained_with_positive_priority(tmp_path):
    folder, layout, _ = proof_case(tmp_path)
    frame, negative = add_negative(folder, layout)
    # The same actual image can also carry a target and remains one positive source.
    shared = deepcopy(negative);shared['frame_id'] = 1
    layout['cells'][-1]['evidence'].append(shared)
    layout['frames'][0].update(fused_pose=deepcopy(frame['fused_pose']),
                              observation=deepcopy(frame['observation']))
    for proof in layout['frames'][0]['observation'].values():
        proof.update(source_frame_id=1,source_frame_time=101.)
    _, sources, _, errors = positive_sources(layout, folder, tmp_path, 100.)
    assert not errors
    assert len(sources) == 7
    assert sources[-1]['role'] == 'negative' and sources[-1]['geometry_proof']
    assert sources[0]['role'] == 'positive' and sources[0]['cell_proofs']
    assert sources[-1]['cell_proofs'][0]['target_coverage']['coverage_valid']


@pytest.mark.parametrize('defect', ['legacy','coverage_frame','coverage_time','coverage_revision','not_executed','not_valid','not_renewed','fused_revision'])
def test_unbound_negative_proof_is_never_backfilled(tmp_path, defect):
    folder, layout, _ = proof_case(tmp_path)
    frame, _ = add_negative(folder, layout)
    coverage = frame['observation']['target_coverage']
    if defect == 'legacy':frame.pop('generation')
    if defect == 'coverage_frame':coverage['source_frame_id'] = 999
    if defect == 'coverage_time':coverage['source_frame_time'] += .01
    if defect == 'coverage_revision':coverage['source_map_revision'] = 1
    if defect == 'not_executed':coverage['model_executed'] = False
    if defect == 'not_valid':coverage['coverage_valid'] = False
    if defect == 'not_renewed':frame['observation']['refine_diagnostic']['renewed'] = False
    if defect == 'fused_revision':frame['fused_pose']['map_revision'] = 1
    _, sources, _, errors = positive_sources(layout, folder, tmp_path, 100.)
    assert not errors
    assert not any(s['role'] == 'negative' or s['cell_proofs'] for s in sources)


def test_reused_negative_pixels_fail_a_later_round_even_with_new_positive_sources(tmp_path):
    folder, layout, entities = proof_case(tmp_path)
    add_negative(folder, layout)
    fingerprint = {'code_sha256':'1'*64,'model_sha256':'2'*64,'config_sha256':'3'*64}
    ledger = AcceptanceLedger(tmp_path/'ledger.json',tmp_path);ledger.initialize(fingerprint)
    oracle = []
    from tools.deep_dive_live_acceptance import sha
    for i in (1, 2):
        image = tmp_path/f'oracle{i}.png';image.write_bytes(f'unit oracle {i}'.encode())
        oracle.append(dict(path=image.name,sha256=sha(image)))
    ledger.register_truth(dict(truth_id='unit_truth',board_id='unit_board',state_id='unit_state',
        reviewer='unit test fixture',coordinate_frame='manual reset frame',method='manual_multiview',
        independent_of_tested_output=True,ambiguous=False,images=oracle,entities=entities))
    def run(index):
        _, sources, _, errors = positive_sources(layout, folder, tmp_path, 100.)
        assert not errors
        return dict(run_id=f'unit_{index}',truth_id='unit_truth',fingerprint=fingerprint,
                    business_ready=True,business_status='ready',state_unchanged=True,
                    dispatch_wall_sec=45.,invocation_elapsed_sec=43.,entities=entities,sources=sources)
    assert ledger.record_run(run(1))['passed']
    for frame in layout['frames']:
        old = frame['frame_id'];frame['generation'] += 10;frame['frame_time'] += 10
        if old <= 6:(folder/frame['path']).write_bytes(f'new unit image {old}'.encode())
        for proof in frame.get('observation', {}).values():proof['source_frame_time'] += 10
    for cell in layout['cells']:
        for proof in cell['evidence']:
            if 'association_evidence' in proof:proof['association_evidence']['source_frame_time'] += 10
    result = ledger.record_run(run(2))
    assert not result['passed'] and 'reused_image_evidence' in result['failures']
    assert ledger.summary()['consecutive_passes'] == 0


def test_declared_target_mismatch_is_not_silently_repaired(tmp_path):
    _, layout, entities = proof_case(tmp_path)
    layout['inspiration_cells'] = []
    actual, errors = declared_entities(layout)
    assert len(actual) == 2 and 'declared_targets_disagree_with_cells' in errors
    assert layout['cells'][2]['occupant'] == 'inspiration'


def test_actual_provider_assertions_do_not_use_requested_provider_as_evidence():
    actual = dict(opencv_threads=1,main_ort_version=None,main_ort_distribution_version='1.27.0',
                  detector=dict(provider='CPUExecutionProvider',worker=dict(ort_version='1.24.4')))
    errors = runtime_errors(actual, dict(entity_provider='DmlExecutionProvider',main_ort_version='1.27.0'))
    assert errors == ['actual_runtime_mismatch:entity_provider','actual_runtime_mismatch:main_ort_version']
    assert not runtime_errors(actual, dict(entity_provider='CPUExecutionProvider',
        main_ort_version=None,main_ort_distribution_version='1.27.0',worker_ort_version='1.24.4'))


def test_main_ort_rule_is_explicit_if_loaded_and_rejects_an_isolated_main_mapping():
    actual = dict(opencv_threads=1,main_ort_version=None,detector={})
    assert not runtime_errors(actual, {'main_ort_version_if_loaded':'1.27.0'})
    actual.update(main_ort_version='1.24.4',main_ort_file='.pytest_tmp/worker/onnxruntime/__init__.py')
    errors = runtime_errors(actual, {'main_ort_version_if_loaded':'1.27.0'})
    assert 'actual_runtime_mismatch:main_ort_version_if_loaded' in errors
    assert 'actual_main_ort_not_from_repository_venv' in errors


def test_public_invocation_measurement_includes_postcheck_and_unknown_falls_back_to_dispatch():
    assert final_invocation_elapsed({'final_result':{'summary':{'invocation_elapsed_sec':47.2}}}, 50.) == 47.2
    assert final_invocation_elapsed({'status':'error'}, 92.) == 92.


def test_formal_campaign_requires_reviewed_truth_and_harness_frozen_before_input(tmp_path):
    code = tmp_path/'tools/deep_dive_live_acceptance.py';code.parent.mkdir();code.write_text('reviewed helper')
    model = tmp_path/'model.onnx';model.write_bytes(b'unit model')
    fingerprint = fingerprint_files(tmp_path,['tools/deep_dive_live_acceptance.py'],'model.onnx',config())
    ledger = AcceptanceLedger(tmp_path/'ledger.json',tmp_path);ledger.initialize(fingerprint)
    with pytest.raises(ValueError,match='already'):
        campaign(ledger,'missing',config(),tmp_path)
    # Isolated preflight test, no fake registration or live attempt.
    ledger.data['truths']['reviewed'] = {'images':[]}
    assert campaign(ledger,'reviewed',config(),tmp_path) == fingerprint
    changed = config();changed['entity_detector']['session.intra_op_num_threads'] = 8
    with pytest.raises(ValueError,match='differs'):
        campaign(ledger,'reviewed',changed,tmp_path)
    code.write_text('changed after freezing')
    with pytest.raises(ValueError,match='differs'):
        campaign(ledger,'reviewed',config(),tmp_path)


def test_repository_paths_cannot_escape(tmp_path):
    with pytest.raises(ValueError):
        local_path('../outside.png',tmp_path)


def wrapper_state():
    return dict(wrapper_entered=threading.Event(),wrapper_done=threading.Event(),errors=[])


def test_terminal_before_wrapper_finally_waits_for_saved_outcome_and_hud():
    state = wrapper_state();state['wrapper_entered'].set()
    state['errors'].append('public_dispatch_budget_exceeded')
    release = threading.Event()
    def finish():
        assert release.wait(1.)
        state.update(outcome={'layout':{'status':'cancelled'}},hud_samples=[1,2,3],state_unchanged=True)
        state['wrapper_done'].set()
    worker = threading.Thread(target=finish);worker.start()
    timer = threading.Timer(.01,release.set);timer.start()
    try:
        drain = wait_wrapper_done(state,timeout_sec=1.)
        assert drain['entered'] and drain['completed']
        assert state['outcome']['layout']['status'] == 'cancelled'
        assert state['state_unchanged'] and len(state['hud_samples']) == 3
        assert state['errors'] == ['public_dispatch_budget_exceeded']
    finally:
        release.set();worker.join(1.);timer.join(1.)
    assert not worker.is_alive()


def test_entered_wrapper_drain_has_bounded_failure_and_no_fake_done():
    state = wrapper_state();state['wrapper_entered'].set()
    drain = wait_wrapper_done(state,timeout_sec=.001)
    assert drain['entered'] and drain['completed'] is False
    assert not state['wrapper_done'].is_set()
    assert state['errors'] == ['scan_wrapper_drain_timeout']


def test_task_cancelled_before_scan_entry_does_not_wait_or_invent_outcome():
    state = wrapper_state()
    drain = wait_wrapper_done(state,timeout_sec=1.)
    assert drain['entered'] is False and drain['completed'] is None
    assert 'outcome' not in state and not state['errors']


def test_dispatch_timer_excludes_disclosed_fingerprint_guard_but_includes_wrapper_drain(tmp_path,monkeypatch):
    """Fake clock/runtime construction only; never load or control a game."""
    import sys
    from tools import deep_dive_live_acceptance as module
    clock = [0.]
    captured = []
    detector_close = []
    scan_module = SimpleNamespace(run_layout_scan=lambda:None)
    fake_cv = SimpleNamespace(setNumThreads=lambda n:None,getNumThreads=lambda:1)
    class Runner:
        def __init__(self,**kwargs):pass
        def start(self):clock[0] += 2.;return {}
        def run_task(self,**kwargs):
            clock[0] += 1.
            state = scan_module.run_layout_scan.__self__.round
            state['wrapper_entered'].set()
            state.update(outcome={'layout':{'success':False,'status':'cancelled'}},
                         state_unchanged=True,wrapper_elapsed_sec=2.)
            return {'cid':'unit_fake_dispatch'}
        def get_run(self,cid):clock[0] += .5;return {'status':'cancelled'}
        def cancel_task(self,cid):pass
        def wait_for_run(self,*args,**kwargs):return {'status':'cancelled'}
        def close(self):detector_close.append(True)
    class Ledger:
        data = {'fingerprint':{'model_sha256':'1'*64},'runs':[]}
        def record_run(self,payload):
            captured.append(payload)
            return dict(payload,attempt_index=1,passed=False,failures=['business_not_ready'])
        def summary(self):return {'consecutive_passes':0}
    def guard(*args):clock[0] += 7.
    original_wait = module.wait_wrapper_done
    def drain(state,*args,**kwargs):
        clock[0] += .75
        state['wrapper_done'].set()
        return original_wait(state,*args,**kwargs)
    monkeypatch.setattr(module,'ROOT',tmp_path)
    monkeypatch.setattr(module,'_OWNED_RUNTIME_CLOSED',False)
    monkeypatch.setattr(module.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(module,'campaign',guard)
    monkeypatch.setattr(module,'wait_wrapper_done',drain)
    monkeypatch.setattr(module,'positive_sources',lambda *args:([],[],[],[]))
    monkeypatch.setitem(sys.modules,'cv2',fake_cv)
    monkeypatch.setitem(sys.modules,'packages.aura_game',SimpleNamespace(EmbeddedGameRunner=Runner))
    monkeypatch.setitem(sys.modules,'plans.resonance_pc.src.actions',
                        SimpleNamespace(consciousness_deep_dive_scan_pc_actions=scan_module))
    args = SimpleNamespace(output=str(tmp_path/'output'),count=1,truth='unit_truth',keep_going=False)
    assert module.run_campaign(args,Ledger(),config()) == 1
    row = captured[0]
    assert row['freeze_guard_elapsed_sec'] == 7.
    assert row['task_terminal_observed_wall_sec'] == 1.5
    assert row['dispatch_wall_sec'] == 2.25
    assert row['invocation_elapsed_sec'] == 2.25
    assert row['state_unchanged'] and row['wrapper_drain']['completed']
    assert row['task_status'] == 'cancelled' and not row['business_ready']
    assert 'task_not_terminal_success' in row['harness_errors']
    assert detector_close == [True]
