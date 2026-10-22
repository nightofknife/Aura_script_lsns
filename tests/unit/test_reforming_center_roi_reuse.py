"""One observation may reuse ROI grayscale; frames and errors stay isolated."""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import threading

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import _reforming_center_room_templates as rooms


REGION = (2, 3, 64, 48)
POSITIONS = {"a": (6, 7), "b": (36, 28)}


@pytest.fixture
def scene(monkeypatch):
    image = np.random.default_rng(812).integers(0, 256, (60, 80, 3), dtype=np.uint8)
    assets = {name: image[y:y + 7, x:x + 9].copy() for name, (x, y) in POSITIONS.items()}
    catalog = {"rooms": {name: {"map_title": {"image": name, "threshold": .9999}}
                         for name in POSITIONS}}
    monkeypatch.setattr(rooms, "_asset", assets.__getitem__)
    return image, assets, catalog


def _count_conversions(monkeypatch):
    original = cv2.cvtColor
    calls = []

    def convert(image, code, *args, **kwargs):
        calls.append((image.shape, code))
        return original(image, code, *args, **kwargs)

    monkeypatch.setattr(rooms.cv2, "cvtColor", convert)
    return calls


def _assert_hits(hits, shift=0):
    assert [hit["room_name"] for hit in hits] == list(POSITIONS)
    for hit in hits:
        x, y = POSITIONS[hit["room_name"]]
        assert hit["rect"] == [x + shift, y, 9, 7]
        assert hit["center"] == [x + shift + 4, y + 3]
        assert hit["score"] == pytest.approx(1.0, abs=1e-5)


def test_one_roi_conversion_per_observation_but_every_template_is_converted(monkeypatch, scene):
    image, _, catalog = scene
    # A repeated variant is still visited and converted, even when already found.
    catalog["rooms"]["a"]["map_title_variants"] = [dict(catalog["rooms"]["a"]["map_title"])]
    before = image.copy()
    calls = _count_conversions(monkeypatch)
    _assert_hits(rooms.room_hits(image, catalog, REGION))
    assert Counter(shape for shape, _ in calls) == Counter({(7, 9, 3): 3, (48, 64, 3): 1})
    assert all(code == cv2.COLOR_BGR2GRAY for _, code in calls)
    np.testing.assert_array_equal(image, before)


@pytest.mark.parametrize("next_frame", ["same", "different", "mutated"])
def test_each_call_reprepares_roi_even_for_same_identity_or_shape(monkeypatch, scene, next_frame):
    image, _, catalog = scene
    calls = _count_conversions(monkeypatch)
    _assert_hits(rooms.room_hits(image, catalog, REGION))
    second, shift = image, 0
    if next_frame == "different":
        second, shift = np.roll(image, 1, axis=1), 1
    elif next_frame == "mutated":
        image[:] = np.roll(image, 2, axis=1)
        shift = 2
    _assert_hits(rooms.room_hits(second, catalog, REGION), shift=shift)
    assert sum(shape == (48, 64, 3) for shape, _ in calls) == 2
    assert sum(shape == (7, 9, 3) for shape, _ in calls) == 4


def test_cache_key_distinguishes_equal_sized_rois_at_different_positions(monkeypatch, scene):
    image, _, catalog = scene
    catalog["rooms"]["a"]["map_title"]["search_region"] = (2, 3, 28, 22)
    catalog["rooms"]["b"]["map_title"]["search_region"] = (30, 24, 28, 22)
    catalog["rooms"]["a"]["map_title_variants"] = [dict(catalog["rooms"]["a"]["map_title"])]
    calls = _count_conversions(monkeypatch)
    _assert_hits(rooms.room_hits(image, catalog, region=None))
    assert Counter(shape for shape, _ in calls) == Counter({(7, 9, 3): 3, (22, 28, 3): 2})


def test_standalone_match_asset_converts_source_on_every_call(monkeypatch, scene):
    image, _, catalog = scene
    calls = _count_conversions(monkeypatch)
    spec = catalog["rooms"]["a"]["map_title"]
    first = rooms.match_asset(image, spec, REGION)
    second = rooms.match_asset(image, spec, REGION)
    assert first == second
    assert Counter(shape for shape, _ in calls) == Counter({(7, 9, 3): 2, (48, 64, 3): 2})


