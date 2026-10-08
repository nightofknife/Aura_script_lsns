import numpy as np
import hashlib

from plans.resonance_pc.src.actions import _deep_dive_four_view_targets as targets
from test_deep_dive_four_view_production_reader import source


def cell(col=1, normal=True):
    x = 100+col*100
    result = dict(face='D', row=1, col=col, quad=[[x-35, 200], [x+35, 200],
                                                      [x+35, 270], [x-35, 270]])
    if normal:
        result.update(normal_projection=[0., -60.], normal_evidence={'camera_fit': 'actual_geometry'})
    return result


def star(point):
    return dict(kind='singularity', point=point, core_point=point, core_evidence='native_radial_keypoint',
                box=[point[0]-20, point[1]-20, 40, 40], confidence=.9, confirmable=True)


def associate(cells, point=[200, 170], group=1):
    return targets.associate_targets(np.zeros((400, 500, 3), np.uint8), [star(point)], cells,
        source=source(group, float(group)), view_group=group, scan_epoch='scan1')[0]


def test_outward_lift_assigns_support_cell_not_center_cell():
    result = associate([cell()])
    assert result['cell'] == dict(face='D', row=1, col=1)
    assert result['anchor_point'][1] < 200  # outside owning quad
    assert result['candidates'][0]['geometry']['inferred_lift_panel_units'] > .9


def test_missing_normal_is_unknown_not_center_fallback():
    assert associate([cell(normal=False)])['cell'] is None


def test_equal_competing_cells_cannot_be_selected_by_confidence():
    assert associate([cell(), cell()])['cell'] is None


def test_temporal_frames_never_make_independent_target_views():
    result = targets.fuse_target_associations([associate([cell()], group=1)]*3)
    assert result['targets'][0]['independent_view_count'] == 1
    assert result['targets'][0]['occupant_status'] == 'unknown'
    assert result['targets_ready'] is False


def test_canonical_dedup_preserves_real_groups():
    result = targets.fuse_target_associations([associate([cell()], group=1), associate([cell()], group=2)])
    assert len(result['targets']) == 1
    assert result['targets'][0]['independent_view_count'] == 2
    assert result['targets'][0]['occupant_status'] == 'confirmed'
    assert not result['targets_ready']  # no actual player evidence


def test_unassigned_side_peek_stays_in_report():
    unresolved = associate([cell(normal=False)])
    result = targets.fuse_target_associations([associate([cell()]), unresolved])
    assert len(result['unassociated_target_candidates']) == 1
    assert not result['targets_ready']


def test_mixed_map_invalidates_all_target_evidence():
    row = associate([cell()], group=2)
    row['source']['map_revision'] = 1
    result = targets.fuse_target_associations([associate([cell()]), row])
    assert result['reason'] == 'mixed_scan_target_evidence'


def packet(x=100, y=200, kind='inspiration'):
    return dict(kind=kind, point=[x, y], box=[x-15, y-20, 30, 40],
                confidence=.9, confirmable=True)


def test_tracker_measured_vertical_motion_preserves_instance():
    tracker = targets.VerticalTargetTracker()
    first = tracker.update([packet()], source(1, 1.))
    second = tracker.update([packet(104, 245)], source(2, 2.))
    assert first[0]['track_id'] == second[0]['track_id']
    assert second[0]['tracking_evidence']['actual_observation_count'] == 2


def test_tracker_same_kind_crossing_and_duplicate_is_ambiguous():
    tracker = targets.VerticalTargetTracker()
    first = tracker.update([packet(100), packet(200)], source(1, 1.))
    crossing = tracker.update([packet(150), packet(152)], source(2, 2.))
    assert all(t['track_id'] is None for t in crossing)
    after = tracker.update([packet(110), packet(190)], source(3, 3.))
    assert all(t['track_id'] is None for t in after)


def test_tracker_stale_capture_and_long_gap_do_not_reuse_id():
    tracker = targets.VerticalTargetTracker()
    first = tracker.update([packet()], source(1, 1.))
    assert tracker.update([packet()], source(1, 1.))[0]['track_id'] is None
    new = tracker.update([packet()], source(2, 8.))[0]
    assert new['track_id'] != first[0]['track_id']
    assert new['tracking_evidence']['previous_ids_not_linked'] == [first[0]['track_id']]
    assert new['tracking_evidence']['actual_observation_count'] == 1
    following = tracker.update([packet(102, 202)], source(3, 9.))[0]
    assert following['track_id'] == new['track_id']
    assert following['tracking_evidence']['actual_observation_count'] == 2


def test_tracker_map_rebind_cannot_reuse_old_identity():
    tracker = targets.VerticalTargetTracker()
    old = tracker.update([packet()], source(1, 1.))[0]['track_id']
    new = tracker.update([packet()], source(2, 2., revision=1))[0]['track_id']
    assert old != new


def test_tracked_side_peek_explains_identity_without_forging_owner_group():
    rows = [associate([cell()], group=1), associate([cell()], group=2)]
    for row in rows:
        row['track_id'] = 3
    peek = associate([cell(normal=False)], group=3)
    peek.update(track_id=3, tracking_evidence=dict(reason='measured_consecutive_vertical_trajectory'))
    result = targets.fuse_target_associations(rows+[peek])
    assert not result['unassociated_target_candidates']
    assert result['targets'][0]['independent_view_count'] == 2
    assert result['identity_explained_candidates'][0]['adds_ownership_group'] is False


