import cv2
import numpy as np
import pytest

from research.deep_dive_glyph_lattice import COORDS, propose_glyph_lattices


def points():
    h = np.array(((95., 22., 340.), (-18., 103., 210.), (.008, -.01, 1.)))
    return cv2.perspectiveTransform(COORDS.astype(np.float64)[None], h)[0]


@pytest.mark.parametrize('missing', [(4,), (0, 8), (1, 4)])
def test_missing_one_or_two_are_prediction_only(missing):
    observed = np.delete(points(), missing, axis=0)
    result = propose_glyph_lattices(observed)
    assert result['status'] == 'proposal_only'
    candidate = next(c for c in result['candidates'] if c['observed_support'] == len(observed))
    assert candidate['missing_sites'] == len(missing)
    assert len({s['observation_index'] for s in candidate['sites'] if not s['predicted_only']}) == len(observed)
    assert all(s['observed_centre'] is None and s['observation_index'] is None
               for s in candidate['sites'] if s['predicted_only'])
    assert candidate['observed_rows'] == candidate['observed_columns'] == [0, 1, 2]
    assert result['missing_observations_never_used_to_fit'] is True
    assert result['bank_ready'] is result['targets_ready'] is False


def test_repeated_classes_do_not_change_geometry():
    a = propose_glyph_lattices(points())
    b = propose_glyph_lattices(points(), ['purple_ring']*9)
    assert [c['observed_support'] for c in a['candidates']] == [c['observed_support'] for c in b['candidates']]
    assert b['candidates'][0]['observed_support'] == 9
    assert all(s['label'] == 'purple_ring' for s in b['candidates'][0]['sites'])


def test_extension_and_shifted_subgrid_remain_diagnosed():
    observed = np.array([(330.+90*c, 200.+95*r) for r in range(3) for c in range(4)])
    result = propose_glyph_lattices(observed, max_candidates=32)
    full = [c for c in result['candidates'] if c['observed_support'] == 9]
    assert len(full) >= 2
    assert result['spatial_ambiguity']
    assert all(c['outside_extension_support'] and c['repeated_grid_continues_outside_3x3'] for c in full)
    assert not result['outer_boundary_measured']


def test_disjoint_wrong_sheets_cannot_supply_seven_consistent_centres():
    a = points()[:4]
    b = points()[4:7]+[550., 300.]
    result = propose_glyph_lattices(np.vstack((a, b)))
    assert result['status'] == 'rejected'
    assert result['physical_sheet_proven'] is False


def test_aligned_wrong_sheet_is_not_claimed_identifiable_from_points():
    result = propose_glyph_lattices(points())
    assert result['candidates']
    assert all(c['physical_sheet_proven'] is False for c in result['candidates'])
    assert result['face_bound'] is result['body_bound'] is False


def test_rotations_and_reflections_are_explicit_identity_ambiguity():
    result = propose_glyph_lattices(points())
    candidate = result['candidates'][0]
    aliases = candidate['orientation_aliases']
    assert len({tuple(a['observation_indices']) for a in aliases}) == 8
    assert result['ambiguous'] and not result['orientation_bound']
    assert all(a['identity_proven'] is False for a in aliases)


def test_budget_truncation_never_implies_no_alternatives():
    result = propose_glyph_lattices(points(), max_seed_tests=1)
    assert result['candidate_search_truncated'] and result['spatial_ambiguity']
    assert result['reason'] == 'candidate_budget_hides_other_hypotheses'


def test_output_truncation_remains_ambiguous():
    observed = np.array([(330.+90*c, 200.+95*r) for r in range(3) for c in range(4)])
    result = propose_glyph_lattices(observed, max_candidates=1)
    assert len(result['candidates']) == 1
    assert result['output_candidates_truncated'] and result['candidate_search_truncated']
    assert result['spatial_ambiguity'] and result['ambiguous']


def test_six_points_and_duplicate_observations_do_not_fill_missing_cells():
    assert propose_glyph_lattices(points()[:6])['status'] == 'rejected'
    assert propose_glyph_lattices(np.vstack((points()[:6], points()[0])))['reason'] == 'near_duplicate_actual_centres'
