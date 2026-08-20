"""Warehouse item recognition helpers for Resonance PC player data."""

from __future__ import annotations

import copy
import json
import re
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import cv2
import numpy as np

from packages.aura_core.utils.exceptions import StopTaskException
from packages.aura_core.observability.logging.core_logger import logger


Region = Tuple[int, int, int, int]

STACK_POLICY_MERGE = "merge"
STACK_POLICY_SPLIT_BY_EXPIRY = "split_by_expiry"
_SUPPORTED_STACK_POLICIES = frozenset(
    {STACK_POLICY_MERGE, STACK_POLICY_SPLIT_BY_EXPIRY}
)

_PLAN_ROOT = Path(__file__).resolve().parents[2]
_INVENTORY_CATALOG_FILE = _PLAN_ROOT / "data" / "meta" / "inventory_items.json"
_DEFAULT_GRID_REGION: Region = (407, 104, 650, 616)
_DEFAULT_SCROLL_START = (1000, 620)
_DEFAULT_SCROLL_END = (1000, 300)
_DEFAULT_MATCH_THRESHOLD = 0.94
_DEFAULT_MAX_SCROLLS = 12


def _inventory_error(message: str) -> StopTaskException:
    return StopTaskException(f"Inventory refresh failed: {message}", success=False)


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _coerce_int_sequence(value: Any, *, length: int, label: str) -> Tuple[int, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValueError(f"inventory catalog {label} must contain {length} integers")
    try:
        return tuple(int(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"inventory catalog {label} must contain integers") from exc


def _catalog_by_item_id(catalog: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    default_policy = str(catalog.get("default_stack_policy") or STACK_POLICY_MERGE)
    if default_policy not in _SUPPORTED_STACK_POLICIES:
        raise ValueError(f"unsupported default inventory stack policy: {default_policy}")
    result: Dict[str, Dict[str, Any]] = {}
    items = catalog.get("items", [])
    if not isinstance(items, list):
        raise ValueError("inventory item catalog items must be a list")
    for raw_item in items:
        if not isinstance(raw_item, Mapping):
            raise ValueError("inventory item catalog entries must be objects")
        item_id = str(raw_item.get("item_id") or "").strip()
        if not item_id:
            raise ValueError("inventory item catalog entry is missing item_id")
        if item_id in result:
            raise ValueError(f"duplicate inventory item_id in catalog: {item_id}")
        policy = str(raw_item.get("stack_policy") or default_policy)
        if policy not in _SUPPORTED_STACK_POLICIES:
            raise ValueError(f"unsupported inventory stack policy for {item_id}: {policy}")
        entry = dict(raw_item)
        entry["stack_policy"] = policy
        result[item_id] = entry
    return result


def load_inventory_catalog(
    catalog_path: Optional[Path] = None,
    *,
    plan_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Load and validate the supported item catalog and its 100x70 templates."""

    path = Path(catalog_path or _INVENTORY_CATALOG_FILE)
    root = Path(plan_root or _PLAN_ROOT).resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to load inventory item catalog: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("inventory item catalog must be a JSON object")
    layout = payload.get("layout")
    if not isinstance(layout, dict):
        raise ValueError("inventory item catalog is missing layout")

    template_size = _coerce_int_sequence(
        layout.get("template_size"), length=2, label="template_size"
    )
    template_offset = _coerce_int_sequence(
        layout.get("template_offset_from_card"),
        length=2,
        label="template_offset_from_card",
    )
    card_size = _coerce_int_sequence(
        layout.get("card_size", (122, 121)), length=2, label="card_size"
    )
    grid_region = _coerce_int_sequence(
        layout.get("grid_region", _DEFAULT_GRID_REGION),
        length=4,
        label="grid_region",
    )
    expiry_roi = _coerce_int_sequence(
        layout.get("expiry_roi_from_template"),
        length=4,
        label="expiry_roi_from_template",
    )
    count_roi = _coerce_int_sequence(
        layout.get("count_roi_from_template"),
        length=4,
        label="count_roi_from_template",
    )
    if template_size != (100, 70):
        raise ValueError("inventory templates must be exactly 100x70")
    if any(value <= 0 for value in (*template_size, *card_size, grid_region[2], grid_region[3])):
        raise ValueError("inventory catalog dimensions must be positive")

    items_by_id = _catalog_by_item_id(payload)
    if not items_by_id:
        raise ValueError("inventory item catalog must contain at least one supported item")
    normalized_items: List[Dict[str, Any]] = []
    for item_id, raw_entry in items_by_id.items():
        template_ref = str(raw_entry.get("template") or "").strip()
        if not template_ref:
            raise ValueError(f"inventory item {item_id} is missing template")
        template_path = (root / template_ref).resolve()
        if not _path_is_within(template_path, root):
            raise ValueError(f"inventory item template escapes plan root: {item_id}")
        template_image = cv2.imread(str(template_path), cv2.IMREAD_COLOR)
        if template_image is None:
            raise ValueError(f"unable to read inventory item template: {template_path}")
        actual_size = (int(template_image.shape[1]), int(template_image.shape[0]))
        if actual_size != template_size:
            raise ValueError(
                f"inventory item template {item_id} must be {template_size[0]}x{template_size[1]}, "
                f"got {actual_size[0]}x{actual_size[1]}"
            )
        entry = dict(raw_entry)
        entry["_template_path"] = str(template_path)
        entry["_template_image"] = template_image
        normalized_items.append(entry)

    result = copy.deepcopy(payload)
    result["layout"] = {
        **layout,
        "template_size": template_size,
        "template_offset_from_card": template_offset,
        "card_size": card_size,
        "grid_region": grid_region,
        "expiry_roi_from_template": expiry_roi,
        "count_roi_from_template": count_roi,
    }
    result["items"] = normalized_items
    result["_items_by_id"] = {item["item_id"]: item for item in normalized_items}
    return result


def relative_roi(
    match_top_left: Sequence[int],
    spec: Sequence[int],
    frame_shape: Sequence[int],
) -> Optional[Region]:
    """Resolve a template-relative ROI, or None for a partially visible card."""

    if len(match_top_left) != 2 or len(spec) != 4 or len(frame_shape) < 2:
        raise ValueError("invalid relative ROI arguments")
    x = int(match_top_left[0]) + int(spec[0])
    y = int(match_top_left[1]) + int(spec[1])
    width = int(spec[2])
    height = int(spec[3])
    frame_height = int(frame_shape[0])
    frame_width = int(frame_shape[1])
    if width <= 0 or height <= 0:
        raise ValueError("relative ROI dimensions must be positive")
    if x < 0 or y < 0 or x + width > frame_width or y + height > frame_height:
        return None
    return (x, y, width, height)


def parse_count_text(text: str) -> Optional[int]:
    runs = re.findall(r"\d+", str(text or ""))
    if not runs:
        return None
    value = int(max(runs, key=len))
    return value if value > 0 else None


def parse_expiry_text(text: str) -> Optional[Dict[str, Any]]:
    raw = str(text or "").strip()
    compact = re.sub(r"\s+", "", raw)
    for pattern, kind in (
        (r"(\d+)(?:小时|时)", "hours_remaining"),
        (r"(\d+)(?:分钟|分)", "minutes_remaining"),
        (r"(\d+)天", "days_remaining"),
    ):
        match = re.search(pattern, compact)
        if match:
            return {"kind": kind, "value": int(match.group(1)), "raw": raw}
    fallback = re.search(r"\d+", compact)
    if fallback:
        return {"kind": "days_remaining", "value": int(fallback.group(0)), "raw": raw}
    return None


def _text_of_ocr_result(item: Any) -> str:
    if isinstance(item, Mapping):
        return str(item.get("text") or "")
    return str(getattr(item, "text", "") or "")


def _ocr_variants(image: np.ndarray) -> List[np.ndarray]:
    enlarged = cv2.resize(image, None, fx=4.0, fy=4.0, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(enlarged, cv2.COLOR_BGR2GRAY) if enlarged.ndim == 3 else enlarged
    _, thresholded = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return [enlarged, gray, thresholded]


def _read_parsed_ocr(
    image: np.ndarray,
    ocr: Any,
    parser: Callable[[str], Any],
    *,
    label: str,
) -> Any:
    observed: List[str] = []
    for variant in _ocr_variants(image):
        try:
            multi = ocr.recognize_all(source_image=variant)
        except Exception as exc:
            observed.append(f"<ocr-error:{exc}>")
            continue
        text = " ".join(
            part
            for part in (
                _text_of_ocr_result(item).strip()
                for item in getattr(multi, "results", [])
            )
            if part
        )
        observed.append(text)
        parsed = parser(text)
        if parsed is not None:
            return parsed
    raise _inventory_error(f"unable to read {label}; OCR={observed}")


def _find_template_instances(
    source_image: np.ndarray,
    template_image: np.ndarray,
    *,
    threshold: float,
) -> List[Dict[str, Any]]:
    """Find every instance with local-peak suppression scoped to this reader."""

    score_map = cv2.matchTemplate(source_image, template_image, cv2.TM_CCOEFF_NORMED)
    local_max = cv2.dilate(score_map, np.ones((3, 3), dtype=np.uint8))
    ys, xs = np.where((score_map >= float(threshold)) & (score_map >= local_max - 1e-7))
    candidates = sorted(
        ((float(score_map[y, x]), int(x), int(y)) for y, x in zip(ys, xs)),
        reverse=True,
    )
    template_width = int(template_image.shape[1])
    template_height = int(template_image.shape[0])
    kept: List[Dict[str, Any]] = []
    for confidence, x, y in candidates:
        if any(
            abs(x - int(item["top_left"][0])) < template_width // 2
            and abs(y - int(item["top_left"][1])) < template_height // 2
            for item in kept
        ):
            continue
        kept.append(
            {
                "top_left": (x, y),
                "confidence": confidence,
                "rect": (x, y, template_width, template_height),
            }
        )
    return kept


def _suppress_cross_template_overlaps(
    candidates: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    kept: List[Dict[str, Any]] = []
    for candidate in sorted(candidates, key=lambda item: float(item["confidence"]), reverse=True):
        anchor = candidate["card_top_left"]
        if any(
            abs(int(anchor[0]) - int(other["card_top_left"][0])) < 40
            and abs(int(anchor[1]) - int(other["card_top_left"][1])) < 40
            for other in kept
        ):
            continue
        kept.append(candidate)
    return kept


def scan_inventory_page(
    page_image: np.ndarray,
    catalog: Mapping[str, Any],
    ocr: Any,
) -> List[Dict[str, Any]]:
    """Recognize all fully visible supported cards in one warehouse frame."""

    if page_image is None or not isinstance(page_image, np.ndarray) or page_image.size == 0:
        raise _inventory_error("warehouse grid capture is empty")
    layout = catalog.get("layout")
    items = catalog.get("items")
    if not isinstance(layout, Mapping) or not isinstance(items, list):
        raise ValueError("inventory catalog must be loaded with load_inventory_catalog")
    template_offset = tuple(int(value) for value in layout["template_offset_from_card"])
    card_width, card_height = (int(value) for value in layout["card_size"])
    threshold = float(layout.get("match_threshold", _DEFAULT_MATCH_THRESHOLD))

    candidates: List[Dict[str, Any]] = []
    for item in items:
        template_image = item.get("_template_image")
        if not isinstance(template_image, np.ndarray):
            template_image = cv2.imread(str(item.get("_template_path") or ""), cv2.IMREAD_COLOR)
        if template_image is None:
            raise ValueError(f"inventory item template is unavailable: {item.get('item_id')}")
        score_map = cv2.matchTemplate(page_image, template_image, cv2.TM_CCOEFF_NORMED)
        best_confidence = float(cv2.minMaxLoc(score_map)[1])
        matches = _find_template_instances(
            page_image,
            template_image,
            threshold=float(item.get("match_threshold", threshold)),
        )
        logger.info(
            "Inventory template scan: item_id=%s best_confidence=%.4f matches=%s",
            item.get("item_id"),
            best_confidence,
            len(matches),
        )
        for match in matches:
            match_x, match_y = match["top_left"]
            card_top_left = (int(match_x) - template_offset[0], int(match_y) - template_offset[1])
            if relative_roi(card_top_left, (0, 0, card_width, card_height), page_image.shape) is None:
                continue
            candidates.append({**match, "item": item, "card_top_left": card_top_left})

    observations: List[Dict[str, Any]] = []
    for candidate in _suppress_cross_template_overlaps(candidates):
        match_top_left = candidate["top_left"]
        item = candidate["item"]
        count_region = relative_roi(
            match_top_left, layout["count_roi_from_template"], page_image.shape
        )
        if count_region is None:
            continue
        count_x, count_y, count_width, count_height = count_region
        count = _read_parsed_ocr(
            page_image[count_y : count_y + count_height, count_x : count_x + count_width],
            ocr,
            parse_count_text,
            label=f"count for {item['item_id']}",
        )
        observation: Dict[str, Any] = {
            "item_id": str(item["item_id"]),
            "name": str(item.get("name") or item["item_id"]),
            "count": int(count),
            "confidence": float(candidate["confidence"]),
            "card_top_left": [int(candidate["card_top_left"][0]), int(candidate["card_top_left"][1])],
        }
        if item.get("stack_policy") == STACK_POLICY_SPLIT_BY_EXPIRY:
            expiry_region = relative_roi(
                match_top_left, layout["expiry_roi_from_template"], page_image.shape
            )
            if expiry_region is None:
                continue
            expiry_x, expiry_y, expiry_width, expiry_height = expiry_region
            observation["expiry"] = _read_parsed_ocr(
                page_image[expiry_y : expiry_y + expiry_height, expiry_x : expiry_x + expiry_width],
                ocr,
                parse_expiry_text,
                label=f"expiry for {item['item_id']}",
            )
        observations.append(observation)
        logger.info(
            "Inventory observation: item_id=%s count=%s expiry=%s confidence=%.4f anchor=%s",
            observation["item_id"],
            observation["count"],
            observation.get("expiry"),
            observation["confidence"],
            observation["card_top_left"],
        )
    return observations


def _capture_grid(app: Any, region: Region) -> np.ndarray:
    capture = app.capture(rect=region)
    if not capture.success or capture.image is None:
        raise _inventory_error(f"warehouse grid capture failed for region {region}")
    return capture.image


def _capture_stable_grid(
    app: Any,
    region: Region,
    *,
    attempts: int = 5,
    interval_sec: float = 0.2,
) -> np.ndarray:
    previous = _capture_grid(app, region)
    for _ in range(max(int(attempts), 1)):
        time.sleep(max(float(interval_sec), 0.01))
        current = _capture_grid(app, region)
        if previous.shape == current.shape and float(cv2.absdiff(previous, current).mean()) <= 1.5:
            return current
        previous = current
    return previous


def _observation_content_key(observation: Mapping[str, Any]) -> Tuple[Any, ...]:
    expiry = observation.get("expiry")
    expiry_key = (
        (expiry.get("kind"), expiry.get("value"))
        if isinstance(expiry, Mapping)
        else (None, None)
    )
    return (observation.get("item_id"), observation.get("count"), *expiry_key)


def _estimate_scroll_delta(
    previous_image: np.ndarray,
    current_image: np.ndarray,
    previous_observations: Sequence[Mapping[str, Any]],
    current_observations: Sequence[Mapping[str, Any]],
) -> Tuple[int, float]:
    content_deltas: List[int] = []
    for previous in previous_observations:
        previous_x, previous_y = previous["card_top_left"]
        for current in current_observations:
            current_x, current_y = current["card_top_left"]
            delta = int(previous_y) - int(current_y)
            if (
                _observation_content_key(previous) == _observation_content_key(current)
                and abs(int(previous_x) - int(current_x)) <= 5
                and 0 <= delta <= 500
            ):
                content_deltas.append(delta)
    if content_deltas:
        return int(round(float(median(content_deltas)))), 1.0

    previous_gray = cv2.cvtColor(previous_image, cv2.COLOR_BGR2GRAY)
    current_gray = cv2.cvtColor(current_image, cv2.COLOR_BGR2GRAY)
    height = previous_gray.shape[0]
    strip_height = min(160, max(height // 4, 40))
    strip_bottom = max(height - 20, strip_height)
    strip_top = strip_bottom - strip_height
    template = previous_gray[strip_top:strip_bottom, :]
    if template.shape[0] > current_gray.shape[0] or template.shape[1] > current_gray.shape[1]:
        return 0, 0.0
    score_map = cv2.matchTemplate(current_gray, template, cv2.TM_CCOEFF_NORMED)
    _, confidence, _, location = cv2.minMaxLoc(score_map)
    delta = int(strip_top - location[1])
    if delta < 0 or delta > 500:
        return 0, float(confidence)
    return delta, float(confidence)


def _dedupe_physical_observations(
    observations: Iterable[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    kept: List[Dict[str, Any]] = []
    for raw in observations:
        observation = dict(raw)
        virtual_x, virtual_y = observation["_virtual_card_top_left"]
        duplicate = any(
            observation.get("item_id") == previous.get("item_id")
            and abs(int(virtual_x) - int(previous["_virtual_card_top_left"][0])) <= 6
            and abs(int(virtual_y) - int(previous["_virtual_card_top_left"][1])) <= 8
            for previous in kept
        )
        if not duplicate:
            kept.append(observation)
    return kept


def _expiry_key(observation: Mapping[str, Any]) -> Tuple[str, Any, str]:
    expiry = observation.get("expiry")
    if not isinstance(expiry, Mapping):
        raise ValueError("expiry is required for an inventory item using split_by_expiry")
    kind = str(expiry.get("kind") or "").strip()
    value = expiry.get("value")
    raw = str(expiry.get("raw") or "").strip()
    if not kind or value is None:
        raise ValueError(
            "expiry kind and value are required for an inventory item using split_by_expiry"
        )
    return kind, value, raw


def aggregate_inventory_observations(
    observations: Iterable[Mapping[str, Any]],
    catalog: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    """Aggregate de-duplicated observations according to catalog policy."""

    catalog_items = _catalog_by_item_id(catalog)
    grouped: "OrderedDict[Tuple[Any, ...], Dict[str, Any]]" = OrderedDict()
    for raw_observation in observations:
        if not isinstance(raw_observation, Mapping):
            raise ValueError("inventory observations must be objects")
        item_id = str(raw_observation.get("item_id") or "").strip()
        if item_id not in catalog_items:
            raise ValueError(f"inventory observation has unknown item_id: {item_id}")
        try:
            count = int(raw_observation.get("count"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"inventory count is invalid for {item_id}") from exc
        if count <= 0:
            raise ValueError(f"inventory count must be positive for {item_id}")
        entry = catalog_items[item_id]
        policy = str(entry["stack_policy"])
        expiry = None
        if policy == STACK_POLICY_SPLIT_BY_EXPIRY:
            expiry_key = _expiry_key(raw_observation)
            key: Tuple[Any, ...] = (item_id, *expiry_key[:2])
            expiry = {"kind": expiry_key[0], "value": expiry_key[1], "raw": expiry_key[2]}
        else:
            key = (item_id,)
        if key not in grouped:
            item = {
                "item_id": item_id,
                "name": str(entry.get("name") or raw_observation.get("name") or item_id),
                "count": 0,
            }
            if expiry is not None:
                item["expiry"] = expiry
            grouped[key] = item
        grouped[key]["count"] = int(grouped[key]["count"]) + count
    return [dict(item) for item in grouped.values()]


def read_inventory_items(
    app: Any,
    ocr: Any,
    *,
    catalog_path: Optional[Path] = None,
    max_scrolls: int = _DEFAULT_MAX_SCROLLS,
) -> Dict[str, Any]:
    """Read supported items, stopping immediately once every type is found."""

    catalog = load_inventory_catalog(catalog_path)
    layout = catalog["layout"]
    region = tuple(int(value) for value in layout["grid_region"])
    scroll_start = tuple(int(value) for value in layout.get("scroll_start", _DEFAULT_SCROLL_START))
    scroll_end = tuple(int(value) for value in layout.get("scroll_end", _DEFAULT_SCROLL_END))
    supported_ids = {str(item["item_id"]) for item in catalog["items"]}
    seen_ids: set[str] = set()
    all_observations: List[Dict[str, Any]] = []
    previous_image: Optional[np.ndarray] = None
    previous_observations: List[Dict[str, Any]] = []
    virtual_scroll_y = 0
    stationary_scrolls = 0
    pages_scanned = 0
    completion_reason = ""
    page_image = _capture_stable_grid(app, region)

    while True:
        page_observations = scan_inventory_page(page_image, catalog, ocr)
        pages_scanned += 1
        if previous_image is not None:
            scroll_delta, confidence = _estimate_scroll_delta(
                previous_image, page_image, previous_observations, page_observations
            )
            if scroll_delta <= 2:
                stationary_scrolls += 1
            else:
                stationary_scrolls = 0
                virtual_scroll_y += int(scroll_delta)
            if scroll_delta > 2 and confidence < 0.55:
                raise _inventory_error(
                    f"unable to align overlapping warehouse pages (confidence={confidence:.3f})"
                )
        for observation in page_observations:
            item = dict(observation)
            card_x, card_y = item["card_top_left"]
            item["_virtual_card_top_left"] = [int(card_x), int(card_y) + int(virtual_scroll_y)]
            all_observations.append(item)
            seen_ids.add(str(item["item_id"]))
        if supported_ids.issubset(seen_ids):
            completion_reason = "all_supported_items_found"
            break
        if stationary_scrolls >= 2:
            completion_reason = "warehouse_bottom_reached"
            break
        if pages_scanned > max(int(max_scrolls), 0):
            raise _inventory_error("warehouse scan exceeded maximum scroll count")
        previous_image = page_image
        previous_observations = page_observations
        app.drag(
            int(scroll_start[0]),
            int(scroll_start[1]),
            int(scroll_end[0]),
            int(scroll_end[1]),
            duration=0.5,
        )
        time.sleep(0.4)
        page_image = _capture_stable_grid(app, region)

    unique_observations = _dedupe_physical_observations(all_observations)
    public_observations = [
        {
            key: copy.deepcopy(value)
            for key, value in observation.items()
            if not key.startswith("_") and key not in {"confidence", "card_top_left"}
        }
        for observation in unique_observations
    ]
    return {
        "category": "items",
        "scan_scope": "catalog_only",
        "catalog_schema_version": int(catalog.get("schema_version", 1)),
        "supported_item_count": len(supported_ids),
        "matched_stack_count": len(unique_observations),
        "pages_scanned": pages_scanned,
        "scan_complete": True,
        "completion_reason": completion_reason,
        "source": "template+ocr",
        "scanned_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "items": aggregate_inventory_observations(public_observations, catalog),
    }