def test_same_track_cannot_confirm_two_different_support_slots():
    rows = [associate([cell()], group=1), associate([cell()], group=2),
            associate([cell(2)], point=[300, 170], group=3),
            associate([cell(2)], point=[300, 170], group=4)]
    for row in rows:
        row['track_id'] = 3
    result = targets.fuse_target_associations(rows)
    assert all(row['occupant_status'] == 'conflict' for row in result['targets'])
    assert not result['targets_ready']


def projected_cell():
    projected = cell()
    projected.update(candidate_only=True, projection_evidence=dict(
        source_rgb_sha256=hashlib.sha256(np.zeros((400, 500, 3), np.uint8).tobytes()).hexdigest(),
        actual_fit_supported=True, visible_fit_rms_px=2.2, inlier_count=4,
        actual_matched_data=[dict(face='D', row=1, col=1, source='actual_visible_quad')]))
    return projected


def test_unique_hidden_projection_is_candidate_never_owner():
    result = associate([projected_cell()])
    assert result['cell_index'] is None
    assert result['ownership_status'] == 'unknown'
    assert result['candidate_projection_complete']
    assert result['candidates'][0]['candidate_only'] is True
    assert result['candidates'][0]['adds_ownership_group'] is False
    assert result['candidates'][0]['projection_evidence']['visible_fit_rms_px'] == 2.2


def test_projection_from_other_rgb_is_rejected_not_used_for_elimination():
    projected = projected_cell()
    projected['projection_evidence']['source_rgb_sha256'] = 'otherframe'
    result = associate([projected])
    assert not result['candidates']
    assert not result['candidate_projection_complete']
    assert result['rejected_candidate_projections']


def test_projection_requires_actual_fit_support():
    projected = projected_cell()
    projected['projection_evidence']['actual_fit_supported'] = False
    result = associate([projected])
    assert not result['candidate_projection_complete']
    assert not result['candidates']


def test_direct_visible_cell_remains_valid_owner_with_distant_hidden_proposal():
    projected = projected_cell()
    projected.update(col=2)
    projected['quad'] = [[265, 200], [335, 200], [335, 270], [265, 270]]
    result = associate([cell(), projected])
    assert result['cell_index'] == 31
    assert result['candidates'][0]['candidate_only'] is False


def test_hidden_projection_cannot_become_confirmed_through_repeated_groups():
    rows = [associate([projected_cell()], group=1), associate([projected_cell()], group=2)]
    result = targets.fuse_target_associations(rows)
    assert not result['targets']
    assert len(result['unassociated_target_candidates']) == 2
    assert not result['targets_ready']


def test_fusion_cannot_upgrade_hidden_candidate_by_filling_its_slot():
    rows = [associate([projected_cell()], group=1), associate([projected_cell()], group=2)]
    for row in rows:
        row.update(cell_index=31, cell=dict(face='D', row=1, col=1))
    result = targets.fuse_target_associations(rows)
    assert not result['targets']
    assert not result['targets_ready']


def head_image_and_packet():
    import cv2
    rgb = np.zeros((400, 500, 3), np.uint8)
    cv2.circle(rgb, (200, 170), 11, (210, 70, 185), -1)
    target = dict(kind='player', point=[200., 170.], box=[189, 159, 22, 22],
                  circle_box=[189, 159, 22, 22], anchor_type='head', confidence=.82,
                  confirmable=True, source='deep_dive_local_pink_head')
    return rgb, target


def test_actual_head_can_propose_hidden_cell_but_never_direct_owner():
    rgb, target = head_image_and_packet()
    hidden = projected_cell()
    hidden['projection_evidence']['source_rgb_sha256'] = hashlib.sha256(rgb.tobytes()).hexdigest()
    rows = targets.associate_targets(rgb, [target], [cell(), hidden],
        source=source(1), view_group=1, scan_epoch='scan1')
    row = rows[0]
    assert row['anchor_point'] is None  # no foot/contact pixels visible
    assert row['cell_index'] is None
    assert len(row['candidates']) == 1
    assert row['candidates'][0]['candidate_only'] is True
    assert row['candidates'][0]['geometry']['projected_head_only'] is True
    assert row['candidates'][0]['geometry']['candidate_anchor_point'] == [200., 170.]


def test_head_packet_without_actual_pink_pixels_cannot_propose_hidden_cell():
    _, target = head_image_and_packet()
    rows = targets.associate_targets(np.zeros((400, 500, 3), np.uint8), [target], [projected_cell()],
        source=source(1), view_group=1, scan_epoch='scan1')
    assert not rows[0]['candidates']


def test_onnx_head_label_without_verified_local_circle_is_not_contact_candidate():
    rgb, target = head_image_and_packet()
    target['source'] = 'deep_dive_entity_onnx'
    hidden = projected_cell()
    hidden['projection_evidence']['source_rgb_sha256'] = hashlib.sha256(rgb.tobytes()).hexdigest()
    rows = targets.associate_targets(rgb, [target], [hidden],
        source=source(1), view_group=1, scan_epoch='scan1')
    assert not rows[0]['candidates']
