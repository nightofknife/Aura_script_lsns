from pathlib import Path
import json

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions._deep_dive_four_view_geometry import (
    FourViewGeometry, PANEL_FACES, VIEW_PANELS, canonical_cell, local_cell,
    orientation_feedback, _normal_projections,
    _paired_vertical_family_selection,
)
from plans.resonance_pc.src.actions._deep_dive_planner_rules import (
    BASES, FACES, cell_to_slot, geometry,
)


def test_all_54_cells_round_trip_and_planner_slots():
    mapped = []
    for panel in PANEL_FACES:
        for row in range(3):
            for col in range(3):
                canonical = canonical_cell(panel, row, col)
                mapped.append(canonical['slot'])
                assert canonical['slot'] == cell_to_slot(canonical)
                local = local_cell(canonical['face'], canonical['row'], canonical['col'])
                assert (local['panel'], local['row'], local['col']) == (panel, row, col)
    assert sorted(mapped) == list(range(54))


def test_bottom_and_second_pair_orientation_against_projected_base_axes():
    # Start from F/R visible beneath U. Pure camera-horizontal rotation gives
    # the four ordered local face axes; no glyph labels determine this mapping.
    yaw = np.deg2rad(45.)
    reset = np.array([[np.cos(yaw), 0, np.sin(yaw)], [0, 1, 0],
                      [-np.sin(yaw), 0, np.cos(yaw)]])
    for panel, angle in {'view1left': 0., 'view1right': 0., 'view2main': -90.,
                         'view3left': -180., 'view3right': -180., 'view4main': -270.}.items():
        radians = np.deg2rad(angle)
        rotate = np.array([[1, 0, 0], [0, np.cos(radians), -np.sin(radians)],
                           [0, np.sin(radians), np.cos(radians)]])@reset
        face = PANEL_FACES[panel]
        normal, column, row = map(np.asarray, BASES[face])
        assert (rotate@normal)[2] < -.6
        if panel == 'view2main':
            assert (rotate@column)[0] > 0 and (rotate@column)[1] < 0
            assert (rotate@row)[0] > 0 and (rotate@row)[1] > 0
            assert canonical_cell(panel, 0, 0) == dict(face='D', row=0, col=2, slot=29)
        elif panel.startswith('view3'):
            assert (rotate@column)[0] < 0 and (rotate@row)[1] < 0
            assert canonical_cell(panel, 0, 0)['row'] == 2
            assert canonical_cell(panel, 0, 0)['col'] == 2
        elif panel == 'view4main':
            assert (rotate@row)[0] < 0 and (rotate@row)[1] > 0
            assert (rotate@column)[0] > 0 and (rotate@column)[1] > 0


def test_mapping_remains_bijective_under_all_actual_layer_permutations():
    cube = geometry()
    slots = [canonical_cell(p, r, c)['slot'] for p in PANEL_FACES for r in range(3) for c in range(3)]
    for permutation in cube.rotations:
        destinations = permutation[slots]
        assert sorted(destinations.tolist()) == list(range(54))
        for destination in destinations:
            face, rest = divmod(int(destination), 9)
            row, col = divmod(rest, 3)
            local = local_cell(FACES[face], row, col)
            assert canonical_cell(local['panel'], local['row'], local['col'])['slot'] == destination


def test_shared_edges_agree_in_planner_cube_coordinates():
    def point(cell):
        n, u, v = map(np.asarray, BASES[cell['face']])
        return n+u*(cell['col']-1)+v*(cell['row']-1)
    for i in range(3):
        # Initial front top touches U's lower edge; R's top touches U's
        # right edge with a reverse row. After the half turn, local bottom
        # rows are the physical upper L/B rows, rather than D boundaries.
        pairs = [
            (canonical_cell('view1left', 0, i), dict(face='U', row=2, col=i)),
            (canonical_cell('view1right', 0, i), dict(face='U', row=2-i, col=2)),
            (canonical_cell('view3left', 2, i), dict(face='U', row=2-i, col=0)),
            (canonical_cell('view3right', 2, i), dict(face='U', row=0, col=i)),
            (canonical_cell('view1left', 2, i), dict(face='D', row=0, col=i)),
        ]
        for first, second in pairs:
            assert np.array_equal(point(first), point(second))


@pytest.mark.parametrize('panel,row,col', [('bogus', 0, 0), ('view1left', True, 0),
                                        ('view1left', 3, 0), ('view2main', 0, -1)])
def test_invalid_indices_rejected(panel, row, col):
    with pytest.raises(ValueError):
        canonical_cell(panel, row, col)


def test_packaged_geometry_contains_no_semantic_teacher():
    geo = FourViewGeometry()
    for panel in geo.panels:
        for cell in panel['cells']:
            assert set(cell) == {'row', 'col', 'quad'}
    serialized = json.dumps(geo.data)
    for forbidden in ('expected_icon', 'expected_target', 'player_cell', 'singularity_cell'):
        assert forbidden not in serialized


@pytest.mark.parametrize('view', [1, 2, 3, 4])
def test_blank_does_not_return_reference_quads_as_actual_detection(view):
    result = FourViewGeometry().locate(np.zeros((720, 1280, 3), np.uint8), expected_view=view)
    assert result['status'] == 'unknown'
    assert result['cells'] == []
    assert not result['identity_from_geometry']
    assert not orientation_feedback(result, expected_view=view)['arrival_candidate']


