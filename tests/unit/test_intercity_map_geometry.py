"""Native map geometry checked against map-space and viewport invariants."""

from itertools import product
import json
import math
from types import SimpleNamespace

import pytest

from plans.resonance_pc.src.actions import _intercity_map_geometry as geometry


@pytest.fixture
def cities():
    return geometry.load_native_cities()


def label(city_key, center):
    return {"city_key": city_key, "center": list(center), "score": .99}


def label_for_pose(cities, city_key, map_origin, error=(0, 0)):
    """Project a label with an independent canvas transform and shared offset."""
    native_x, native_y = cities[city_key]["native_position"]
    return label(city_key, (
        map_origin[0] + native_x * 1280 / 1920 + error[0],
        map_origin[1] - (native_y - 63) * 720 / 1080 + error[1],
    ))


def test_native_catalog_keeps_known_original_positions_and_unique_stations(cities):
    assert len(cities) == 21
    assert cities["cape_city"]["native_position"] == [-1300, -1650]
    assert cities["qiyu_station"]["native_position"] == [2350, 675]
    assert cities["wulin_source"]["native_position"] == [-3425, 50]
    assert cities["gronru_city"]["native_position"] == [-300, -1250]
    assert len({city["station_id"] for city in cities.values()}) == len(cities)
    assert len({city["name"] for city in cities.values()}) == len(cities)


@pytest.mark.parametrize("size", [(1280, 720), [1280, 720]])
def test_reference_client_accepts_exact_runtime_size(size):
    geometry.require_reference_client(SimpleNamespace(get_window_size=lambda: size))


@pytest.mark.parametrize("size", [(1920, 1080), (1280, 721), (720, 1280), None])
def test_other_client_sizes_are_rejected_before_using_native_scale(size):
    with pytest.raises(geometry.MapNavigationError) as caught:
        geometry.require_reference_client(SimpleNamespace(get_window_size=lambda: size))
    assert caught.value.code == "map_navigation_window_size_invalid"
    assert caught.value.detail["expected"] == [1280, 720]
    assert caught.value.detail["actual"] == size


@pytest.mark.parametrize("native,screen", [
    ((150, 0), (100, 0)),
    ((-150, 0), (-100, 0)),
    ((0, 150), (0, -100)),
    ((0, -150), (0, 100)),
    ((150, -75), (100, 50)),
])
def test_canvas_projection_scales_axes_and_flips_native_y(native, screen):
    assert geometry.screen_delta((10, 20), (10 + native[0], 20 + native[1])) == pytest.approx(screen)


def test_cape_to_qiyu_drag_moves_target_to_train_center(cities):
    target_screen_from_train = geometry.screen_delta(
        cities["cape_city"]["native_position"], cities["qiyu_station"]["native_position"])
    assert target_screen_from_train == pytest.approx((2433 + 1 / 3, -1550))
    drag = geometry.coarse_drag(cities, "cape_city", "qiyu_station")
    assert drag == pytest.approx((-2433 - 1 / 3, 1550))
    assert tuple(target_screen_from_train[axis] + drag[axis] for axis in (0, 1)) == pytest.approx((0, 0))
    assert geometry.coarse_drag(cities, "qiyu_station", "cape_city") == pytest.approx(tuple(-v for v in drag))
    assert geometry.coarse_drag(cities, "cape_city", "cape_city") == (0, 0)


def test_long_trip_is_one_precomputed_batch_with_exact_integer_total(cities):
    displacement = geometry.coarse_drag(cities, "cape_city", "qiyu_station")
    segments = geometry.plan_drag_batch(displacement, geometry.SAFE_DRAG_RECT, 900)
    assert len(segments) == 4  # 1550 px vertical travel / 396 px usable vertical span.
    assert tuple(map(sum, zip(*segments))) == (-2433, 1550)
    assert all(dx < 0 and dy > 0 for dx, dy in segments)


def test_every_catalog_route_fits_safe_gestures_and_preserves_total_displacement(cities):
    _, _, width, height = geometry.SAFE_DRAG_RECT
    for source, target in product(cities, repeat=2):
        delta = geometry.coarse_drag(cities, source, target)
        segments = geometry.plan_drag_batch(delta, geometry.SAFE_DRAG_RECT, 900)
        total = tuple(sum(segment[axis] for segment in segments) for axis in (0, 1))
        assert total == tuple(round(value) for value in delta), (source, target)
        for dx, dy in segments:
            assert isinstance(dx, int) and isinstance(dy, int)
            assert abs(dx) <= 896 and abs(dy) <= 396
            # A 32 px activation segment plus the effective drag must fit inside
            # the safe rectangle, including its integer endpoint convention.
            distance = math.hypot(dx, dy)
            prime = (32 * dx / distance, 32 * dy / distance)
            for axis, extent in enumerate((width, height)):
                path = (0, prime[axis], prime[axis] + (dx, dy)[axis])
                assert max(path) - min(path) <= extent - 1


@pytest.mark.parametrize("delta", [(0, 0), (.2, -.2), (-.49, .49)])
def test_subpixel_only_displacement_does_not_issue_empty_drags(delta):
    assert geometry.plan_drag_batch(delta, geometry.SAFE_DRAG_RECT, 900) == []


