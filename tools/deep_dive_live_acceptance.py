"""Owned-process live harness for an already frozen, independently labelled ledger.

Runs a warm EmbeddedGameRunner repeatedly. Never initializes a campaign, creates
truth, changes a board, or substitutes a configured provider for actual metadata.
Only the CLI is live; parsing/proof helpers do not import the game runtime.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from copy import deepcopy
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import sys
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
PREFIX = 'resonance_pc.deep_dive.entity_detector.'
TASK = 'tasks:consciousness_deep_dive_scan_pc.yaml:consciousness_deep_dive_scan_pc'
HUD_FIELDS = ('plane_index', 'rounds_remaining', 'moves_used', 'moves_total',
              'rotations_used', 'rotations_total', 'collected_count', 'inspiration_total')
POST_HUD_MIN_REMAINING_SEC = .5  # Reserve one complete fresh native read/save.
INITIAL_HUD_MAX_ATTEMPTS = 4
KINDS = ('player', 'singularity', 'inspiration')
TERMINAL = {'success', 'failed', 'failure', 'error', 'cancelled', 'canceled', 'stopped', 'timeout'}
_OWNED_RUNTIME_CLOSED = False


def local_path(value, root=ROOT):
    root = Path(root).resolve()
    path = (root/Path(value)).resolve()
    if not path.is_relative_to(root):
        raise ValueError('all acceptance paths must stay in repository')
    return path


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix+'.writing')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf8')
    os.replace(temporary, path)


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


class EntityConfig:
    """Override only a dedicated entity detector's config prefix."""
    def __init__(self, base, suffixes):
        self.base = base
        self.suffixes = deepcopy(suffixes)

    def get(self, key, default=None):
        if key.startswith(PREFIX) and key[len(PREFIX):] in self.suffixes:
            return self.suffixes[key[len(PREFIX):]]
        return self.base.get(key, default) if self.base is not None else default


def shared_ocr(registry):
    """Resolve the exact singleton dependency already injected into public scan."""
    return registry.get_service_instance('plans/aura_base/ocr')


def validate_config(value):
    if not isinstance(value, dict):
        raise ValueError('config must be an object')
    if value.get('opencv_threads') != 1 or type(value.get('opencv_threads')) is not int:
        raise ValueError('this harness requires explicit opencv_threads=1')
    inputs = value.get('scan_inputs')
    if (not isinstance(inputs, dict) or inputs.get('recognition_goal') != 'targets'
            or inputs.get('scan_route') not in ('cells', 'mixed', 'faces', 'vertices', 'target_cells', 'target_faces', 'target_framed')):
        raise ValueError('scan_inputs require explicit targets goal and scan_route')
    allowed = {'max_steps', 'time_budget_sec', 'drag_step_px', 'drag_duration_sec',
               'settle_sec', 'recognition_goal', 'scan_route', 'startup_fast'}
    if set(inputs)-allowed:
        raise ValueError('unsupported scan task input')
    if 'startup_fast' in inputs and type(inputs['startup_fast']) is not bool:
        raise ValueError('targets startup_fast must be an explicit boolean')
    budget = inputs.get('time_budget_sec')
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not 0 < budget <= 90:
        raise ValueError('scan_inputs.time_budget_sec must be explicit and <=90')
    entity = value.get('entity_detector')
    if not isinstance(entity, dict) or not entity or any(not isinstance(k, str) or k.startswith(PREFIX) for k in entity):
        raise ValueError('entity_detector requires unprefixed configuration suffixes')
    assertions = value.get('runtime_assertions')
    if not isinstance(assertions, dict) or not isinstance(assertions.get('entity_provider'), str):
        raise ValueError('runtime_assertions.entity_provider is required')
    allowed_assertions = {'entity_provider', 'main_ort_version', 'main_ort_distribution_version',
                          'main_ort_version_if_loaded', 'worker_ort_version', 'worker_provider', 'worker_runtime_site'}
    if set(assertions)-allowed_assertions:
        raise ValueError('unsupported actual-runtime assertion')
    return deepcopy(value)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--ledger', required=True)
    result.add_argument('--output', required=True)
    result.add_argument('--truth', required=True, help='Existing registered truth_id; never a generated oracle.')
    result.add_argument('--count', type=int, default=1)
    result.add_argument('--config', required=True, help='Repository JSON path or literal JSON object.')
    result.add_argument('--keep-going', action='store_true', help='Default stops at first failed attempt.')
    return result


def campaign(ledger, truth_id, config, root=ROOT):
    """Fail preflight before runtime/input if truth or actual freeze disagrees."""
    from tools.deep_dive_acceptance import fingerprint_files
    ledger._require()
    if truth_id not in ledger.data['truths']:
        raise ValueError('truth must already be independently registered')
    frozen = ledger.data['fingerprint']
    if not frozen.get('code_files') or not frozen.get('model_file'):
        raise ValueError('formal live campaign needs actual frozen code/model files')
    if 'tools/deep_dive_live_acceptance.py' not in frozen['code_files']:
        raise ValueError('freeze the reviewed live harness itself before running')
    current = fingerprint_files(root, list(frozen['code_files']), frozen['model_file'], config)
    if current != frozen:
        raise ValueError('actual code/model/config differs from frozen fingerprint')
    for image in ledger.data['truths'][truth_id]['images']:
        ledger._image(image)
    return deepcopy(frozen)


def declared_entities(layout):
    result = []
    for kind, name in (('player', 'player_cell'), ('singularity', 'singularity_cell')):
        if layout.get(name) is not None:
            result.append(dict(kind=kind, **{k:layout[name].get(k) for k in ('face', 'row', 'col')}))
    for cell in layout.get('inspiration_cells', []) or []:
        result.append(dict(kind='inspiration', **{k:cell.get(k) for k in ('face', 'row', 'col')}))
    def key(row):
        return tuple(row.get(k) for k in ('kind', 'face', 'row', 'col'))
    confirmed = [dict(kind=c['occupant'], **{k:c.get(k) for k in ('face', 'row', 'col')})
                 for c in layout.get('cells', []) if c.get('occupant') in KINDS
                 and c.get('occupant_status') == 'confirmed']
    errors = [] if Counter(map(key, result)) == Counter(map(key, confirmed)) else ['declared_targets_disagree_with_cells']
    return result, errors