def test_hud_only_frame_cannot_become_cube():
    rgb = np.zeros((720, 1280, 3), np.uint8)
    cv2.rectangle(rgb, (970, 100), (1240, 590), (255, 255, 255), 3)
    cv2.rectangle(rgb, (30, 20), (1220, 80), (255, 0, 0), 3)
    for view in VIEW_PANELS:
        assert FourViewGeometry().locate(rgb, expected_view=view)['cells'] == []


def test_actual_structural_projection_requires_observed_lines_not_glyph_texture():
    geo = FourViewGeometry()
    rgb = np.zeros((720, 1280, 3), np.uint8)
    for panel in geo.panels:
        if panel['view'] == 2:
            for anchor in panel['anchors']:
                points = np.rint(anchor['endpoints']).astype(np.int32)
                cv2.line(rgb, tuple(points[0]), tuple(points[1]), (255, 255, 255), 1)
    result = geo.locate(rgb, expected_view=2)
    assert result['proposal_accepted']
    assert len(result['cells']) == 9
    assert all(p['structural_lines'] for p in result['results'])
    # Replacing the entire region revokes observed geometry rather than emitting
    # the stored quads with a fabricated confidence.
    rgb[90:620, 320:960] = 0
    assert geo.locate(rgb, expected_view=2)['cells'] == []


def test_planar_branches_without_real_sidewall_do_not_invent_normal():
    cell = dict(panel='view2main', quad=[[641, 119], [697, 164], [641, 215], [585, 166]])
    projection, evidence, branches = _normal_projections(cell, np.empty((0, 4)))
    assert projection is None
    assert len(branches) == 2
    assert not evidence['physical_pose_verified']
    assert evidence['status'] == 'pending_planar_branch_ambiguity'
    assert branches[0]['normal_projection'][1]*branches[1]['normal_projection'][1] < 0


def test_image_angles_are_not_physical_pose_or_identity():
    result = dict(proposal_accepted=True, results=[dict(reference_panel='view2main',
                  structural_support_valid=True, homography=np.eye(3).tolist(),
                  selection_quality=dict(complete_bright_center_count=7, centering_panel_units=.05),
                  cells=[dict(center=[640+(i%3-1)*70, 300+(i//3-1)*70]) for i in range(9)],
                  reference_panel_center_px=[640, 300], reference_panel_span_y_px=360)])
    feedback = orientation_feedback(result, expected_view=2)
    assert feedback['arrival_candidate']
    assert feedback['signed_error_deg'] == pytest.approx(0)
    assert not feedback['formal_pose']
    assert not feedback['phase_identity']
    assert 'not_physical' in feedback['metrics']['unit']


def test_paired_independent_candidates_require_opposite_tilts():
    def candidate(angle, name):
        h = np.eye(3);h[1, 0] = np.tan(np.deg2rad(angle))
        return dict(proposal_accepted=True, structural_support_valid=True, cells=[], homography=h.tolist(),
                    reference_panel=name, selection_quality=dict(centering_panel_units=.1,
                    complete_bright_center_count=9, actual_supported_lines=10))
    left_bad, left_good = candidate(-6, 'left'), candidate(6, 'left')
    right = candidate(-6, 'right')
    result = _paired_vertical_family_selection([left_bad, left_good], [right])
    assert result[0] is left_good
    assert result[1] is right
    assert not result[0]['paired_motion_family_evidence']['homography_borrowed']


def test_zero_shape_error_without_actual_center_support_cannot_arrive():
    result = dict(proposal_accepted=True, results=[dict(reference_panel='view2main',
                  structural_support_valid=True, homography=np.eye(3).tolist(),
                  selection_quality=dict(complete_bright_center_count=7, centering_panel_units=.35))])
    measured = orientation_feedback(result, expected_view=2)
    assert not measured['valid_diagnostic']
    assert not measured['arrival_candidate']


def test_main_signed_position_distinguishes_before_after_even_shape_ratio():
    def measured(delta):
        h = np.eye(3);h[1, 1] = 1.05
        result = dict(proposal_accepted=True, results=[dict(reference_panel='view2main',
            structural_support_valid=True, homography=h.tolist(),
            selection_quality=dict(complete_bright_center_count=7, centering_panel_units=.05),
            cells=[dict(center=[640+(i%3-1)*70, 300+delta+(i//3-1)*70]) for i in range(9)],
            reference_panel_center_px=[640, 300], reference_panel_span_y_px=360)])
        return orientation_feedback(result, expected_view=2)
    before, after = measured(12), measured(-12)
    assert before['signed_error_deg'] > 0
    assert after['signed_error_deg'] < 0
    assert before['metrics']['panels']['view2main']['vertical_horizontal_shape_deg'] > 0
    assert after['metrics']['panels']['view2main']['vertical_horizontal_shape_deg'] > 0


def test_opposite_paired_wall_tilts_without_actual_centers_cannot_control():
    panels = []
    for name, angle in [('view3left', -13.), ('view3right', 13.)]:
        h = np.eye(3);h[1, 0] = np.tan(np.deg2rad(angle))
        panels.append(dict(reference_panel=name, structural_support_valid=True,
            homography=h.tolist(), selection_quality=dict(complete_bright_center_count=3,
                                                        centering_panel_units=.4)))
    feedback = orientation_feedback(dict(proposal_accepted=True, results=panels), expected_view=3)
    assert not feedback['valid_diagnostic']
    assert feedback['signed_error_deg'] is None
    assert not feedback['arrival_candidate']
    assert feedback['diagnostic_signed_error_deg'] == pytest.approx(-13.)
    assert feedback['reason'] == 'paired_actual_complete_center_support_required'
