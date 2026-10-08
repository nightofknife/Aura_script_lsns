"""Actual current-RGB recovery without changing glyph or pose acceptance gates."""
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_layout_vision as vision


FIXTURES = Path(__file__).parents[1] / 'fixtures/deep_dive_anchor/matching_centres_15'
RECORDS = {row['frame_id']: row for row in json.loads(
    (FIXTURES / 'metadata.json').read_text(encoding='utf-8'))}


def actual_source(frame_id):
    row = RECORDS[frame_id]
    crop = cv2.cvtColor(cv2.imread(str(FIXTURES / f'{frame_id}.png')), cv2.COLOR_BGR2RGB)
    image = np.zeros((720, 1280, 3), np.uint8)
    x1, y1, x2, y2 = row['fixture_roi']
    image[y1:y2, x1:x2] = crop
    scanner = vision.LayoutScanner(target_detector=lambda _: pytest.fail('no model call'))
    for key in ('rvec', 'tvec', 'rotation', 'pivot_reference'):
        setattr(scanner, key, np.asarray(row['pose'][key], float))
    # Source-confirmed labels only: final atlas U00 was not yet known at247.
    scanner.icon_anchors = {int(i): icon for i, icon in row['known'].items()}
    scanner._anchor_source = dict(source_frame_id=frame_id,
        source_frame_time=row['source_frame_time'], source_map_revision=0)
    return scanner, image, row


@pytest.mark.parametrize('frame_id', [251, 257, 272])
def test_matching_glyph_components_restore_actual_seven_point_fit(frame_id):
    scanner, image, row = actual_source(frame_id)
    scanner._refine_centres_once(image, row['targets'])
    original = scanner.refine_diagnostic.copy()
    assert original['reason'] == 'seven_inliers_missing'
    assert original['candidate_cell_indices'] == row['original_diagnostic']['candidate_cell_indices']
    assert original['confirmed_cell_indices'] == row['original_diagnostic']['confirmed_cell_indices']
    scanner._refine_centres(image, row['targets'])
    result = scanner.refine_diagnostic
    assert result['renewed']
    assert result['reason'] == 'seven_centres'
    assert result['median_after'] <= 5
    assert result['max_after'] < 9
    assert result['matching_localization_retry']['attempts'] == 1
    assert scanner.glyph_anchor_at == row['source_frame_time']
    assert scanner.glyph_anchor_frame_id == frame_id
    assert scanner.glyph_anchor_map_revision == 0
    assert scanner._same_frame_entity_pose()
    assert scanner.icon_anchors == {int(i): icon for i, icon in row['known'].items()}
    assert not any(scanner.evidence)  # Pose measurement never adds label/entity votes.


def test_six_known_source_keeps_seven_gate_and_does_not_expand_four_consensus():
    scanner, image, row = actual_source(247)
    before = scanner.rotation.copy()
    scanner._refine_centres(image, row['targets'])
    assert scanner.refine_diagnostic['candidates'] == 7
    assert scanner.refine_diagnostic['confirmed'] == 6
    assert scanner.refine_diagnostic['reason'] == 'seven_inliers_missing'
    assert not scanner.refine_diagnostic['renewed']
    assert scanner.glyph_anchor_at is None
    np.testing.assert_array_equal(scanner.rotation, before)


def test_missing_localization_retains_original_centres_and_cannot_renew(monkeypatch):
    scanner, image, row = actual_source(257)
    calls = []
    monkeypatch.setattr(vision, '_localized_known_glyph',
        lambda rgb, item, expected: calls.append((rgb, item['index'], expected)) or None)
    before = scanner.rotation.copy()
    scanner._refine_centres(image, row['targets'])
    assert len(calls) == 7
    assert all(rgb is image for rgb, _, _ in calls)
    assert scanner.refine_diagnostic['candidates'] == 8
    assert scanner.refine_diagnostic['confirmed'] == 7
    assert not scanner.refine_diagnostic['renewed']
    assert scanner.glyph_anchor_at is None
    np.testing.assert_array_equal(scanner.rotation, before)


@pytest.mark.parametrize('index, expected', [(0, 'blue_scales'), (0, 'purple_ring'),
    (2, 'red_single_eye'), (2, 'purple_ring'), (6, 'red_single_eye'),
    (6, 'blue_scales'), (4, 'red_single_eye'), (4, 'blue_scales'), (4, 'purple_ring')])
def test_wrong_glyph_and_actual_player_never_supply_matching_component(index, expected):
    scanner, image, _ = actual_source(257)
    item = next(item for item in scanner.visible() if item['index'] == index)
    assert vision._localized_known_glyph(image, item, expected) is None


def test_actual_target_exclusions_apply_to_retry_as_well(monkeypatch):
    scanner, image, row = actual_source(257)
    visible = scanner.visible()
    targets = list(row['targets']) + [dict(point=item['centre'].tolist())
        for item in visible if item['index'] < 9]
    monkeypatch.setattr(vision, '_localized_known_glyph',
        lambda *args: pytest.fail('masked glyph must never localize'))
    scanner._refine_centres(image, targets)
    assert scanner.refine_diagnostic['candidates'] == 0
    assert not scanner.refine_diagnostic['renewed']
    assert 'matching_localization_retry' not in scanner.refine_diagnostic


def test_no_glyph_image_does_not_retry_or_renew(monkeypatch):
    scanner, image, row = actual_source(257)
    scanner._refine_centres(np.zeros_like(image), row['targets'])
    assert scanner.refine_diagnostic['candidates'] == 0
    assert not scanner.refine_diagnostic['renewed']
    assert 'matching_localization_retry' not in scanner.refine_diagnostic


@pytest.mark.parametrize('reason, renewed, changed, expected_calls', [
    ('seven_centres', True, False, 1), ('confirmed_residual_gate', False, False, 1),
    ('seven_inliers_missing', False, True, 1), ('seven_inliers_missing', False, False, 2)])
def test_retry_is_bounded_and_only_after_unchanged_rejected_pose(monkeypatch,
        reason, renewed, changed, expected_calls):
    scanner, image, row = actual_source(257)
    calls = []
    def once(rgb, targets, *, localized_matching=False):
        calls.append(localized_matching)
        assert rgb is image
        assert targets is row['targets']
        scanner.refine_diagnostic = dict(reason=reason, renewed=renewed, candidates=8, confirmed=7)
        if changed:
            scanner.tvec[0, 0] += .01
    monkeypatch.setattr(scanner, '_refine_centres_once', once)
    scanner._refine_centres(image, row['targets'])
    assert len(calls) == expected_calls
    assert calls[0] is False
    if expected_calls == 2:
        assert calls[1] is True
        assert scanner.refine_diagnostic['matching_localization_retry']['attempts'] == 1
