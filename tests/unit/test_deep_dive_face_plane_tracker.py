"""Synthetic physical planar fixtures, independent face translations."""
import cv2
import numpy as np
import pytest

from research.deep_dive_face_plane_tracker import fit_face_plane, FacePlaneScanner
from plans.resonance_pc.src.actions._deep_dive_layout_vision import BASES, DISTANCE, K, _angle


def fixture(face='U', count=7, vector=(.45, .2, .08), shift=(0., 0., 0.)):
    normal, right, down = map(np.asarray, BASES[face])
    objects = np.array([normal*DISTANCE+right*x+down*y
                       for x in np.linspace(-1.2, 1.2, count)
                       for y in np.linspace(-1.2, 1.2, count)])
    # U is visible under an approximately quarter-turn from the body seed.
    rv = np.asarray(vector, float).reshape(3, 1)
    if face == 'U':
        rv = np.array([[1.15], [.20], [.08]])
    rotation = cv2.Rodrigues(rv)[0]
    tv = np.array([[.1], [-.2], [35.]])+np.asarray(shift).reshape(3, 1)
    pixels = cv2.projectPoints(objects, rv, tv, K, None)[0].reshape(-1, 2)
    return objects, pixels, rotation, tv


def test_face_translation_absorbs_breathing_without_false_rotation():
    objects, pixels, true_rotation, true_translation = fixture(shift=(.05, -.07, .2))
    old_translation = true_translation-np.array([[.05], [-.07], [.2]])
    result = fit_face_plane(objects, pixels, 'U', true_rotation, old_translation)
    assert result['success'], result
    assert _angle(np.asarray(result['rotation']), true_rotation) < .01
    assert np.allclose(result['translation'], true_translation.ravel(), atol=1e-5)
    assert np.allclose(result['translation_delta_camera'], [.05, -.07, .2], atol=1e-5)


def test_ransac_rejects_ten_large_mismatches_with_real_support():
    objects, pixels, rotation, tv = fixture()
    pixels = pixels.copy()
    pixels[:10] += [50., -40.]
    result = fit_face_plane(objects, pixels, 'U', rotation, tv)
    assert result['success'], result
    assert result['inliers'] == 39
    assert result['fraction'] > .75
    assert result['max_error_px'] <= 2.


def test_insufficient_support_or_local_patch_cannot_fit():
    objects, pixels, rotation, tv = fixture()
    assert not fit_face_plane(objects[:24], pixels[:24], 'U', rotation, tv)['success']
    assert not fit_face_plane(objects, pixels*.1, 'U', rotation, tv)['success']
    assert not fit_face_plane(objects, pixels, 'F', rotation, tv)['success']


def test_real_pose_jump_and_unreasonable_depth_are_rejected():
    objects, pixels, rotation, tv = fixture()
    assert not fit_face_plane(objects, pixels, 'U', np.eye(3), tv, limit=10.)['success']
    assert not fit_face_plane(objects, pixels, 'U', rotation, tv*.2)['success']


def test_adapter_keeps_original_body_objects_and_marks_geometry_only():
    objects, pixels, rotation, tv = fixture()
    scanner = FacePlaneScanner(preferred_face='U')
    scanner.rotation, scanner.rvec, scanner.tvec = rotation, cv2.Rodrigues(rotation)[0], tv
    assert scanner._fit_pose(objects, pixels)
    assert np.array_equal(scanner.objects, objects)
    assert scanner.face_plane_diagnostic['selected_face'] == 'U'
    assert not scanner.face_plane_diagnostic['full_cube_coordinates_valid']
    assert not scanner.face_plane_diagnostic['target_evidence']
    scanner.ready = True
    visible = scanner.visible()
    assert visible
    assert all(scanner.cells[item['index']]['face'] == 'U' for item in visible)
    # A later failed fit must not reauthorize other faces with the old face T.
    assert not scanner._fit_pose(objects[:20], pixels[:20])
    assert all(scanner.cells[item['index']]['face'] == 'U' for item in scanner.visible())


def test_ambiguous_distinct_planar_branches_are_not_selected_by_tie():
    objects, pixels, rotation, tv = fixture(face='F', vector=(.015, .008, .002))
    tv = np.array([[0.], [0.], [35.]])
    pixels = cv2.projectPoints(objects, cv2.Rodrigues(rotation)[0], tv, K, None)[0].reshape(-1, 2)
    solved = cv2.solvePnPGeneric(objects, pixels, K, None, flags=cv2.SOLVEPNP_IPPE)
    first, second = [cv2.Rodrigues(rv)[0] for rv in solved[1]]
    delta = cv2.Rodrigues(second@first.T)[0]
    midpoint = cv2.Rodrigues(delta*.5)[0]@first
    result = fit_face_plane(objects, pixels, 'F', midpoint, tv)
    assert not result['success'], result
    assert result['reason'] == 'ambiguous_planar_branches'


@pytest.mark.parametrize('face', list(BASES))
@pytest.mark.parametrize('floating_normal_error', [0., 1e-14])
def test_all_offset_faces_and_machine_precision_plane_noise_preserve_physical_pose(face, floating_normal_error):
    normal, right, down = map(lambda value:np.asarray(value, float), BASES[face])
    towards_camera = np.array([0., 0., -1.])
    cross = np.cross(normal, towards_camera)
    cosine = float(normal@towards_camera)
    if cosine > .999:
        align = np.eye(3)
    elif cosine < -.999:
        align = cv2.Rodrigues(np.array([np.pi, 0., 0.]))[0]
    else:
        align = cv2.Rodrigues(cross/np.linalg.norm(cross)*np.arccos(cosine))[0]
    rotation = cv2.Rodrigues(np.array([.25, -.18, .12]))[0]@align
    objects = np.array([normal*DISTANCE+right*x+down*y
        for x in np.linspace(-1.2, 1.2, 7) for y in np.linspace(-1.2, 1.2, 7)])
    # Original optical-flow object coordinates remain body-space offset planes.
    # Tiny normal noise mimics ray/plane floating cancellation during reseeding.
    objects += np.random.default_rng(42).normal(size=(len(objects), 1))*floating_normal_error*normal
    tv = np.array([[.13], [-.17], [35.]])
    pixels = cv2.projectPoints(objects, cv2.Rodrigues(rotation)[0], tv, K, None)[0].reshape(-1, 2)
    result = fit_face_plane(objects, pixels, face, rotation, tv)
    assert result['success'], result
    assert result['solver_coordinates'] == 'explicit_face_plane_uv0'
    assert result['inliers'] == len(objects)
    assert result['max_error_px'] < 1e-5
    assert _angle(np.asarray(result['rotation']), rotation) < 1e-4
    assert np.allclose(result['translation'], tv.ravel(), atol=1e-5)
