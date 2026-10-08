import numpy as np

from research.deep_dive_four_view_pose_feedback import orientation_feedback


def panel(face, view, h, *, hud=False):
    points = [(x, y) for y in (100., 150., 250., 300.) for x in (400., 450., 550., 600.)]
    pairs = []
    for index, point in enumerate(points):
        projected = h @ (*point, 1.)
        pairs.append(dict(inlier=True, residual_px=0., reference_point=list(point),
                          query_point=(projected[:2]/projected[2]).tolist(),
                          reference_cell=str(index//4)))
    return dict(view=view, reference_panel=face, homography=h.tolist(), matched_pairs=pairs,
                proposal_accepted=not hud,
                projection_failures=['2,2:overlaps_reset_hud'] if hud else [])


def alignment(*panels):
    return dict(query_feature_count=100, results=list(panels))


def test_main_residual_is_signed_and_ignores_translation_uniform_scale():
    for scale in (1., 1.2):
        h = np.array([[scale,0.,12.],[0.,scale,-8.],[0.,0.,1.]])
        result = orientation_feedback(alignment(panel('view2main',2,h)),expected_view=2)
        assert result['arrival_candidate']
        assert abs(result['signed_error_deg']) < 1e-6
    h = np.diag([1.,.96,1.])
    result = orientation_feedback(alignment(panel('view2main',2,h)),expected_view=2)
    assert result['signed_error_deg'] < 0
    assert result['direction_up_error_sign'] == -1
    assert not result['arrival_candidate']


def test_paired_opposite_tilts_resolve_scale_symmetry():
    left = np.array([[1.,0.,0.],[-.05,1.,0.],[0.,0.,1.]])
    right = np.array([[1.,0.,0.],[.05,1.,0.],[0.,0.,1.]])
    result = orientation_feedback(alignment(panel('view1left',1,left),panel('view1right',1,right)),expected_view=1)
    assert result['signed_error_deg'] < -2.
    assert not result['arrival_candidate']
    reverse = orientation_feedback(alignment(panel('view1left',1,right),panel('view1right',1,left)),expected_view=1)
    assert reverse['signed_error_deg'] > 2.


def test_hud_overlap_retains_diagnostic_but_cannot_arrive():
    result = orientation_feedback(alignment(panel('view4main',4,np.eye(3),hud=True)),expected_view=4)
    assert result['valid_diagnostic']
    assert not result['arrival_candidate']


def test_cross_view_and_blank_are_rejected():
    result = orientation_feedback(alignment(panel('view2main',2,np.eye(3))),expected_view=4)
    assert not result['valid_diagnostic']
    assert result['direction_up_error_sign'] == 'unknown'
    assert not orientation_feedback(dict(query_feature_count=0,results=[]),expected_view=1)['valid_diagnostic']


def test_lost_required_face_or_cell_diversity_is_rejected():
    first = panel('view1left',1,np.eye(3))
    assert not orientation_feedback(alignment(first),expected_view=1)['valid_diagnostic']
    second = panel('view1right',1,np.eye(3))
    for pair in second['matched_pairs']: pair['reference_cell']='0,0'
    assert not orientation_feedback(alignment(first,second),expected_view=1)['valid_diagnostic']
