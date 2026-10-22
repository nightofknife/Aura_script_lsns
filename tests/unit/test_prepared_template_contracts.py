"""Lazy prepared assets must preserve the raw template request contracts."""
from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, is_dataclass
import math
import threading

import cv2
import numpy as np
import pytest

from plans.aura_base.src.services import _vision_timing as timing_module
from plans.aura_base.src.services.prepared_template import PreparedTemplate
from plans.aura_base.src.services.vision_service import (
    MatchResult, MultiMatchResult, PreparedTemplate as ExportedPreparedTemplate, VisionService,
)


OPERATIONS = (
    "find_template", "find_all_templates", "find_templates_batch", "find_all_templates_batch",
)
BATCH_OPERATIONS = OPERATIONS[2:]


@pytest.fixture(autouse=True)
def isolated_timing(monkeypatch):
    monkeypatch.setattr(timing_module, "_SAMPLE_EVERY", 0)
    monkeypatch.setattr(timing_module, "_sample_counts", defaultdict(int))


@pytest.fixture
def scene():
    source = np.random.default_rng(1941).integers(0, 256, (48, 64, 3), dtype=np.uint8)
    positions = [(2, 3), (24, 3), (4, 27), (39, 28)]
    templates = [source[y:y + 11, x:x + 13].copy() for x, y in positions]
    masks = [np.full((11, 13), 255, dtype=np.uint8) for _ in templates]
    return source, templates, masks


def _arguments(operation, source, templates, *, masks=None, use_grayscale=True, preprocess="none"):
    result = dict(source_image=source, threshold=0.9, use_grayscale=use_grayscale,
                  preprocess=preprocess, match_method=cv2.TM_CCORR_NORMED)
    if operation in BATCH_OPERATIONS:
        result.update(template_images=templates, mask_images=masks)
    else:
        result.update(template_image=templates[0], mask_image=masks[0] if masks is not None else None)
    return result


def _invoke(service, operation, execution, arguments):
    method = getattr(service, operation + ("_async" if execution == "async" else ""))
    return asyncio.run(method(**arguments)) if execution == "async" else method(**arguments)


def _assert_equal(actual, expected):
    """Compare complete results, including order, debug fields and non-finite scores."""
    if is_dataclass(expected):
        assert type(actual) is type(expected)
        return _assert_equal(asdict(actual), asdict(expected))
    if isinstance(expected, dict):
        assert actual.keys() == expected.keys()
        for key in expected:
            _assert_equal(actual[key], expected[key])
    elif isinstance(expected, (list, tuple)):
        assert type(actual) is type(expected)
        assert len(actual) == len(expected)
        for left, right in zip(actual, expected):
            _assert_equal(left, right)
    elif isinstance(expected, (float, np.floating)) and math.isnan(expected):
        assert math.isnan(actual)
    else:
        assert actual == expected


def _assert_handled_failure(operation, result, count):
    results = result if operation in BATCH_OPERATIONS else [result]
    assert len(results) == (count if operation in BATCH_OPERATIONS else 1)
    for item in results:
        if operation in ("find_all_templates", "find_all_templates_batch"):
            assert item == MultiMatchResult()
        else:
            assert isinstance(item, MatchResult)
            assert item.found is False
            assert item.top_left is None and item.rect is None
            assert isinstance(item.debug_info.get("error"), str)


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("execution", ["sync", "async"])
@pytest.mark.parametrize("grayscale,preprocess,masked", [
    (False, "none", False), (True, "none", False), (True, "edge", True),
])
def test_raw_and_prepared_results_match_cold_and_warm(
    operation, execution, grayscale, preprocess, masked, scene,
):
    source, templates, all_masks = scene
    masks = all_masks if masked else None
    prepared = [PreparedTemplate(image, all_masks[index] if masked else None,
                                 use_grayscale=grayscale, preprocess=preprocess)
                for index, image in enumerate(templates)]
    assert all(not item.is_prepared for item in prepared)
    service = VisionService()
    options = dict(use_grayscale=grayscale, preprocess=preprocess)
    expected = _invoke(service, operation, execution,
                       _arguments(operation, source, templates, masks=masks, **options))
    for _ in range(2):
        actual = _invoke(service, operation, execution,
                         _arguments(operation, source, prepared, **options))
        _assert_equal(actual, expected)
    used = prepared if operation in BATCH_OPERATIONS else prepared[:1]
    assert all(item.is_prepared for item in used)


