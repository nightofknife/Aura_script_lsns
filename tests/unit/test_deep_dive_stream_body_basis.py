from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions._deep_dive_scan_stream import (
    _accepted_anchor_observation, _geometry_body_basis,
)


def turn(axis, angle):
    value = np.zeros(3)
    value[axis] = np.radians(angle)
    return cv2.Rodrigues(value)[0]


def source_fixture():
    packet_rotation = turn(0, 24) @ turn(1, -39)
    packet_basis = turn(2, 7) @ turn(0, -3)
    local_correction = turn(1, 4) @ turn(2, -2)
    glyph_rotation = packet_rotation @ local_correction
    scanner = SimpleNamespace(glyph_anchor_frame_id=17, glyph_anchor_map_revision=0,
                              glyph_anchor_rotation=glyph_rotation)
    packet = dict(frame_id=17, session_id=123456789, map_revision=0, frame_time=20.,
                  pose={'rotation': packet_rotation}, geometry_body_basis=packet_basis)
    observation = dict(fusion_paused=False, refine_diagnostic=dict(renewed=True,
        source_frame_id=17, source_map_revision=0, source_frame_time=20.,
        accepted_faces={'D':4, 'L':9}))
    return scanner, packet, observation


def test_only_applied_body_corrections_advance_in_right_order():
    first, second = turn(0, 13), turn(1, 17)
    basis = _geometry_body_basis(np.eye(3), {'body_rotation':first}, True)
    rejected = _geometry_body_basis(basis, {'body_rotation':second}, False)
    np.testing.assert_allclose(rejected, first)
    final = _geometry_body_basis(rejected, {'body_rotation':second}, True)
    np.testing.assert_allclose(final, first @ second)
    assert not np.allclose(final, second @ first)
    rejected[0,0] = 0.
    assert basis[0,0] != 0.


def test_historical_source_restores_across_noncommuting_body_epochs():
    scanner, packet, observation = source_fixture()
    accepted = _accepted_anchor_observation(scanner, packet, observation)
    assert accepted['frame_id'] == 17 and accepted['accepted_faces'] == {'D':4,'L':9}
    source_basis = np.asarray(accepted['body_basis'])
    current_basis = packet['geometry_body_basis'] @ turn(2, 8) @ turn(0, 5)
    navigation_base = np.asarray(accepted['rotation']) @ source_basis.T
    goal = navigation_base @ current_basis
    expected = packet['pose']['rotation'] @ packet['geometry_body_basis'].T @ current_basis
    np.testing.assert_allclose(goal, expected, atol=1e-12)
    # This hook supplies no present-time association or occupancy vote.
    assert 'positive_vote' not in accepted and 'cell_index' not in accepted
    accepted['accepted_faces']['D'] = 999
    assert observation['refine_diagnostic']['accepted_faces']['D'] == 4


def test_actual_accepted_indices_are_copied_without_inventing_candidate_support():
    scanner,packet,observation=source_fixture()
    diagnostic=observation['refine_diagnostic']
    diagnostic.update(accepted_cell_indices=[27,28,30,31,36,37,38,39,40,41,42,43,44],
                      accepted_confirmed_cell_indices=[27,28,30,31,36,37],
                      candidate_cell_indices=[27,28,29,30,31,32,36,37,38,39,40,41,42,43,44])
    accepted=_accepted_anchor_observation(scanner,packet,observation)
    assert accepted['accepted_cell_indices']==diagnostic['accepted_cell_indices']
    assert 29 not in accepted['accepted_cell_indices'] and 32 not in accepted['accepted_cell_indices']
    assert accepted['accepted_confirmed_cell_indices']==diagnostic['accepted_confirmed_cell_indices']
    accepted['accepted_cell_indices'].append(53)
    assert 53 not in diagnostic['accepted_cell_indices']
    diagnostic.pop('accepted_cell_indices')
    diagnostic.pop('accepted_confirmed_cell_indices')
    missing=_accepted_anchor_observation(scanner,packet,observation)
    assert missing['accepted_cell_indices'] is None
    assert missing['accepted_confirmed_cell_indices'] is None


@pytest.mark.parametrize('broken', ['frame', 'map', 'time', 'not_renewed', 'paused',
                                   'bad_source_pose', 'missing_basis', 'bad_basis'])
def test_unproven_or_mismatched_source_cannot_be_a_navigation_anchor(broken):
    scanner, packet, observation = source_fixture()
    if broken == 'frame':
        scanner.glyph_anchor_frame_id = 16
    elif broken == 'map':
        observation['refine_diagnostic']['source_map_revision'] = 1
    elif broken == 'time':
        observation['refine_diagnostic']['source_frame_time'] = 19.
    elif broken == 'not_renewed':
        observation['refine_diagnostic']['renewed'] = False
    elif broken == 'paused':
        observation['fusion_paused'] = True
    elif broken == 'bad_source_pose':
        scanner.glyph_anchor_rotation = np.diag([1.,1.,-1.])
    elif broken == 'missing_basis':
        packet.pop('geometry_body_basis')
    elif broken == 'bad_basis':
        packet['geometry_body_basis'] = np.eye(3)*2
    assert _accepted_anchor_observation(scanner, packet, observation) is None


def test_invalid_applied_correction_faults_instead_of_inventing_a_basis():
    with pytest.raises(RuntimeError, match='applied_body_correction_invalid'):
        _geometry_body_basis(np.eye(3), {'body_rotation':np.eye(3)*2}, True)
