"""Confirmation uses the saved vote poses, rather than a group's seed pose."""
from copy import deepcopy
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_layout_vision as vision


def vote(frame, angle, *, confidence=.9, kind='inspiration', group=None):
    return dict(frame_id=frame, group=frame if group is None else group,
                occupant=kind, icon_id=None, confidence=confidence,
                association_evidence=dict(source_frame_id=frame, source_map_revision=0,
                    source_frame_time=float(frame),
                    rotation=cv2.Rodrigues(np.array([0., 0., np.radians(angle)]))[0].tolist()))


def mapper(rows, slot=26):
    scanner = vision.LayoutScanner()
    scanner.evidence[slot] = rows
    return scanner


def test_real_f22_group_centres_do_not_prove_saved_best_images_independent():
    source = Path(__file__).parents[1]/'fixtures/deep_dive_anchor/actual_positive_pose_f22_explore02.json'
    case = json.loads(source.read_text(encoding='utf8'))
    rows = case['evidence']
    assert sorted(e['frame_id'] for e in rows) == [392, 404]
    assert len({e['group'] for e in rows}) == 2
    angle = vision._angle(*(np.asarray(e['association_evidence']['rotation']) for e in rows))
    assert angle == pytest.approx(7.2624421477, abs=1e-8)
    scanner = mapper(deepcopy(rows))
    before = deepcopy(scanner.evidence)
    result = scanner.result()
    cell = result['cells'][26]
    assert cell['occupant_status'] == 'unknown'
    assert cell['occupant_evidence_counts'] == {'inspiration': 2}
    assert not result['inspiration_cells']
    assert scanner.evidence == before


def test_any_pair_finds_minus_six_plus_six_despite_stronger_middle_vote():
    rows = [vote(1, 0, confidence=.99), vote(2, -6, confidence=.85), vote(3, 6, confidence=.85)]
    cell = mapper(rows).result()['cells'][26]
    assert cell['occupant_status'] == 'confirmed'
    assert {e['frame_id'] for e in cell['evidence'][:2]} == {2, 3}
    assert cell['occupant_evidence_counts'] == {'inspiration': 3}


def test_real_witness_pair_survives_six_vote_export_without_copying_votes():
    rows = [vote(i+1, i*.5, confidence=.99-i*.01) for i in range(6)]
    rows.append(vote(7, 20, confidence=.70))
    scanner = mapper(rows)
    before = deepcopy(scanner.evidence)
    cell = scanner.result()['cells'][26]
    assert cell['occupant_status'] == 'confirmed'
    assert len(cell['evidence']) == 6
    assert {e['frame_id'] for e in cell['evidence'][:2]} == {1, 7}
    assert len({e['frame_id'] for e in cell['evidence']}) == 6
    assert cell['occupant_evidence_counts'] == {'inspiration': 7}
    assert scanner.evidence == before


@pytest.mark.parametrize('bad', ['missing', 'shape', 'scaled', 'reflection', 'nan', 'map',
                               'wrong_frame', 'same_frame', 'same_group', 'weak', 'bool_frame'])
def test_invalid_or_nonindependent_actual_proof_stays_unknown(bad):
    rows = [vote(1, 0), vote(2, 20)]
    proof = rows[1]['association_evidence']
    if bad == 'missing': proof.pop('rotation')
    elif bad == 'shape': proof['rotation'] = [[1., 0.], [0., 1.]]
    elif bad == 'scaled': proof['rotation'] = (np.eye(3)*1.1).tolist()
    elif bad == 'reflection': proof['rotation'] = np.diag([1., 1., -1.]).tolist()
    elif bad == 'nan': proof['rotation'][0][0] = float('nan')
    elif bad == 'map': proof['source_map_revision'] = 1
    elif bad == 'wrong_frame': proof['source_frame_id'] = 3
    elif bad == 'same_frame': rows[1]['frame_id'] = proof['source_frame_id'] = 1
    elif bad == 'same_group': rows[1]['group'] = rows[0]['group']
    elif bad == 'weak': rows[1]['confidence'] = .69
    elif bad == 'bool_frame': proof['source_frame_id'] = True; rows[1]['frame_id'] = 1
    cell = mapper(rows).result()['cells'][26]
    assert cell['occupant_status'] == 'unknown'
    assert cell['occupant_evidence_counts'] == {'inspiration': 2}


def suppression_case(angles):
    scanner = mapper([vote(i+1, a, confidence=.95, kind='player') for i, a in enumerate(angles)], slot=4)
    scanner.evidence[5] = [vote(101, 0, confidence=.70, kind='player'),
                           vote(102, 20, confidence=.70, kind='player')]
    scanner.evidence[5] += [dict(frame_id=200+i, group=200+i, occupant='none',
        icon_id='blue_scales', confidence=.9) for i in range(5)]
    return scanner


def test_six_nearby_group_votes_cannot_suppress_other_target_conflict():
    scanner = suppression_case([0, 1, 2, 3, 4, 20])
    before = deepcopy(scanner.evidence)
    cells = scanner.result()['cells']
    assert cells[4]['occupant_status'] == 'confirmed'  # A genuine pair exists.
    assert cells[5]['occupant_status'] == 'conflict'
    assert 'constraint_resolution' not in cells[5]
    assert scanner.evidence == before


def test_six_actual_separated_votes_preserve_original_conservative_suppression():
    scanner = suppression_case([0, 10, 20, 30, 40, 50])
    cells = scanner.result()['cells']
    assert cells[5]['occupant'] == 'none'
    assert cells[5]['occupant_status'] == 'confirmed'
    assert cells[5]['constraint_resolution']['reason'] == 'unique_target_and_multiple_clear_negative_views'


def test_original_conflict_remains_even_with_valid_positive_pair():
    rows = [vote(1, 0, confidence=.7), vote(2, 20, confidence=.7)]
    rows += [dict(frame_id=10+i, group=10+i, occupant='none', icon_id='blue_scales',
                  confidence=.9) for i in range(5)]
    assert mapper(rows).result()['cells'][26]['occupant_status'] == 'conflict'


def test_masks_and_candidate_association_survive_missing_confirmation_proof(monkeypatch):
    scanner = mapper([vote(1, 0), vote(2, 7)])
    targets = [dict(kind='inspiration', point=[500., 220.], box=[480., 200., 40., 40.],
                    confidence=.9, confirmable=True)]
    scanner.last_targets = targets
    monkeypatch.setattr(scanner, '_associated_targets', lambda *args: ({}, set()))
    result = scanner.result()
    assert result['cells'][26]['occupant_status'] == 'unknown'
    assert result['target_clues'] == targets
    assert result['target_candidate_associations'][0]['point'] == targets[0]['point']
    assert 'cell_index' not in result['target_candidate_associations'][0]