def test_prepared_snapshots_are_independent_and_cannot_be_made_writable():
    assert ExportedPreparedTemplate is PreparedTemplate
    backing = np.arange(24 * 28 * 3, dtype=np.uint8).reshape(24, 28, 3)
    mask_backing = np.full((24, 28), 255, dtype=np.uint8)
    template, mask = backing[::2, ::2], mask_backing[::2, ::2]
    expected_template, expected_mask = template.copy(), mask.copy()
    item = PreparedTemplate(template, mask, preprocess="EDGE")
    assert item.preprocess == "edge"
    assert item.use_grayscale is True and item.is_prepared is False
    backing[:] = 0
    mask_backing[:] = 0
    for snapshot, expected, original in (
        (item.template_image, expected_template, backing),
        (item.mask_image, expected_mask, mask_backing),
    ):
        np.testing.assert_array_equal(snapshot, expected)
        assert not np.shares_memory(snapshot, original)
        assert snapshot.flags.writeable is False
        with pytest.raises(ValueError):
            snapshot.setflags(write=True)
        with pytest.raises(ValueError):
            snapshot.reshape(-1).setflags(write=True)
        with pytest.raises(ValueError):
            snapshot.flat[0] = 1
    for name, value in (("template_image", expected_template), ("mask_image", None),
                        ("use_grayscale", False), ("preprocess", "none"), ("is_prepared", True)):
        with pytest.raises(AttributeError):
            setattr(item, name, value)


@pytest.mark.parametrize("bad_input", [None, [[1, 2]], "image.png"])
def test_constructor_rejects_non_array_template(bad_input):
    with pytest.raises(TypeError):
        PreparedTemplate(bad_input)


@pytest.mark.parametrize("bad_mask", [[[255]], "mask.png"])
def test_constructor_rejects_non_array_mask(scene, bad_mask):
    with pytest.raises(TypeError):
        PreparedTemplate(scene[1][0], bad_mask)


