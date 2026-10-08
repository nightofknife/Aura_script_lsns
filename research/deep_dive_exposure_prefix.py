"""Pure bounded camera-navigation proposals for an unmeasured face hint.

Only the measured reference face supplies a camera ray and geometry. A target
body normal is a relative direction hint, never an unknown face pose or grid.
The caller owns actual source, grip, settle, feedback and total route budgets.
"""
import math

import numpy as np


def _unit(value):
    vector = np.asarray(value, float).reshape(3)
    length = float(np.linalg.norm(vector))
    if not np.isfinite(vector).all() or not math.isfinite(length) or length < 1e-9:
        raise ValueError('invalid_vector')
    return vector/length


def _exp(vector):
    angle = float(np.linalg.norm(vector))
    if angle < 1e-12:
        return np.eye(3)
    x, y, z = vector/angle
    skew = np.array(((0., -z, y), (z, 0., -x), (-y, x, 0.)))
    return np.eye(3)+math.sin(angle)*skew+(1.-math.cos(angle))*(skew @ skew)


def choose_exposure_prefix(reference_rotation, reference_camera_ray,
                           reference_body_normal, target_body_normal_hint,
                           response_axes, *, max_step_px=24., max_step_deg=5.,
                           min_reference_cosine=.20, exposure_hint_cosine=.60,
                           min_step_px=1., min_cosine_improvement=1e-5,
                           max_response_condition=30.):
    """Choose a finite exact SO(3) hint-improving step, including from behind.

    Response axes are camera-space rotation vectors in radians/mouse pixel.
    The measured reference camera ray stays fixed for this local prediction;
    no unknown T, face center or pixel projection is created. Thresholds/caps
    are research controls. ``exposure_hint_ready`` requires pixel discovery,
    and grants no actual visibility, identity, grid, or target readiness.
    """
    result = dict(status='rejected', reason='invalid_input', displacement_px=[0., 0.],
        direction=None, distance_px=0., geometry_proposal_only=True,
        unknown_face_projection_valid=False, actual_grid_ready=False,
        identity_ready=False, targets_ready=False, full_cube_coordinates_valid=False,
        current_source_validated=False, total_route_budget_owned_by_caller=True)
    try:
        rotation = np.asarray(reference_rotation, float)
        ray = _unit(reference_camera_ray)
        reference = _unit(reference_body_normal)
        target = _unit(target_body_normal_hint)
        axes = np.column_stack([np.asarray(response_axes[i], float).reshape(3) for i in (0, 1)])
        config = np.asarray((max_step_px, max_step_deg, min_reference_cosine,
            exposure_hint_cosine, min_step_px, min_cosine_improvement, max_response_condition), float)
        if (rotation.shape != (3, 3) or not all(np.isfinite(value).all() for value in (rotation, axes, config))
                or not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-5)
                or abs(np.linalg.det(rotation)-1.) > 1e-5):
            return result
        if not (0 < min_step_px <= max_step_px <= 24. and 0 < max_step_deg <= 5.
                and 0 < min_reference_cosine < 1. and 0 < exposure_hint_cosine <= 1.
                and 0 < min_cosine_improvement < 1. and 1. <= max_response_condition <= 30.):
            result['reason'] = 'invalid_configuration'
            return result
    except (ValueError, TypeError, KeyError, IndexError):
        return result
    reference_cosine = float((rotation @ reference) @ ray)
    target_cosine = float((rotation @ target) @ ray)
    result.update(reference_cosine=reference_cosine, target_hint_cosine=target_cosine,
        reference_margin=reference_cosine-min_reference_cosine,
        reference_camera_ray=ray.tolist(), target_hint_normal_camera=(rotation @ target).tolist())
    gains = np.linalg.norm(axes, axis=0)
    if np.any(gains < 1e-6) or np.any(gains > .05):
        result['reason'] = 'invalid_response_gain'
        return result
    singular = np.linalg.svd(axes, compute_uv=False)
    rank = int(np.sum(singular > max(1e-6, singular[0]*1e-8)))
    condition = float(singular[0]/singular[-1]) if singular[-1] > 0 else None
    result.update(response_axes_rank=rank, response_axes_condition=condition)
    if rank < 2 or condition is None or condition > max_response_condition:
        result['reason'] = 'uncontrollable_response_axes'
        return result
    if reference_cosine < min_reference_cosine:
        result['reason'] = 'reference_visibility_margin_insufficient'
        return result
    if target_cosine >= exposure_hint_cosine:
        result.update(status='exposure_hint_ready', reason='relative_hint_requires_independent_pixel_discovery',
            predicted_target_hint_cosine=target_cosine, predicted_reference_cosine=reference_cosine,
            predicted_reference_margin=reference_cosine-min_reference_cosine,
            predicted_rotation_deg=0.)
        return result
    candidates = [axis*sign*max_step_px for axis in np.eye(2) for sign in (-1., 1.)]
    candidates.extend(np.array((sx, sy))*max_step_px/math.sqrt(2.)
                      for sx in (-1., 1.) for sy in (-1., 1.))
    options = []
    for candidate in candidates:
        angle = float(np.linalg.norm(axes @ candidate))
        scale = min(1., math.radians(max_step_deg)/max(angle, 1e-12))
        for fraction in (1., .5, .25, .125):
            step = candidate*scale*fraction
            distance = float(np.linalg.norm(step))
            if distance < min_step_px:
                continue
            movement = axes @ step
            predicted = _exp(movement) @ rotation
            after_reference = float((predicted @ reference) @ ray)
            after_target = float((predicted @ target) @ ray)
            if (after_reference >= min_reference_cosine
                    and after_target-target_cosine >= min_cosine_improvement):
                options.append((after_target, after_reference, -distance, step, movement))
    result['finite_candidates_accepted'] = len(options)
    if not options:
        result['reason'] = 'no_safe_hint_improving_step'
        return result
    after_target, after_reference, _, step, movement = max(options, key=lambda value:value[:3])
    distance = float(np.linalg.norm(step))
    result.update(status='move', reason='bounded_exact_SO3_relative_hint_improvement',
        displacement_px=step.tolist(), direction=(step/distance).tolist(), distance_px=distance,
        predicted_target_hint_cosine=after_target, predicted_reference_cosine=after_reference,
        predicted_reference_margin=after_reference-min_reference_cosine,
        predicted_rotation_deg=math.degrees(float(np.linalg.norm(movement))))
    return result
