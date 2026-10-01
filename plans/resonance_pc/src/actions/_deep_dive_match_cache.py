"""Reuse page-template results only for exactly identical source ROI pixels."""
from collections import OrderedDict, namedtuple
from copy import deepcopy
from functools import wraps
from threading import Lock, local

import numpy as np


CacheInfo = namedtuple('CacheInfo', 'hits misses maxsize currsize')


def exact_roi_cache(*, region, dependencies, maxsize=32):
    """Cache a match(gray, name[, box]) with current template/function identities.

    Each thread owns at most ``maxsize`` ROI/result entries for this matcher.
    Dependency callbacks return the loader, actual template/mask objects and
    matching functions. Strong references prevent object-ID reuse. Replacing
    them invalidates the corresponding entry; in-place template edits require
    ``cache_clear()``, which invalidates all threads on their next access.
    Only the matcher's ROI is reused; scene or HUD decisions are never cached.
    """
    if maxsize < 1:
        raise ValueError('maxsize must be positive')

    def decorate(match):
        thread = local()
        generation = 0
        clear_lock = Lock()

        def state():
            if getattr(thread, 'generation', None) != generation:
                thread.generation = generation
                thread.entries = OrderedDict()
                thread.hits = 0
                thread.misses = 0
            return thread

        @wraps(match)
        def cached(gray, name, *args, **kwargs):
            box = tuple(region(name, *args, **kwargs))
            x1, y1, x2, y2 = box
            roi = gray[y1:y2, x1:x2]
            current_dependencies = tuple(dependencies(name, *args, **kwargs))
            key = (name, box, roi.dtype.str)
            current = state()
            prior = current.entries.get(key)
            if (prior is not None
                    and len(prior[0]) == len(current_dependencies)
                    and all(old is new for old, new in zip(prior[0], current_dependencies))
                    and np.array_equal(roi, prior[1])):
                current.hits += 1
                current.entries.move_to_end(key)
                return deepcopy(prior[2])

            current.misses += 1
            pixels = roi.copy()
            result = match(gray, name, *args, **kwargs)
            current.entries[key] = (current_dependencies, pixels, deepcopy(result))
            current.entries.move_to_end(key)
            while len(current.entries) > maxsize:
                current.entries.popitem(last=False)
            return result

        def cache_clear():
            nonlocal generation
            with clear_lock:
                generation += 1

        def cache_info():
            current = state()
            return CacheInfo(current.hits, current.misses, maxsize, len(current.entries))

        cached.cache_clear = cache_clear
        cached.cache_info = cache_info
        return cached

    return decorate
