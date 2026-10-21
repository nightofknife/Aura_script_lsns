"""Native city coordinates for the default 1280x720 intercity map."""

from __future__ import annotations

from functools import lru_cache
import json
import math
from pathlib import Path
from statistics import median
from typing import Any, Sequence


REFERENCE_CLIENT = (1280, 720)
SCREEN_SCALE = 2 / 3
SAFE_DRAG_RECT = (160, 140, 960, 460)
ANCHOR_TOLERANCE_PX = 12
MAX_CORRECTION_ROUNDS = 3
_CATALOG = Path(__file__).resolve().parents[2] / "data/meta/intercity_map_native.json"


class MapNavigationError(RuntimeError):
    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail or {}


@lru_cache(maxsize=1)
def load_native_cities() -> dict[str, Any]:
    try:
        catalog = json.loads(_CATALOG.read_text(encoding="utf-8"))
        if (catalog["schema_version"] != 1
                or catalog["reference_client"] != list(REFERENCE_CLIENT)
                or catalog["canvas_reference"] != [1920, 1080]
                or catalog["canvas_screen_match"] != "expand"
                or catalog["default_zoom"] != 1
                or catalog["coordinate_system"] != "x_right_y_up"
                or catalog["screen_units_per_native_unit_fraction"] != [2, 3]):
            raise ValueError("Unsupported intercity map coordinate system")
        cities = catalog["cities"]
        if not isinstance(cities, dict) or not cities:
            raise ValueError("Native city coordinates are missing")
        for city in cities.values():
            point = city["native_position"]
            if (not isinstance(point, list) or len(point) != 2
                    or any(type(value) not in (int, float) or not math.isfinite(value) for value in point)
                    or not isinstance(city["name"], str) or not city["name"]):
                raise ValueError("Invalid native city coordinate")
        return cities
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise MapNavigationError("native_map_coordinates_invalid", str(exc)) from exc


def require_reference_client(app: Any) -> None:
    size = app.get_window_size()
    if not isinstance(size, (tuple, list)) or tuple(size) != REFERENCE_CLIENT:
        raise MapNavigationError(
            "map_navigation_window_size_invalid",
            "Native intercity navigation requires a 1280x720 client.",
            {"expected": list(REFERENCE_CLIENT), "actual": size},
        )


def screen_delta(source: Sequence[float], target: Sequence[float]) -> tuple[float, float]:
    """Native Y points up; screenshot Y points down."""
    return ((target[0] - source[0]) * SCREEN_SCALE,
            (source[1] - target[1]) * SCREEN_SCALE)


def coarse_drag(cities: dict[str, Any], source: str, target: str) -> tuple[float, float]:
    dx, dy = screen_delta(cities[source]["native_position"], cities[target]["native_position"])
    return -dx, -dy


def plan_drag_batch(delta: Sequence[float], safe_rect: Sequence[int], span: int) -> list[tuple[int, int]]:
    """Split the effective displacement, leaving room for threshold activation."""
    if len(delta) != 2 or not all(math.isfinite(float(value)) for value in delta):
        raise MapNavigationError("map_drag_invalid", "Map displacement must contain two finite numbers")
    dx, dy = (int(round(value)) for value in delta)
    cap_x, cap_y = min(span, safe_rect[2] - 64), min(span, safe_rect[3] - 64)
    if cap_x <= 0 or cap_y <= 0:
        raise MapNavigationError("map_drag_region_invalid", "Map drag region is too small")
    count = max(math.ceil(abs(dx) / cap_x), math.ceil(abs(dy) / cap_y))
    result = []
    previous_x = previous_y = 0
    for index in range(1, count + 1):
        x, y = round(dx * index / count), round(dy * index / count)
        result.append((x - previous_x, y - previous_y))
        previous_x, previous_y = x, y
    return result


def predict_target_from_labels(
    points: list[dict[str, Any]], cities: dict[str, Any], target: str,
) -> dict[str, Any]:
    """Use fresh city-label positions, rejecting conflicting map poses.

    Every city's label has the same native offset. Relative label-to-label
    coordinates therefore cancel that offset without treating text as an icon.
    """
    predictions = []
    for point in points:
        key = point["city_key"]
        dx, dy = screen_delta(cities[key]["native_position"], cities[target]["native_position"])
        predictions.append({**point, "predicted_target": [point["center"][0] + dx, point["center"][1] + dy]})
    if not predictions:
        raise MapNavigationError("map_anchor_not_found", "No supported city labels were recognized")

    clusters = set()
    for candidate in predictions:
        clusters.add(tuple(index for index, other in enumerate(predictions)
                           if max(abs(candidate["predicted_target"][axis] - other["predicted_target"][axis])
                                  for axis in (0, 1)) <= ANCHOR_TOLERANCE_PX))
    support = max(len(cluster) for cluster in clusters)
    winners = [cluster for cluster in clusters if len(cluster) == support]
    if len(winners) != 1:
        raise MapNavigationError(
            "map_anchors_inconsistent", "City labels do not agree on the current map position",
            {"predictions": predictions, "tolerance_px": ANCHOR_TOLERANCE_PX},
        )
    accepted = [predictions[index] for index in winners[0]]
    predicted = [median(row["predicted_target"][axis] for row in accepted) for axis in (0, 1)]
    return {"predicted_target": predicted, "screen_scale": SCREEN_SCALE,
            "anchors": accepted,
            "rejected_city_keys": [row["city_key"] for index, row in enumerate(predictions) if index not in winners[0]]}
