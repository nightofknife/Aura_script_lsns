"""Fast startup keeps actual capture sources and physical response ownership."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from pathlib import Path

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import consciousness_deep_dive_scan_pc_actions as scan
from plans.resonance_pc.src.actions._deep_dive_layout_vision import LayoutScanner


def test_fast_startup_crosses_real_dispatch_and_public_action_boundary(monkeypatch):
    import yaml
    from packages.aura_core.scheduler.validation import InputValidator
    from plans.resonance_pc.src.actions import _deep_dive_planned_run_vision as page
    task = yaml.safe_load(Path('plans/resonance_pc/tasks/consciousness_deep_dive_scan_pc.yaml').read_text('utf8'))
    definition = task['consciousness_deep_dive_scan_pc']
    valid, inputs = InputValidator(None).validate_inputs_against_meta(
        definition['meta']['inputs'], {'startup_fast': True, 'recognition_goal': 'targets'})
    assert valid, inputs
    assert inputs['startup_fast'] is True
    valid, defaults = InputValidator(None).validate_inputs_against_meta(definition['meta']['inputs'], {})
    assert valid and defaults['startup_fast'] is False
    rgb = np.zeros((720,1280,3),np.uint8)
    capture = SimpleNamespace(success=True,image=rgb)
    async def captured(*args, **kwargs): return capture
    async def after(*args, **kwargs): return capture, {}
    seen = []
    async def core(app, **kwargs):
        seen.append(kwargs['startup_fast'])
        return {'summary': {'status':'partial'}}
    monkeypatch.setattr(scan,'_capture_serial',captured)
    monkeypatch.setattr(scan,'_capture_after_input',after)
    monkeypatch.setattr(scan,'run_layout_scan',core)
    monkeypatch.setattr(page,'observe',lambda image: dict(valid=True,scene='board',player_turn=True))
    monkeypatch.setattr(page,'read_hud',lambda *args, **kwargs: dict(collected_count=0,inspiration_total=2))
    asyncio.run(scan.scan_consciousness_deep_dive_layout(
        app=object(),entity_detector=object(),startup_fast=inputs['startup_fast']))
    assert seen == [True]


def source(fid, stamp, generation=None):
    return dict(frame_id=fid, frame_time=stamp, generation=fid if generation is None else generation,
        session_id=123, capture_backend='wgc', generation_source='atomic_wgc')


class Scanner:
    def __init__(self):
        self.ready = False
        self.identity_corrections = 0
        self.rotation = np.eye(3)
        self.pending_drag = None
        self.drag_in_progress = False
        self.response_axes = {}
        self.last_drag_response = None
        self.calls = []
        self.model_executions = self.cache_hits = 0
        self.last_image = None
        self.last_targets = []
        self.next_rotation = None
        self.semantic_rotation = None
        self.evidence = []
        self.target = dict(kind='player', box=[600, 150, 40, 50], point=[620., 175.],
            object_point=[1., 2., 3.], cell_index=4, source_depth=42., association_evidence={'old': True})

    def _detect_targets(self, rgb):
        self.calls.append(('detect', int(rgb[0, 0, 0])))
        if self.last_image is not None and np.array_equal(self.last_image, rgb):
            self.cache_hits += 1
        else:
            self.model_executions += 1
            self.last_image = rgb.copy()
        self.last_targets = [deepcopy(self.target)]
        return self.last_targets

    def observe(self, rgb, frame_id, *, semantic=True):
        self.calls.append(('observe', frame_id, semantic))
        self._detect_targets(rgb)
        self.ready = True
        if semantic:
            self.evidence.append(dict(frame_id=frame_id, stamp='incorrect_processing_time'))
        return dict(tracking_ok=True)

    def track_frame(self, rgb, frame_id, *, mask_targets):
        self.calls.append(('track', frame_id, deepcopy(mask_targets)))
        if self.next_rotation is not None:
            self.rotation = self.next_rotation.copy()
        return dict(tracking_ok=True)

    def pose_snapshot(self):
        return dict(rotation=self.rotation.copy())

    def semantic_view(self, rgb, frame_id, pose, *, source_frame_time):
        self.calls.append(('semantic', frame_id, source_frame_time, pose['rotation'].copy()))
        self._detect_targets(rgb)
        self.evidence.append(dict(frame_id=frame_id, stamp=source_frame_time))
        if self.semantic_rotation is not None:
            self.rotation = self.semantic_rotation.copy()
        return dict(tracking_ok=True)

    def target_mask_observation(self):
        return deepcopy(self.last_targets)

    def _seed_features(self, gray, targets, *, save_keyframe):
        self.calls.append(('seed', int(gray[0, 0]), deepcopy(targets), save_keyframe,
                           self.rotation.copy()))

    def annotate(self, rgb):
        return rgb.copy()

    _learn_response = LayoutScanner._learn_response
    note_drag = LayoutScanner.note_drag
    finish_drag = LayoutScanner.finish_drag


def image(value=0):
    return np.full((720, 1280, 3), value, np.uint8)


@pytest.fixture
def clock(monkeypatch):
    state = [100.1]
    monkeypatch.setattr(scan.time, 'monotonic', lambda: state[0])
    return state


def test_init_has_single_actual_timestamp_vote_and_honest_model_cache(clock):
    scanner = Scanner()
    observed = scan._calibration_observe(scanner, image(), 0, source(0, 100.), semantic=True)
    assert observed['tracking_ok']
    assert ('observe', 0, False) in scanner.calls
    assert scanner.evidence == [dict(frame_id=0, stamp=100.)]
    assert scanner.model_executions == 1 and scanner.cache_hits == 1
    assert observed['calibration_source']['frame_time'] == 100.
    seed = [call for call in scanner.calls if call[0] == 'seed']
    assert len(seed) == 1 and seed[0][1] == 0 and seed[0][3] is True


@pytest.mark.parametrize('defect', ['missing', 'fallback', 'backend', 'frame', 'future', 'old', 'session', 'generation'])
def test_invalid_capture_metadata_never_bootstraps_or_votes(clock, defect):
    scanner = Scanner(); metadata = source(0, 100.)
    if defect == 'missing': metadata = {}
    elif defect == 'fallback': metadata['generation_source'] = 'post_capture_health'
    elif defect == 'backend': metadata['capture_backend'] = 'printwindow'
    elif defect == 'frame': metadata['frame_id'] = 1
    elif defect == 'future': metadata['frame_time'] = 101.
    elif defect == 'old': metadata['frame_time'] = 99.
    elif defect == 'session': metadata['session_id'] = None
    elif defect == 'generation': metadata['generation'] = True
    observed = scan._calibration_observe(scanner, image(), 0, metadata, semantic=True)
    assert not observed['tracking_ok'] and scanner.calls == [] and scanner.evidence == []


@pytest.mark.parametrize('defect', ['same', 'generation', 'time', 'session'])
def test_reusing_old_source_cannot_renew_anchor(clock, defect):
    scanner = Scanner(); scan._calibration_observe(scanner, image(), 0, source(0, 100.))
    previous = deepcopy(scanner.evidence)
    metadata = source(1, 100.05)
    if defect == 'same': metadata = source(0, 100.)
    elif defect == 'generation': metadata['generation'] = 0
    elif defect == 'time': metadata['frame_time'] = 100.
    elif defect == 'session': metadata['session_id'] = 456
    observed = scan._calibration_observe(scanner, image(), metadata['frame_id'], metadata, semantic=True)
    assert observed['reason'] == 'calibration_source_not_advanced' and scanner.evidence == previous


def test_mid_axis_geometry_uses_recent_projected_masks_without_model_or_votes(clock):
    scanner = Scanner(); scan._calibration_observe(scanner, image(), 0, source(0, 100.))
    previous = deepcopy(scanner.evidence)
    clock[0] = 100.3
    observed = scan._calibration_observe(scanner, image(1), 1, source(1, 100.2))
    assert observed['calibration_stage'] == 'geometry'
    assert scanner.model_executions == 1 and scanner.evidence == previous
    track = [call for call in scanner.calls if call[0] == 'track'][-1]
    assert track[2]['age_sec'] == pytest.approx(.2)


def test_expired_mask_refresh_is_current_box_only_not_old_cell_depth(clock):
    scanner = Scanner(); scan._calibration_observe(scanner, image(), 0, source(0, 100.))
    previous = deepcopy(scanner.evidence)
    clock[0] = 101.1
    scan._calibration_observe(scanner, image(1), 1, source(1, 101.))
    assert scanner.model_executions == 2 and scanner.evidence == previous
    track = [call for call in scanner.calls if call[0] == 'track'][-1]
    assert track[2]['age_sec'] == 0.
    assert track[2]['mask_observation'] == [dict(kind='player', box=[600,150,40,50], point=[620.,175.])]
    assert scanner._calibration_masks['source']['frame_id'] == 1


@pytest.mark.parametrize('axis', [0, 1])
def test_axis_end_explicit_refine_uses_capture_stamp_and_physical_response(clock, axis):
    scanner = Scanner(); scan._calibration_observe(scanner, image(), 0, source(0, 100.))
    dx, dy = ((150, 0) if axis == 0 else (0, 150))
    scanner.note_drag(dx, dy)
    vector = np.zeros(3); vector[axis] = .2
    scanner.next_rotation = cv2.Rodrigues(vector)[0]
    scanner.semantic_rotation = cv2.Rodrigues(vector*2)[0]
    scanner.finish_drag(dx, dy)
    clock[0] = 100.5
    observed = scan._calibration_observe(scanner, image(1), 1, source(1, 100.4), semantic=True)
    assert observed['tracking_ok'] and scanner.last_drag_response['axis'] == axis
    assert scanner.response_axes[axis] == pytest.approx(vector/150.)
    assert scanner.last_drag_response['angle_deg'] == pytest.approx(np.degrees(.2))
    assert scanner.evidence[-1] == dict(frame_id=1, stamp=100.4)
    assert scanner.model_executions == 2
    seed = [call for call in scanner.calls if call[0] == 'seed'][-1]
    assert seed[1] == 1 and seed[3] is True
    np.testing.assert_allclose(seed[4], scanner.semantic_rotation)


def test_semantic_while_mouse_held_cannot_finish_physical_response(clock):
    scanner = Scanner(); scan._calibration_observe(scanner, image(), 0, source(0, 100.))
    scanner.note_drag(150, 0)
    scanner.next_rotation = cv2.Rodrigues(np.array([0., .2, 0.]))[0]
    clock[0] = 100.5
    scan._calibration_observe(scanner, image(1), 1, source(1, 100.4), semantic=True)
    assert scanner.pending_drag is not None and scanner.response_axes == {}


def test_startup_fast_is_explicit_and_default_does_not_change_full_scan(monkeypatch):
    seen = []
    async def run(**kwargs):
        seen.append(dict(scan._SCAN_CONTROL.get()))
        return {}
    monkeypatch.setattr(scan, '_run_layout_scan', run)
    asyncio.run(scan.run_layout_scan(object()))
    asyncio.run(scan.run_layout_scan(object(), recognition_goal='targets', entity_detector=object(), startup_fast=True))
    assert seen[0]['startup_fast'] is False and seen[1]['startup_fast'] is True
    with pytest.raises(ValueError, match='model target scan'):
        asyncio.run(scan.run_layout_scan(object(), startup_fast=True))


def test_segment_cancel_always_releases_once_without_semantic_vote(tmp_path, monkeypatch):
    class CancelController:
        def __init__(self): self.releases = 0
        async def mouse_down_async(self, button): pass
        async def mouse_up_async(self, button): self.releases += 1
    controller = CancelController()
    app = SimpleNamespace(controller=controller)
    count = [0]
    async def move(*args, **kwargs):
        count[0] += 1
        if count[0] == 2: raise asyncio.CancelledError()
    app.move_to_async = move
    async def settle(*args): pass
    monkeypatch.setattr(scan, '_settle', settle)
    monkeypatch.setattr(scan, '_cancel_check', lambda: None)
    scanner = Scanner(); action = {}
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scan._segmented_drag(app, scanner, (400,200), (550,200), .15,.04,
            tmp_path, [], action, scan.time.monotonic(), scan.time.monotonic()+20.))
    assert controller.releases == 1 and not scanner.drag_in_progress and scanner.evidence == []


@pytest.mark.parametrize('fast, unknown', [(True, False), (False, False), (True, True)])
def test_segment_fast_keeps_raw_and_scene_release_but_skips_overlay_and_semantics(
        tmp_path, monkeypatch, clock, fast, unknown):
    releases = []
    async def noop(*args, **kwargs): pass
    async def up(*args): releases.append(True)
    app = SimpleNamespace(move_to_async=noop,
        controller=SimpleNamespace(mouse_down_async=noop, mouse_up_async=up))
    scanner = Scanner()
    scan._calibration_observe(scanner, image(), 0, source(0, 100.))
    before = deepcopy(scanner.evidence)
    control = dict(startup_fast=fast, observe_fn=lambda rgb: dict(
        valid=not unknown, scene='unknown' if unknown else 'board', player_turn=True),
        latest_frame_metadata={}, initial_board_observation=None)
    token = scan._SCAN_CONTROL.set(control)
    captured = [0]; written = []
    async def capture(*args):
        captured[0] += 1
        stamp = 100.+captured[0]*.2; clock[0] = stamp+.1
        control['latest_frame_metadata'] = source(captured[0], stamp)
        return SimpleNamespace(success=True, image=image(captured[0]), backend='wgc'), {}
    monkeypatch.setattr(scan, '_capture_after_input', capture)
    monkeypatch.setattr(scan, '_settle', noop)
    monkeypatch.setattr(scan, '_cancel_check', lambda: None)
    monkeypatch.setattr(scan.cv2, 'imwrite', lambda path, rgb: written.append(path) or True)
    frames = [dict(frame_id=0)]; action = {}
    try:
        failure = asyncio.run(scan._segmented_drag(app, scanner, (400,200), (550,200), .15,.04,
            tmp_path, frames, action, 100., 120.))
    finally:
        scan._SCAN_CONTROL.reset(token)
    assert releases == [True] and not scanner.drag_in_progress
    if unknown:
        assert failure == 'unexpected_scene:unknown' and captured[0] == 1
        assert scanner.evidence == before and len(written) == 1
    elif fast:
        assert failure is None and captured[0] == 3 and len(written) == 3
        assert all('_overlay' not in path for path in written)
        assert scanner.model_executions == 1 and scanner.evidence == before
    else:
        assert failure is None and len(written) == 6
        assert len([c for c in scanner.calls if c[0] == 'observe' and c[2] == 'auto']) == 3
