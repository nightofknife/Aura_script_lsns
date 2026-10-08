import hashlib
from copy import deepcopy

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions._deep_dive_four_view_geometry import (
    FourViewGeometry, canonical_cell, project_points,
)
from plans.resonance_pc.src.actions._deep_dive_four_view_epoch_registration import (
    EpochViewRegistration, _fit, epoch_orientation_feedback,
)


def source(index, **changes):
    result = dict(generation_source='atomic_wgc', capture_backend='wgc',
                  session_id=7, map_revision=0, generation=index,
                  frame_id=index, frame_time=100+index*.3)
    result.update(changes)
    return result


@pytest.fixture()
def standard():
    rgb = np.zeros((720, 1280, 3), np.uint8)
    random = np.random.default_rng(12005)
    cells = []
    for panel in FourViewGeometry().panels:
        if panel['view'] != 1:
            continue
        for original in panel['cells']:
            identity = canonical_cell(panel['panel'], original['row'], original['col'])
            quad = np.asarray(original['quad'], np.float32)
            pattern = random.integers(0, 256, (96, 96, 3), dtype=np.uint8)
            pattern = cv2.GaussianBlur(pattern, (3, 3), 0)
            for _ in range(18):
                x, y = random.integers(9, 87, 2)
                cv2.circle(pattern, (int(x), int(y)), int(random.integers(2, 7)),
                           tuple(int(v) for v in random.integers(0, 256, 3)), 1)
            matrix = cv2.getPerspectiveTransform(np.float32([[0, 0], [95, 0], [95, 95], [0, 95]]), quad)
            image = cv2.warpPerspective(pattern, matrix, (1280, 720))
            mask = np.zeros((720, 1280), np.uint8)
            cv2.fillConvexPoly(mask, np.rint(quad).astype(np.int32), 1)
            rgb[mask > 0] = image[mask > 0]
            cells.append(dict(identity, panel=panel['panel'], local_row=original['row'],
                              local_col=original['col'], quad=quad.tolist(),
                              expected_icon='forbidden_teacher', normal_projection=[999, 999]))
    geometry = dict(proposal_accepted=True, expected_view=1, cells=cells,
                    source_rgb_sha256=hashlib.sha256(rgb.tobytes()).hexdigest())
    return rgb, geometry


def registration(standard):
    rgb, geometry = standard
    return EpochViewRegistration(rgb, geometry, 'epoch-a', source(3),
                                 stable_sources=[source(1), source(2), source(3)])


def test_actual_same_epoch_per_face_homographies_do_not_read_teachers(standard):
    rgb, original = standard
    reg = registration(standard)
    # The same visible scene in a new capture establishes zero local motion;
    # this is synthetic unit-test source evidence, not a live-game claim.
    result = reg.locate(rgb.copy(), source(4))
    assert result['proposal_accepted']
    assert result['feedback']['arrival_candidate']
    assert len(result['cells']) == 18
    assert {p['reference_panel'] for p in result['results']} == {'view1left', 'view1right'}
    for panel in result['results']:
        assert panel['actual_inliers'] >= 12
        assert len(panel['reference_cells']) >= 3
        assert min(panel['reference_span_px']) >= 100
    for cell in result['cells']:
        assert 'expected_icon' not in cell
        assert cell['normal_projection'] != [999, 999]
        assert cell['normal_evidence']['source_rgb_sha256'] == result['source_rgb_sha256']
    assert not result['identity_from_geometry']
    assert not result['formal_pose']


def test_new_actual_warp_projects_cells_and_measures_relative_shape(standard):
    rgb, _ = standard
    reg = registration(standard)
    h = np.array([[1., 0, 2.], [.015, 1., -9.6], [0, 0, 1.]])
    image = cv2.warpPerspective(rgb, h, (1280, 720))
    result = reg.locate(image, source(4))
    assert result['proposal_accepted']
    assert result['feedback']['valid_diagnostic']
    for cell in result['cells']:
        baseline = next(c for c in standard[1]['cells'] if c['slot'] == cell['slot'])
        expected = project_points(baseline['quad'], h)
        assert np.max(np.linalg.norm(np.asarray(cell['quad'])-expected, axis=1)) < 2.0


@pytest.mark.parametrize('changes, reason', [
    ({'session_id': 9}, 'capture_session_or_map_changed'),
    ({'map_revision': 1}, 'capture_session_or_map_changed'),
    ({'generation': 3}, 'stale_or_repeated_epoch_source'),
    ({'frame_id': 3}, 'stale_or_repeated_epoch_source'),
    ({'frame_time': 100.9}, 'stale_or_repeated_epoch_source'),
    ({'scan_epoch': 'other'}, 'epoch_changed_requires_new_standard'),
])
def test_source_changes_revoke_registration(standard, changes, reason):
    result = registration(standard).locate(standard[0], source(4, **changes))
    assert not result['proposal_accepted']
    assert result['cells'] == []
    assert result['reason'] == reason


