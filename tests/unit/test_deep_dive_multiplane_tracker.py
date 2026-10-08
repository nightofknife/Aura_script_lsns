"""Actual LK on synthetic textured planes; no game or model execution."""
import cv2
import numpy as np
import pytest

from research.deep_dive_multiplane_tracker import MultiPlaneTracker
from plans.resonance_pc.src.actions._deep_dive_layout_vision import BASES, DISTANCE, K, _point


def source(index, **changes):
    return dict(session_id=7, map_revision=0, generation=index,
                frame_time=100.+index*.1, **changes)


def scene(shifts=None, hide=(), rotations=None):
    shifts, rotations = shifts or {}, rotations or {}
    rotation = cv2.Rodrigues(np.array([.65, .7, .05]))[0]
    translation = np.array([[0.], [0.], [45.]])
    image = np.zeros((720, 1280, 3), np.uint8)
    objects, pixels = [], []
    for face in ('U', 'R', 'F'):
        normal, right, down = map(np.asarray, BASES[face])
        xyz = np.array([normal*DISTANCE+right*x+down*y
                       for x in np.linspace(-1.2, 1.2, 8) for y in np.linspace(-1.2, 1.2, 8)])
        rv = cv2.Rodrigues(rotations.get(face, rotation))[0]
        tv = translation+np.asarray(shifts.get(face, [0., 0., 0.])).reshape(3, 1)
        projected = cv2.projectPoints(xyz, rv, tv, K, None)[0].reshape(-1, 2)
        objects.extend(xyz)
        pixels.extend(projected)
        if face not in hide:
            for index, point in enumerate(projected):
                center = tuple(np.rint(point).astype(int))
                cv2.rectangle(image, (center[0]-3, center[1]-3), (center[0]+3, center[1]+3),
                              (100+index%5*30,)*3, -1)
                cv2.line(image, (center[0]-2, center[1]), (center[0]+2, center[1]), (255,)*3, 1)
    return image, np.asarray(objects), np.asarray(pixels, np.float32), rotation, translation


def tracker():
    rgb, objects, pixels, rotation, translation = scene()
    value = MultiPlaneTracker(reseed=False)
    initial = value.bootstrap(rgb, source(1), objects, pixels, rotation, translation)
    assert set(initial['shared_support_faces']) == {'U', 'R', 'F'}, initial
    return value


def test_independent_clouds_track_distinct_plane_translations():
    value = tracker()
    rgb, *_ = scene(shifts={'U': [0., .025, 0.], 'R': [-.015, -.02, 0.], 'F': [.015, -.02, 0.]})
    actual = value.update(rgb, source(2))
    assert set(actual['shared_support_faces']) == {'U', 'R', 'F'}, actual
    assert actual['status'] == 'geometry_only'
    for face, row in actual['faces'].items():
        assert row['current']
        assert row['source']['generation'] == 2
        assert len(row['indexed_features']) >= 25
    assert len(value.visible('R')) == 9
    assert all(row['face'] == 'R' and row['source']['generation'] == 2 for row in value.visible('R'))


def test_missing_face_retains_history_without_current_projection():
    value = tracker()
    rgb, *_ = scene(hide=('R',))
    actual = value.update(rgb, source(2))
    assert not actual['faces']['R']['current']
    assert actual['faces']['R']['history_source']['generation'] == 1
    assert value.visible('R') == []
    assert actual['faces']['U']['current']


def test_returning_face_can_reuse_its_own_last_good_image_cloud():
    value = tracker()
    rgb, *_ = scene(hide=('R',))
    value.update(rgb, source(2))
    rgb, *_ = scene()
    actual = value.update(rgb, source(3))
    assert actual['faces']['R']['current']
    assert actual['faces']['R']['source']['generation'] == 3


def test_duplicate_source_revokes_current_projections_and_context_change_clears_bank():
    value = tracker()
    rgb, *_ = scene()
    with pytest.raises(ValueError, match='source_not_advanced'):
        value.update(rgb, source(1))
    assert value.visible('U') == []
    changed = source(2)
    changed['map_revision'] = 1
    with pytest.raises(ValueError, match='context_changed'):
        value.update(rgb, changed)
    assert value.snapshot()['faces'] == {}


def test_snapshot_isolated_and_no_entity_or_unseen_face_authority():
    value = tracker()
    first = value.snapshot()
    first['faces']['U']['rotation'][0][0] = 999
    actual = value.snapshot()
    assert actual['faces']['U']['rotation'][0][0] != 999
    assert actual['shared_rotation_method'] == 'one_actual_face_no_averaging'
    assert not actual['unseen_face_discovery']
    assert not actual['full_cube_coordinates_valid']
    assert not actual['target_evidence']


