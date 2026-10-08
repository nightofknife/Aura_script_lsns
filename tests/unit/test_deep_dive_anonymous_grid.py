"""Pixel-only synthetic grid contracts; no pose/source/target truth inputs."""
import inspect

import cv2
import numpy as np
import pytest

from research.deep_dive_anonymous_grid import propose_anonymous_grid


def draw_grid(image, x=60, y=60, n=3, boundary=True):
    for row in range(n):
        for col in range(n):
            a, b = x+col*60, y+row*60
            cv2.rectangle(image, (a, b), (a+50, b+50), (15, 15, 15), -1)
            cv2.circle(image, (a+25, b+25), 9, (20, 240, 30), -1)
    if boundary:
        cv2.rectangle(image, (x-5, y-5), (x+(n-1)*60+55, y+(n-1)*60+55), (220, 220, 220), 2)
    return image


def image(boundary=True, n=3):
    return draw_grid(np.full((400, 400, 3), 110, np.uint8), n=n, boundary=boundary)


@pytest.fixture(autouse=True)
def single_cv_thread():
    previous = cv2.getNumThreads()
    cv2.setNumThreads(1)
    yield
    cv2.setNumThreads(previous)


def test_actual_bordered_nine_tiles_produce_only_anonymous_candidate():
    outcome = propose_anonymous_grid(image(), (0, 0, 400, 400))
    assert outcome['status'] == 'anonymous_geometry_candidate'
    candidate = next(c for c in outcome['candidates'] if c['complete_anonymous_geometry'])
    assert len(candidate['quads']) == len(candidate['centres']) == 9
    assert candidate['complete_outer_boundary_support']['complete']
    assert min(candidate['complete_outer_boundary_support']['measured_side_support']) >= .7
    assert not outcome['face_bound'] and not outcome['body_bound']
    assert not outcome['source_bound'] and not outcome['targets_ready'] and not outcome['input_ready']
    assert not candidate['orientation_bound']


def test_nine_repeated_glyphs_without_outer_boundary_cannot_be_complete():
    outcome = propose_anonymous_grid(image(boundary=False), (0, 0, 400, 400))
    assert outcome['status'] == 'partial_candidates'
    assert not any(c['complete_anonymous_geometry'] for c in outcome['candidates'])


def test_centres_only_never_invent_cell_quads_or_boundary():
    outcome = propose_anonymous_grid(image(), (0, 0, 400, 400), method='glyph_centres')
    assert outcome['status'] == 'partial_candidates'
    assert all(c['quads'] is None for c in outcome['candidates'])
    assert all('tile_boundaries_unobserved' in c['reasons'] for c in outcome['candidates'])


def test_two_independent_complete_grids_remain_ambiguous():
    rgb = np.full((800, 400, 3), 110, np.uint8)
    draw_grid(rgb); draw_grid(rgb, y=420)
    outcome = propose_anonymous_grid(rgb, (0, 0, 400, 800))
    assert outcome['status'] == 'ambiguous' and outcome['ambiguous']
    capped = propose_anonymous_grid(rgb, (0, 0, 400, 800), max_candidates=1)
    assert capped['status'] == 'ambiguous' and capped['candidate_search_truncated']


def test_subgrid_in_repeated_four_by_four_does_not_claim_complete_face():
    outcome = propose_anonymous_grid(image(n=4), (0, 0, 400, 400))
    assert not any(c['complete_anonymous_geometry'] for c in outcome['candidates'])
    assert any('repeated_grid_continues_outside_3x3' in c['reasons'] for c in outcome['candidates'])


def test_clipped_roi_boundary_is_not_outer_boundary_support():
    outcome = propose_anonymous_grid(image(), (55, 55, 236, 236))
    assert not any(c['complete_anonymous_geometry'] for c in outcome['candidates'])


@pytest.mark.parametrize('method', ['dark_tiles', 'glyph_centres'])
def test_blank_and_wall_have_no_grid(method):
    for rgb in (np.zeros((400, 400, 3), np.uint8), np.full((400, 400, 3), (180, 30, 20), np.uint8)):
        assert not propose_anonymous_grid(rgb, (0, 0, 400, 400), method=method)['candidates']


def test_rotated_rgb_can_propose_without_any_pose_input():
    matrix = cv2.getRotationMatrix2D((200, 200), 17, 1)
    rgb = cv2.warpAffine(image(), matrix, (400, 400), borderValue=(110, 110, 110))
    outcome = propose_anonymous_grid(rgb, (0, 0, 400, 400))
    assert any(c['complete_anonymous_geometry'] for c in outcome['candidates'])
    assert not any(k in inspect.signature(propose_anonymous_grid).parameters for k in ('rotation', 'translation', 'face', 'quads'))


@pytest.mark.parametrize('roi', [None, (1, 2), (0, 0, 401, 400), (0, 0, True, 400)])
def test_invalid_roi_fails_closed(roi):
    assert propose_anonymous_grid(image(), roi)['reasons'] == ['invalid_roi']


def test_invalid_method_and_float_rgb_are_rejected():
    assert propose_anonymous_grid(image(), (0, 0, 400, 400), method='pose')['status'] == 'rejected'
    assert propose_anonymous_grid(image().astype(float), (0, 0, 400, 400))['status'] == 'rejected'
