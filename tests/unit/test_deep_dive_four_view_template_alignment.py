import numpy as np
import pytest

from research.deep_dive_four_view_template_alignment import fit_pairs, camera_branches, _rgb


def distributed_pairs():
    points = np.array([[x, y] for y in (100., 150., 250., 300.)
                       for x in (100., 150., 250., 300.)], np.float32)
    cells = [f'{int(y >= 200)},{int(x >= 200)}' for x, y in points]
    return points, cells


def test_distributed_actual_pairs_form_geometry_proposal_only():
    reference, cells = distributed_pairs()
    result = fit_pairs(reference, reference + (11., -8.), cells)
    assert result['proposal_accepted']
    assert result['coverage']['inliers'] == 16
    assert len(result['coverage']['cells']) == 4
    assert result['coverage']['median_inlier_residual_px'] < .001
    assert not any(result[key] for key in ('phase_identity', 'targets_ready',
                                          'full_cube', 'formal_pose', 'formal_recognition'))
    assert result['decomposition']['selected_branch'] is None


def test_many_perfect_matches_on_one_cell_are_rejected():
    reference, _ = distributed_pairs()
    result = fit_pairs(reference, reference + (11., -8.), ['1,1'] * len(reference))
    assert not result['proposal_accepted']
    assert 'fewer_than_3_reference_cells' in result['reason']
    assert result['coverage']['inliers'] == 16


def test_cell_diversity_cannot_replace_actual_spatial_span():
    reference, cells = distributed_pairs()
    reference[:, 1] *= .1
    result = fit_pairs(reference, reference + (11., -8.), cells)
    assert not result['proposal_accepted']
    assert 'actual_support_span_below_100x100' in result['reason']


def test_pair_identity_and_finiteness_are_required():
    reference, cells = distributed_pairs()
    with pytest.raises(ValueError, match='pair_lengths_disagree'):
        fit_pairs(reference, reference[:-1], cells)
    reference[0, 0] = np.nan
    with pytest.raises(ValueError, match='nonfinite_actual_pairs'):
        fit_pairs(reference, reference, cells)


def test_every_decomposition_branch_is_retained_without_selection():
    h = np.array([[1., 0., 30.], [0., 1., -5.], [0., 0., 1.]])
    result = camera_branches(h)
    assert len(result['branches']) == 4
    assert result['ambiguous']
    assert result['selected_branch'] is None
    assert not result['physical_branch_verified']
    assert not result['formal_pose']


def test_rgb_rejects_non_client_images():
    with pytest.raises(ValueError, match='requires_1280x720_uint8_rgb'):
        _rgb(np.zeros((100, 100, 3), np.uint8))


def test_position_window_is_applied_before_descriptor_nearest_two():
    import cv2
    from research.deep_dive_four_view_template_alignment import position_search_mask
    reference_descriptor = np.float32([[0., 0.]])
    query_descriptors = np.float32([[0., 0.], [.001, 0.], [.1, 0.], [2., 0.]])
    mask = position_search_mask([[100,100]], [[900,900],[901,900],[101,101],[105,101]])
    best, competitor = cv2.BFMatcher(cv2.NORM_L2).knnMatch(
        reference_descriptor, query_descriptors, k=2, mask=mask)[0]
    assert (best.trainIdx, competitor.trainIdx) == (2, 3)
    assert best.distance < .70 * competitor.distance
    assert np.count_nonzero(mask) == 2
