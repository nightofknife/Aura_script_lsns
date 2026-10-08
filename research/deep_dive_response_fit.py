"""Research-only local camera-space SO(3) response fit; no runtime or input.

Each record is {dx, dy, rotation_delta: [rx, ry, rz]} in pixels/radians.
The caller must establish actual released/settled motion, common camera basis,
capture/source ownership and context. This numerical fit cannot establish them.
No main-axis division, previous-axis prior or synthetic proof is used.
"""
from collections.abc import Mapping
import math

import numpy as np


def fit_response_axes(records, *, previous_axes=None, max_records=8,
                      min_input_px=2., min_rotation_deg=.3,
                      max_rotation_deg=12., max_condition=30., max_gain=.05,
                      recency_decay=.65, max_residual_deg=1.,
                      max_relative_residual=.25, max_point_residual_deg=3.,
                      min_gain=1e-6):
    """Return accepted, axes ({0:x,1:y}, rad/px), matrix (3x2), diagnostics.

    Newest ``max_records`` input positions have exponentially larger weight.
    Invalid records in that window are excluded and explicitly reported; at
    least two independent valid directions are still required. A rejection
    returns axes/matrix=None and never promotes previous_axes as a new fit.
    Residual gates assess a locally constant response, not unpredictable FPS.
    ``rotation_unit`` may explicitly be 'radians'/'rad'; another unit is rejected.
    Without a unit marker radians are the API contract, not inferred from pixels.
    """
    diag = dict(rejected_records=[], valid_records=0, input_records=0,
                units='radians_per_pixel', evidence_kind='numerical_research_only')

    def result(reason, *, axes=None, matrix=None):
        return dict(accepted=axes is not None, rejected=axes is None,
                    reason=reason, axes=axes, matrix=matrix, diagnostics=diag)

    parameters = (min_input_px, min_rotation_deg, max_rotation_deg,
                  max_condition, max_gain, recency_decay, max_residual_deg,
                  max_relative_residual, max_point_residual_deg, min_gain)
    if (type(max_records) is not int or max_records < 2 or
            any(isinstance(p, bool) or not isinstance(p, (int, float)) or
                not math.isfinite(p) or p <= 0 for p in parameters) or
            recency_decay > 1 or min_rotation_deg >= max_rotation_deg or
            max_condition < 1 or min_gain >= max_gain):
        return result('invalid_parameters')
    try:
        records = list(records)
    except (TypeError, ValueError):
        return result('invalid_records')
    diag['input_records'] = len(records)
    selected = records[-max_records:]
    inputs, rotations, weights, indices = [], [], [], []
    for offset, record in enumerate(selected):
        index = len(records)-len(selected)+offset
        why = None
        try:
            if not isinstance(record, Mapping):
                raise ValueError('record_not_mapping')
            if record.get('rotation_unit', 'radians') not in ('radians', 'rad'):
                raise ValueError('rotation_unit_not_radians')
            scalars = (record['dx'], record['dy'])
            if any(isinstance(v, bool) or not isinstance(v, (int, float, np.number))
                   for v in scalars):
                raise ValueError('invalid_displacement')
            displacement = np.asarray(scalars, dtype=float)
            raw_rotation = np.asarray(record['rotation_delta'])
            if raw_rotation.shape != (3,) or raw_rotation.dtype.kind not in 'fiu':
                raise ValueError('invalid_rotation_shape_or_type')
            rotation = raw_rotation.astype(float)
            if not np.isfinite(displacement).all() or not np.isfinite(rotation).all():
                raise ValueError('nonfinite_measurement')
            if np.linalg.norm(displacement) < min_input_px:
                raise ValueError('input_too_small')
            angle = math.degrees(float(np.linalg.norm(rotation)))
            if angle < min_rotation_deg:
                raise ValueError('rotation_too_small')
            if angle > max_rotation_deg:
                raise ValueError('rotation_too_large')
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            why = str(exc)
        if why is not None:
            diag['rejected_records'].append(dict(index=index, reason=why))
            continue
        inputs.append(displacement)
        rotations.append(rotation)
        weights.append(recency_decay ** (len(selected)-1-offset))
        indices.append(index)
    diag.update(valid_records=len(inputs), used_indices=indices, weights=weights)
    if len(inputs) < 2:
        return result('independent_measurements_missing')
    x, y = np.asarray(inputs), np.asarray(rotations)
    w = np.asarray(weights)
    root_w = np.sqrt(w)
    weighted_x, weighted_y = x*root_w[:, None], y*root_w[:, None]
    try:
        singular = np.linalg.svd(weighted_x, compute_uv=False)
        rank = int(np.linalg.matrix_rank(weighted_x))
        condition = float(singular[0]/singular[-1]) if singular[-1] > 0 else math.inf
        diag.update(design_rank=rank, design_condition=condition,
                    design_singular_values=singular.tolist())
        if rank < 2:
            return result('independent_directions_missing')
        if condition > max_condition:
            return result('design_ill_conditioned')
        coefficients = np.linalg.lstsq(weighted_x, weighted_y, rcond=None)[0]
        matrix = coefficients.T
        if not np.isfinite(matrix).all():
            return result('nonfinite_fit')
        gains = np.linalg.norm(matrix, axis=0)
        operator_gain = float(np.linalg.norm(matrix, ord=2))
        diag.update(axis_gains=gains.tolist(), operator_gain=operator_gain)
        if min(gains) < min_gain or operator_gain > max_gain:
            return result('gain_out_of_bounds')
        response_singular = np.linalg.svd(matrix, compute_uv=False)
        response_rank = int(np.linalg.matrix_rank(matrix))
        response_condition = (float(response_singular[0]/response_singular[-1])
                              if response_singular[-1] > 0 else math.inf)
        diag.update(response_rank=response_rank, response_condition=response_condition,
                    residual_degrees_of_freedom=len(inputs)-2)
        if response_rank < 2 or response_condition > max_condition:
            return result('response_axes_not_independent')
        errors = np.linalg.norm(y-x@coefficients, axis=1)
        rms = float(np.sqrt(np.average(errors**2, weights=w)))
        signal = float(np.sqrt(np.average(np.sum(y*y, axis=1), weights=w)))
        relative = rms/signal
        diag.update(residual_deg=np.degrees(errors).tolist(),
                    weighted_rms_deg=math.degrees(rms), relative_residual=relative,
                    max_point_residual_deg=math.degrees(float(max(errors))))
        if (math.degrees(rms) > max_residual_deg or relative > max_relative_residual or
                math.degrees(float(max(errors))) > max_point_residual_deg):
            return result('response_inconsistent')
    except np.linalg.LinAlgError:
        return result('linear_algebra_failed')
    if previous_axes is not None:
        try:
            previous = (np.column_stack([previous_axes.get(i, previous_axes.get(str(i)))
                        for i in (0, 1)]) if isinstance(previous_axes, Mapping)
                        else np.asarray(previous_axes, float))
            if previous.shape != (3, 2) or not np.isfinite(previous).all():
                raise ValueError('invalid_previous_axes')
            diag['previous_delta'] = float(np.linalg.norm(matrix-previous))
        except (TypeError, ValueError):
            diag['previous_axes_ignored'] = True
    return result('local_response_fit', axes={0: matrix[:, 0].tolist(),
                  1: matrix[:, 1].tolist()}, matrix=matrix.tolist())