def test_empty_catalog_does_not_touch_image_assets_or_color_conversion(monkeypatch):
    class UnreadableImage:
        def __getitem__(self, key):
            pytest.fail("empty catalog must not slice its image")

    monkeypatch.setattr(rooms, "_asset", lambda *args: pytest.fail("unexpected asset read"))
    monkeypatch.setattr(rooms.cv2, "cvtColor", lambda *args: pytest.fail("unexpected conversion"))
    assert rooms.room_hits(UnreadableImage(), {"rooms": {}}, REGION) == []


@pytest.mark.parametrize("error_type", [FileNotFoundError, cv2.error])
def test_asset_error_precedes_bad_source_and_propagates_original(monkeypatch, error_type):
    error = error_type("asset failure")

    class BadSource:
        def __getitem__(self, key):
            pytest.fail("source was accessed before the failing asset")

    def fail(path):
        raise error

    monkeypatch.setattr(rooms, "_asset", fail)
    with pytest.raises(error_type) as raised:
        rooms.room_hits(BadSource(), {"rooms": {"a": {"map_title": {"image": "missing"}}}}, REGION)
    assert raised.value is error


def test_source_slice_failure_precedes_target_conversion(monkeypatch, scene):
    _, _, catalog = scene
    error = IndexError("source cannot be sliced")

    class BadSource:
        def __getitem__(self, key):
            raise error

    monkeypatch.setattr(rooms.cv2, "cvtColor", lambda *args: pytest.fail("slice error must occur first"))
    with pytest.raises(IndexError) as raised:
        rooms.room_hits(BadSource(), catalog, REGION)
    assert raised.value is error


def test_target_conversion_failure_precedes_invalid_source_conversion(monkeypatch, scene):
    _, assets, catalog = scene
    source = np.zeros((60, 80, 1), dtype=np.uint8)
    error = cv2.error("target conversion failed first")
    seen = []

    def fail_target(image, code):
        seen.append(image)
        assert image is assets["a"], "source conversion ran before target conversion"
        raise error

    monkeypatch.setattr(rooms.cv2, "cvtColor", fail_target)
    with pytest.raises(cv2.error) as raised:
        rooms.room_hits(source, catalog, REGION)
    assert raised.value is error
    assert len(seen) == 1


def test_too_small_roi_preserves_std_short_circuit(monkeypatch, scene):
    image, assets, catalog = scene
    original = cv2.cvtColor
    calls = []

    class StdMustNotRun(np.ndarray):
        def std(self, *args, **kwargs):
            pytest.fail("size rejection must occur before template std")

    def convert(array, code):
        calls.append(array.shape)
        gray = original(array, code)
        return gray.view(StdMustNotRun) if array is assets["a"] or array is assets["b"] else gray

    monkeypatch.setattr(rooms.cv2, "cvtColor", convert)
    monkeypatch.setattr(rooms.cv2, "matchTemplate", lambda *args: pytest.fail("undersized ROI must not match"))
    assert rooms.room_hits(image, catalog, (0, 0, 2, 2)) == []
    assert Counter(calls) == Counter({(7, 9, 3): 2, (2, 2, 3): 1})


def test_source_conversion_failure_is_not_cached_when_caller_continues(monkeypatch, scene):
    image, _, catalog = scene
    original_color, original_match = cv2.cvtColor, rooms.match_asset
    source_attempts = 0
    error = cv2.error("temporary source conversion failure")

    def convert(array, code):
        nonlocal source_attempts
        if array.shape == (48, 64, 3):
            source_attempts += 1
            if source_attempts == 1:
                raise error
        return original_color(array, code)

    def continue_after_first_error(frame, spec, region):
        try:
            return original_match(frame, spec, region)
        except cv2.error as caught:
            assert caught is error
            return None

    monkeypatch.setattr(rooms.cv2, "cvtColor", convert)
    monkeypatch.setattr(rooms, "match_asset", continue_after_first_error)
    result = rooms.room_hits(image, catalog, REGION)
    assert [hit["room_name"] for hit in result] == ["b"]
    assert source_attempts == 2


