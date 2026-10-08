"""Conditional geometric certificates for a target's uncertain pixel centre.

This does not estimate model or pose accuracy.  The caller supplies projected
corridors and a documented pixel-radius assumption.  The proof is rigorous
only for that fixed geometry and that disk, not for unknown camera-pose errors.
"""
from __future__ import annotations

import math

import numpy as np


PIXEL_QUANTIZATION_RADIUS = math.sqrt(.5)


def certify_target_anchor(point, corridors, scales, winner_index, *, valid=None,
                          pixel_radius=PIXEL_QUANTIZATION_RADIUS):
    """Certify all 54 competitors over an entire disk without sampling angles.

    For normalized corridor distance e_i, its Lipschitz constant is 1/scale_i.
    Thus e_i + radius/scale_i bounds the winner's error above, while
    max(0, e_j-radius/scale_j) bounds each rival below.  Accept only when every
    possible pixel centre preserves the scanner's strict .30/.12 gates.

    ``valid`` may exclude only corridors whose geometry is actually invalid
    (e.g. behind the camera), not invisible faces or inconvenient competitors.
    Radius sqrt(.5) encloses a +/- .5 pixel quantization square.  A larger
    caller-supplied radius remains an assumption, not an estimated error bound.
    """
    rejected = dict(ready=False, reason='invalid_uncertainty_inputs')
    try:
        point = np.asarray(point, float)
        corridors = np.asarray(corridors, float)
        scales = np.asarray(scales, float)
        mask = np.ones(54, bool) if valid is None else np.asarray(valid)
        radius = float(pixel_radius)
    except (TypeError, ValueError, OverflowError):
        return rejected
    if (point.shape != (2,) or not np.isfinite(point).all()
            or corridors.ndim != 3 or corridors.shape[0] != 54
            or corridors.shape[1] < 1 or corridors.shape[2] != 2
            or scales.shape != (54,) or mask.shape != (54,)
            or mask.dtype.kind != 'b' or type(winner_index) is not int
            or not 0 <= winner_index < 54 or isinstance(pixel_radius, bool)
            or not math.isfinite(radius) or radius < PIXEL_QUANTIZATION_RADIUS
            or not mask[winner_index] or np.count_nonzero(mask) < 2
            or not np.isfinite(corridors[mask]).all()
            or not np.isfinite(scales[mask]).all() or np.any(scales[mask] <= 0)):
        return rejected

    indices = np.flatnonzero(mask)
    distances = np.linalg.norm(corridors[mask] - point, axis=2).min(axis=1)
    errors = np.full(54, np.inf)
    errors[indices] = distances / scales[mask]
    upper = float(errors[winner_index] + radius/scales[winner_index])
    lower = np.full(54, np.inf)
    lower[indices] = np.maximum(0., errors[indices] - radius/scales[indices])
    lower[winner_index] = np.inf
    competitor = int(np.argmin(lower))
    margin = float(lower[competitor] - upper)
    ready = upper < .30 and margin > .12
    return dict(ready=ready,
                reason='all54_disk_certified' if ready else 'pixel_disk_competitor_ambiguous',
                winner_index=winner_index, competitor_index=competitor,
                nominal_error=float(errors[winner_index]), winner_error_upper=upper,
                competitor_error_lower=float(lower[competitor]), margin_lower=margin,
                pixel_radius=radius, valid_corridors=int(np.count_nonzero(mask)),
                proof='fixed_geometry_lipschitz_disk',
                radius_assumption=('pixel_quantization_square'
                                   if radius == PIXEL_QUANTIZATION_RADIUS
                                   else 'caller_supplied_pixel_radius'))