INDEPENDENCE_DEG = 8.  # LayoutScanner.semantic_view/observe: min(view_rotation angles) >= 8.


def valid_so3(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return False
    if any(not isinstance(row, (list, tuple)) or len(row) != 3 for row in value):
        return False
    if any(type(v) not in (int, float) or not math.isfinite(v) for row in value for v in row):
        return False
    if any(abs(sum(value[k][i]*value[k][j] for k in range(3))-(1. if i == j else 0.)) > 1e-5
           for i in range(3) for j in range(3)):
        return False
    a, b, c = value
    det = a[0]*(b[1]*c[2]-b[2]*c[1])-a[1]*(b[0]*c[2]-b[2]*c[0])+a[2]*(b[0]*c[1]-b[1]*c[0])
    return abs(det-1.) <= 1e-5


def source_angle(left, right):
    trace = sum(left[i][j]*right[i][j] for i in range(3) for j in range(3))
    return math.degrees(math.acos(max(-1., min(1., (trace-1.)/2.))))


def positive_sources(layout, scan_dir, root, dispatch_at):
    """Audit positive independence and every available actual fused source."""
    root = Path(root).resolve()
    scan_dir = local_path(scan_dir, root)
    entities, errors = declared_entities(layout)
    frames = {frame['frame_id']:frame for frame in layout.get('frames', [])}
    cells = {(c.get('face'), c.get('row'), c.get('col')):c for c in layout.get('cells', [])}
    selected = {}
    role_rank = {'geometry':0, 'negative':1, 'positive':2}

    def register(frame, role):
        generation, session, captured = (frame.get(k) for k in ('generation', 'session_id', 'frame_time'))
        if (type(generation) is not int or generation < 0
                or not isinstance(session, (str, int)) or isinstance(session, bool) or session == ''
                or type(captured) not in (int, float) or not math.isfinite(captured) or captured < dispatch_at
                or not frame.get('semantic_fused')):
            return None
        identity = (str(session), generation)
        if identity not in selected:
            try:
                path = local_path(scan_dir/frame['path'], root)
                if not path.is_relative_to(scan_dir):
                    errors.append('source_path_outside_scan_dir')
                    return None
                digest = sha(path)
            except (OSError, ValueError, KeyError):
                errors.append('source_file_missing_or_outside_repository')
                return None
            selected[identity] = dict(path=path.relative_to(root).as_posix(), sha256=digest,
                backend='wgc', role=role, session_id=str(session), generation=generation,
                frame_time=captured, map_revision=layout['map_revision'], frame_id=frame['frame_id'],
                target_proofs=[], cell_proofs=[])
        source = selected[identity]
        if source['frame_id'] != frame['frame_id'] or source['frame_time'] != captured:
            errors.append('one_capture_identity_has_inconsistent_frame_metadata')
            return None
        if role_rank[role] > role_rank[source['role']]:
            source['role'] = role
        return source

    for entity in entities:
        key = tuple(entity.get(k) for k in ('face', 'row', 'col'))
        for evidence in cells.get(key, {}).get('evidence', []):
            confidence = evidence.get('confidence')
            if evidence.get('occupant') != entity['kind'] or type(confidence) not in (int, float) or confidence < .70:
                continue
            frame = frames.get(evidence.get('frame_id'), {})
            association = evidence.get('association_evidence') or {}
            group, captured = evidence.get('group'), frame.get('frame_time')
            stamp = association.get('source_frame_time')
            if (type(group) is not int or group < 1 or association.get('source_frame_id') != frame.get('frame_id')
                    or association.get('source_map_revision') != layout.get('map_revision')
                    or type(stamp) not in (int, float) or type(captured) not in (int, float)
                    or not math.isfinite(stamp) or not math.isclose(stamp, captured, rel_tol=0., abs_tol=1e-6)):
                continue
            if not valid_so3(association.get('rotation')):
                errors.append('invalid_positive_source_so3')
                continue
            source = register(frame, 'positive')
            if source is not None:
                source['target_proofs'].append(dict(entity=deepcopy(entity), group=group,
                    confidence=confidence, association_evidence=deepcopy(association)))

    def bound_frame_proof(frame, proof):
        stamp, captured = proof.get('source_frame_time'), frame.get('frame_time')
        return (proof.get('source_frame_id') == frame.get('frame_id')
                and proof.get('source_map_revision') == layout.get('map_revision')
                and type(stamp) in (int, float) and type(captured) in (int, float)
                and math.isfinite(stamp) and math.isfinite(captured)
                and math.isclose(stamp, captured, rel_tol=0., abs_tol=1e-6))

    def renewed_geometry(frame):
        diagnostic = frame.get('observation', {}).get('refine_diagnostic') or {}
        fused = frame.get('fused_pose') or {}
        return (diagnostic.get('renewed') is True and bound_frame_proof(frame, diagnostic)
                and fused.get('map_revision') == layout.get('map_revision')
                and valid_so3(fused.get('rotation')))

    # Retain true fused negative/node observations, not only target sources.
    # Legacy seed frames lacking atomic metadata are not invented retrospectively.
    for cell in layout.get('cells', []):
        for evidence in cell.get('evidence', []):
            if evidence.get('occupant') in KINDS:
                continue
            frame = frames.get(evidence.get('frame_id'), {})
            coverage = frame.get('observation', {}).get('target_coverage') or {}
            if (not renewed_geometry(frame) or coverage.get('model_executed') is not True
                    or coverage.get('coverage_valid') is not True or not bound_frame_proof(frame, coverage)):
                continue
            role = 'negative' if evidence.get('occupant') == 'none' else 'geometry'
            source = register(frame, role)
            if source is not None:
                source['cell_proofs'].append(dict(face=cell.get('face'), row=cell.get('row'), col=cell.get('col'),
                    evidence=deepcopy(evidence), target_coverage=deepcopy(coverage)))
    for frame in frames.values():
        diagnostic = frame.get('observation', {}).get('refine_diagnostic') or {}
        fused = frame.get('fused_pose') or {}
        if renewed_geometry(frame):
            source = register(frame, 'geometry')
            if source is not None:
                source['geometry_proof'] = dict(refine_diagnostic=deepcopy(diagnostic), pose=deepcopy(fused))

    # One source may support both targets and ordinary cells. Prefer its positive
    # role. Identical pixels across new captures are one image, not extra proof.
    unique_hashes = {}
    for source in sorted(selected.values(), key=lambda s:(-role_rank[s['role']], s['frame_time'])):
        if source['sha256'] in unique_hashes:
            if source['role'] == 'positive':
                errors.append('identical_image_for_distinct_positive_sources')
            continue
        unique_hashes[source['sha256']] = source
    sources = sorted(unique_hashes.values(), key=lambda s:(s['frame_time'], s['session_id'], s['generation']))
    proofs = []
    for entity in entities:
        observations = [(source, proof) for source in sources for proof in source['target_proofs']
                        if proof['entity'] == entity]
        groups = sorted({proof['group'] for _, proof in observations})
        pairs = []
        for i, (left, lp) in enumerate(observations):
            for right, rp in observations[i+1:]:
                if (left['session_id'], left['generation']) == (right['session_id'], right['generation']) or lp['group'] == rp['group']:
                    continue
                angle = source_angle(lp['association_evidence']['rotation'], rp['association_evidence']['rotation'])
                if angle >= INDEPENDENCE_DEG:
                    pairs.append(dict(left_frame=left['frame_id'], right_frame=right['frame_id'],
                        left_generation=left['generation'], right_generation=right['generation'], angle_deg=angle))
        proofs.append(dict(entity=deepcopy(entity), independent_groups=groups,
                           required_angle_deg=INDEPENDENCE_DEG, independent_source_pairs=pairs))
        label = str(tuple(entity.get(k) for k in ('kind', 'face', 'row', 'col')))
        if len(groups) < 2:
            errors.append('target_has_fewer_than_two_provenanced_groups:'+label)
        if not pairs:
            errors.append('target_has_no_independent_actual_source_pair:'+label)
    return entities, sources, proofs, sorted(set(errors))


def full_hud_unchanged(samples):
    if len(samples) != 3:
        return False, 'requires_two_initial_and_one_final_full_hud'
    baseline = None
    previous = None
    for sample in samples:
        hud, source, scene = sample.get('hud', {}), sample.get('source', {}), sample.get('observation', {})
        values = {field:hud.get(field) for field in HUD_FIELDS}
        if hud.get('status') != 'complete' or any(type(v) is not int for v in values.values()):
            return False, 'incomplete_full_hud'
        if not (scene.get('valid') is True and scene.get('scene') == 'board' and scene.get('player_turn') is True
                and not scene.get('enemy_turn') and not scene.get('unknown_modal_evidence')):
            return False, 'hud_source_not_player_board'
        age = sample.get('source_age_sec')
        if (source.get('backend') != 'wgc' or type(age) not in (int, float) or not math.isfinite(age)
                or not 0 <= age <= .5 or type(source.get('generation')) is not int
                or not isinstance(source.get('session_id'), (str, int))
                or isinstance(source.get('session_id'), bool)
                or type(source.get('frame_time')) not in (int, float)
                or not math.isfinite(source['frame_time'])):
            return False, 'hud_source_not_fresh_wgc'
        if previous is not None and (source.get('session_id') != previous.get('session_id')
                or source.get('generation', -1) <= previous.get('generation', -1)
                or source.get('frame_time', -math.inf) <= previous.get('frame_time', -math.inf)):
            return False, 'hud_source_did_not_advance'
        if baseline is not None and values != baseline:
            return False, 'hud_state_changed'
        baseline, previous = values, source
    return True, 'eight_hud_fields_stable'


def post_hud_attempt_reason(sample, baseline, previous, expected_path, root):
    """A partial native read can retry only with a real unchanged new source."""
    import cv2
    source, scene, hud = (sample.get(k, {}) for k in ('source', 'observation', 'hud'))
    age, captured = sample.get('source_age_sec'), source.get('frame_time')
    if (source.get('backend') != 'wgc' or type(age) not in (int, float) or not math.isfinite(age)
            or not 0 <= age <= .5 or type(captured) not in (int, float) or not math.isfinite(captured)
            or not 0 <= time.monotonic()-captured <= .5 or type(source.get('generation')) is not int
            or source['generation'] < 0 or not isinstance(source.get('session_id'), (str, int))
            or isinstance(source.get('session_id'), bool) or source.get('session_id') == ''):
        return 'hud_source_not_fresh_wgc'
    old = previous.get('source', {})
    if (source['session_id'] != old.get('session_id') or source['generation'] <= old.get('generation', -1)
            or captured <= old.get('frame_time', math.inf)):
        return 'hud_source_did_not_advance'
    if not (scene.get('valid') is True and scene.get('scene') == 'board' and scene.get('player_turn') is True
            and not scene.get('enemy_turn') and not scene.get('unknown_modal_evidence')
            and hud.get('scene') == 'board' and not hud.get('enemy_turn')
            and not hud.get('insufficient_battle_roles')):
        return 'hud_source_not_player_board'
    try:
        path = local_path(source['path'], root)
        if path != Path(expected_path).resolve() or sha(path) != source.get('sha256'):
            return 'hud_raw_source_mismatch'
        pixels = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if pixels is None or pixels.shape != (720, 1280, 3):
            return 'hud_raw_source_mismatch'
        digest = hashlib.sha256(cv2.cvtColor(pixels, cv2.COLOR_BGR2RGB).tobytes()).hexdigest()
        if digest != sample.get('rgb_sha256'):
            return 'hud_raw_source_mismatch'
        # Native glyph alternatives contain tuples; the saved JSON represents
        # those as lists. Compare the complete JSON representation without
        # coercing unsupported objects or allowing nonfinite evidence.
        json_sample = json.loads(json.dumps(sample, allow_nan=False))
        if json.loads(path.with_suffix('.json').read_text(encoding='utf8')) != json_sample:
            return 'hud_raw_source_mismatch'
    except (OSError, ValueError, KeyError, TypeError):
        return 'hud_raw_source_mismatch'
    native = dict(plane_index='native_plane_icon', rounds_remaining='native_round_glyph_templates',
                  **{field:'native_font_pair_templates' for field in HUD_FIELDS[2:]})
    values = {field:hud.get(field) for field in HUD_FIELDS}
    for field, value in values.items():
        if value is None:
            continue
        if type(value) is not int or hud.get('evidence_source', {}).get(field) != native[field]:
            return 'hud_field_not_native'
        if value != baseline['hud'].get(field):
            return 'hud_state_changed'
    if any(value is None for value in values.values()):
        return 'incomplete_full_hud'
    if hud.get('status') != 'complete':
        return 'hud_full_status_not_complete'
    return 'eight_hud_fields_stable'


def initial_hud_attempt_reason(sample, previous, observed_values, expected_path, root):
    """One complete source only; partial values can detect change, never fill gaps."""
    import cv2
    source, scene, hud = (sample.get(k, {}) for k in ('source', 'observation', 'hud'))
    values = {field:hud.get(field) for field in HUD_FIELDS}
    if any(type(value) is int and field in observed_values and value != observed_values[field]
           for field, value in values.items()):
        return 'hud_state_changed'
    captured, age = source.get('frame_time'), sample.get('source_age_sec')
    if (source.get('backend') != 'wgc' or type(captured) not in (int, float) or not math.isfinite(captured)
            or type(age) not in (int, float) or not math.isfinite(age) or age < 0
            or type(source.get('generation')) is not int or source['generation'] < 0
            or not isinstance(source.get('session_id'), (str, int))
            or isinstance(source.get('session_id'), bool) or source['session_id'] == ''):
        return 'hud_source_identity_invalid'
    if previous is not None:
        old = previous.get('source', {})
        if (source['session_id'] != old.get('session_id') or source['generation'] <= old.get('generation', -1)
                or captured <= old.get('frame_time', math.inf)):
            return 'hud_source_did_not_advance'
    if not (scene.get('valid') is True and scene.get('scene') == 'board' and scene.get('player_turn') is True
            and not scene.get('enemy_turn') and not scene.get('unknown_modal_evidence')
            and hud.get('scene') == 'board' and not hud.get('enemy_turn')
            and not hud.get('insufficient_battle_roles')):
        return 'hud_source_not_player_board'
    try:
        path = local_path(source['path'], root)
        if path != Path(expected_path).resolve() or sha(path) != source.get('sha256'):
            return 'hud_raw_source_mismatch'
        pixels = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if pixels is None or pixels.shape != (720, 1280, 3):
            return 'hud_raw_source_mismatch'
        digest = hashlib.sha256(cv2.cvtColor(pixels, cv2.COLOR_BGR2RGB).tobytes()).hexdigest()
        json_sample = json.loads(json.dumps(sample, allow_nan=False))
        if (digest != sample.get('rgb_sha256')
                or json.loads(path.with_suffix('.json').read_text(encoding='utf8')) != json_sample):
            return 'hud_raw_source_mismatch'
    except (OSError, ValueError, KeyError, TypeError):
        return 'hud_raw_source_mismatch'
    native = dict(plane_index='native_plane_icon', rounds_remaining='native_round_glyph_templates',
                  **{field:'native_font_pair_templates' for field in HUD_FIELDS[2:]})
    if any(value is not None and (type(value) is not int or hud.get('evidence_source', {}).get(field) != native[field])
           for field, value in values.items()):
        return 'hud_field_not_native'
    current_age = time.monotonic()-captured
    if current_age < 0:
        return 'hud_source_identity_invalid'
    if age > .5 or current_age > .5:
        return 'hud_source_stale'
    if any(value is None for value in values.values()):
        return 'incomplete_full_hud'
    if hud.get('status') != 'complete':
        return 'hud_full_status_not_complete'
    return 'eight_hud_fields_complete'


def actual_runtime(detector, cv2):
    module = sys.modules.get('onnxruntime')
    try:
        distribution = importlib.metadata.version('onnxruntime')
    except importlib.metadata.PackageNotFoundError:
        distribution = None
    return dict(opencv_threads=cv2.getNumThreads(),
                main_ort_version=getattr(module, '__version__', None),
                main_ort_file=getattr(module, '__file__', None),
                main_ort_distribution_version=distribution,
                detector_model_sha256=sha(detector.model_path), detector=detector.status())


def runtime_errors(actual, assertions):
    detector = actual.get('detector', {})
    # Explicit fields supplied by the isolated service, not requested config.
    worker = detector.get('worker') or {}
    values = dict(entity_provider=detector.get('provider'),
        main_ort_version=actual.get('main_ort_version'),
        main_ort_distribution_version=actual.get('main_ort_distribution_version'),
        worker_ort_version=worker.get('ort_version'), worker_provider=worker.get('provider'),
        worker_runtime_site=worker.get('runtime_site'))
    errors = []
    if actual.get('opencv_threads') != 1:
        errors.append('actual_runtime_mismatch:opencv_threads')
    if actual.get('main_ort_version') is not None:
        module_path = actual.get('main_ort_file')
        if not isinstance(module_path, str) or not Path(module_path).resolve().is_relative_to((ROOT/'.venv').resolve()):
            errors.append('actual_main_ort_not_from_repository_venv')
    for key, expected in assertions.items():
        if key == 'main_ort_version_if_loaded':
            if actual.get('main_ort_version') not in (None, expected):
                errors.append('actual_runtime_mismatch:'+key)
            continue
        found = values.get(key)
        matches = (Path(found).resolve() == Path(expected).resolve()
                   if key == 'worker_runtime_site' and isinstance(found, str) and isinstance(expected, str)
                   else found == expected)
        if not matches:
            errors.append('actual_runtime_mismatch:'+key)
    return errors


class ScanHarness:
    """Own one dedicated lazy detector across all rounds of one warm runtime."""
    def __init__(self, original, config, root=ROOT, model_sha256=None):
        self.original, self.config, self.root = original, config, Path(root).resolve()
        self.model_sha256 = model_sha256
        self.detector = None
        self.ocr = None
        self.round = None

    async def hud_sample(self, app, path, previous=None, *, hard_deadline=None, native_only=False):
        import cv2
        from plans.resonance_pc.src.actions import _deep_dive_planned_run_vision as vision
        from plans.resonance_pc.src.actions.consciousness_deep_dive_scan_pc_actions import _await_serial
        not_before = time.monotonic()
        if hard_deadline is not None and hard_deadline-not_before <= POST_HUD_MIN_REMAINING_SEC:
            raise RuntimeError('post_hud_dispatch_budget_exhausted')
        adapter = app.target_runtime._get_or_create_session()
        deadline = min(not_before+2., hard_deadline) if hard_deadline is not None else not_before+2.
        generation = previous['source']['generation'] if previous else -1
        session = previous['source']['session_id'] if previous else None
        while time.monotonic() < deadline:
            if hard_deadline is not None and hard_deadline-time.monotonic() <= POST_HUD_MIN_REMAINING_SEC:
                raise RuntimeError('post_hud_dispatch_budget_exhausted')
            packet, cancelled = await _await_serial(asyncio.to_thread(
                adapter.capture_stream_frame, generation, expected_session_id=session))
            if cancelled:
                raise asyncio.CancelledError()
            if packet is not None:
                captured_at, packet_generation, packet_session = (packet.get(k) for k in
                    ('arrived_at_monotonic', 'generation', 'session_id'))
                if (type(captured_at) not in (int, float) or not math.isfinite(captured_at)
                        or type(packet_generation) is not int or packet_generation < 0
                        or not isinstance(packet_session, (str, int)) or isinstance(packet_session, bool)
                        or packet_session == ''):
                    raise RuntimeError('atomic_hud_source_identity_invalid')
                if captured_at >= not_before:
                    break
                # The latest cached packet may predate release/post-check.
                # Require a newly delivered atomic source, not a health ID.
                generation = packet_generation
                session = packet['session_id']
            await asyncio.sleep(.005)
        else:
            raise RuntimeError('atomic_hud_capture_timeout')
        capture = packet['capture']
        if not capture.success or capture.image is None or str(capture.backend).lower() != 'wgc':
            raise RuntimeError('atomic_hud_capture_not_wgc')
        rgb = capture.image.copy()
        if rgb.dtype.name != 'uint8' or rgb.shape != (720, 1280, 3):
            raise RuntimeError('atomic_hud_capture_invalid_rgb')
        captured = float(packet['arrived_at_monotonic'])
        if not 0 <= time.monotonic()-captured <= .5:
            raise RuntimeError('atomic_hud_source_stale')
        source = dict(backend='wgc', generation=int(packet['generation']),
                      session_id=packet['session_id'], frame_time=captured,
                      path=path.relative_to(self.root).as_posix())
        # Preserve each actual post source even if observation is cancelled.
        path.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(path), cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)):
            raise RuntimeError('hud_png_write_failed')
        source['sha256'] = sha(path)
        sample = dict(source=source, rgb_sha256=hashlib.sha256(rgb.tobytes()).hexdigest(),
                      status='read_pending')
        write_json(path.with_suffix('.json'), sample)
        if hard_deadline is not None and time.monotonic() >= hard_deadline:
            raise RuntimeError('post_hud_dispatch_budget_exhausted')
        observation, cancelled = await _await_serial(asyncio.to_thread(vision.observe, rgb))
        if cancelled:
            raise asyncio.CancelledError()
        if self.ocr is None:
            raise RuntimeError('shared_public_action_ocr_not_resolved')
        hud, cancelled = await _await_serial(asyncio.to_thread(
            vision.read_hud, rgb, None if native_only else self.ocr, observation=observation))
        if cancelled:
            raise asyncio.CancelledError()
        age = time.monotonic()-captured
        sample = dict(source=source, source_age_sec=age, observation=observation, hud=hud,
                      rgb_sha256=hashlib.sha256(rgb.tobytes()).hexdigest())
        write_json(path.with_suffix('.json'), sample)
        return sample

    async def initial_hud(self, app, folder, hard_deadline):
        """At most four new sources; only two consecutive whole proofs become baselines."""
        started = time.monotonic()
        attempts = self.round['initial_hud_attempts'] = []
        observed_values, ready_samples, previous = {}, [], None
        reason = 'initial_full_hud_not_complete_stable'
        self.round['initial_hud_reason'] = reason
        try:
            for index in range(INITIAL_HUD_MAX_ATTEMPTS):
                if hard_deadline-time.monotonic() <= POST_HUD_MIN_REMAINING_SEC:
                    reason = 'initial_hud_dispatch_budget_exhausted'
                    break
                path = folder/f'hud_pre_{index+1}.png'
                tick = time.monotonic()
                attempt = dict(index=index, path=path.relative_to(self.root).as_posix(), started_at=tick)
                attempts.append(attempt)
                try:
                    sample = await self.hud_sample(app, path, previous, hard_deadline=hard_deadline, native_only=True)
                    attempt['sample'] = sample
                    reason = initial_hud_attempt_reason(sample, previous, observed_values, path, self.root)
                    if time.monotonic() >= hard_deadline:
                        reason = 'initial_hud_dispatch_budget_exhausted'
                    attempt['reason'] = reason
                except BaseException as exc:
                    attempt.update(reason=type(exc).__name__+':'+str(exc), elapsed_sec=time.monotonic()-tick)
                    raise
                attempt['elapsed_sec'] = time.monotonic()-tick
                for field in HUD_FIELDS:
                    value = sample.get('hud', {}).get(field)
                    if type(value) is int:
                        observed_values.setdefault(field, value)
                previous = sample
                if reason == 'eight_hud_fields_complete':
                    ready_samples.append(sample)
                    if len(ready_samples) == 2:
                        self.round['hud_samples'].extend(ready_samples)
                        reason = 'two_initial_native_hud_sources_stable'
                        return ready_samples
                elif reason in ('incomplete_full_hud', 'hud_source_stale'):
                    ready_samples.clear()
                else:
                    break
            raise RuntimeError(reason if reason not in ('incomplete_full_hud', 'hud_source_stale', 'eight_hud_fields_complete')
                               else 'initial_full_hud_not_complete_stable')
        finally:
            self.round['initial_hud_reason'] = reason
            self.round['initial_hud_elapsed_sec'] = time.monotonic()-started

    async def post_hud(self, app, folder, baselines, hard_deadline):
        """At most two new native retries, entirely within dispatch's 90s."""
        started = time.monotonic()
        attempts = self.round['post_hud_attempts'] = []
        previous = baselines[-1]
        selected = None
        reason = 'incomplete_full_hud'
        try:
            for index in range(3):
                if hard_deadline-time.monotonic() <= POST_HUD_MIN_REMAINING_SEC:
                    reason = 'post_hud_dispatch_budget_exhausted'
                    break
                path = folder/('hud_post.png' if index == 0 else f'hud_post_retry_{index}.png')
                tick = time.monotonic()
                attempt = dict(index=index, path=path.relative_to(self.root).as_posix(), started_at=tick)
                attempts.append(attempt)
                try:
                    sample = await self.hud_sample(app, path, previous, hard_deadline=hard_deadline, native_only=True)
                    attempt['sample'] = sample
                    selected = sample
                    reason = post_hud_attempt_reason(sample, baselines[-1], previous, path, self.root)
                    if time.monotonic() >= hard_deadline:
                        reason = 'post_hud_dispatch_budget_exhausted'
                    attempt['reason'] = reason
                except BaseException as exc:
                    attempt.update(reason=type(exc).__name__+':'+str(exc), elapsed_sec=time.monotonic()-tick)
                    raise
                attempt['elapsed_sec'] = time.monotonic()-tick
                if reason != 'incomplete_full_hud':
                    break
                previous = sample
            if selected is not None:
                self.round['hud_samples'].append(selected)
            ready = reason == 'eight_hud_fields_stable'
            if ready:
                ready, reason = full_hud_unchanged(baselines+[selected])
            if time.monotonic() >= hard_deadline:
                ready, reason = False, 'post_hud_dispatch_budget_exhausted'
            return ready, reason
        finally:
            self.round['post_hud_elapsed_sec'] = time.monotonic()-started

    async def scan(self, app, **kwargs):
        import cv2
        state = self.round
        state['wrapper_entered'].set()
        folder = state['folder']
        start = time.monotonic()
        samples = self.round['hud_samples']
        try:
            if self.ocr is None:
                from packages.aura_core.api import service_registry
                self.ocr = shared_ocr(service_registry)
            self.round['shared_ocr'] = dict(service_id='plans/aura_base/ocr', object_id=id(self.ocr),
                                           class_name=type(self.ocr).__name__)
            original_detector = kwargs.get('entity_detector')
            if self.detector is None:
                if original_detector is None or original_detector.status().get('ready'):
                    raise RuntimeError('original_detector_missing_or_already_loaded')
                from plans.resonance_pc.src.services.deep_dive_entity_detector_service import DeepDiveEntityDetectorService
                self.detector = DeepDiveEntityDetectorService(EntityConfig(
                    getattr(original_detector, '_config', None), self.config['entity_detector']))
            from plans.resonance_pc.src.actions.consciousness_deep_dive_scan_pc_actions import _await_serial
            _, cancelled = await _await_serial(asyncio.to_thread(self.detector.initialize))
            if cancelled:
                raise asyncio.CancelledError()
            runtime = actual_runtime(self.detector, cv2)
            self.round['actual_runtime_before_core'] = runtime
            actual_errors = runtime_errors(runtime, self.config['runtime_assertions'])
            if runtime['detector_model_sha256'] != self.model_sha256:
                actual_errors.append('actual_detector_model_mismatch')
            if actual_errors:
                self.round['errors'].extend(actual_errors)
                raise RuntimeError('actual_runtime_contract_not_met_before_scan')
            # Cold model startup must not age the pre-input HUD proofs.
            deadline = self.round.get('dispatch_deadline')
            if type(deadline) not in (int, float) or not math.isfinite(deadline):
                raise RuntimeError('initial_hud_dispatch_deadline_missing')
            initial_samples = await self.initial_hud(app, folder, deadline)
            initial = [s['hud'] for s in initial_samples]
            if kwargs.get('expected_inspirations') != initial[1]['inspiration_total']-initial[1]['collected_count']:
                raise RuntimeError('initial_target_inventory_changed_before_core')
            remaining = float(kwargs['time_budget_sec'])-(time.monotonic()-start)
            if remaining < 5.:
                raise RuntimeError('acceptance_initialization_budget_exhausted')
            kwargs.update(time_budget_sec=remaining, output_dir=folder/'scan', entity_detector=self.detector)
            outcome = await self.original(app, **kwargs)
            self.round['outcome'] = outcome
            self.round['actual_runtime'] = actual_runtime(self.detector, cv2)
            self.round['errors'].extend(runtime_errors(self.round['actual_runtime'], self.config['runtime_assertions']))
            # Original scan has released input and drained its vision owners.
            deadline = self.round.get('dispatch_deadline')
            if type(deadline) not in (int, float) or not math.isfinite(deadline):
                raise RuntimeError('post_hud_dispatch_deadline_missing')
            self.round['state_unchanged'], reason = await self.post_hud(app, folder, samples.copy(), deadline)
            self.round['hud_reason'] = reason
            if not self.round['state_unchanged']:
                self.round['errors'].append(reason)
            return outcome
        except BaseException as exc:
            self.round['errors'].append(type(exc).__name__+':'+str(exc))
            raise
        finally:
            self.round['wrapper_elapsed_sec'] = time.monotonic()-start
            try:
                write_json(folder/'harness_scan.json', {k:v for k,v in self.round.items()
                    if k not in ('outcome', 'folder', 'wrapper_entered', 'wrapper_done')})
            finally:
                # Cancellation can publish a terminal task before the async
                # scan releases input and its finally writes outcome/HUD.
                state['wrapper_done'].set()

    def close(self):
        if self.detector is not None:
            self.detector.close()
            return self.detector.status()
        return None


