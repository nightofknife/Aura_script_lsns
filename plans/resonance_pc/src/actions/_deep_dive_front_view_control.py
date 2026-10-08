"""Pure local face-normal control; no frame, input, or recognition authority.

Responses are camera-frame rotation vectors in radians per mouse pixel.
The caller owns measurement provenance, response learning and settled evidence.
"""

import math

import numpy as np


_NORMALS = dict(U=(0., -1., 0.), R=(1., 0., 0.), F=(0., 0., -1.),
                D=(0., 1., 0.), L=(-1., 0., 0.), B=(0., 0., 1.))


def _unit(value):
    return value / np.linalg.norm(value)


def _matrix(vector):
    angle = float(np.linalg.norm(vector))
    if angle < 1e-12:
        return np.eye(3)
    axis = vector / angle
    x, y, z = axis
    skew = np.array([[0., -z, y], [z, 0., -x], [-y, x, 0.]])
    return np.eye(3) + math.sin(angle)*skew + (1.-math.cos(angle))*(skew @ skew)


def _tangent(normal):
    # A camera-frame basis independent of the face's screen roll.
    seed = np.eye(3)[int(np.argmin(np.abs(normal)))]
    horizontal = _unit(np.cross(seed, normal))
    return np.column_stack((horizontal, np.cross(normal, horizontal)))


def _target(rotation, translation, center, tilt, azimuth):
    position = translation + rotation @ center
    if position[2] <= 0 or np.linalg.norm(position) < 1e-9:
        raise ValueError('invalid_face_position')
    ray = -_unit(position)
    # Use camera horizontal where possible, rather than face-local screen roll.
    horizontal = np.array([1., 0., 0.]) - ray[0]*ray
    if np.linalg.norm(horizontal) < 1e-8:
        horizontal = _tangent(ray)[:, 0]
    else:
        horizontal = _unit(horizontal)
    vertical = np.cross(ray, horizontal)
    target = math.cos(tilt)*ray + math.sin(tilt)*(
        math.cos(azimuth)*horizontal + math.sin(azimuth)*vertical)
    return target, ray


def _angle(left, right):
    return math.acos(float(np.clip(left @ right, -1., 1.)))


