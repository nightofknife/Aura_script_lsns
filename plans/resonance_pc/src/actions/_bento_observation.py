"""Pure bento observations; callers retain capture, timeout and input policies."""
from __future__ import annotations

import math

import numpy as np


def work_meal_slot_state(hits, thresholds, *, require_finite=False):
    """Classify an already validated present/absent pair at its own thresholds.

    Recovery records raw confidence values; consumption additionally rejects
    non-finite scores. Keep that choice explicit at the observation boundary.
    """
    if require_finite:
        present, absent = [
            bool(hit.found) and math.isfinite(float(hit.confidence))
            and float(hit.confidence) >= threshold
            for hit, threshold in zip(hits, thresholds)
        ]
    else:
        present, absent = [
            hit.found and hit.confidence >= threshold
            for hit, threshold in zip(hits, thresholds)
        ]
    if present == absent:
        return "ambiguous_both" if present else "unrecognized_neither"
    return "present" if present else "absent"


def frames_unchanged(a, b):
    """Allow fewer than 1% changed pixels, with a per-channel tolerance of 15."""
    delta = np.max(np.abs(a.astype(np.int16) - b.astype(np.int16)), axis=2)
    return float(np.mean(delta > 15)) < 0.01


class ConsecutiveFrameStability:
    """Count consecutive unchanged comparisons without scheduling any captures.

    The first valid frame establishes a baseline, rather than a confirmation.
    A missing page or unloaded frame must reset the baseline at the caller.
    """

    def __init__(self, unchanged, *, previous=None):
        self.unchanged = unchanged
        self.previous = previous
        self.count = 0

    def observe(self, frame):
        self.count = (self.count + 1
                      if self.previous is not None and self.unchanged(self.previous, frame)
                      else 0)
        self.previous = frame
        return self.count

    def reset(self):
        self.previous = None
        self.count = 0
