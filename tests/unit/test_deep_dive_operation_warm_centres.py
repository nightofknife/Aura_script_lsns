"""Replay actual move registration, retaining independent glyph/geometry gates."""
from copy import deepcopy
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_operation_frame as operation


REPO = Path(__file__).resolve().parents[2]
EVIDENCE = REPO / 'docs/developer-guide/deep-dive-recognition-20261001/option1'
FIXTURES = REPO / 'tests/fixtures/deep_dive_anchor'


def rgb(path):
    return cv2.cvtColor(cv2.imread(str(path)), cv2.COLOR_BGR2RGB)


@pytest.fixture
def actual_case():
    snapshot = json.loads((EVIDENCE / 'operation_failure_snapshot.json').read_text(encoding='utf-8'))
    audit = json.loads((EVIDENCE / 'operation_segment02_audit.json').read_text(encoding='utf-8'))
    return rgb(EVIDENCE / 'operation_failure.png'), snapshot, audit


def replay(case):
    image, snapshot, audit = case
    def detector(current_rgb):
        assert current_rgb is image
        return deepcopy(audit['model_packet'])
    return operation.build_operation_frame(
        image, snapshot['layout'], 'move', scan_epoch=snapshot['scan_epoch'],
        map_revision=snapshot['map_revision'], view_epoch=snapshot['registration_frame']['view_epoch'] + 1,
        registration_frame=snapshot['registration_frame'], target_detector=detector)


def old_warm_mask(crop, icon):
    hue, saturation, value = cv2.split(cv2.cvtColor(crop, cv2.COLOR_RGB2HSV))
    if icon == 'red_single_eye':
        return ((hue <= 6) | (hue >= 168)) & (saturation > 140) & (value > 140)
    if icon == 'orange_triple_eye':
        return (hue >= 7) & (hue <= 20) & (saturation > 110) & (value > 150)


def test_real_move_image_registers_against_same_wide_reference(actual_case):
    result = replay(actual_case)
    assert result['status'] == 'ready'
    assert result['reason'] == 'three_face_reference_and_current_top_registration_confirmed'
    assert result['actor_operation_slot'] == result['actor_slot'] == 4
    assert len(result['Q_candidates']) == 1
    candidate = result['Q_candidates'][0]
    assert candidate['matched'] >= 5 and candidate['conflicts'] == 0
    assert candidate['consensus'] == 1.
    assert result['evidence']['current_top_anchors'] >= 5
    assert result['evidence']['camera_transition']['confirmed']
    assert result['geometry_quality']['rmse_px'] <= operation._MAX_RMSE
    binding = operation.bind_move(result, actual_case[1]['pending_action']['destination_slot'])
    assert binding['status'] == 'ready' and binding['logical_slot'] == 1


def test_older_colour_mask_reproduces_actual_four_anchor_failure(monkeypatch, actual_case):
    current_mask = operation._colour_mask
    monkeypatch.setattr(operation, '_colour_mask', lambda crop, icon:
        old_warm_mask(crop, icon) if icon in ('red_single_eye', 'orange_triple_eye') else current_mask(crop, icon))
    result = replay(actual_case)
    assert result['status'] == 'waiting'
    assert result['reason'] == 'three_face_geometry_or_content_unconfirmed'


def test_actual_pink_eye_is_shape_confirmed_before_centre_pixels(actual_case):
    image, snapshot, audit = actual_case
    seed = audit['model']['fits'][0]['seed']
    intrinsic = np.array(snapshot['registration_frame']['pose']['K'])
    ratio = operation._camera('choose_move')[0][0, 0] / operation._camera('choose_rotate')[0][0, 0]
    intrinsic[0, 0] *= ratio
    intrinsic[1, 1] *= ratio
    surface = operation._surface(intrinsic, np.array(seed['rvec']), np.array(seed['tvec']))
    crop = operation._crop(image, surface[5]['quad'])
    classification, measured, gain = operation._classify_operation_crop(crop)
    assert classification['icon_id'] == 'red_single_eye'
    assert classification['confidence'] >= operation._CONFIDENCE and gain > 1.
    previous = old_warm_mask(measured, classification['icon_id'])
    current = operation._colour_mask(measured, classification['icon_id'])
    assert np.count_nonzero(previous[13:83, 13:83]) < 50
    assert np.count_nonzero(current[13:83, 13:83]) >= 50


def reading_patch(patch):
    image = np.zeros((720, 1280, 3), np.uint8)
    image[200:296, 450:546] = patch
    surface = [dict(operation_slot=0, centre=np.array([497.5, 247.5]),
                    quad=np.array([[450., 200.], [545., 200.], [545., 295.], [450., 295.]]))]
    return image, surface


@pytest.mark.parametrize('hue', [0, 7, 15, 23, 162, 170])
def test_bright_warm_wall_is_not_a_shape_or_anchor(hue):
    hsv = np.full((96, 96, 3), [hue, 220, 230], np.uint8)
    patch = cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)
    assert operation._classify_operation_crop(patch)[0]['icon_id'] is None
    image, surface = reading_patch(patch)
    assert not operation._readings(image, surface, operation._features(image, []))


def test_warm_border_seams_remain_unclassified():
    hsv = np.zeros((96, 96, 3), np.uint8)
    hsv[:12] = hsv[84:] = [7, 220, 230]
    hsv[:, :12] = hsv[:, 84:] = [7, 220, 230]
    patch = cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)
    assert operation._classify_operation_crop(patch)[0]['icon_id'] is None
    image, surface = reading_patch(patch)
    assert not operation._readings(image, surface, operation._features(image, []))


def test_actual_boss_flare_cannot_supply_operation_anchor():
    patch = rgb(FIXTURES / 'whole_warm_boss_glow_130.png')
    assert operation._classify_operation_crop(patch)[0]['icon_id'] is None
    image, surface = reading_patch(patch)
    assert not operation._readings(image, surface, operation._features(image, []))


def test_even_confirmed_pink_eye_cannot_bypass_entity_mask(actual_case):
    image, snapshot, audit = actual_case
    seed = audit['model']['fits'][0]['seed']
    intrinsic = np.array(snapshot['registration_frame']['pose']['K'])
    ratio = operation._camera('choose_move')[0][0, 0] / operation._camera('choose_rotate')[0][0, 0]
    intrinsic[0, 0] *= ratio
    intrinsic[1, 1] *= ratio
    surface = operation._surface(intrinsic, np.array(seed['rvec']), np.array(seed['tvec']))
    target = dict(kind='player', box=[654, 376, 192, 100], point=[750, 425])
    readings = operation._readings(image, surface[:9], operation._features(image, [target]), actor=4)
    assert 5 not in {row['operation_slot'] for row in readings}
