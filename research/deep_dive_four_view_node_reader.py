"""Pure experimental glyph reader for manual four-view crops.

The original classifier and all its acceptance gates remain unchanged. Input
normalization gives tight visible-panel crops more dark context. Expected
labels are never accepted by this API. Multiple accepted classes are a conflict,
not a confidence competition. This helper does not infer empty / unknown nodes
from absence and provides no face ownership or rotation arrival evidence.
"""
from __future__ import annotations

import hashlib
import math

import cv2
import numpy as np

from plans.resonance_pc.src.actions._deep_dive_layout_semantics import classify_icon


DEFAULT_SCALES = (1.0, 0.85, 0.70, 0.60)


def _normalized_input(crop_rgb: np.ndarray, scale: float) -> np.ndarray:
    if scale == 1.0:
        return np.ascontiguousarray(crop_rgb.copy())
    matrix = np.float32(((scale, 0.0, (96 - 1) * (1 - scale) / 2),
                         (0.0, scale, (96 - 1) * (1 - scale) / 2)))
    return cv2.warpAffine(crop_rgb, matrix, (96, 96),
                          flags=cv2.INTER_AREA, borderMode=cv2.BORDER_CONSTANT,
                          borderValue=(0, 0, 0))


def read_four_view_node(crop_rgb: np.ndarray, *, annotation_occluded: bool = False,
                        scales: tuple[float, ...] = DEFAULT_SCALES) -> dict:
    """Read one actual RGB crop, keeping every normalization's real input hash.

    ``annotation_occluded`` is reporting metadata only; it neither forces nor
    selects a class. A null result means recognition remains unconfirmed.
    Confidence uses the least strong agreeing accepted reading; it is heuristic
    evidence, not a calibrated probability or measured accuracy. Scaled inputs
    are correlated versions of one frame, not independent view witnesses. Null
    does not assert the game-specific white-frame unknown-node type.
    """
    rgb = np.asarray(crop_rgb)
    if rgb.shape != (96, 96, 3) or rgb.dtype != np.uint8:
        raise ValueError('expected actual 96x96 uint8 RGB crop')
    if type(annotation_occluded) is not bool:
        raise ValueError('annotation_occluded must be bool')
    if (not scales or len(scales) > 8 or
            any(isinstance(s, bool) or not isinstance(s, (int, float))
                or not math.isfinite(s) or not 0 < s <= 1 for s in scales)):
        raise ValueError('scales must contain 1 to 8 finite values in (0, 1]')
    records = []
    for scale in scales:
        normalized = _normalized_input(rgb, float(scale))
        reading = classify_icon(normalized)
        records.append(dict(scale=float(scale), input_shape=list(normalized.shape),
                            input_sha256=hashlib.sha256(normalized.tobytes()).hexdigest(),
                            reading=dict(reading)))
    accepted = [item['reading'] for item in records
                if item['reading'].get('icon_id') is not None]
    classes = sorted({item['icon_id'] for item in accepted})
    conflict = len(classes) > 1
    final = dict(icon_id=classes[0] if len(classes) == 1 else None,
                 confidence=min(float(item['confidence']) for item in accepted)
                 if len(classes) == 1 else 0.0)
    return dict(reading=final, normalization_readings=records,
                accepted_classes=classes, conflict=conflict,
                unknown_reason='conflicting_normalizations' if conflict else
                'no_confirmed_class' if not accepted else None,
                annotation_occluded=annotation_occluded,
                crop_sha256=hashlib.sha256(np.ascontiguousarray(rgb).tobytes()).hexdigest(),
                target_ownership_confirmed=False, rotation_arrival_confirmed=False)