def test_edge_preparation_is_once_per_template_but_every_source_is_processed(monkeypatch, scene):
    source, templates, masks = scene
    item = PreparedTemplate(templates[0], masks[0], preprocess="edge")
    original_canny = cv2.Canny
    canny_shapes = []

    def count_canny(image, *args, **kwargs):
        canny_shapes.append(image.shape)
        return original_canny(image, *args, **kwargs)

    monkeypatch.setattr(cv2, "Canny", count_canny)
    service = VisionService()
    for _ in range(2):
        hit = service.find_template(source, item, preprocess="edge", match_method=cv2.TM_CCORR_NORMED)
        assert "error" not in hit.debug_info
        assert hit.debug_info["preprocess"] == "edge"
    assert Counter(canny_shapes) == Counter({source.shape[:2]: 2, templates[0].shape[:2]: 1})


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("execution", ["sync", "async"])
@pytest.mark.parametrize("conflict", ["grayscale", "preprocess", "external_mask"])
def test_conflicts_follow_each_api_failure_contract(operation, execution, conflict, scene):
    source, templates, masks = scene
    prepared = [PreparedTemplate(image, masks[index]) for index, image in enumerate(templates)]
    arguments = _arguments(operation, source, prepared)
    if conflict == "grayscale":
        arguments["use_grayscale"] = False
    elif conflict == "preprocess":
        arguments["preprocess"] = "edge"
    else:
        arguments["mask_images" if operation in BATCH_OPERATIONS else "mask_image"] = (
            masks if operation in BATCH_OPERATIONS else masks[0]
        )
    result = _invoke(VisionService(), operation, execution, arguments)
    _assert_handled_failure(operation, result, len(prepared))


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("execution", ["sync", "async"])
@pytest.mark.parametrize("invalid", ["shape", "channels", "mask_size", "preprocess"])
def test_invalid_array_requests_match_raw_error_behavior(operation, execution, invalid, scene):
    source, templates, masks = scene
    template, mask, preprocess = templates[0], None, "none"
    if invalid == "shape":
        template = np.arange(13, dtype=np.uint8)
    elif invalid == "channels":
        template = np.zeros((11, 13, 2), dtype=np.uint8)
    elif invalid == "mask_size":
        mask = masks[0][:-1]
    else:
        preprocess = "unsupported_preprocess"
    prepared = PreparedTemplate(template, mask, preprocess=preprocess)
    assert prepared.is_prepared is False
    service = VisionService()
    expected = _invoke(service, operation, execution,
                       _arguments(operation, source, [template], masks=[mask], preprocess=preprocess))
    actual = _invoke(service, operation, execution,
                     _arguments(operation, source, [prepared], preprocess=preprocess))
    _assert_equal(actual, expected)


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("execution", ["sync", "async"])
def test_bad_source_is_processed_before_lazy_template(monkeypatch, operation, execution, scene):
    bad_template = np.zeros((11, 13, 2), dtype=np.uint8)
    item = PreparedTemplate(bad_template)
    service = VisionService()
    expected = _invoke(service, operation, execution,
                       _arguments(operation, object(), [bad_template]))
    original = service._prepare_image
    preparation_calls = []

    def track(image, *args, **kwargs):
        preparation_calls.append(image)
        return original(image, *args, **kwargs)

    monkeypatch.setattr(service, "_prepare_image", track)
    actual = _invoke(service, operation, execution, _arguments(operation, object(), [item]))
    _assert_equal(actual, expected)
    assert len(preparation_calls) == 1
    assert item.is_prepared is False


@pytest.mark.parametrize("operation", BATCH_OPERATIONS)
@pytest.mark.parametrize("execution", ["sync", "async"])
def test_batch_mask_length_error_still_escapes(operation, execution, scene):
    item = PreparedTemplate(scene[1][0], scene[2][0])
    arguments = _arguments(operation, scene[0], [item, item], masks=[None])
    with pytest.raises(ValueError, match="mask_images length must match template_images length"):
        _invoke(VisionService(), operation, execution, arguments)
    assert item.is_prepared is False


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("execution", ["sync", "async"])
@pytest.mark.parametrize("error_type", [TypeError, cv2.error])
@pytest.mark.parametrize("failure_role", ["template", "mask"])
def test_failed_preparation_is_not_cached_and_can_retry(
    monkeypatch, operation, execution, error_type, failure_role, scene,
):
    source, templates, masks = scene
    item = PreparedTemplate(templates[0], masks[0])
    service = VisionService()
    original = service._prepare_image
    attempts = 0
    error = error_type("transient preparation failure")
    failure_shape = templates[0].shape if failure_role == "template" else masks[0].shape

    def fail_once(image, *args, **kwargs):
        nonlocal attempts
        if isinstance(image, np.ndarray) and image.shape == failure_shape:
            attempts += 1
            if attempts == 1:
                raise error
        return original(image, *args, **kwargs)

    monkeypatch.setattr(service, "_prepare_image", fail_once)
    arguments = _arguments(operation, source, [item])
    if error_type is cv2.error:
        with pytest.raises(cv2.error) as raised:
            _invoke(service, operation, execution, arguments)
        assert raised.value is error
    else:
        _assert_handled_failure(operation, _invoke(service, operation, execution, arguments), 1)
    assert item.is_prepared is False
    expected = _invoke(VisionService(), operation, execution,
                       _arguments(operation, source, templates[:1], masks=masks[:1]))
    _assert_equal(_invoke(service, operation, execution, arguments), expected)
    assert item.is_prepared is True
    assert attempts == 2