def final_invocation_elapsed(record, dispatch_wall):
    durations = []
    def visit(value):
        if isinstance(value, dict):
            elapsed = value.get('invocation_elapsed_sec')
            if type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0:
                durations.append(elapsed)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(record)
    return max(durations) if durations else dispatch_wall


def wait_wrapper_done(state, timeout_sec=20.):
    """Terminal scheduler status alone does not acknowledge async teardown."""
    started = time.monotonic()
    entered = state['wrapper_entered'].is_set()
    completed = state['wrapper_done'].wait(timeout_sec) if entered else None
    if completed is False:
        state['errors'].append('scan_wrapper_drain_timeout')
    return dict(entered=entered, completed=completed, elapsed_sec=time.monotonic()-started)


def detector_close_error(status):
    """Audit optional owned workers without requiring new fields on old paths."""
    status = status or {}
    worker_close = status.get('worker_close')
    if worker_close is not None and (not isinstance(worker_close, dict) or worker_close.get('drained') is not True):
        return 'entity_worker_close_not_drained'
    head = status.get('head_thread')
    if head is None:
        return None
    if not isinstance(head, dict):
        return 'entity_head_close_status_invalid'
    if head.get('owned') is True or head.get('in_flight') is True:
        return 'entity_head_thread_still_owned_or_in_flight'
    head_close = head.get('close')
    if head_close is not None:
        if not isinstance(head_close, dict) or head_close.get('drained') is not True:
            return 'entity_head_close_not_drained'
        if head.get('owned') is not False or head.get('in_flight') is not False:
            return 'entity_head_thread_close_not_proven'
    return None


