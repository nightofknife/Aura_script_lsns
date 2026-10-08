"""Exact ROI reuse preserves match results, mutable outputs, and dependencies."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions._deep_dive_match_cache import exact_roi_cache
from plans.resonance_pc.src.actions import _deep_dive_planned_run_vision as page
from plans.resonance_pc.src.actions import _deep_dive_single_run_vision as single
from plans.resonance_pc.src.actions import _deep_dive_battle_vision as battle
from plans.resonance_pc.src.actions import _deep_dive_event_vision as event


MATCHERS = [(page, '_match'), (single, '_match'), (battle, 'match'), (event, 'match')]


@pytest.fixture(autouse=True)
def clear_page_caches():
    for module, name in MATCHERS:
        getattr(module, name).cache_clear()
    yield
    for module, name in MATCHERS:
        getattr(module, name).cache_clear()


def test_only_exact_roi_pixels_reuse_and_mutable_results_are_isolated():
    calls = []

    @exact_roi_cache(region=lambda name: (1, 1, 4, 4), dependencies=lambda name: ())
    def match(gray, name):
        calls.append(name)
        return {'point': [int(gray[1:4, 1:4].sum()), 3], 'nested': {'rows': [1]}}

    image = np.zeros((6, 6), np.uint8)
    original = match(image, 'a')
    original['point'][1] = 99
    original['nested']['rows'].append(7)
    image[0, 0] = 255
    hit = match(image, 'a')
    assert hit == {'point': [0, 3], 'nested': {'rows': [1]}}
    hit['point'][0] = -1
    assert match(image.copy(), 'a')['point'] == [0, 3]
    image[1, 1] = 1
    assert match(image, 'a')['point'] == [1, 3]
    assert len(calls) == 2
    assert match.cache_info().hits == 2


def test_template_or_function_replacement_and_explicit_clear_recompute():
    dependency = [np.zeros((2, 2), np.uint8), lambda: None]
    calls = []

    @exact_roi_cache(region=lambda name: (0, 0, 3, 3),
                     dependencies=lambda name: dependency)
    def match(gray, name):
        calls.append(1)
        return len(calls)

    image = np.zeros((4, 4), np.uint8)
    assert match(image, 'a') == match(image, 'a') == 1
    dependency[0] = dependency[0].copy()
    assert match(image, 'a') == 2
    dependency[1] = lambda: None
    assert match(image, 'a') == 3
    dependency[0][:] = 2
    match.cache_clear()
    assert match(image, 'a') == 4


def test_lru_is_bounded_and_refreshes_hits():
    calls = []

    @exact_roi_cache(region=lambda name: (0, 0, 2, 2),
                     dependencies=lambda name: (), maxsize=2)
    def match(gray, name):
        calls.append(name)
        return name

    image = np.zeros((3, 3), np.uint8)
    for name in ['a', 'b', 'a', 'c', 'b']:
        assert match(image, name) == name
    assert calls == ['a', 'b', 'c', 'b']
    assert match.cache_info().currsize == 2


def test_dtype_and_coordinate_changes_do_not_share_match_results():
    calls = []

    @exact_roi_cache(region=lambda name, box: box,
                     dependencies=lambda name, box: ())
    def match(gray, name, box):
        calls.append((gray.dtype.str, tuple(box)))
        return list(box)

    image = np.zeros((6, 6), np.uint8)
    assert match(image, 'a', [0, 0, 3, 3]) == [0, 0, 3, 3]
    match(image.astype(np.float32), 'a', [0, 0, 3, 3])
    match(image, 'a', [1, 1, 4, 4])
    assert len(calls) == 3


def test_cache_is_thread_local_and_clear_invalidates_each_thread():
    barrier = Barrier(3)
    proceed = Event()

    @exact_roi_cache(region=lambda name: (0, 0, 3, 3), dependencies=lambda name: ())
    def match(gray, name):
        return [1]

    def worker():
        image = np.zeros((4, 4), np.uint8)
        match(image, 'a')
        match(image, 'a')
        before = match.cache_info()
        barrier.wait(timeout=5)
        assert proceed.wait(timeout=5)
        match(image, 'a')
        return before, match.cache_info()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker) for _ in range(2)]
        barrier.wait(timeout=5)
        assert match.cache_info().currsize == 0
        match.cache_clear()
        proceed.set()
        for future in futures:
            before, after = future.result(timeout=5)
            assert (before.hits, before.misses, before.currsize) == (1, 1, 1)
            assert (after.hits, after.misses, after.currsize) == (0, 1, 1)


@pytest.mark.parametrize('module,name', MATCHERS)
def test_each_page_matcher_reuses_exact_pixels_but_respects_template_monkeypatch(monkeypatch, module, name):
    rng = np.random.default_rng(11)
    image = rng.integers(0, 255, (8, 8), dtype=np.uint8)
    reference = rng.integers(0, 255, (3, 3), dtype=np.uint8)
    calls = []
    original = cv2.matchTemplate

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(cv2, 'matchTemplate', counted)
    loader = '_template' if name == '_match' else 'template'
    if module is page:
        monkeypatch.setattr(module, loader, lambda key: (reference, None))
    else:
        monkeypatch.setattr(module, loader, lambda key: reference)
    if name == '_match':
        monkeypatch.setattr(module, 'REGIONS', {'probe': (0, 0, 8, 8)})
        args = (image, 'probe')
    else:
        args = (image, 'probe', (0, 0, 8, 8))
    matcher = getattr(module, name)
    first = matcher(*args)
    assert matcher(*args) == first
    assert len(calls) == 1
    if module is page:
        monkeypatch.setattr(module, loader, lambda key: (reference, None))
    else:
        monkeypatch.setattr(module, loader, lambda key: reference)
    assert matcher(*args) == first
    assert len(calls) == 2
    image[0, 0] ^= 1
    assert matcher(*args) == matcher.__wrapped__(*args)
    assert len(calls) == 4


def test_saved_complete_page_observation_matches_all_uncached_fields(monkeypatch):
    path = Path(__file__).parents[1] / 'fixtures/deep_dive_anchor/oblique_630.png'
    image = cv2.cvtColor(cv2.imread(str(path)), cv2.COLOR_BGR2RGB)
    with monkeypatch.context() as patch:
        for module, name in MATCHERS:
            patch.setattr(module, name, getattr(module, name).__wrapped__)
        expected = page.observe(image)
    assert page.observe(image) == expected
    assert page.observe(image.copy()) == expected
    assert sum(getattr(module, name).cache_info().hits for module, name in MATCHERS) > 0
