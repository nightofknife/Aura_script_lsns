import hashlib
from copy import deepcopy

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions._deep_dive_four_view_body_projection import (
    CAMERA, BASES, _point, _quad, project_sidepeek_candidates,
)
from plans.resonance_pc.src.actions._deep_dive_planner_rules import cell_to_slot


def source(index=3, **changes):
    result = dict(generation_source='atomic_wgc', capture_backend='wgc',
                  session_id=11, map_revision=0, generation=index,
                  frame_id=index, frame_time=100+index)
    result.update(changes)
    return result


def observed(faces=('F', 'R'), distance=2.6, pitch=0.):
    # Synthetic photogrammetry fixture; no claim of real-game observations.
    yaw, pitch = np.deg2rad(45), np.deg2rad(pitch)
    ry = np.array([[np.cos(yaw), 0, np.sin(yaw)], [0, 1, 0], [-np.sin(yaw), 0, np.cos(yaw)]])
    rx = np.array([[1, 0, 0], [0, np.cos(pitch), -np.sin(pitch)], [0, np.sin(pitch), np.cos(pitch)]])
    rotation = cv2.Rodrigues(rx@ry)[0]
    translation = np.array([0., .5, 34.]).reshape(3, 1)
    rgb = np.zeros((720, 1280, 3), np.uint8)
    digest = hashlib.sha256(rgb.tobytes()).hexdigest()
    cells = []
    matrix = rx@ry
    for face in faces:
        normal = matrix@np.asarray(BASES[face][0])
        for row in range(3):
            for col in range(3):
                cell = dict(face=face, row=row, col=col)
                cell['slot'] = cell_to_slot(cell)
                cell['quad'] = cv2.projectPoints(_quad(cell, distance), rotation, translation, CAMERA, None)[0][:, 0].tolist()
                cell['normal_projection'] = (normal[:2]*85).tolist()
                cell['normal_evidence'] = dict(source_rgb_sha256=digest,
                    actual_sidewall_witnesses=[dict(actual_segment=[500, 350, 500, 380])])
                cells.append(cell)
    return rgb, dict(proposal_accepted=True, source_rgb_sha256=digest, cells=cells)


def pair_prior():
    rgb, geometry = observed()
    return project_sidepeek_candidates(rgb, geometry, source=source(), scan_epoch='epoch')


def test_pair_estimates_distance_from_current_visible_reprojection():
    rgb, geometry = observed()
    result = project_sidepeek_candidates(rgb, geometry, source=source(), scan_epoch='epoch')
    assert result['body_pose_supported']
    assert result['distance'] == pytest.approx(2.6, abs=.035)
    assert result['fit_evidence']['non_coplanar']
    assert result['fit_evidence']['accepted_visible_reprojection']['rms_all_px'] < .1
    assert not result['hidden_cells_observed']
    assert not result['usable_for_node_reading']
    assert not result['negative_occupancy_proof']
    assert not result['target_assignment_confirmed']
    for candidate in result['candidate_cells']:
        assert candidate['face'] not in ('F', 'R')
        assert candidate['candidate_only']
        assert not candidate['actually_observed']
        assert not candidate['negative_occupancy_proof']


def test_single_face_requires_genuine_same_epoch_pair_distance():
    rgb, geometry = observed(('D',), pitch=-116.)
    missing = project_sidepeek_candidates(rgb, geometry, source=source(4), scan_epoch='epoch')
    assert missing['reason'] == 'single_face_distance_unobservable_requires_pair_prior'
    prior = pair_prior()
    result = project_sidepeek_candidates(rgb, geometry, source=source(4), scan_epoch='epoch', distance_prior=prior)
    assert result['body_pose_supported']
    assert not result['fit_evidence']['non_coplanar']
    assert len(result['fit_evidence']['planar_branches']) == 2
    assert result['fit_evidence']['branch_selection'] == 'actual_current_sidewall_outward_sign_not_lowest_residual'


def test_single_face_without_actual_normal_witness_keeps_branches_pending():
    rgb, geometry = observed(('D',), pitch=-116.)
    for cell in geometry['cells']:
        cell['normal_evidence'] = {}
    result = project_sidepeek_candidates(rgb, geometry, source=source(4), scan_epoch='epoch', distance_prior=pair_prior())
    assert not result['body_pose_supported']
    assert result['reason'] == 'planar_branches_not_uniquely_supported_by_actual_sidewall'
    assert len(result['fit_evidence']['planar_branches']) == 2


@pytest.mark.parametrize('change', ['epoch', 'session', 'revision', 'repeated'])
def test_invalid_pair_prior_rejected(change):
    rgb, geometry = observed(('D',), pitch=-116.)
    prior = pair_prior()
    if change == 'epoch':
        prior['scan_epoch'] = 'other'
    elif change == 'session':
        prior['source']['session_id'] = 12
    elif change == 'revision':
        prior['source']['map_revision'] = 1
    else:
        prior['source']['generation'] = 4
    result = project_sidepeek_candidates(rgb, geometry, source=source(4), scan_epoch='epoch', distance_prior=prior)
    assert not result['body_pose_supported']
    assert result['candidate_cells'] == []


def test_wrong_current_geometry_and_insufficient_actual_coverage_rejected():
    rgb, geometry = observed()
    changed = deepcopy(geometry)
    changed['source_rgb_sha256'] = 'other'
    assert not project_sidepeek_candidates(rgb, changed, source=source(), scan_epoch='epoch')['body_pose_supported']
    changed = deepcopy(geometry)
    changed['cells'].pop()
    assert not project_sidepeek_candidates(rgb, changed, source=source(), scan_epoch='epoch')['body_pose_supported']


def test_nonrigid_visible_centers_cannot_project_hidden_cells():
    rgb, geometry = observed()
    for index, cell in enumerate(geometry['cells']):
        if index % 3 == 0:
            cell['quad'] = (np.asarray(cell['quad'])+[28, -22]).tolist()
    result = project_sidepeek_candidates(rgb, geometry, source=source(), scan_epoch='epoch')
    assert not result['body_pose_supported']
    assert result['reason'] == 'actual_body_reprojection_gate_failed'
    assert result['candidate_cells'] == []
