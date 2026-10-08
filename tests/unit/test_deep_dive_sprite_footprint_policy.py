"""Saved real Boss box planning regression; geometry never confirms labels."""
from copy import deepcopy

import cv2
import numpy as np

from plans.resonance_pc.src.actions._deep_dive_scan_policy import CellScanPolicy, _matrix
from plans.resonance_pc.src.actions._deep_dive_scan_stream import ScanVisionStream


ROTATION = np.array([[.36601682984859774, .5925513907834902, -.7175754521638363],
                     [.7967097703821374, .1989578899286505, .5706744254062738],
                     [.4809212222861708, -.7805758177881829, -.39926954634567985]])
TRANSLATION = np.array([[.001209955599467798], [.49578439602387314], [42.38852835192919]])
# live_hybrid_01 frame348: independently observed detector rectangle and the
# same source frame's accepted Boss cell evidence. No generated screenshots.
BOSS_EVIDENCE = dict(frame_id=348, group=35, quality=.97,
    quad=[[801.6, 399.0], [833.2, 470.8], [769.2, 524.0], [736.7, 452.0]],
    cosine=.755, occupant='singularity', confidence=.89,
    target_box=[765, 399, 159, 156], target_point=[844.0590515136719, 477.2379608154297])


def real_case():
    policy = CellScanPolicy()
    policy.tvec = TRANSLATION.copy()
    rows = [dict(face=f, row=r, col=c, occupant='none', occupant_status='confirmed',
                 node_status='known', confidence=.83, evidence=[])
            for f in 'URFDLB' for r in range(3) for c in range(3)]
    rows[31].update(occupant='singularity', node_status='not_required_target',
                    evidence=[deepcopy(BOSS_EVIDENCE)])
    return policy, rows


def project(policy, rotation, points):
    camera = np.einsum('ij,...j->...i', rotation, points)+policy.tvec.reshape(3)
    return camera[..., :2]/camera[..., 2, None]*policy._K[0, 0]+policy._K[:2, 2]


def test_real_large_boss_box_blocks_a_neighbour_the_old_14px_prior_misses():
    policy, rows = real_case()
    before_rows = deepcopy(rows)
    baseline = deepcopy(policy)
    baseline._sprite_footprint = lambda row: None
    quad = np.float32(project(policy, ROTATION, policy._quads[28]))  # D01
    x, y, w, h = BOSS_EVIDENCE['target_box']
    rectangle = np.float32([(x-8, y-8), (x+w+8, y-8),
                           (x+w+8, y+h+8), (x-8, y+h+8)])
    overlap, _ = cv2.intersectConvexConvex(quad, rectangle)
    assert overlap > cv2.contourArea(quad)*.08  # actual semantic obstruction gate
    assert baseline._readability(ROTATION[None], rows)[0, 28] > 0
    assert policy._readability(ROTATION[None], rows)[0, 28] == 0
    # The object's own cell is not self-occluded, and no labels/votes change.
    assert policy._readability(ROTATION[None], rows)[0, 31] > 0
    assert rows == before_rows


def test_without_accepted_real_box_readability_is_exactly_the_old_fallback():
    policy, rows = real_case()
    rows[31]['evidence'][0].pop('target_box')
    baseline = deepcopy(policy)
    baseline._sprite_footprint = lambda row: None
    poses = np.array([_matrix(np.array([angle, 0., 0.])) @ ROTATION
                      for angle in (-.7, -.3, 0., .3, .7)])
    assert policy._sprite_footprint(rows[31]) is None
    assert np.array_equal(policy._readability(poses, rows), baseline._readability(poses, rows))
    assert np.array_equal(policy._anchor_support(poses, rows)[0],
                          baseline._anchor_support(poses, rows)[0])


def test_stream_snapshot_preserves_the_real_sprite_profile_for_planning():
    policy, rows = real_case()
    result = dict(cells=rows, layout_complete=True, known_cells=54, faces_observed=6)
    snapshot = ScanVisionStream._result_state(result)
    expected = policy._sprite_footprint(rows[31])
    actual = policy._sprite_footprint(snapshot['cells'][31])
    assert actual is not None
    np.testing.assert_array_equal(actual[0], expected[0])
    assert actual[1] == expected[1]
    snapshot['cells'][31]['evidence'][0]['target_box'][0] = -1
    assert rows[31]['evidence'][0]['target_box'][0] == 765


def test_sprite_extent_stays_screen_facing_and_height_is_physically_bounded():
    policy, rows = real_case()
    radius, heights = policy._sprite_footprint(rows[31])
    assert .20 <= heights[0] <= heights[1] <= 1.
    assert min(radius) > .7  # real ~160px sprite, rather than 14px radius
    poses = np.array([ROTATION, _matrix(np.array([0., .5, 0.])) @ ROTATION])
    corridors = np.array([project(policy, r, policy._corridors[31]) for r in poses])
    # A fixed screen pixel/world scale is independent of face foreshortening.
    lo, hi = policy._sprite_occlusion_bounds(poses, 31, rows[31],
        np.array([100., 100.]), corridors)
    assert (hi-lo >= np.array([159., 156.])).all()


def test_invalid_or_other_occupant_box_cannot_create_a_sprite_profile():
    policy, rows = real_case()
    for changes in [dict(occupant='inspiration'), dict(confidence=.59),
                    dict(target_box=[1, 2, 0, 100]), dict(target_box=[1, 2, 500, 100]),
                    dict(cosine=float('nan')), dict(quad=[[1, 2]])]:
        row = deepcopy(rows[31]);row['evidence'][0].update(changes)
        assert policy._sprite_footprint(row) is None


def test_cross_face_support_still_requires_six_known_glyphs_on_two_faces():
    policy, rows = real_case()
    def visible(poses, rows, **kwargs):
        output = np.zeros((len(poses), 54));output[:, :4] = 1.;output[:, 9:11] = 1.
        return output
    policy._readability = visible
    counts, supported = policy._anchor_support(ROTATION[None], rows)
    assert counts.tolist() == [6] and supported.tolist() == [True]
    def single_face(poses, rows, **kwargs):
        output = np.zeros((len(poses), 54));output[:, :7] = 1.;return output
    policy._readability = single_face
    assert policy._anchor_support(ROTATION[None], rows)[1].tolist() == [False]
