"""Known-support recovery regression from live_rules_02, source frame 586."""
import math

import numpy as np

from plans.resonance_pc.src.actions._deep_dive_scan_policy import (
    CellScanPolicy, _matrix, _pose_angle, _pose_vector, _unit,
)


def recovery_case():
    policy = CellScanPolicy()
    anchor = np.array([[.488290539097404, .01975027838730098, -.8724576069538224],
                       [.8008686523603186, .3870065581874556, .45698503868996265],
                       [.346672397364621, -.9218654188239862, .1731542620958047]])
    policy.tvec = np.array([[-.056330174428681434], [.447987958497361], [42.324855539686006]])
    axes = {0: np.array([.00003909706256454894, -.0018007050695612844, -.0009569505407939314]),
            1: np.array([.0020685080846131474, .000039663380204862544, -.00006074888116843977])}
    known = {2, 5, 6, 8, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 30, 33,
             36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 52}
    targets = {3: 'inspiration', 4: 'player', 31: 'singularity'}
    cells = [dict(face=face, row=row, col=col, occupant='unknown',
                  node_status='unknown', occupant_status='unknown', confidence=0.)
             for face in 'URFDLB' for row in range(3) for col in range(3)]
    for index in known:
        cells[index].update(occupant='none', node_status='known',
                            occupant_status='confirmed', confidence=.83)
    for index, occupant in targets.items():
        cells[index].update(occupant=occupant, node_status='not_required_target',
                            occupant_status='confirmed', confidence=.8)
    return policy, anchor, axes, cells


def test_local_search_preserves_both_faces_and_avoids_weaker_fixed_second_axis():
    policy, anchor, axes, cells = recovery_case()
    goal = policy._recovery_search_goal(anchor, axes, cells, [anchor])
    preferred = _matrix(_unit(axes[0])*math.radians(6.)) @ anchor
    assert np.allclose(goal, preferred, atol=1e-12)
    visible = policy._readability(np.array([goal]), cells, anchors=True)[0]
    assert sum(visible[27:36] > 0) >= 3
    assert sum(visible[36:45] > 0) >= 2
    fixed = _matrix(_unit(axes[1])*math.radians(12.)) @ anchor
    assert visible.sum() > policy._readability(np.array([fixed]), cells, anchors=True)[0].sum()


def test_search_uses_both_signs_of_measured_response_axes():
    policy, anchor, axes, cells = recovery_case()
    goal = policy._recovery_search_goal(anchor, axes, cells, [anchor])
    reversed_axes = {key: -axis for key, axis in axes.items()}
    other = policy._recovery_search_goal(anchor, reversed_axes, cells, [anchor])
    assert np.allclose(other, goal, atol=1e-12)
    assert np.dot(_pose_vector(other, anchor), reversed_axes[0]) < 0.


def test_second_trial_cannot_repeat_already_failed_local_pose():
    policy, anchor, axes, cells = recovery_case()
    first = policy._recovery_search_goal(anchor, axes, cells, [anchor])
    second = policy._recovery_search_goal(anchor, axes, cells, [anchor, first])
    assert _pose_angle(second, first) > math.radians(4.)
    assert _pose_angle(second, anchor) <= math.radians(12.)+1e-8


def test_predicted_support_does_not_finish_recovery_without_current_image_fit():
    policy, anchor, axes, cells = recovery_case()
    observed = _matrix(_unit(axes[1])*math.radians(20.)) @ anchor
    feedback = dict(glyph_anchor_at=100., glyph_anchor_age_sec=1.4,
                    glyph_anchor_rotation=anchor.tolist(),
                    refine_diagnostic=dict(source_frame_id=1, renewed=False))
    policy.choose(observed, cells, axes, elapsed=1.8, anchor_feedback=feedback)
    feedback['refine_diagnostic']['source_frame_id'] = 2
    policy.choose(observed, cells, axes, elapsed=2., anchor_feedback=feedback)
    policy.choose(anchor, cells, axes, elapsed=2.1, anchor_feedback=feedback)
    policy.choose(anchor, cells, axes, elapsed=2.8, anchor_feedback=feedback)
    assert policy._anchor_recovery['attempt'] == 1
    goal = policy._anchor_recovery['goal'].copy()
    choice = policy.choose(goal, cells, axes, elapsed=2.9, anchor_feedback=feedback)
    assert choice['reason'] == 'await_current_anchor_evidence'
    assert policy._anchor_recovery is not None
    choice = policy.choose(goal, cells, axes, elapsed=6.1, anchor_feedback=feedback)
    assert choice['reason'] == 'anchor_recovery_failed'