def test_concurrent_first_use_prepares_template_and_mask_once(monkeypatch, scene):
    source, templates, masks = scene
    item = PreparedTemplate(templates[0], masks[0], preprocess="edge")
    service = VisionService()
    original = service._prepare_image
    requests = 8
    entered = threading.Barrier(requests, timeout=5)
    all_sources_prepared = threading.Event()
    counter_lock = threading.Lock()
    counts = Counter()

    def track(image, *args, **kwargs):
        role = "source" if image.shape == source.shape else (
            "template" if image.shape == templates[0].shape else "mask"
        )
        with counter_lock:
            counts[role] += 1
            if counts["source"] == requests:
                all_sources_prepared.set()
        if role == "template":
            assert all_sources_prepared.wait(timeout=5), "other requests did not reach source preparation"
        return original(image, *args, **kwargs)

    monkeypatch.setattr(service, "_prepare_image", track)

    def request():
        entered.wait()
        return service.find_template(source, item, preprocess="edge", match_method=cv2.TM_CCORR_NORMED)

    with ThreadPoolExecutor(max_workers=requests) as executor:
        results = list(executor.map(lambda _: request(), range(requests)))
    assert counts == Counter(source=requests, template=1, mask=1)
    assert item.is_prepared is True
    for result in results:
        assert "error" not in result.debug_info
        _assert_equal(result, results[0])


def test_city_catalog_preserves_raw_unpacking_and_cache_clear_rebuilds_prepared_assets():
    from plans.resonance_pc.src.actions import _city_panel_vision as city

    city.load_templates.cache_clear()
    try:
        first = city.load_templates()
        cities, templates, masks, visit = first
        assert isinstance(first, tuple) and len(first) == 4
        assert len(cities) == len(templates) == len(masks) == len(first.prepared_templates) == 21
        assert isinstance(first.prepared_templates, tuple)
        assert visit.shape == (29, 35, 3)
        for prepared, raw, mask in zip(first.prepared_templates, templates, masks):
            assert isinstance(prepared, PreparedTemplate)
            assert prepared.use_grayscale is True and prepared.preprocess == "edge"
            assert prepared.is_prepared is False
            np.testing.assert_array_equal(prepared.template_image, raw)
            np.testing.assert_array_equal(prepared.mask_image, mask)
        assert city.load_templates() is first
        VisionService().find_template(templates[0], first.prepared_templates[0],
                                      preprocess="edge", match_method=cv2.TM_CCORR_NORMED)
        assert first.prepared_templates[0].is_prepared is True
        city.load_templates.cache_clear()
        second = city.load_templates()
        assert second is not first
        for old, new in zip(first.prepared_templates, second.prepared_templates):
            assert new is not old and new.is_prepared is False
    finally:
        city.load_templates.cache_clear()


@pytest.mark.parametrize("operation", BATCH_OPERATIONS)
@pytest.mark.parametrize("execution", ["sync", "async"])
@pytest.mark.parametrize("bad_template", ["missing", "object", "channels"])
def test_raw_template_error_precedes_malformed_mask_lookup(
    operation, execution, bad_template, scene, tmp_path,
):
    source, _, _ = scene
    image = {
        "missing": str(tmp_path / "missing.png"),
        "object": object(),
        "channels": np.zeros((5, 6, 2), dtype=np.uint8),
    }[bad_template]
    service = VisionService()
    expected = _invoke(service, operation, execution, _arguments(operation, source, [image]))
    _assert_handled_failure(operation, expected, 1)
    # Length is correct, but indexing zero would raise KeyError. The legacy
    # template failure must still win, before a mask element is accessed.
    actual = _invoke(service, operation, execution,
                     _arguments(operation, source, [image], masks={1: None}))
    _assert_equal(actual, expected)