def test_current_orientation_conflict_cannot_be_hidden_by_averaging():
    rgb, objects, pixels, rotation, translation = scene()
    changed = cv2.Rodrigues(np.array([.06, 0., 0.]))[0]@rotation
    rgb, objects, pixels, _, _ = scene(rotations={'U': changed})
    value = MultiPlaneTracker(reseed=False, orientation_conflict_deg=1.)
    actual = value.bootstrap(rgb, source(1), objects, pixels, rotation, translation)
    assert actual['status'] == 'orientation_conflict', actual
    assert actual['shared_rotation'] is None
    assert value.visible('U') == []


@pytest.mark.parametrize('invalid', [dict(session_id=7, map_revision=0, generation=None, frame_time=100.2),
                                   dict(session_id=7, map_revision=0, generation=2, frame_time=float('nan'))])
def test_malformed_new_source_does_not_leave_old_face_marked_current(invalid):
    value = tracker()
    rgb, *_ = scene()
    with pytest.raises(ValueError):
        value.update(rgb, invalid)
    assert value.visible('U') == []
    assert not value.snapshot()['faces']['U']['current']


def test_default_local_reseeding_preserves_current_single_face_projection_authority():
    rgb, objects, pixels, rotation, translation = scene()
    value = MultiPlaneTracker(max_features_per_face=100)
    initial = value.bootstrap(rgb, source(1), objects, pixels, rotation, translation)
    assert set(initial['shared_support_faces']) == {'U', 'R', 'F'}
    rgb, *_ = scene(shifts={'U': [0., .025, 0.], 'R': [-.015, -.02, 0.], 'F': [.015, -.02, 0.]})
    current = value.update(rgb, source(2))
    assert set(current['shared_support_faces']) == {'U', 'R', 'F'}, current
    for face, row in current['faces'].items():
        assert 25 <= row['next_feature_count'] <= 100
        assert row['keyframe_count'] <= 2
        assert row['source']['generation'] == 2
        assert all(item['face'] == face for item in value.visible(face))


def test_opt_in_retention_keeps_initial_body_bindings_and_respects_feature_cap():
    rgb, objects, pixels, rotation, translation = scene()
    value = MultiPlaneTracker(retain_bindings=True, refill_below=200, max_features_per_face=100)
    initial = value.bootstrap(rgb, source(1), objects, pixels, rotation, translation)
    for face, local in initial['faces'].items():
        retained = np.asarray([pair['object'] for pair in local['indexed_features']])
        bank = value._banks[face]
        assert np.array_equal(bank['objects'][:len(retained)], retained)
        assert len(bank['objects']) <= 100
    rgb, *_ = scene(shifts={'U': [0., .025, 0.], 'R': [-.015, -.02, 0.], 'F': [.015, -.02, 0.]})
    updated = value.update(rgb, source(2))
    assert set(updated['shared_support_faces']) == {'U', 'R', 'F'}
    for face, local in updated['faces'].items():
        retained = np.asarray([pair['object'] for pair in local['indexed_features']])
        assert np.array_equal(value._banks[face]['objects'][:len(retained)], retained)
        assert len(value._banks[face]['objects']) <= 100


def direct_tracker():
    rgb, objects, pixels, rotation, translation = scene()
    value = MultiPlaneTracker(retain_bindings=True, direct_check_interval=2)
    value.bootstrap(rgb, source(1), objects, pixels, rotation, translation)
    value.update(rgb, source(2))
    value.update(rgb, source(3))
    return value, rgb, objects, pixels, rotation, translation


def test_legal_direct_pairs_choose_actual_current_pose_with_original_keyframe_source():
    value, rgb, *_ = direct_tracker()
    updated = value.update(rgb, source(4))
    assert set(updated['direct_checks']) == {'U', 'R', 'F'}
    for face, report in updated['direct_checks'].items():
        assert report['selected']
        assert report['reason'] == 'direct_all_original_gates_passed'
        assert report['current_source'] == source(4)
        assert report['keyframe_source'] == source(1)
        assert updated['faces'][face]['source'] == source(4)
        assert updated['faces'][face]['fit'] == report['direct_fit']
        assert report['direct_fit']['fraction'] > .75


def test_rejected_direct_image_never_promotes_historical_source_as_current():
    value, rgb, *_ = direct_tracker()
    # Simulate an unusable/corrupt saved keyframe image while keeping the real
    # current chained RGB intact. Its object labels must not become fresh proof.
    for bank in value._banks.values():
        bank['keyframes'][-1]['gray'] = np.zeros((720, 1280), np.uint8)
    updated = value.update(rgb, source(4))
    for face, report in updated['direct_checks'].items():
        assert not report['selected']
        assert report['reason'] != 'direct_all_original_gates_passed'
        assert report['keyframe_source'] == source(1)
        assert updated['faces'][face]['current']
        assert updated['faces'][face]['source'] == source(4)
        assert updated['faces'][face]['fit'] == report['chained_fit']