def test_exception_exit_drops_a_previously_cached_roi(monkeypatch, scene):
    image, assets, catalog = scene
    original = cv2.cvtColor
    error = cv2.error("second target conversion failed")
    source_calls = 0

    def convert(array, code):
        nonlocal source_calls
        if array is assets["b"]:
            raise error
        if array.shape == (48, 64, 3):
            source_calls += 1
        return original(array, code)

    monkeypatch.setattr(rooms.cv2, "cvtColor", convert)
    with pytest.raises(cv2.error) as raised:
        rooms.room_hits(image, catalog, REGION)
    assert raised.value is error
    assert source_calls == 1
    hit = rooms.match_asset(image, catalog["rooms"]["a"]["map_title"], REGION)
    assert hit is not None
    assert source_calls == 2


@pytest.mark.parametrize("same_location", [False, True])
def test_variant_ties_and_similar_name_competition_keep_input_order(monkeypatch, same_location):
    catalog = {"rooms": {
        "a": {"map_title": {"image": "a-first"}, "map_title_variants": [{"image": "a-second"}]},
        "b": {"map_title": {"image": "b"}},
    }}
    calls = []

    def match(image, spec, region):
        calls.append(spec["image"])
        x = {"a-first": 10, "a-second": 100, "b": 10 if same_location else 200}[spec["image"]]
        return {"score": .99, "rect": [x, 10, 20, 10], "center": [x + 10, 15]}

    monkeypatch.setattr(rooms, "match_asset", match)
    result = rooms.room_hits(object(), catalog, REGION)
    assert calls == ["a-first", "a-second", "b"]
    if same_location:
        assert result == []
    else:
        assert [hit["room_name"] for hit in result] == ["a", "b"]
        assert result[0]["rect"] == [10, 10, 20, 10]


@pytest.mark.parametrize("same_image", [False, True])
def test_nested_observation_restores_outer_roi_cache(monkeypatch, scene, same_image):
    image, assets, catalog = scene
    inner_image = image if same_image else np.roll(image, 1, axis=1)
    nested = []
    entered = False
    calls = _count_conversions(monkeypatch)

    def asset(path):
        nonlocal entered
        # The first outer template has already populated the outer ROI.
        if path == "b" and not entered:
            entered = True
            nested.extend(rooms.room_hits(inner_image, catalog, REGION))
        return assets[path]

    monkeypatch.setattr(rooms, "_asset", asset)
    _assert_hits(rooms.room_hits(image, catalog, REGION))
    _assert_hits(nested, shift=0 if same_image else 1)
    assert sum(shape == (48, 64, 3) for shape, _ in calls) == 2
    assert sum(shape == (7, 9, 3) for shape, _ in calls) == 4


def test_nested_exception_restores_outer_cache(monkeypatch, scene):
    image, assets, catalog = scene
    error = FileNotFoundError("inner asset failure")
    entered = False
    calls = _count_conversions(monkeypatch)

    def asset(path):
        nonlocal entered
        if path == "missing":
            raise error
        if path == "b" and not entered:
            entered = True
            with pytest.raises(FileNotFoundError) as raised:
                rooms.room_hits(image, {"rooms": {"inner": {"map_title": {"image": "missing"}}}}, REGION)
            assert raised.value is error
        return assets[path]

    monkeypatch.setattr(rooms, "_asset", asset)
    _assert_hits(rooms.room_hits(image, catalog, REGION))
    assert sum(shape == (48, 64, 3) for shape, _ in calls) == 1


def test_concurrent_observations_do_not_share_roi_pixels(monkeypatch, scene):
    image, _, catalog = scene
    second = np.roll(image, 1, axis=1)
    original = cv2.cvtColor
    barrier = threading.Barrier(2, timeout=5)
    lock = threading.Lock()
    source_calls = 0

    def convert(array, code):
        nonlocal source_calls
        if array.shape == (48, 64, 3):
            with lock:
                source_calls += 1
            barrier.wait()
        return original(array, code)

    monkeypatch.setattr(rooms.cv2, "cvtColor", convert)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(rooms.room_hits, frame, catalog, REGION) for frame in (image, second)]
        first_result, second_result = [future.result() for future in futures]
    _assert_hits(first_result)
    _assert_hits(second_result, shift=1)
    assert source_calls == 2