@pytest.mark.parametrize("delta", [(2400.4, -999.6), (-2400.4, 999.6), (0, 2400.5), (2400.5, 0)])
def test_smaller_span_changes_segment_count_without_accumulating_rounding_error(delta):
    segments = geometry.plan_drag_batch(delta, geometry.SAFE_DRAG_RECT, 150)
    assert len(segments) == max(math.ceil(abs(round(value)) / 150) for value in delta)
    assert tuple(sum(row[axis] for row in segments) for axis in (0, 1)) == tuple(round(v) for v in delta)
    assert all(max(abs(dx), abs(dy)) <= 150 for dx, dy in segments)


@pytest.mark.parametrize("delta", [(float("nan"), 1), (1, float("inf")), (1,), (1, 2, 3)])
def test_invalid_displacement_fails_with_structured_error(delta):
    with pytest.raises(geometry.MapNavigationError) as caught:
        geometry.plan_drag_batch(delta, geometry.SAFE_DRAG_RECT, 900)
    assert caught.value.code == "map_drag_invalid"


@pytest.mark.parametrize("rectangle,span", [((0, 0, 64, 400), 900), ((0, 0, 900, 64), 900), (geometry.SAFE_DRAG_RECT, 0)])
def test_no_room_for_activation_or_nonpositive_span_is_rejected(rectangle, span):
    with pytest.raises(geometry.MapNavigationError) as caught:
        geometry.plan_drag_batch((100, 100), rectangle, span)
    assert caught.value.code == "map_drag_region_invalid"


def test_native_anchor_prediction_cancels_shared_label_offset(cities):
    origin = (610, 280)
    observed = [label_for_pose(cities, key, origin) for key in (
        "shoggolith_city", "brcl_outpost", "wilderness_station")]
    result = geometry.predict_target_from_labels(observed, cities, "qiyu_station")
    expected = label_for_pose(cities, "qiyu_station", origin)["center"]
    assert result["predicted_target"] == pytest.approx(expected)
    assert result["screen_scale"] == pytest.approx(2 / 3)
    assert len(result["anchors"]) == 3
    assert result["rejected_city_keys"] == []


def test_consensus_uses_median_and_rejects_false_city_label(cities):
    origin = (610, 280)
    keys = ["shoggolith_city", "brcl_outpost", "wilderness_station", "freeport"]
    errors = [(0, 0), (3, -2), (-2, 4), (120, 180)]
    observed = [label_for_pose(cities, key, origin, error) for key, error in zip(keys, errors)]
    result = geometry.predict_target_from_labels(observed, cities, "qiyu_station")
    assert result["predicted_target"] == pytest.approx(label_for_pose(cities, "qiyu_station", origin)["center"])
    assert {row["city_key"] for row in result["anchors"]} == set(keys[:3])
    assert result["rejected_city_keys"] == ["freeport"]


def test_single_fresh_anchor_can_locate_target_without_guessing_train_center(cities):
    point = label_for_pose(cities, "brcl_outpost", (710, 180))
    result = geometry.predict_target_from_labels([point], cities, "qiyu_station")
    assert result["predicted_target"] == pytest.approx(label_for_pose(cities, "qiyu_station", (710, 180))["center"])
    assert len(result["anchors"]) == 1


def test_equal_conflicting_anchor_groups_are_rejected(cities):
    keys = ["shoggolith_city", "brcl_outpost", "wilderness_station", "freeport"]
    origins = [(600, 280), (603, 282), (700, 380), (701, 378)]
    points = [label_for_pose(cities, key, origin) for key, origin in zip(keys, origins)]
    with pytest.raises(geometry.MapNavigationError) as caught:
        geometry.predict_target_from_labels(points, cities, "qiyu_station")
    assert caught.value.code == "map_anchors_inconsistent"


def test_no_supported_labels_cannot_produce_a_guessed_target(cities):
    with pytest.raises(geometry.MapNavigationError) as caught:
        geometry.predict_target_from_labels([], cities, "qiyu_station")
    assert caught.value.code == "map_anchor_not_found"


def test_old_four_ninths_coordinate_scale_cannot_pass_native_anchor_consensus(cities):
    points = [label(key, (600 + cities[key]["native_position"][0] * 4 / 9,
                          280 - cities[key]["native_position"][1] * 4 / 9))
              for key in ("shoggolith_city", "brcl_outpost", "wilderness_station")]
    with pytest.raises(geometry.MapNavigationError) as caught:
        geometry.predict_target_from_labels(points, cities, "qiyu_station")
    assert caught.value.code == "map_anchors_inconsistent"


@pytest.mark.parametrize("mutation", [
    lambda catalog: catalog.update(reference_client=[1920, 1080]),
    lambda catalog: catalog.update(screen_units_per_native_unit_fraction=[4, 9]),
    lambda catalog: catalog.update(coordinate_system="x_right_y_down"),
    lambda catalog: catalog["cities"]["cape_city"].update(native_position=[float("nan"), 0]),
    lambda catalog: catalog["cities"]["cape_city"].update(native_position=[True, 0]),
])
def test_incompatible_or_invalid_native_catalog_is_rejected(tmp_path, monkeypatch, mutation):
    catalog = json.loads(geometry._CATALOG.read_text(encoding="utf-8"))
    mutation(catalog)
    catalog_path = tmp_path / "native.json"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    monkeypatch.setattr(geometry, "_CATALOG", catalog_path)
    geometry.load_native_cities.cache_clear()
    try:
        with pytest.raises(geometry.MapNavigationError) as caught:
            geometry.load_native_cities()
        assert caught.value.code == "native_map_coordinates_invalid"
    finally:
        geometry.load_native_cities.cache_clear()
