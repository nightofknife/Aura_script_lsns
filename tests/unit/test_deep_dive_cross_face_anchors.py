"""Pose support comes from a single captured image, including its side faces."""
from collections import Counter

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_layout_vision as vision


@pytest.fixture
def scanner(monkeypatch):
    scanner = vision.LayoutScanner()
    scanner.ready = True
    scanner.quality = .95
    scanner.pivot_reference = scanner.tvec.copy()
    scanner.identity_corrections = 3
    # Isolate grid geometry from the separate shape classifier. Pixel centres
    # still come from real, independently warped patches in the current image.
    def known_blue(crop):
        hsv = cv2.cvtColor(crop, cv2.COLOR_RGB2HSV)
        valid = (hsv[:, :, 0] >= 100) & (hsv[:, :, 0] <= 125)
        valid &= (hsv[:, :, 1] > 100) & (hsv[:, :, 2] > 135)
        return dict(icon_id='blue_scales' if valid.sum() > 100 else None,
                    confidence=.95)
    monkeypatch.setattr(vision, 'classify_icon', known_blue)
    monkeypatch.setattr(scanner, '_atlas_alignment', lambda *a, **k: True)
    monkeypatch.setattr(scanner, '_observe_cells', lambda *a, **k: None)
    monkeypatch.setattr(scanner, 'target_mask_observation', lambda: [])
    return scanner


def glyph_frame(scanner, indices):
    image = np.zeros((720, 1280, 3), np.uint8)
    patch = np.zeros((96, 96, 3), np.uint8)
    patch[30:66, 30:66] = (0, 0, 255)
    visible = {item['index']: item for item in scanner.visible()}
    corners = np.float32(((0, 0), (95, 0), (95, 95), (0, 95)))
    for index in indices:
        matrix = cv2.getPerspectiveTransform(corners, np.float32(visible[index]['quad']))
        image = np.maximum(image, cv2.warpPerspective(patch, matrix, (1280, 720)))
        scanner.icon_anchors[index] = 'blue_scales'
    return image


def test_known_side_faces_jointly_renew_from_current_capture(scanner, monkeypatch):
    # Three glyphs on U and three on F; each face alone has too few known glyphs.
    indices = [0, 2, 6, 18, 20, 24]
    image = glyph_frame(scanner, indices)
    monkeypatch.setattr(vision.time, 'monotonic', lambda: 100.5)
    result = scanner.semantic_view(image, 17, scanner.pose_snapshot(), targets=[],
                                   source_frame_time=100.)
    support = result['refine_diagnostic']
    assert result['tracking_ok']
    assert support['renewed']
    assert support['candidate_faces'] == {'U': 3, 'F': 3}
    assert support['confirmed_faces'] == {'U': 3, 'F': 3}
    assert support['accepted_faces'] == {'U': 3, 'F': 3}
    assert support['source_frame_id'] == 17
    assert support['source_map_revision'] == 3
    assert result['glyph_anchor_at'] == 100.
    assert result['glyph_anchor_age_sec'] == .5
    assert result['glyph_anchor_frame_id'] == 17
    assert result['glyph_anchor_map_revision'] == 3
    np.testing.assert_allclose(result['glyph_anchor_rotation'], scanner.rotation)
    assert scanner.glyph_anchor_reason.startswith('known_multi_face_')


def test_previous_faces_do_not_supply_missing_current_glyphs(scanner, monkeypatch):
    image = glyph_frame(scanner, [0, 2, 6, 18, 20, 24])
    monkeypatch.setattr(vision.time, 'monotonic', lambda: 100.)
    scanner.semantic_view(image, 17, scanner.pose_snapshot(), targets=[],
                          source_frame_time=100.)
    rotation = scanner.glyph_anchor_rotation.copy()
    later = glyph_frame(scanner, [18, 20])
    monkeypatch.setattr(vision.time, 'monotonic', lambda: 101.)
    result = scanner.semantic_view(later, 18, scanner.pose_snapshot(), targets=[],
                                   source_frame_time=101.)
    assert result['refine_diagnostic']['confirmed_faces'] == {'F': 2}
    assert not result['refine_diagnostic']['renewed']
    assert result['glyph_anchor_frame_id'] == 17
    assert result['glyph_anchor_at'] == 100.
    np.testing.assert_array_equal(result['glyph_anchor_rotation'], rotation)


def test_local_known_glyphs_cannot_renew_anchor(scanner, monkeypatch):
    image = glyph_frame(scanner, [0, 1, 3, 4])
    scanner.glyph_anchor_at = 100.
    scanner.glyph_anchor_rotation = scanner.rotation.copy()
    monkeypatch.setattr(vision.time, 'monotonic', lambda: 101.)
    result = scanner.semantic_view(image, 19, scanner.pose_snapshot(), targets=[],
                                   source_frame_time=101.)
    assert result['refine_diagnostic']['confirmed'] == 4
    assert result['refine_diagnostic']['reason'] == 'confirmed_support_too_local'
    assert not result['refine_diagnostic']['renewed']
    assert result['glyph_anchor_at'] == 100.


@pytest.mark.parametrize(('source_age', 'expected_ok', 'paused'),
                         [(2.5, True, True), (6.1, False, None)])
def test_processing_delay_never_freshens_capture(scanner, monkeypatch, source_age,
                                               expected_ok, paused):
    image = glyph_frame(scanner, [0, 2, 6, 18, 20, 24])
    monkeypatch.setattr(vision.time, 'monotonic', lambda: 110.)
    result = scanner.semantic_view(image, 23, scanner.pose_snapshot(), targets=[],
                                   source_frame_time=110.-source_age)
    assert result['tracking_ok'] is expected_ok
    assert result['glyph_anchor_age_sec'] == pytest.approx(source_age)
    assert result['glyph_anchor_at'] == 110.-source_age
    assert result['glyph_anchor_frame_id'] == 23
    if paused is not None:
        assert result['fusion_paused'] is paused
    else:
        assert result['reason'].startswith('glyph_anchor_support_lost:')


def test_fork_preserves_anchor_provenance_without_shared_pose(scanner, monkeypatch):
    image = glyph_frame(scanner, [0, 2, 6, 18, 20, 24])
    monkeypatch.setattr(vision.time, 'monotonic', lambda: 100.)
    scanner.semantic_view(image, 30, scanner.pose_snapshot(), targets=[],
                          source_frame_time=100.)
    other = scanner.fork_semantic()
    assert other.glyph_anchor_frame_id == 30
    assert other.glyph_anchor_map_revision == 3
    assert Counter(other.glyph_anchor_faces) == Counter({'U': 3, 'F': 3})
    other.glyph_anchor_rotation[0, 0] += .1
    assert other.glyph_anchor_rotation[0, 0] != scanner.glyph_anchor_rotation[0, 0]