def run_campaign(args, ledger, config):
    global _OWNED_RUNTIME_CLOSED
    import cv2
    # Set once before starting any runtime worker. Never adjust during rounds.
    cv2.setNumThreads(config['opencv_threads'])
    from packages.aura_game import EmbeddedGameRunner
    from plans.resonance_pc.src.actions import consciousness_deep_dive_scan_pc_actions as scan
    output = local_path(args.output)
    if output.exists() and any(output.iterdir()):
        raise ValueError('output must be new or empty; do not overwrite an earlier attempt')
    output.mkdir(parents=True, exist_ok=True)
    os_tmp = output/'os_tmp'
    os_tmp.mkdir()
    for name in ('TEMP', 'TMP', 'TMPDIR'):
        os.environ[name] = str(os_tmp)
    harness = ScanHarness(scan.run_layout_scan, config, model_sha256=ledger.data['fingerprint']['model_sha256'])
    runner = EmbeddedGameRunner(profile='embedded_full')
    records, cid = [], None
    scan.run_layout_scan = harness.scan
    try:
        environment_started = time.monotonic()
        runtime_start = runner.start()
        environment_sec = time.monotonic()-environment_started
        write_json(output/'initial_environment.json', dict(elapsed_sec=environment_sec,
            preflight_freeze_guard_elapsed_sec=getattr(args, 'preflight_freeze_guard_elapsed_sec', None),
            runtime_start=runtime_start, opencv_threads=cv2.getNumThreads(), fingerprint=ledger.data['fingerprint']))
        for _ in range(args.count):
            index = len(ledger.data['runs'])+1
            folder = output/f'round_{index:04d}'
            folder.mkdir()
            run_id = uuid4().hex
            state = dict(folder=folder, hud_samples=[], outcome=None, errors=[], state_unchanged=False,
                         wrapper_entered=threading.Event(), wrapper_done=threading.Event())
            harness.round = state
            dispatch_at = None
            freeze_guard_started = time.monotonic()
            freeze_guard_elapsed = None
            record = {}
            try:
                try:
                    campaign(ledger, args.truth, config)
                finally:
                    freeze_guard_elapsed = time.monotonic()-freeze_guard_started
                # File validation is separately disclosed. Recognition timing
                # begins immediately before the public dispatch, including
                # model startup, HUD, input, report and wrapper drain.
                dispatch_at = time.monotonic()
                state['dispatch_deadline'] = dispatch_at+90.
                dispatch = runner.run_task(game_name='resonance_pc', task_ref=TASK,
                                           inputs=config['scan_inputs'], wait=False)
                cid = str(dispatch['cid'])
                write_json(folder/'dispatch.json', dispatch)
                cancelled = False
                while True:
                    record = runner.get_run(cid)
                    if str(record.get('status', '')).lower() in TERMINAL and not record.get('execution_pending'):
                        break
                    elapsed = time.monotonic()-dispatch_at
                    if elapsed > 90 and not cancelled:
                        runner.cancel_task(cid)
                        cancelled = True
                        state['errors'].append('public_dispatch_budget_exceeded')
                    if elapsed > 120:
                        raise TimeoutError('cancelled_runtime_did_not_drain')
                    time.sleep(.05)
            except BaseException as exc:
                state['errors'].append(type(exc).__name__+':'+str(exc))
                if cid:
                    runner.cancel_task(cid)
                    try:
                        record = runner.wait_for_run(cid, timeout_sec=20)
                    except Exception as drain:
                        state['errors'].append('drain_failed:'+str(drain))
            terminal_observed_wall = time.monotonic()-dispatch_at if dispatch_at is not None else 0.
            wrapper_drain = wait_wrapper_done(state)
            dispatch_wall = time.monotonic()-dispatch_at if dispatch_at is not None else 0.
            write_json(folder/'run_record.json', record)
            outcome = state.get('outcome') or {}
            layout = outcome.get('layout') or {}
            try:
                entities, sources, proofs, errors = positive_sources(layout, folder/'scan', ROOT,
                                                                    dispatch_at if dispatch_at is not None else math.inf)
            except Exception as exc:
                entities, sources, proofs = [], [], []
                errors = ['positive_provenance_build_failed:'+str(exc)]
            state['errors'].extend(errors)
            if str(record.get('status', '')).lower() != 'success' or record.get('execution_pending'):
                state['errors'].append('task_not_terminal_success')
            summary = outcome.get('summary') or {}
            business = (layout.get('success') is True and layout.get('targets_ready') is True
                        and layout.get('status') == 'targets_ready')
            payload = dict(run_id=run_id, truth_id=args.truth, fingerprint=deepcopy(ledger.data['fingerprint']),
                business_ready=business and not state['errors'], business_status=layout.get('status'),
                scan_success=layout.get('success') is True, targets_readiness=layout.get('targets_ready') is True,
                state_unchanged=state['state_unchanged'], dispatch_wall_sec=dispatch_wall,
                invocation_elapsed_sec=final_invocation_elapsed(record, dispatch_wall),
                entities=entities, sources=sources, target_source_proofs=proofs,
                harness_errors=state['errors'], task_status=record.get('status'), scan_summary=summary,
                core_elapsed_sec=layout.get('elapsed_sec'), wrapper_elapsed_sec=state.get('wrapper_elapsed_sec'),
                actual_runtime=state.get('actual_runtime'), hud_reason=state.get('hud_reason'),
                initial_hud_attempts=state.get('initial_hud_attempts', []),
                initial_hud_reason=state.get('initial_hud_reason'), initial_hud_elapsed_sec=state.get('initial_hud_elapsed_sec'),
                post_hud_attempts=state.get('post_hud_attempts', []), post_hud_elapsed_sec=state.get('post_hud_elapsed_sec'),
                initial_environment_sec=environment_sec,
                freeze_guard_elapsed_sec=freeze_guard_elapsed,
                dispatch_started=dispatch_at is not None,
                task_terminal_observed_wall_sec=terminal_observed_wall, wrapper_drain=wrapper_drain,
                artifacts=folder.relative_to(ROOT).as_posix())
            judged = ledger.record_run(payload)
            write_json(folder/'acceptance_record.json', judged)
            records.append(judged)
            write_json(output/'campaign_summary.json', ledger.summary())
            print(json.dumps(dict(attempt=judged['attempt_index'], passed=judged['passed'],
                elapsed_sec=dispatch_wall, failures=judged['failures'], harness_errors=state['errors']), ensure_ascii=False), flush=True)
            if wrapper_drain['completed'] is False:
                # Do not dispatch another round into a wrapper still draining.
                break
            cid = None
            if not judged['passed'] and not args.keep_going:
                break
        return 0 if records and all(row['passed'] for row in records) else 1
    finally:
        drain_error = None
        try:
            if cid:
                runner.cancel_task(cid)
                runner.wait_for_run(cid, timeout_sec=20)
                if harness.round is not None:
                    drained = wait_wrapper_done(harness.round)
                    if drained['completed'] is False:
                        drain_error = 'scan_wrapper_drain_timeout'
        except Exception as exc:
            drain_error = str(exc)
        finally:
            scan.run_layout_scan = harness.original
            detector_closed = None
            try:
                detector_closed = harness.close()
                drain_error = drain_error or detector_close_error(detector_closed)
            except Exception as exc:
                drain_error = drain_error or 'entity_detector_close_failed:'+str(exc)
            finally:
                runner.close()
                _OWNED_RUNTIME_CLOSED = True
                write_json(output/'runtime_closed.json', dict(closed_at=time.monotonic(),
                    only_owned_runtime=True, drain_error=drain_error, detector_closed=detector_closed))
        if drain_error is not None:
            raise RuntimeError('runtime_drain_failed:'+drain_error)


