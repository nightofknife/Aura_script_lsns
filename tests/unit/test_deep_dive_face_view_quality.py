"""Pure projected-quad contracts, not game/model/recognition validation."""
from copy import deepcopy

import cv2
import numpy as np
import pytest

from research.deep_dive_face_view_quality import evaluate_face_view_quality, official_ui_mask


SOURCE = dict(session_id='test', generation=5, frame_id=3, frame_time=1., map_revision=0)


def rectangle(x, y, w=50., h=50.):
    return np.array([[x, y], [x+w, y], [x+w, y+h], [x, y+h]])


def quads():
    return np.array([rectangle(330+col*65, 110+row*65) for row in range(3) for col in range(3)])


def evaluate(q=None, **kwargs):
    defaults = dict(face='U', source=deepcopy(SOURCE), source_valid=True,
        current_support=dict(valid=True, face='U', source=deepcopy(SOURCE)),
        settle_evidence=dict(settled=True, source=deepcopy(SOURCE)))
    defaults.update(kwargs)
    return evaluate_face_view_quality(quads() if q is None else q, **defaults)


@pytest.fixture(autouse=True)
def cv_single_thread():
    original = cv2.getNumThreads()
    cv2.setNumThreads(1)
    yield
    cv2.setNumThreads(original)


def test_exact_official_mask_matches_actual_glyph_exclusion():
    from plans.resonance_pc.src.actions._deep_dive_layout_vision import _ui_mask
    np.testing.assert_array_equal(official_ui_mask(), _ui_mask())


def test_good_quads_caller_bound_can_only_be_geometry_shot_ready():
    outcome = evaluate()
    assert outcome['geometry_quads_ok'] and outcome['geometry_shot_ready']
    assert not outcome['targets_ready'] and not outcome['action_ready']
    assert not outcome['complete_recognition'] and not outcome['formal_acceptance']
    assert outcome['freshness_evaluated'] is False
    assert outcome['settle_evaluated'] is False


def test_saved_quad_quality_does_not_invent_freshness_or_support():
    outcome = evaluate_face_view_quality(quads(), face='U')
    assert outcome['geometry_quads_ok'] and not outcome['geometry_shot_ready']
    assert outcome['caller_contract'] == dict(source_bound=False, support_bound=False, settle_bound=False)


@pytest.mark.parametrize('change', [dict(source_valid=False), dict(source_valid=1),
    dict(current_support=None), dict(settle_evidence=None), dict(source={}),
    dict(current_support=dict(valid=True, face='R', source=SOURCE)),
    dict(current_support=dict(valid=1, face='U', source=SOURCE)),
    dict(settle_evidence=dict(settled=1, source=SOURCE))])
def test_missing_or_invalid_caller_assertion_does_not_authorize_shot(change):
    assert not evaluate(**change)['geometry_shot_ready']


@pytest.mark.parametrize('field,value', [('session_id', 'other'), ('generation', 6),
    ('frame_id', 4), ('frame_time', 2.), ('map_revision', 1)])
def test_support_and_settle_cannot_borrow_other_source(field, value):
    other = dict(SOURCE, **{field: value})
    assert not evaluate(current_support=dict(valid=True, face='U', source=other))['geometry_shot_ready']
    assert not evaluate(settle_evidence=dict(settled=True, source=other))['geometry_shot_ready']


def test_source_bool_revision_and_nan_time_are_invalid_even_caller_true():
    assert not evaluate(source=dict(SOURCE, map_revision=True))['geometry_shot_ready']
    assert not evaluate(source=dict(SOURCE, frame_time=float('nan')))['geometry_shot_ready']


def test_exact_hud_overlap_blocks_even_inside_original_roi():
    q = quads(); q[8] = rectangle(610, 495)
    outcome = evaluate(q)
    assert outcome['cells'][8]['within_roi']
    assert 'official_ui_overlap' in outcome['cells'][8]['reasons']
    assert not outcome['geometry_shot_ready']


def test_extra_margin_mask_is_separate_from_glyph_mask():
    q = quads(); q[8] = rectangle(550, 445)
    outcome = evaluate(q)
    cell = outcome['cells'][8]
    assert cell['exact_ui_clear'] and cell['exact_ui_overlap_pixels'] == 0
    assert not cell['navigation_margin_clear']
    assert cell['reasons'] == ['navigation_margin_overlap']
    assert outcome['exact_mask_quads_ok'] and not outcome['geometry_quads_ok']
    assert evaluate(q, safety_margin_px=0)['geometry_shot_ready']


def test_different_mask_cannot_silently_remove_official_hud():
    assert evaluate(ui_mask=np.ones((720, 1280), np.uint8)*255)['reasons'] == ['official_ui_mask_mismatch']


@pytest.mark.parametrize('quad,reason', [
    (rectangle(330, 110, 39.9, 39.9), 'area_too_small'),
    (rectangle(330, 110, 27, 70), 'edge_too_short'),
    (rectangle(330, 110, 30, 100), 'edge_ratio'),
    (rectangle(285, 110), 'outside_roi'),
    (np.array([[330, 110], [380, 160], [380, 110], [330, 160]]), 'nonconvex_quad'),
])
def test_size_roi_deformation_and_convexity_gates(quad, reason):
    q = quads(); q[0] = quad
    outcome = evaluate(q)
    assert reason in outcome['cells'][0]['reasons']
    assert not outcome['geometry_shot_ready']


def test_jacobian_is_evaluated_at_corners_not_only_average_edge_ratio():
    q = quads(); q[8] = np.array([[650, 150], [730, 150], [700, 230], [680, 230]])
    outcome = evaluate(q)
    assert outcome['cells'][8]['max_jacobian_condition'] > 3
    assert 'jacobian_condition' in outcome['cells'][8]['reasons']


def test_duplicate_or_flipped_quads_reject_global_geometry():
    q = quads(); q[8] = q[0]
    assert 'overlapping_quads' in evaluate(q)['reasons']
    q = quads(); q[8] = q[8][::-1]
    assert 'inconsistent_quad_orientation' in evaluate(q)['reasons']


@pytest.mark.parametrize('q', [np.zeros((8, 4, 2)), np.full((9, 4, 2), np.nan),
                              np.full((9, 4, 2), 1e200)])
def test_malformed_inputs_fail_closed(q):
    assert not evaluate(q)['geometry_shot_ready']


def test_returned_source_does_not_alias_caller():
    source = deepcopy(SOURCE)
    outcome = evaluate(source=source)
    outcome['source']['session_id'] = 'changed'
    assert source == SOURCE


@pytest.mark.parametrize('kwargs', [dict(min_area=float('nan')), dict(min_edge=True),
                                   dict(safety_margin_px=-1), dict(face='?')])
def test_invalid_parameters_fail_closed(kwargs):
    assert evaluate(**kwargs)['reasons'] == ['invalid_configuration']
