"""Route-specific scan protection and the independent post-scan reader budget."""
from __future__ import annotations

import math
from numbers import Real


SCAN_ROUTES = ('cells', 'faces', 'mixed', 'vertices', 'target_cells',
               'target_faces', 'target_framed', 'four_views')
LEGACY_SCAN_DEFAULT_SEC = 60.
LEGACY_SCAN_MAX_SEC = 90.
FOUR_VIEW_SCAN_DEFAULT_SEC = 300.
FOUR_VIEW_SCAN_MAX_SEC = 900.
POST_SCAN_RECOGNITION_MAX_SEC = 90.


def normalize_scan_route(scan_route):
    if not isinstance(scan_route, str) or scan_route not in SCAN_ROUTES:
        raise ValueError('Unknown scan_route')
    return scan_route


def resolve_scan_budget(scan_route, time_budget_sec=None):
    """Keep legacy routes capped at 90 seconds without capping four views."""
    route = normalize_scan_route(scan_route)
    four_views = route == 'four_views'
    if time_budget_sec is None:
        return FOUR_VIEW_SCAN_DEFAULT_SEC if four_views else LEGACY_SCAN_DEFAULT_SEC
    if (isinstance(time_budget_sec, bool) or not isinstance(time_budget_sec, Real)
            or not math.isfinite(time_budget_sec) or not 5. <= time_budget_sec <= FOUR_VIEW_SCAN_MAX_SEC):
        raise ValueError('Scan time budget must be a finite number from 5 to 900')
    maximum = FOUR_VIEW_SCAN_MAX_SEC if four_views else LEGACY_SCAN_MAX_SEC
    return min(maximum, float(time_budget_sec))


def post_scan_recognition_budget(state):
    """Return (limit, elapsed) for reset, registration, and required-cell reads.

    Four-view scanning has its own protection. Its elapsed time must not consume
    the allowance needed to establish the operation reference after the scan.
    Legacy sessions retain the previous shared scan-plus-reader allowance.
    """
    route = normalize_scan_route(state.get('scan_route', 'cells'))
    if route == 'four_views':
        return POST_SCAN_RECOGNITION_MAX_SEC, state.get('post_scan_recognition_elapsed_sec', 0.)
    return resolve_scan_budget(route, state.get('scan_time_budget_sec')), state.get('recognition_elapsed_sec', 0.)