def choose_front_view(rotation, translation, face, response_axes, *,
                      face_center=None, tilt_deg=5., tilt_azimuth_deg=90.,
                      tolerance_deg=2., max_step_px=24., max_step_deg=6.,
                      max_condition=30., max_response_condition=30., damping_ratio=.05,
                      min_gain=1e-6, max_gain=.05, min_step_px=1.,
                      min_descent_deg=.02):
    """Return a bounded predicted normal-improving mouse displacement.

    ``face_center`` is an optional *body-space* physical face center. If absent,
    the camera ray uses the cube center; no world-unit cube size is assumed.
    ``aligned`` concerns this geometric objective only, never image readiness.
    A hidden face must first be exposed by a separate transfer planner.
    """
    result = dict(status='rejected', reason='invalid_input', direction=None,
                  displacement_px=[0., 0.], distance_px=0.)
    try:
        r = np.asarray(rotation, float)
        t = np.asarray(translation, float).reshape(3)
        normal = np.asarray(_NORMALS[face], float)
        center = np.zeros(3) if face_center is None else np.asarray(face_center, float).reshape(3)
        axes = np.column_stack([np.asarray(response_axes[i], float).reshape(3) for i in (0, 1)])
        config = np.asarray([tilt_deg, tilt_azimuth_deg, tolerance_deg, max_step_px,
                             max_step_deg, max_condition, max_response_condition, damping_ratio, min_gain,
                             max_gain, min_step_px, min_descent_deg], float)
        if (r.shape != (3, 3) or not all(np.isfinite(v).all() for v in (r, t, center, axes, config))
                or not np.allclose(r.T @ r, np.eye(3), atol=1e-5)
                or abs(np.linalg.det(r)-1.) > 1e-5):
            return result
        if not (0 <= tilt_deg <= 30 and 0 < tolerance_deg <= 30
                and 0 < min_step_px <= max_step_px and 0 < max_step_deg <= 30
                and max_condition >= 1 and max_response_condition >= 1 and damping_ratio >= 0
                and 0 < min_gain <= max_gain and min_descent_deg >= 0):
            result['reason'] = 'invalid_configuration'
            return result
        target, ray = _target(r, t, center, math.radians(tilt_deg), math.radians(tilt_azimuth_deg))
    except (ValueError, TypeError, KeyError, IndexError):
        return result
    n = r @ normal
    error = _angle(n, target)
    result.update(error_deg=math.degrees(error), normal=n.tolist(), target_normal=target.tolist())
    if n @ ray <= 0:
        result['reason'] = 'face_not_front_facing'
        return result
    if error <= math.radians(tolerance_deg):
        result.update(status='aligned', reason='normal_within_tolerance', predicted_error_deg=math.degrees(error))
        return result
    gains = np.linalg.norm(axes, axis=0)
    if np.any(gains < min_gain) or np.any(gains > max_gain):
        result['reason'] = 'invalid_response_gain'
        return result
    response_singular = np.linalg.svd(axes, compute_uv=False)
    response_rank = int(np.sum(response_singular > max(min_gain, response_singular[0]*1e-8)))
    response_condition = float(response_singular[0]/response_singular[-1]) if response_singular[-1] > 0 else None
    result.update(response_axes_rank=response_rank, response_axes_condition=response_condition)
    if response_rank < 2 or response_condition is None or response_condition > max_response_condition:
        result['reason'] = 'uncontrollable_response_axes'
        return result
    tangent = _tangent(n)
    jacobian = tangent.T @ np.column_stack([np.cross(axes[:, i], n) for i in (0, 1)])
    singular = np.linalg.svd(jacobian, compute_uv=False)
    rank = int(np.sum(singular > max(min_gain, singular[0]*1e-8)))
    condition = float(singular[0]/singular[-1]) if singular[-1] > 0 else None
    result.update(jacobian=jacobian.tolist(), jacobian_rank=rank, jacobian_condition=condition)
    reliable_ls = rank == 2 and condition is not None and condition <= max_condition
    result.update(normal_ls_used=reliable_ls,
                  normal_ls_reason='well_conditioned' if reliable_ls else 'normal_jacobian_rank_or_condition')
    proposals = []
    if reliable_ls:
        cross = np.cross(n, target)
        rot_error = cross*(error/np.linalg.norm(cross))
        desired = tangent.T @ np.cross(rot_error, n)
        damping = damping_ratio*singular[0]
        pixels = np.linalg.solve(jacobian.T @ jacobian + damping*damping*np.eye(2), jacobian.T @ desired)
        proposals.append(pixels)
    # Exact SO(3) predictions authorize descent; these bounded probes can rescue
    # a damped local linear proposal near a nonlinear geometry configuration.
    # A valid input axis parallel to n causes normal-Jacobian degeneracy, not
    # invalid input calibration. In that case skip LS and retain these exact
    # bounded axis candidates; only strict predicted descent authorizes motion.
    proposals.extend(np.eye(2)[i]*sign*max_step_px for i in (0, 1) for sign in (-1., 1.))
    options = []
    for proposal in proposals:
        length = float(np.linalg.norm(proposal))
        angle = float(np.linalg.norm(axes @ proposal))
        scale = min(1., max_step_px/max(length, 1e-12), math.radians(max_step_deg)/max(angle, 1e-12))
        for fraction in (1., .5, .25, .125):
            step = proposal*scale*fraction
            if np.linalg.norm(step) < min_step_px:
                continue
            movement = axes @ step
            predicted = _matrix(movement) @ r
            try:
                predicted_target, predicted_ray = _target(predicted, t, center,
                    math.radians(tilt_deg), math.radians(tilt_azimuth_deg))
            except ValueError:
                continue
            predicted_normal = predicted @ normal
            after = _angle(predicted_normal, predicted_target)
            if (predicted_normal @ predicted_ray > 0 and after < error
                    and error-after >= math.radians(min_descent_deg)):
                options.append((after, float(np.linalg.norm(step)), step, movement))
    if not options:
        result['reason'] = 'no_predicted_descent'
        return result
    after, length, step, movement = min(options, key=lambda row: (row[0], row[1]))
    result.update(status='move', reason='predicted_normal_descent',
                  direction=(step/length).tolist(), displacement_px=step.tolist(), distance_px=length,
                  predicted_error_deg=math.degrees(after), predicted_rotation_deg=math.degrees(np.linalg.norm(movement)))
    return result