def test_context_change_resets_periodic_counters_and_revokes_current_projection():
    value, rgb, objects, pixels, rotation, translation = direct_tracker()
    assert value.snapshot()['current_measurement_counts']['U'] == 3
    changed = source(4)
    changed['map_revision'] = 1
    with pytest.raises(ValueError, match='context_changed'):
        value.update(rgb, changed)
    assert value.snapshot()['current_measurement_counts'] == {}
    assert value.snapshot()['direct_checks'] == {}
    assert value.visible('U') == []
    boot = value.bootstrap(rgb, changed, objects, pixels, rotation, translation)
    assert all(count == 1 for count in boot['current_measurement_counts'].values())
    assert boot['direct_checks'] == {}


def test_duplicate_source_revokes_direct_diagnostics_without_consuming_periodic_count():
    value, rgb, *_ = direct_tracker()
    actual = value.update(rgb, source(4))
    assert actual['direct_checks']
    with pytest.raises(ValueError, match='source_not_advanced'):
        value.update(rgb, source(4))
    rejected = value.snapshot()
    assert rejected['direct_checks'] == {}
    assert rejected['current_measurement_counts'] == actual['current_measurement_counts']
    assert value.visible('U') == []


def conflict_tracker():
    _, _, _, rotation, translation = scene()
    changed = cv2.Rodrigues(np.array([.06, 0., 0.]))[0]@rotation
    rgb, objects, pixels, _, _ = scene(rotations={'U': changed}, shifts={'U': [.04, -.025, .1]})
    value = MultiPlaneTracker(reseed=False, orientation_conflict_deg=1.)
    actual = value.bootstrap(rgb, source(1), objects, pixels, rotation, translation)
    assert actual['status'] == 'orientation_conflict'
    return value, rgb


def test_global_conflict_revokes_global_quads_but_explicit_local_uses_own_pose_and_source():
    value, _ = conflict_tracker()
    snapshot = value.snapshot()
    assert snapshot['shared_rotation'] is None
    assert not snapshot['full_cube_coordinates_valid']
    assert not snapshot['target_evidence']
    for face in ('U', 'R', 'F'):
        assert value.visible(face) == []
        local = value.local_visible(face)
        assert len(local) == 9
        # Compare to the physical synthetic pose, not the helper's projection
        # implementation. U deliberately has independent R and T here.
        true_rotation = cv2.Rodrigues(np.array([.65, .7, .05]))[0]
        true_translation = np.array([[0.], [0.], [45.]])
        if face == 'U':
            true_rotation = cv2.Rodrigues(np.array([.06, 0., 0.]))[0]@true_rotation
            true_translation += np.array([[.04], [-.025], [.1]])
        objects = np.asarray([_point(dict(face=face, row=r, col=c)) for r in range(3) for c in range(3)])
        expected = cv2.projectPoints(objects, cv2.Rodrigues(true_rotation)[0], true_translation, K, None)[0].reshape(-1, 2)
        assert np.allclose([row['centre'] for row in local], expected, atol=1e-4)
        assert all(row['face'] == face and row['source'] == source(1) for row in local)
        assert all(not row['shared_orientation_valid'] for row in local)
        assert all(row['projection_kind'] == 'local_face_geometry_shared_orientation_unknown' for row in local)
        assert all(not row['full_cube_coordinates_valid'] and not row['target_evidence'] for row in local)
    u = snapshot['faces']['U']
    wrong = value._projection('R', np.asarray(snapshot['faces']['R']['rotation']), np.asarray(u['translation']).reshape(3, 1))
    assert not np.allclose([row['centre'] for row in value.local_visible('R')], [row['centre'] for row in wrong], atol=.01)
    assert value.local_visible('D') == []


def test_local_navigation_does_not_refresh_unadvanced_or_changed_source():
    value, rgb = conflict_tracker()
    assert value.local_visible('U')
    with pytest.raises(ValueError, match='source_not_advanced'):
        value.update(rgb, source(1))
    assert all(value.local_visible(face) == [] for face in ('U', 'R', 'F'))
    changed = source(2)
    changed['map_revision'] = 1
    with pytest.raises(ValueError, match='context_changed'):
        value.update(rgb, changed)
    assert value.snapshot()['faces'] == {}
    assert all(value.local_visible(face) == [] for face in ('U', 'R', 'F'))