def main(argv=None):
    args = parser().parse_args(argv)
    if Path.cwd().resolve() != ROOT:
        raise ValueError('run acceptance from repository root')
    if args.count < 1 or args.count > 100:
        raise ValueError('count must be 1..100')
    config = validate_config(json.loads(args.config) if args.config.lstrip().startswith('{') else
                             json.loads(local_path(args.config).read_text('utf8')))
    from tools.deep_dive_acceptance import AcceptanceLedger
    ledger = AcceptanceLedger(local_path(args.ledger), ROOT)
    preflight_started = time.monotonic()
    campaign(ledger, args.truth, config)
    args.preflight_freeze_guard_elapsed_sec = time.monotonic()-preflight_started
    return run_campaign(args, ledger, config)


if __name__ == '__main__':
    sys.path.insert(0, str(ROOT))
    try:
        code = main()
    except Exception:
        import traceback
        traceback.print_exc()
        code = 1
    # This disposable process owns its runtime and has closed/drained it. Avoid
    # Qt interpreter teardown crashes without terminating any game process.
    if os.name == 'nt' and _OWNED_RUNTIME_CLOSED:
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.TerminateProcess.argtypes = (ctypes.c_void_p, ctypes.c_uint)
        kernel.TerminateProcess(kernel.GetCurrentProcess(), code)
    raise SystemExit(code)
