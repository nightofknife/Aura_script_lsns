"""Plan new face coverage without treating projections as accepted glyphs."""
from copy import deepcopy
import math

import numpy as np

from plans.resonance_pc.src.actions._deep_dive_scan_policy import CellScanPolicy, _matrix


def late_case():
    policy = CellScanPolicy()
    rotation = np.array([[.45392895619395157, .8850024324571355, .10353355626859918],
                         [.8327478038958793, -.380024269432235, -.4026321519067825],
                         [-.31698516974025776, .26898373405737663, -.909487852022934]])
    policy.tvec = np.array([[.0204124554721601], [.4725413318087198], [42.31972370427177]])
    axes = {0: np.array([.00003583653210658639, -.0018201630040696611, -.0009873677827411474]),
            1: np.array([.002123573019597519, .00000522092060990554, -.000032764306562824856])}
    known = {0, 2, 5, 6, 8, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 30, 33, 34,
             36, 37, 38, 39, 40, 41, 42, 43, 44, 46, 47, 48, 49, 50, 51, 52, 53}
    cells = [dict(face=face, row=row, col=col, occupant='unknown',
                  node_status='unknown', occupant_status='unknown', confidence=0.)
             for face in 'URFDLB' for row in range(3) for col in range(3)]
    for index in known:
        cells[index].update(occupant='none', node_status='known',
                            occupant_status='confirmed', confidence=.83)
    for index, occupant in {3: 'inspiration', 4: 'player', 31: 'singularity'}.items():
        cells[index].update(occupant=occupant, node_status='not_required_target',
                            occupant_status='confirmed', confidence=.8)
    # The one old R observation made the whole face "seen", though no R cell
    # was confirmed. Preserve that distinction from the actual late scan.
    cells[10]['occupant_evidence_counts'] = {'none': 1}
    policy._last_gain = np.zeros(54)
    policy._last_attempt = np.full(54, -6.)
    return policy, rotation, axes, cells


def test_late_scan_prefers_full_missing_face_with_independent_overlap():
    policy, rotation, axes, cells = late_case()
    baseline = deepcopy(policy)
    def known_only_support(poses, rows, *, route=False):
        counts, multi = baseline._anchor_support(poses, rows)
        support = np.clip(counts/(4. if route else 6.), .12, 1.)
        if not route:
            support[multi] = 1.25
        return support
    baseline._planning_anchor_support = known_only_support
    baseline._plan(rotation, cells, axes, 56.765)
    before = deepcopy(cells)
    policy._plan(rotation, cells, axes, 56.765)
    old_read = baseline._readability(np.array([baseline._final_rotation]), cells)[0, 9:18]
    new_read = policy._readability(np.array([policy._final_rotation]), cells)[0, 9:18]
    assert sum(old_read > 0) == 5
    assert sum(new_read > 0) == 9
    assert new_read.sum() > 8.
    assert policy.target_face == 'R'
    assert cells == before


def test_unlearned_single_face_cannot_receive_independent_anchor_prior():
    policy, _, _, cells = late_case()
    for cell in cells:
        cell.update(occupant='unknown', node_status='unknown', confidence=0.)
    policy.tvec = np.array([[0.], [-.5], [44.]])
    pose = _matrix(np.array([0., math.pi/2., 0.]))
    visible = policy._readability(np.array([pose]), cells, anchors=True, known_only=False)[0] > 0
    assert sum(visible) >= 7
    assert sum(sum(visible[i*9:(i+1)*9]) >= 2 for i in range(6)) == 1
    assert policy._planning_anchor_support(np.array([pose]), cells)[0] == .12


def test_predicted_raised_occupants_do_not_count_as_new_glyph_support():
    policy, _, _, cells = late_case()
    for cell in cells:
        cell.update(occupant='unknown', node_status='unknown', confidence=0.)
    cells[0]['occupant'] = 'player'
    cells[1]['occupant'] = 'singularity'
    # Five regular cells on R plus two raised occupants on U cannot establish
    # a predicted seven-glyph multi-face grid.
    def visibility(poses, rows, **kwargs):
        visible = np.zeros((len(poses), 54))
        visible[:, [0, 1, 9, 10, 11, 12, 13]] = 1.
        return visible
    policy._readability = visibility
    assert policy._planning_anchor_support(np.array([np.eye(3)]), cells)[0] == .12


def test_fully_known_overlap_scores_stay_identical():
    policy, rotation, axes, cells = late_case()
    for cell in cells:
        cell.update(occupant='none', node_status='known', confidence=.83)
    poses = np.array([_matrix(np.array([angle, 0., 0.])) @ rotation
                      for angle in (-.8, -.4, 0., .4, .8)])
    counts, multi = policy._anchor_support(poses, cells)
    original = np.clip(counts/6., .12, 1.)
    original[multi] = 1.25
    assert np.array_equal(policy._planning_anchor_support(poses, cells), original)
    assert np.array_equal(policy._planning_anchor_support(poses, cells, route=True),
                          np.clip(counts/4., .12, 1.))