def test_repeated_processed_source_and_explicit_epoch_rejected(standard):
    reg = registration(standard)
    assert reg.locate(standard[0], source(4))['proposal_accepted']
    assert reg.locate(standard[0], source(4))['reason'] == 'stale_or_repeated_epoch_source'
    assert reg.locate(standard[0], source(5), scan_epoch='other')['reason'] == 'epoch_changed_requires_new_standard'


def test_blank_current_image_returns_no_reference_cells(standard):
    result = registration(standard).locate(np.zeros((720, 1280, 3), np.uint8), source(4))
    assert not result['proposal_accepted']
    assert result['cells'] == []
    assert not result['feedback']['valid_diagnostic']


def test_missing_reference_face_cannot_be_borrowed(standard):
    rgb, geometry = standard
    geometry = deepcopy(geometry)
    geometry['cells'] = [c for c in geometry['cells'] if c['panel'] == 'view1left']
    with pytest.raises(ValueError, match='nine_actual_cells'):
        EpochViewRegistration(rgb, geometry, 'epoch-a', source(3), provisional=True)


def test_mismatched_rgb_and_three_source_attestation_rejected(standard):
    rgb, geometry = standard
    with pytest.raises(ValueError, match='RGB_source_disagrees'):
        EpochViewRegistration(np.zeros_like(rgb), geometry, 'epoch-a', source(3), provisional=True)
    with pytest.raises(ValueError, match='standard_stable_sources_disagree'):
        EpochViewRegistration(rgb, geometry, 'epoch-a', source(3),
                              stable_sources=[source(1), source(2, session_id=8), source(3)])


def test_single_tile_points_cannot_pass_epoch_support_gate():
    points = [[400+i%4*5, 300+i//4*5] for i in range(20)]
    result = _fit(points, points, ['one-cell']*20)
    assert not result['proposal_accepted']
    assert 'fewer_than_3_reference_cells' in result['reason']
    assert 'actual_support_span_below_100x100' in result['reason']


def test_epoch_main_position_feedback_has_sign_when_shape_is_even():
    def measure(delta):
        h = np.eye(3);h[1, 1] = 1.04
        pairs = [dict(reference_point=[x, y], inlier=True) for x in (500, 650, 800) for y in (170, 300, 430)]
        result = dict(expected_view=2, results=[dict(reference_panel='view2main', proposal_accepted=True,
            homography=h.tolist(), matched_pairs=pairs, cells=[dict(center=[650, 300+delta]) for _ in range(9)],
            reference_panel_center_px=[650, 300], reference_panel_span_y_px=360)])
        return epoch_orientation_feedback(result)
    before, after = measure(12), measure(-12)
    assert before['signed_error_deg'] > 0
    assert after['signed_error_deg'] < 0
    assert before['metrics']['panels']['view2main']['vertical_horizontal_shape_deg'] > 0
    assert after['metrics']['panels']['view2main']['vertical_horizontal_shape_deg'] > 0


def test_provisional_reference_is_explicit_and_never_independent_evidence(standard):
    rgb, geometry = standard
    with pytest.raises(ValueError, match='formal_reference_requires_three_stable_sources'):
        EpochViewRegistration(rgb, geometry, 'epoch-a', source(3))
    reg = EpochViewRegistration(rgb, geometry, 'epoch-a', source(3), provisional=True,
        exclusion_targets=[dict(box=[600, 110, 50, 40])])
    result = reg.locate(rgb, source(4), exclusion_targets=[dict(box=[602, 112, 50, 40])])
    assert result['proposal_accepted']
    assert result['provisional_reference']
    assert not result['formal_reference']
    assert not result['independent_view_evidence']
    assert result['reference_excluded_rectangles']
    assert result['query_excluded_rectangles']


def test_excluding_all_actual_features_does_not_lower_coverage_gate(standard):
    rgb, geometry = standard
    with pytest.raises(ValueError, match='standard_actual_feature_coverage_insufficient'):
        EpochViewRegistration(rgb, geometry, 'epoch-a', source(3), provisional=True,
                              exclusion_targets=[dict(box=[320, 90, 640, 530])])
    result = registration(standard).locate(rgb, source(4), exclusion_targets=[dict(box=[320, 90, 640, 530])])
    assert not result['proposal_accepted']
    assert result['cells'] == []
