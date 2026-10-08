"""Independent eye classification and its measured pixels share the warm domain."""
from copy import deepcopy
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_layout_vision as vision


FIXTURES = Path(__file__).parents[1] / 'fixtures/deep_dive_anchor/orange_support_17'
RECORDS = {r['frame_id']: r for r in json.loads((FIXTURES / 'metadata.json').read_text())}
NATIVE = Path(vision.__file__).resolve().parents[2] / 'templates/deep_dive_layout'


def read_rgb(path):
    return cv2.cvtColor(cv2.imread(str(path)), cv2.COLOR_BGR2RGB)


def old_orange_mask(rgb):
    h, s, v = cv2.split(cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV))
    return (h >= 7) & (h <= 20) & (s > 110) & (v > 150)


def inner_mass(mask):
    return int(mask[10:86, 10:86].sum())


@pytest.mark.parametrize('frame_id', [833, 846, 859])
def test_actual_high_confidence_triple_eye_retains_measured_warm_pixels(frame_id):
    image = read_rgb(FIXTURES / f'orange_D22_{frame_id}.png')
    result = vision.classify_icon(image)
    assert result['icon_id'] == 'orange_triple_eye'
    assert result['confidence'] >= .80
    assert inner_mass(old_orange_mask(image)) == 0
    assert inner_mass(vision._icon_mask(image, result['icon_id'])) >= 50


def actual_scanner(frame_id):
    row = RECORDS[frame_id]
    image = np.zeros((720, 1280, 3), np.uint8)
    x1, y1, x2, y2 = row['fixture_roi']
    image[y1:y2, x1:x2] = read_rgb(FIXTURES / f'source_{frame_id}.png')
    scanner = vision.LayoutScanner(target_detector=lambda _: pytest.fail('no model call'))
    for key in ('rvec', 'tvec', 'rotation', 'pivot_reference'):
        setattr(scanner, key, np.asarray(row['pose'][key], float))
    scanner.icon_anchors = {int(i): icon for i, icon in row['known'].items()}
    scanner._anchor_source = dict(source_frame_id=frame_id,
        source_frame_time=row['source_frame_time'], source_map_revision=0)
    return scanner, image, row


def test_actual_source833_supplies_second_D_glyph_under_original_pose_gates(monkeypatch):
    current_mask = vision._icon_mask
    old, image, row = actual_scanner(833)
    targets_before = deepcopy(row['targets'])
    with monkeypatch.context() as patch:
        patch.setattr(vision, '_icon_mask', lambda rgb, name:
            old_orange_mask(rgb) if name == 'orange_triple_eye' else current_mask(rgb, name))
        old._refine_centres(image, row['targets'])
        old_assigned, _ = old._associated_targets(row['targets'], old.visible())
    assert old.refine_diagnostic['accepted_faces'] == {'F': 7, 'D': 1}
    assert 31 not in old_assigned
    new, image, row = actual_scanner(833)
    before = new.rotation.copy()
    new._refine_centres(image, row['targets'])
    assigned, _ = new._associated_targets(row['targets'], new.visible())
    assert new.refine_diagnostic['renewed']
    assert new.refine_diagnostic['accepted_faces'] == {'F': 7, 'D': 2}
    assert 35 in new.refine_diagnostic['candidate_cell_indices']
    assert 29 not in new.refine_diagnostic['candidate_cell_indices']  # Actual weak head mask.
    assert new.refine_diagnostic['median_after'] <= 5
    assert vision._angle(new.rotation, before) <= 4
    assert assigned[31]['kind'] == 'singularity'
    proof = assigned[31]['association_evidence']
    assert proof['target_face_glyph_count'] == 2
    assert proof['anchor_uncertainty']['ready']
    assert proof['error'] < .30 and proof['margin'] > .12
    assert proof['source_frame_id'] == 833
    assert proof['source_frame_time'] == row['source_frame_time']
    assert new.glyph_anchor_at == row['source_frame_time']
    assert row['targets'] == targets_before  # Weak masks remain intact.
    assert not any(new.evidence)  # This is measured pose support, not label votes.


@pytest.mark.parametrize('frame_id', [846, 859])
def test_one_additional_glyph_does_not_relax_insufficient_pose_support(frame_id):
    scanner, image, row = actual_scanner(frame_id)
    before = scanner.rotation.copy()
    scanner._refine_centres(image, row['targets'])
    assert scanner.refine_diagnostic['candidate_cell_indices'] == [29, 35]
    assert scanner.refine_diagnostic['confirmed'] == 1
    assert scanner.refine_diagnostic['reason'] == 'insufficient_centres'
    assert not scanner.refine_diagnostic['renewed']
    assert scanner.glyph_anchor_at is None
    np.testing.assert_array_equal(scanner.rotation, before)


@pytest.mark.parametrize('rotation', range(4))
def test_actual_single_eye_cannot_become_expected_triple_eye(rotation):
    patch = np.ascontiguousarray(np.rot90(read_rgb(NATIVE / 'icon_red_single_eye.png'), rotation))
    assert vision.classify_icon(patch)['icon_id'] == 'red_single_eye'
    # The shared foreground is not permission to replace independent shape.
    assert inner_mass(vision._icon_mask(patch, 'orange_triple_eye')) > 50
    item = dict(quad=np.float32(((0, 0), (95, 0), (95, 95), (0, 95))),
                centre=np.array((47.5, 47.5)))
    assert vision._localized_known_glyph(patch, item, 'orange_triple_eye') is None


@pytest.mark.parametrize('hue', [0, 15, 170])
def test_filled_warm_wall_never_proves_expected_triple_eye(hue):
    image = np.zeros((96, 96, 3), np.uint8)
    image[20:76, 20:76] = cv2.cvtColor(np.uint8([[[hue, 220, 220]]]), cv2.COLOR_HSV2RGB)[0, 0]
    assert vision.classify_icon(image)['icon_id'] is None
    item = dict(quad=np.float32(((0, 0), (95, 0), (95, 95), (0, 95))),
                centre=np.array((47.5, 47.5)))
    assert vision._localized_known_glyph(image, item, 'orange_triple_eye') is None


def test_other_six_colour_masks_remain_exactly_unchanged():
    rng = np.random.default_rng(18)
    image = rng.integers(0, 256, (96, 96, 3), dtype=np.uint8)
    h, s, v = cv2.split(cv2.cvtColor(image, cv2.COLOR_RGB2HSV))
    expected = {
        'white_diamond': (s < 62) & (v > 170),
        'blue_scales': (h >= 100) & (h <= 125) & (s > 100) & (v > 135),
        'green_burst': (h >= 40) & (h <= 91) & (s > 130) & (v > 135),
        'purple_ring': (h >= 126) & (h <= 148) & (s > 120) & (v > 130),
        'yellow_hex': (h >= 21) & (h <= 39) & (s > 110) & (v > 160),
        'red_single_eye': ((h <= 6) | (h >= 168)) & (s > 140) & (v > 140),
    }
    for name, mask in expected.items():
        np.testing.assert_array_equal(vision._icon_mask(image, name), mask)
