"""Operation masks use the injected detector's exact image, with no fallback."""
from copy import deepcopy
from types import SimpleNamespace

import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_operation_frame as operation


TARGET = dict(kind='player', box=[420, 210, 30, 40], point=[435., 220.],
              confidence=.05, confirmable=False, source='current_model')


@pytest.fixture
def inputs(monkeypatch):
    rgb = np.zeros((720, 1280, 3), np.uint8)
    cells = {slot: dict(occupant='none', occupant_status='confirmed',
                       node_status='unknown', icon_id=None) for slot in range(54)}
    monkeypatch.setattr(operation, '_layout', lambda value: (cells, 4))
    monkeypatch.setattr(operation, 'observe', lambda rgb: dict(
        scene='unknown', player_turn=True, cyan_pixels={}))
    monkeypatch.setattr(operation.LayoutScanner, '_bootstrap', lambda self, rgb: True)
    monkeypatch.setattr(operation, '_q_candidates', lambda *a: iter([{}]))
    monkeypatch.setattr(operation, '_refine', lambda *a, **k: None)
    monkeypatch.setattr(operation, '_candidate_shapes', lambda *a: [dict(point=[430, 250]), dict(point=[500, 250])])
    monkeypatch.setattr(operation, '_geometry_seeds', lambda *a: [])
    monkeypatch.setattr(operation, '_surface', lambda *a: [])
    monkeypatch.setattr(operation, '_readings', lambda *a: [])
    monkeypatch.setattr(operation, 'geometry', lambda: SimpleNamespace(actor_rotations={4: [0]}))
    frame = dict(status='ready', mode='choose_rotate', actor_slot=4,
                 layout_digest=operation._layout_digest(cells),
                 pose=dict(K=np.eye(3).tolist(), rvec=[0, 0, 0], tvec=[0, 0, 40]))
    return rgb, {}, frame


def call(api, inputs, detector=None):
    rgb, layout, frame = inputs
    kwargs = {} if detector is None else {'target_detector': detector}
    if api == 'wide':
        return operation.build_wide_reference_frame(rgb, layout, **kwargs)
    if api == 'operation':
        return operation.build_operation_frame(rgb, layout, 'rotate', **kwargs)
    return operation.verify_rotation_preview(rgb, layout, 0, frame, **kwargs)


@pytest.mark.parametrize('api', ['wide', 'operation', 'preview'])
@pytest.mark.parametrize('packet', [False, True, 'cached'])
def test_injected_current_image_masks_replace_rules_without_losing_weak_boxes(monkeypatch, inputs, api, packet):
    if api == 'operation':
        monkeypatch.setattr(operation, 'observe', lambda rgb: dict(scene='choose_rotate', player_turn=True))
    targets = [deepcopy(TARGET)]
    def detector(rgb):
        assert rgb is inputs[0]
        return (dict(targets=targets, coverage_valid=True, model_executed=packet != 'cached',
                     cached=packet == 'cached') if packet else targets)
    def forbidden(rgb):
        raise AssertionError('legacy_detector_called')
    monkeypatch.setattr(operation, 'detect_targets', forbidden)
    seen = []
    def features(rgb, masks, **kwargs):
        assert rgb is inputs[0]
        seen.append(masks)
        return {}
    monkeypatch.setattr(operation, '_features', features)
    result = call(api, inputs, detector)
    assert result['status'] != 'ready'  # No geometry manufactured in this test.
    assert seen == [targets]
    assert seen[0] is not targets and seen[0][0] is not targets[0]
    targets[0]['box'][0] += 10
    assert seen[0][0]['box'][0] == 420


@pytest.mark.parametrize('api', ['wide', 'operation', 'preview'])
def test_default_detector_compatibility(monkeypatch, inputs, api):
    if api == 'operation':
        monkeypatch.setattr(operation, 'observe', lambda rgb: dict(scene='choose_rotate', player_turn=True))
    calls = []
    monkeypatch.setattr(operation, 'detect_targets', lambda rgb: calls.append(rgb) or [])
    monkeypatch.setattr(operation, '_features', lambda *a, **k: {})
    call(api, inputs)
    assert len(calls) == 1 and calls[0] is inputs[0]


@pytest.mark.parametrize('api', ['wide', 'operation', 'preview'])
@pytest.mark.parametrize('bad', [None, {}, {'targets': [], 'coverage_valid': False},
    {'targets': None, 'coverage_valid': True}, [dict(TARGET, box=[420, 210, 0, 40])],
    [dict(TARGET, point=[np.nan, 210])], [dict(TARGET, box=[True, 210, 30, 40])],
    [dict(TARGET, confidence=float('nan'))], [dict(TARGET, kind='invented')],
    [dict(TARGET, box=[1e200, 210, 30, 40])], [dict(TARGET, point=[1280, 210])], 'error'])
def test_failed_detector_blocks_registration_without_empty_or_rules_fallback(monkeypatch, inputs, api, bad):
    if api == 'operation':
        monkeypatch.setattr(operation, 'observe', lambda rgb: dict(scene='choose_rotate', player_turn=True))
    def detector(rgb):
        if isinstance(bad, str):
            raise RuntimeError('model_failed')
        return bad
    def forbidden(*args, **kwargs):
        raise AssertionError('failed_detector_reached_geometry_or_rules')
    monkeypatch.setattr(operation, 'detect_targets', forbidden)
    monkeypatch.setattr(operation, '_features', forbidden)
    result = call(api, inputs, detector)
    assert result['status'] in ('blocked', 'unknown')
    assert result['reason'] == 'operation_target_detector_failed'
    assert result['evidence']['target_detector_error'] in ('TypeError', 'ValueError', 'RuntimeError')
