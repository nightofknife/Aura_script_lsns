"""Warehouse item recognition helpers for Resonance PC player data."""

from __future__ import annotations

import copy
import json
import os
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

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
_INVENTORY_MATERIAL_CATALOG_FILE = (
    _PLAN_ROOT / "data" / "meta" / "inventory_materials.json"
)
_INVENTORY_EQUIPMENT_CATALOG_FILE = (
    _PLAN_ROOT / "data" / "meta" / "inventory_equipment.json"
)
_INVENTORY_DIGIT_CATALOG_FILE = _PLAN_ROOT / "data" / "meta" / "inventory_digits.json"
_INVENTORY_EXPIRY_DIGIT_CATALOG_FILE = (
    _PLAN_ROOT / "data" / "meta" / "inventory_expiry_digits.json"
)
_DEFAULT_GRID_REGION: Region = (397, 94, 680, 626)
_DEFAULT_SCROLL_START = (1000, 620)
_DEFAULT_SCROLL_END = (1000, 310)
_DEFAULT_MATCH_THRESHOLD = 0.94
_DEFAULT_MAX_SCROLLS = 30
_SCROLL_HOLD_BEFORE_RELEASE_SEC = 0.5
_DEBUG_CAPTURE_DIR_ENV = "AURA_INVENTORY_DEBUG_CAPTURE_DIR"
_PLAN_KEY = "resonance_pc"
_COUNT_MODE_DIGIT_TEMPLATE = "digit_template"
_COUNT_MODE_CARD_INSTANCES = "card_instances"
_CATEGORY_SPECS: Dict[str, Dict[str, Any]] = {
    "items": {
        "catalog_path": _INVENTORY_CATALOG_FILE,
        "entry_key": "items",
        "id_key": "item_id",
        "result_key": "items",
        "supported_key": "supported_item_count",
        "template_size": (100, 70),
        "count_mode": _COUNT_MODE_DIGIT_TEMPLATE,
        "supports_expiry": True,
    },
    "materials": {
        "catalog_path": _INVENTORY_MATERIAL_CATALOG_FILE,
        "entry_key": "materials",
        "id_key": "material_id",
        "result_key": "materials",
        "supported_key": "supported_material_count",
        "template_size": (50, 35),
        "count_mode": _COUNT_MODE_DIGIT_TEMPLATE,
        "supports_expiry": False,
    },
    "equipment": {
        "catalog_path": _INVENTORY_EQUIPMENT_CATALOG_FILE,
        "entry_key": "equipment",
        "id_key": "equipment_id",
        "result_key": "equipment",
        "supported_key": "supported_equipment_count",
        "template_size": (100, 60),
        "count_mode": _COUNT_MODE_CARD_INSTANCES,
        "supports_expiry": False,
    },
}
# Temporary live-test switch: keep recognizing item stacks/counts, but do not
# read or persist expiry values until the expiry digit templates are revisited.
_EXPIRY_RECOGNITION_ENABLED = False


def _inventory_error(message: str) -> StopTaskException:
    return StopTaskException(f"Inventory refresh failed: {message}", success=False)


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _load_image_file(vision: Any, path: Path, flags: int) -> np.ndarray:
    loader = getattr(vision, "load_image_file", None)
    if not callable(loader):
        raise RuntimeError("framework vision image loader is required for inventory matching")
    return loader(path, flags)


def _coerce_int_sequence(value: Any, *, length: int, label: str) -> Tuple[int, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValueError(f"inventory catalog {label} must contain {length} integers")
    try:
        return tuple(int(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"inventory catalog {label} must contain integers") from exc


def _catalog_category(catalog: Mapping[str, Any]) -> str:
    category = str(catalog.get("category") or "items").strip()
    if category not in _CATEGORY_SPECS:
        raise ValueError(f"unsupported inventory catalog category: {category}")
    return category


def _catalog_by_item_id(catalog: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    category = _catalog_category(catalog)
    spec = _CATEGORY_SPECS[category]
    entry_key = str(spec["entry_key"])
    id_key = str(spec["id_key"])
    default_policy = str(catalog.get("default_stack_policy") or STACK_POLICY_MERGE)
    if default_policy not in _SUPPORTED_STACK_POLICIES:
        raise ValueError(f"unsupported default inventory stack policy: {default_policy}")
    result: Dict[str, Dict[str, Any]] = {}
    items = catalog.get(entry_key, catalog.get("items", []))
    if not isinstance(items, list):
        raise ValueError("inventory item catalog items must be a list")
    for raw_item in items:
        if not isinstance(raw_item, Mapping):
            raise ValueError("inventory item catalog entries must be objects")
        item_id = str(raw_item.get(id_key) or raw_item.get("item_id") or "").strip()
        if not item_id:
            raise ValueError("inventory item catalog entry is missing item_id")
        if item_id in result:
            raise ValueError(f"duplicate inventory item_id in catalog: {item_id}")
        policy = str(raw_item.get("stack_policy") or default_policy)
        if policy not in _SUPPORTED_STACK_POLICIES:
            raise ValueError(f"unsupported inventory stack policy for {item_id}: {policy}")
        entry = dict(raw_item)
        entry["item_id"] = item_id
        entry["stack_policy"] = policy
        result[item_id] = entry
    return result


def load_inventory_digit_catalog(
    catalog_path: Optional[Path] = None,
    *,
    plan_root: Optional[Path] = None,
    vision: Any = None,
) -> Dict[str, Any]:
    """Load the normalized 0-9 templates and count segmentation parameters."""

    path = Path(catalog_path or _INVENTORY_DIGIT_CATALOG_FILE)
    root = Path(plan_root or _PLAN_ROOT).resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to load inventory digit catalog: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("inventory digit catalog must be a JSON object")

    template_root_ref = str(payload.get("template_root") or "").strip()
    if not template_root_ref:
        raise ValueError("inventory digit catalog is missing template_root")
    template_root = (root / template_root_ref).resolve()
    if not _path_is_within(template_root, root) or not template_root.is_dir():
        raise ValueError(f"inventory digit template root is unavailable: {template_root}")

    digits = payload.get("digits")
    if digits != list("0123456789"):
        raise ValueError("inventory digit catalog digits must be exactly 0 through 9")
    count_band = _coerce_int_sequence(
        payload.get("count_band_from_card"), length=4, label="count_band_from_card"
    )
    component_width = _coerce_int_sequence(
        payload.get("component_width"), length=2, label="component_width"
    )
    component_height = _coerce_int_sequence(
        payload.get("component_height"), length=2, label="component_height"
    )
    normalized_size = _coerce_int_sequence(
        payload.get("normalized_size"), length=2, label="normalized_size"
    )
    digit_gap = _coerce_int_sequence(
        payload.get("digit_gap"), length=2, label="digit_gap"
    )
    right_edge_gap = _coerce_int_sequence(
        payload.get("right_edge_gap"), length=2, label="right_edge_gap"
    )
    if any(
        value <= 0
        for value in (
            count_band[2],
            count_band[3],
            *component_width,
            *component_height,
            *normalized_size,
        )
    ):
        raise ValueError("inventory digit catalog dimensions must be positive")
    white_min = int(payload.get("white_min", 165))
    raw_white_min_candidates = payload.get("white_min_candidates", [white_min])
    if not isinstance(raw_white_min_candidates, list) or not raw_white_min_candidates:
        raise ValueError("inventory digit white_min_candidates must be a non-empty list")
    try:
        white_min_candidates = [int(value) for value in raw_white_min_candidates]
    except (TypeError, ValueError) as exc:
        raise ValueError("inventory digit white_min_candidates must contain integers") from exc
    if (
        not 0 <= white_min <= 255
        or any(not 0 <= value <= 255 for value in white_min_candidates)
        or len(white_min_candidates) != len(set(white_min_candidates))
        or white_min not in white_min_candidates
    ):
        raise ValueError(
            "inventory digit white_min_candidates must contain unique 0-255 values "
            "including white_min"
        )

    templates: Dict[str, List[np.ndarray]] = {}
    template_paths: Dict[str, List[str]] = {}
    expected_width, expected_height = normalized_size
    for digit in digits:
        digit_dir = (template_root / digit).resolve()
        if not _path_is_within(digit_dir, template_root) or not digit_dir.is_dir():
            raise ValueError(f"inventory digit template directory is unavailable: {digit_dir}")
        paths = sorted(digit_dir.glob("*.png"))
        if not paths:
            raise ValueError(f"inventory digit {digit} has no templates")
        samples: List[np.ndarray] = []
        for template_path in paths:
            if not _path_is_within(template_path.resolve(), digit_dir):
                raise ValueError(f"inventory digit template escapes digit directory: {template_path}")
            try:
                image = _load_image_file(vision, template_path, cv2.IMREAD_GRAYSCALE)
            except (OSError, ValueError) as exc:
                raise ValueError(
                    f"unable to read inventory digit template: {template_path}"
                ) from exc
            if image.shape[:2] != (expected_height, expected_width):
                raise ValueError(
                    f"inventory digit template {template_path} must be "
                    f"{expected_width}x{expected_height}"
                )
            _, binary = cv2.threshold(image, 127, 255, cv2.THRESH_BINARY)
            samples.append(binary)
        templates[digit] = samples
        template_paths[digit] = [str(item) for item in paths]

    result = copy.deepcopy(payload)
    result.update(
        {
            "count_band_from_card": count_band,
            "component_width": component_width,
            "component_height": component_height,
            "normalized_size": normalized_size,
            "digit_gap": digit_gap,
            "right_edge_gap": right_edge_gap,
            "white_min": white_min,
            "white_min_candidates": white_min_candidates,
            "_templates": templates,
            "_template_paths": template_paths,
        }
    )
    return result


def load_inventory_expiry_digit_catalog(
    catalog_path: Optional[Path] = None,
    *,
    plan_root: Optional[Path] = None,
    vision: Any = None,
) -> Dict[str, Any]:
    """Load the currently available expiry digit templates and segmentation rules."""

    path = Path(catalog_path or _INVENTORY_EXPIRY_DIGIT_CATALOG_FILE)
    root = Path(plan_root or _PLAN_ROOT).resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to load inventory expiry digit catalog: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("inventory expiry digit catalog must be a JSON object")

    template_root_ref = str(payload.get("template_root") or "").strip()
    if not template_root_ref:
        raise ValueError("inventory expiry digit catalog is missing template_root")
    template_root = (root / template_root_ref).resolve()
    if not _path_is_within(template_root, root) or not template_root.is_dir():
        raise ValueError(
            f"inventory expiry digit template root is unavailable: {template_root}"
        )

    raw_digits = payload.get("available_digits")
    if not isinstance(raw_digits, list):
        raise ValueError("inventory expiry available_digits must be a list")
    digits = [str(digit) for digit in raw_digits]
    if len(digits) < 2 or len(digits) != len(set(digits)):
        raise ValueError(
            "inventory expiry available_digits must contain at least two unique digits"
        )
    if any(digit not in set("0123456789") for digit in digits):
        raise ValueError("inventory expiry available_digits must contain only 0 through 9")

    digit_x_range = _coerce_int_sequence(
        payload.get("digit_x_range"), length=2, label="expiry digit_x_range"
    )
    component_width = _coerce_int_sequence(
        payload.get("component_width"), length=2, label="expiry component_width"
    )
    component_height = _coerce_int_sequence(
        payload.get("component_height"), length=2, label="expiry component_height"
    )
    component_top = _coerce_int_sequence(
        payload.get("component_top"), length=2, label="expiry component_top"
    )
    normalized_size = _coerce_int_sequence(
        payload.get("normalized_size"), length=2, label="expiry normalized_size"
    )
    digit_gap = _coerce_int_sequence(
        payload.get("digit_gap"), length=2, label="expiry digit_gap"
    )
    similarity_mode = str(payload.get("similarity_mode") or "").strip()
    if similarity_mode != "gaussian_cosine":
        raise ValueError(
            "inventory expiry similarity_mode must be gaussian_cosine"
        )
    gaussian_sigma = float(payload.get("gaussian_sigma", 0.0))
    if not (0.0 < gaussian_sigma <= 3.0):
        raise ValueError("inventory expiry gaussian_sigma must be between 0 and 3")
    max_digits = int(payload.get("max_digits", 2))
    if max_digits != 2:
        raise ValueError("inventory expiry max_digits must be exactly 2")
    if (
        digit_x_range[0] < 0
        or digit_x_range[0] >= digit_x_range[1]
        or component_top[0] < 0
        or component_top[0] > component_top[1]
        or digit_gap[0] < 0
        or digit_gap[0] > digit_gap[1]
        or any(value <= 0 for value in (*component_width, *component_height, *normalized_size))
    ):
        raise ValueError("inventory expiry digit catalog dimensions are invalid")

    templates: Dict[str, List[np.ndarray]] = {}
    template_paths: Dict[str, List[str]] = {}
    expected_width, expected_height = normalized_size
    for digit in digits:
        digit_dir = (template_root / digit).resolve()
        if not _path_is_within(digit_dir, template_root) or not digit_dir.is_dir():
            raise ValueError(
                f"inventory expiry digit template directory is unavailable: {digit_dir}"
            )
        paths = sorted(digit_dir.glob("*.png"))
        if len(paths) != 1:
            raise ValueError(
                f"inventory expiry digit {digit} must contain exactly one template"
            )
        samples: List[np.ndarray] = []
        for template_path in paths:
            if not _path_is_within(template_path.resolve(), digit_dir):
                raise ValueError(
                    f"inventory expiry digit template escapes digit directory: {template_path}"
                )
            try:
                image = _load_image_file(vision, template_path, cv2.IMREAD_GRAYSCALE)
            except (OSError, ValueError) as exc:
                raise ValueError(
                    f"unable to read inventory expiry digit template: {template_path}"
                ) from exc
            if image.shape[:2] != (expected_height, expected_width):
                raise ValueError(
                    f"inventory expiry digit template {template_path} must be "
                    f"{expected_width}x{expected_height}"
                )
            _, binary = cv2.threshold(image, 127, 255, cv2.THRESH_BINARY)
            samples.append(binary)
        templates[digit] = samples
        template_paths[digit] = [str(item) for item in paths]

    result = copy.deepcopy(payload)
    result.update(
        {
            "available_digits": digits,
            "digit_x_range": digit_x_range,
            "component_width": component_width,
            "component_height": component_height,
            "component_top": component_top,
            "normalized_size": normalized_size,
            "digit_gap": digit_gap,
            "similarity_mode": similarity_mode,
            "gaussian_sigma": gaussian_sigma,
            "max_digits": max_digits,
            "_templates": templates,
            "_template_paths": template_paths,
        }
    )
    return result


def load_inventory_catalog(
    catalog_path: Optional[Path] = None,
    *,
    plan_root: Optional[Path] = None,
    digit_catalog_path: Optional[Path] = None,
    expiry_digit_catalog_path: Optional[Path] = None,
    vision: Any = None,
) -> Dict[str, Any]:
    """Load and validate one supported inventory category catalog."""

    path = Path(catalog_path or _INVENTORY_CATALOG_FILE)
    root = Path(plan_root or _PLAN_ROOT).resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to load inventory item catalog: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("inventory item catalog must be a JSON object")
    category = _catalog_category(payload)
    spec = _CATEGORY_SPECS[category]
    if category == "equipment" and str(payload.get("aggregation") or "").strip() != (
        "count_cards_by_equipment_id"
    ):
        raise ValueError(
            "inventory equipment catalog aggregation must be "
            "count_cards_by_equipment_id"
        )
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
    source_template_size: Optional[Tuple[int, ...]] = None
    recognition_scale: Optional[float] = None
    blur_kernel: Optional[Tuple[int, ...]] = None
    blur_sigma: Optional[float] = None
    if category == "materials":
        source_template_size = _coerce_int_sequence(
            layout.get("source_template_size"),
            length=2,
            label="source_template_size",
        )
        try:
            recognition_scale = float(layout.get("recognition_scale"))
            blur_sigma = float(layout.get("blur_sigma"))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "inventory materials recognition_scale and blur_sigma must be numbers"
            ) from exc
        blur_kernel = _coerce_int_sequence(
            layout.get("blur_kernel"), length=2, label="blur_kernel"
        )
        if source_template_size != (100, 70):
            raise ValueError("inventory materials source templates must be exactly 100x70")
        if recognition_scale != 0.5:
            raise ValueError("inventory materials recognition_scale must be exactly 0.5")
        if any(value <= 0 or value % 2 == 0 for value in blur_kernel):
            raise ValueError("inventory materials blur_kernel values must be positive and odd")
        if blur_sigma <= 0:
            raise ValueError("inventory materials blur_sigma must be positive")
    expiry_roi: Optional[Tuple[int, ...]] = None
    if bool(spec["supports_expiry"]):
        expiry_roi = _coerce_int_sequence(
            layout.get("expiry_roi_from_template"),
            length=4,
            label="expiry_roi_from_template",
        )
    elif layout.get("expiry_roi_from_template") is not None:
        expiry_roi = _coerce_int_sequence(
            layout.get("expiry_roi_from_template"),
            length=4,
            label="expiry_roi_from_template",
        )
    expected_template_size = tuple(int(value) for value in spec["template_size"])
    allowed_template_sizes = (
        {expected_template_size, (120, 50)}
        if category == "items"
        else {expected_template_size}
    )
    if template_size not in allowed_template_sizes:
        size_description = " or ".join(
            f"{width}x{height}" for width, height in sorted(allowed_template_sizes)
        )
        raise ValueError(
            f"inventory {category} templates must be exactly "
            f"{size_description}"
        )
    if any(value <= 0 for value in (*template_size, *card_size, grid_region[2], grid_region[3])):
        raise ValueError("inventory catalog dimensions must be positive")

    items_by_id = _catalog_by_item_id(payload)
    if not items_by_id:
        raise ValueError(
            f"inventory {category} catalog must contain at least one supported entry"
        )
    normalized_items: List[Dict[str, Any]] = []
    for item_id, raw_entry in items_by_id.items():
        template_ref = str(raw_entry.get("template") or "").strip()
        if not template_ref:
            raise ValueError(f"inventory item {item_id} is missing template")
        entry = dict(raw_entry)
        entry["template"] = template_ref
        normalized_items.append(entry)

    result = copy.deepcopy(payload)
    result["category"] = category
    normalized_layout = {
        **layout,
        "template_size": template_size,
        "template_offset_from_card": template_offset,
        "card_size": card_size,
        "grid_region": grid_region,
    }
    if category == "materials":
        normalized_layout.update(
            {
                "source_template_size": source_template_size,
                "recognition_scale": recognition_scale,
                "blur_kernel": blur_kernel,
                "blur_sigma": blur_sigma,
            }
        )
    if expiry_roi is not None:
        normalized_layout["expiry_roi_from_template"] = expiry_roi
    else:
        normalized_layout.pop("expiry_roi_from_template", None)
    result["layout"] = normalized_layout
    result["items"] = normalized_items
    result["_items_by_id"] = {item["item_id"]: item for item in normalized_items}
    result["_count_mode"] = str(spec["count_mode"])
    result["_supports_expiry"] = bool(spec["supports_expiry"])
    result["_digit_reader"] = (
        load_inventory_digit_catalog(
            digit_catalog_path,
            plan_root=root,
            vision=vision,
        )
        if spec["count_mode"] == _COUNT_MODE_DIGIT_TEMPLATE
        else None
    )
    result["_expiry_digit_reader"] = (
        load_inventory_expiry_digit_catalog(
            expiry_digit_catalog_path,
            plan_root=root,
            vision=vision,
        )
        if spec["supports_expiry"]
        else None
    )
    return result


def _resolve_inventory_template_paths(
    catalog: Dict[str, Any],
    vision: Any,
    *,
    plan_key: str = _PLAN_KEY,
    plan_root: Path = _PLAN_ROOT,
) -> List[str]:
    """Resolve catalog template references through the framework vision service."""

    if vision is None or not callable(getattr(vision, "resolve_template", None)):
        raise RuntimeError("framework vision service is required for inventory matching")
    items = catalog.get("items")
    if not isinstance(items, list):
        raise ValueError("inventory catalog items must be a list")
    root = Path(plan_root).resolve()
    layout = catalog.get("layout")
    if not isinstance(layout, Mapping):
        raise ValueError("inventory catalog is missing layout")
    template_width, template_height = (
        int(value) for value in layout["template_size"]
    )
    resolved_paths: List[str] = []
    resolved_images: List[np.ndarray] = []
    for item in items:
        template_ref = str(item.get("template") or "").strip()
        if not template_ref:
            raise ValueError(f"inventory item {item.get('item_id')} is missing template")
        template_path = Path(
            vision.resolve_template(str(plan_key), template_ref, Path(plan_root))
        ).resolve()
        if not _path_is_within(template_path, root):
            raise ValueError(
                f"inventory template escapes plan root for {item.get('item_id')}: "
                f"{template_path}"
            )
        if not template_path.is_file():
            raise ValueError(
                f"inventory template is unavailable for {item.get('item_id')}: "
                f"{template_path}"
            )
        try:
            template_image = _load_image_file(vision, template_path, cv2.IMREAD_UNCHANGED)
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"unable to read inventory template for {item.get('item_id')}: "
                f"{template_path}"
            ) from exc
        if template_image.shape[:2] != (template_height, template_width):
            raise ValueError(
                f"inventory template {template_path} must be exactly "
                f"{template_width}x{template_height}"
            )
        resolved_paths.append(str(template_path))
        resolved_images.append(template_image)
    catalog["_template_paths"] = resolved_paths
    if _catalog_category(catalog) == "materials":
        catalog["_template_images"] = resolved_images
    return resolved_paths


def prepare_inventory_catalog(
    category: str,
    vision: Any,
    *,
    catalog_path: Optional[Path] = None,
    plan_root: Optional[Path] = None,
    digit_catalog_path: Optional[Path] = None,
    expiry_digit_catalog_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Load, validate and resolve one warehouse category without UI input."""

    normalized_category = str(category or "").strip()
    if normalized_category not in _CATEGORY_SPECS:
        raise ValueError(
            "inventory category must be items, materials or equipment"
        )
    root = Path(plan_root or _PLAN_ROOT).resolve()
    default_catalog_path = Path(_CATEGORY_SPECS[normalized_category]["catalog_path"])
    catalog = load_inventory_catalog(
        catalog_path or default_catalog_path,
        plan_root=root,
        digit_catalog_path=digit_catalog_path,
        expiry_digit_catalog_path=expiry_digit_catalog_path,
        vision=vision,
    )
    actual_category = _catalog_category(catalog)
    if actual_category != normalized_category:
        raise ValueError(
            f"inventory catalog category mismatch: expected {normalized_category}, "
            f"got {actual_category}"
        )
    _resolve_inventory_template_paths(
        catalog,
        vision,
        plan_root=root,
    )
    return catalog


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


def build_quantity_white_mask(
    image: np.ndarray,
    digit_reader: Mapping[str, Any],
) -> np.ndarray:
    """Extract low-saturation white quantity glyphs from a card-bottom band."""

    if image is None or not isinstance(image, np.ndarray) or image.size == 0:
        raise ValueError("inventory quantity band is empty")
    if image.ndim == 2:
        blue = green = red = image
    elif image.ndim == 3 and image.shape[2] >= 3:
        blue, green, red = cv2.split(image[:, :, :3])
    else:
        raise ValueError("inventory quantity band has an unsupported shape")
    white_min = int(digit_reader.get("white_min", 165))
    max_channel_spread = int(digit_reader.get("max_channel_spread", 55))
    maximum = np.maximum.reduce((blue, green, red)).astype(np.int16)
    minimum = np.minimum.reduce((blue, green, red)).astype(np.int16)
    mask = (
        (blue >= white_min)
        & (green >= white_min)
        & (red >= white_min)
        & ((maximum - minimum) <= max_channel_spread)
    )
    return mask.astype(np.uint8) * 255


def segment_quantity_digits(
    card_image: np.ndarray,
    digit_reader: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    """Return the right-aligned digit run from one fully visible inventory card."""

    count_band = tuple(int(value) for value in digit_reader["count_band_from_card"])
    region = relative_roi((0, 0), count_band, card_image.shape)
    if region is None:
        return []
    band_x, band_y, band_width, band_height = region
    band = card_image[band_y : band_y + band_height, band_x : band_x + band_width]

    raw_white_mins = digit_reader.get(
        "white_min_candidates",
        [digit_reader.get("white_min", 165)],
    )
    if not isinstance(raw_white_mins, (list, tuple)) or not raw_white_mins:
        raw_white_mins = [digit_reader.get("white_min", 165)]
    runs: List[List[Dict[str, Any]]] = []
    for raw_white_min in raw_white_mins:
        candidate_reader = dict(digit_reader)
        candidate_reader["white_min"] = int(raw_white_min)
        mask = build_quantity_white_mask(band, candidate_reader)
        run = _segment_quantity_mask(mask, digit_reader, band_width)
        if run:
            runs.append(run)
    if not runs:
        return []

    min_score = float(digit_reader.get("min_digit_score", 0.75))
    min_margin = float(digit_reader.get("min_digit_margin", 0.02))

    def run_rank(run: List[Dict[str, Any]]) -> Tuple[int, int, float]:
        try:
            matches = [
                match_quantity_digit(component["glyph"], digit_reader)
                for component in run
            ]
        except Exception:
            return (0, len(run), 0.0)
        valid = all(
            float(match["score"]) >= min_score
            and float(match["margin"]) >= min_margin
            for match in matches
        )
        average_score = sum(float(match["score"]) for match in matches) / len(matches)
        return (int(valid), len(run), average_score)

    return max(runs, key=run_rank)


def _segment_quantity_mask(
    mask: np.ndarray,
    digit_reader: Mapping[str, Any],
    band_width: int,
) -> List[Dict[str, Any]]:
    """Segment one thresholded quantity mask into a right-aligned digit run."""

    component_count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, 8)
    min_width, max_width = (int(value) for value in digit_reader["component_width"])
    min_height, max_height = (int(value) for value in digit_reader["component_height"])
    min_top = int(digit_reader.get("component_top_min", 0))
    min_area = int(digit_reader.get("component_area_min", 1))
    candidates: List[Dict[str, Any]] = []
    for component_index in range(1, component_count):
        left, top, width, height, area = (
            int(value) for value in stats[component_index]
        )
        if not (min_width <= width <= max_width):
            continue
        if not (min_height <= height <= max_height):
            continue
        if top < min_top or area < min_area:
            continue
        candidates.append(
            {
                "rect": [left, top, width, height],
                "glyph": mask[top : top + height, left : left + width],
                "bottom": top + height,
            }
        )
    if not candidates:
        return []

    rightmost = max(candidates, key=lambda entry: entry["rect"][0] + entry["rect"][2])
    right_edge = int(rightmost["rect"][0]) + int(rightmost["rect"][2])
    right_gap = band_width - right_edge
    min_right_gap, max_right_gap = (
        int(value) for value in digit_reader["right_edge_gap"]
    )
    if not (min_right_gap <= right_gap <= max_right_gap):
        return []

    baseline_tolerance = int(digit_reader.get("baseline_tolerance", 2))
    baseline = int(rightmost["bottom"])
    aligned = sorted(
        (
            entry
            for entry in candidates
            if abs(int(entry["bottom"]) - baseline) <= baseline_tolerance
        ),
        key=lambda entry: entry["rect"][0],
    )
    rightmost_index = next(
        index for index, entry in enumerate(aligned) if entry is rightmost
    )
    min_gap, max_gap = (int(value) for value in digit_reader["digit_gap"])
    run = [rightmost]
    for candidate in reversed(aligned[:rightmost_index]):
        candidate_right = int(candidate["rect"][0]) + int(candidate["rect"][2])
        gap = int(run[0]["rect"][0]) - candidate_right
        if min_gap <= gap <= max_gap:
            run.insert(0, candidate)
            continue
        break
    return run


def normalize_quantity_digit(
    glyph: np.ndarray,
    digit_reader: Mapping[str, Any],
) -> np.ndarray:
    target_width, target_height = (
        int(value) for value in digit_reader["normalized_size"]
    )
    source_height, source_width = glyph.shape[:2]
    if source_width <= 0 or source_height <= 0:
        raise ValueError("inventory quantity glyph is empty")
    scale = min(
        (target_width - 4) / source_width,
        (target_height - 4) / source_height,
    )
    resized_width = max(1, int(round(source_width * scale)))
    resized_height = max(1, int(round(source_height * scale)))
    resized = cv2.resize(
        glyph,
        (resized_width, resized_height),
        interpolation=cv2.INTER_NEAREST,
    )
    canvas = np.zeros((target_height, target_width), dtype=np.uint8)
    offset_x = (target_width - resized_width) // 2
    offset_y = (target_height - resized_height) // 2
    canvas[offset_y : offset_y + resized_height, offset_x : offset_x + resized_width] = resized
    return canvas


def _digit_dice_score(left: np.ndarray, right: np.ndarray) -> float:
    left_mask = left > 0
    right_mask = right > 0
    denominator = int(left_mask.sum()) + int(right_mask.sum())
    if denominator == 0:
        return 0.0
    return float(2 * np.logical_and(left_mask, right_mask).sum() / denominator)


def _shifted_digit_score(
    glyph: np.ndarray,
    template: np.ndarray,
    tolerance: int,
) -> float:
    best = 0.0
    for offset_y in range(-tolerance, tolerance + 1):
        for offset_x in range(-tolerance, tolerance + 1):
            transform = np.float32([[1, 0, offset_x], [0, 1, offset_y]])
            shifted = cv2.warpAffine(
                glyph,
                transform,
                (glyph.shape[1], glyph.shape[0]),
                flags=cv2.INTER_NEAREST,
                borderValue=0,
            )
            best = max(best, _digit_dice_score(shifted, template))
    return best


def match_quantity_digit(
    glyph: np.ndarray,
    digit_reader: Mapping[str, Any],
) -> Dict[str, Any]:
    normalized = normalize_quantity_digit(glyph, digit_reader)
    templates = digit_reader.get("_templates")
    if not isinstance(templates, Mapping):
        raise ValueError("inventory digit templates are not loaded")
    tolerance = max(int(digit_reader.get("shift_tolerance", 1)), 0)
    scores = {
        str(digit): max(
            _shifted_digit_score(normalized, template, tolerance)
            for template in samples
        )
        for digit, samples in templates.items()
    }
    ranking = sorted(scores.items(), key=lambda entry: entry[1], reverse=True)
    if len(ranking) < 2:
        raise ValueError("inventory digit catalog must contain at least two digit classes")
    best_digit, best_score = ranking[0]
    second_digit, second_score = ranking[1]
    return {
        "digit": best_digit,
        "score": float(best_score),
        "margin": float(best_score - second_score),
        "second_digit": second_digit,
        "second_score": float(second_score),
    }


def _gaussian_blur_digit(image: np.ndarray, sigma: float) -> np.ndarray:
    return cv2.GaussianBlur(
        image.astype(np.float32) / 255.0,
        (0, 0),
        sigmaX=sigma,
        sigmaY=sigma,
    )


def _shifted_gaussian_cosine_score(
    glyph: np.ndarray,
    template: np.ndarray,
    tolerance: int,
    sigma: float,
) -> float:
    blurred_template = _gaussian_blur_digit(template, sigma)
    template_norm = float(np.linalg.norm(blurred_template))
    if template_norm <= 0:
        return 0.0
    best = 0.0
    for offset_y in range(-tolerance, tolerance + 1):
        for offset_x in range(-tolerance, tolerance + 1):
            transform = np.float32([[1, 0, offset_x], [0, 1, offset_y]])
            shifted = cv2.warpAffine(
                glyph,
                transform,
                (glyph.shape[1], glyph.shape[0]),
                flags=cv2.INTER_NEAREST,
                borderValue=0,
            )
            blurred_glyph = _gaussian_blur_digit(shifted, sigma)
            glyph_norm = float(np.linalg.norm(blurred_glyph))
            if glyph_norm <= 0:
                continue
            score = float(
                np.sum(blurred_glyph * blurred_template)
                / (glyph_norm * template_norm)
            )
            best = max(best, score)
    return best


def match_expiry_digit(
    glyph: np.ndarray,
    digit_reader: Mapping[str, Any],
) -> Dict[str, Any]:
    """Match an expiry digit while tolerating small rasterization differences."""

    normalized = normalize_quantity_digit(glyph, digit_reader)
    templates = digit_reader.get("_templates")
    if not isinstance(templates, Mapping):
        raise ValueError("inventory expiry digit templates are not loaded")
    tolerance = max(int(digit_reader.get("shift_tolerance", 1)), 0)
    sigma = float(digit_reader.get("gaussian_sigma", 0.9))
    scores = {
        str(digit): max(
            _shifted_gaussian_cosine_score(
                normalized,
                template,
                tolerance,
                sigma,
            )
            for template in samples
        )
        for digit, samples in templates.items()
    }
    ranking = sorted(scores.items(), key=lambda entry: entry[1], reverse=True)
    if len(ranking) < 2:
        raise ValueError("inventory expiry catalog must contain at least two digit classes")
    best_digit, best_score = ranking[0]
    second_digit, second_score = ranking[1]
    return {
        "digit": best_digit,
        "score": float(best_score),
        "margin": float(best_score - second_score),
        "second_digit": second_digit,
        "second_score": float(second_score),
    }


def read_inventory_count(
    card_image: np.ndarray,
    digit_reader: Mapping[str, Any],
    *,
    item_id: str,
) -> int:
    components = segment_quantity_digits(card_image, digit_reader)
    if not components:
        raise _inventory_error(
            f"unable to segment count digits for {item_id}; reason=no_right_aligned_digit_run"
        )
    max_digits = int(digit_reader.get("max_digits", 10))
    if len(components) > max_digits:
        raise _inventory_error(
            f"unable to segment count digits for {item_id}; "
            f"reason=too_many_digits count={len(components)} max={max_digits}"
        )

    matches = [match_quantity_digit(component["glyph"], digit_reader) for component in components]
    min_score = float(digit_reader.get("min_digit_score", 0.75))
    min_margin = float(digit_reader.get("min_digit_margin", 0.02))
    for index, match in enumerate(matches):
        if float(match["score"]) < min_score or float(match["margin"]) < min_margin:
            raise _inventory_error(
                f"unable to match count digit for {item_id}; index={index} "
                f"best={match['digit']} score={float(match['score']):.3f} "
                f"second={match['second_digit']} second_score={float(match['second_score']):.3f} "
                f"margin={float(match['margin']):.3f}"
            )
    digits = "".join(str(match["digit"]) for match in matches)
    value = int(digits)
    if value <= 0:
        raise _inventory_error(f"inventory count must be positive for {item_id}; digits={digits}")
    logger.info(
        "Inventory digit-template count: item_id=%s count=%s scores=%s margins=%s",
        item_id,
        value,
        [round(float(match["score"]), 4) for match in matches],
        [round(float(match["margin"]), 4) for match in matches],
    )
    return value


def segment_expiry_digits(
    expiry_image: np.ndarray,
    digit_reader: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    """Extract the one- or two-digit day count between the clock icon and 天."""

    mask = build_quantity_white_mask(expiry_image, digit_reader)
    component_count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, 8)
    min_x, max_right = (int(value) for value in digit_reader["digit_x_range"])
    min_width, max_width = (int(value) for value in digit_reader["component_width"])
    min_height, max_height = (int(value) for value in digit_reader["component_height"])
    min_top, max_top = (int(value) for value in digit_reader["component_top"])
    min_area = int(digit_reader.get("component_area_min", 1))
    candidates: List[Dict[str, Any]] = []
    for component_index in range(1, component_count):
        left, top, width, height, area = (
            int(value) for value in stats[component_index]
        )
        if left < min_x or left + width > max_right:
            continue
        if not (min_width <= width <= max_width):
            continue
        if not (min_height <= height <= max_height):
            continue
        if not (min_top <= top <= max_top) or area < min_area:
            continue
        candidates.append(
            {
                "rect": [left, top, width, height],
                "glyph": mask[top : top + height, left : left + width],
                "bottom": top + height,
            }
        )
    if not candidates:
        return []

    candidates.sort(key=lambda entry: int(entry["rect"][0]))
    baseline = int(candidates[-1]["bottom"])
    baseline_tolerance = int(digit_reader.get("baseline_tolerance", 2))
    aligned = [
        entry
        for entry in candidates
        if abs(int(entry["bottom"]) - baseline) <= baseline_tolerance
    ]
    min_gap, max_gap = (int(value) for value in digit_reader["digit_gap"])
    for left, right in zip(aligned, aligned[1:]):
        left_right = int(left["rect"][0]) + int(left["rect"][2])
        gap = int(right["rect"][0]) - left_right
        if not (min_gap <= gap <= max_gap):
            return []
    return aligned


def read_inventory_expiry(
    expiry_image: np.ndarray,
    digit_reader: Mapping[str, Any],
    *,
    item_id: str,
) -> Dict[str, Any]:
    """Read a fixed days-only expiry using the currently available digit templates."""

    components = segment_expiry_digits(expiry_image, digit_reader)
    if not components:
        raise _inventory_error(
            f"unable to segment expiry digits for {item_id}; reason=no_digit_run"
        )
    max_digits = int(digit_reader.get("max_digits", 2))
    if len(components) > max_digits:
        raise _inventory_error(
            f"unable to segment expiry digits for {item_id}; "
            f"reason=too_many_digits count={len(components)} max={max_digits}"
        )

    matches = [match_expiry_digit(component["glyph"], digit_reader) for component in components]
    min_score = float(digit_reader.get("min_digit_score", 0.9))
    min_margin = float(digit_reader.get("min_digit_margin", 0.05))
    for index, match in enumerate(matches):
        if float(match["score"]) < min_score or float(match["margin"]) < min_margin:
            raise _inventory_error(
                f"unable to match expiry digit for {item_id}; index={index} "
                f"best={match['digit']} score={float(match['score']):.3f} "
                f"second={match['second_digit']} second_score={float(match['second_score']):.3f} "
                f"margin={float(match['margin']):.3f}"
            )
    digits = "".join(str(match["digit"]) for match in matches)
    value = int(digits)
    if not (1 <= value <= 99):
        raise _inventory_error(
            f"inventory expiry must be between 1 and 99 days for {item_id}; digits={digits}"
        )
    logger.info(
        "Inventory expiry digit-template count: item_id=%s days=%s scores=%s margins=%s",
        item_id,
        value,
        [round(float(match["score"]), 4) for match in matches],
        [round(float(match["margin"]), 4) for match in matches],
    )
    return {
        "kind": "days_remaining",
        "value": value,
        "raw": f"digit_template:{digits}",
    }


def _suppress_cross_template_overlaps(
    candidates: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    kept: List[Dict[str, Any]] = []
    for candidate in sorted(
        candidates,
        key=lambda item: (
            -float(item["confidence"]),
            str(item["item"]["item_id"]),
        ),
    ):
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
    vision: Any,
    *,
    expiry_recognition_enabled: bool = _EXPIRY_RECOGNITION_ENABLED,
) -> List[Dict[str, Any]]:
    """Recognize all fully visible supported cards in one warehouse frame."""

    if page_image is None or not isinstance(page_image, np.ndarray) or page_image.size == 0:
        raise _inventory_error("warehouse grid capture is empty")
    layout = catalog.get("layout")
    items = catalog.get("items")
    template_paths = catalog.get("_template_paths")
    category = _catalog_category(catalog)
    template_images = (
        catalog.get("_template_images") if category == "materials" else template_paths
    )
    digit_reader = catalog.get("_digit_reader")
    expiry_digit_reader = catalog.get("_expiry_digit_reader")
    count_mode = str(catalog.get("_count_mode") or "")
    supports_expiry = bool(catalog.get("_supports_expiry"))
    if (
        not isinstance(layout, Mapping)
        or not isinstance(items, list)
        or not isinstance(template_paths, list)
        or len(template_paths) != len(items)
        or not isinstance(template_images, list)
        or len(template_images) != len(items)
        or count_mode not in {_COUNT_MODE_DIGIT_TEMPLATE, _COUNT_MODE_CARD_INSTANCES}
        or (
            count_mode == _COUNT_MODE_DIGIT_TEMPLATE
            and not isinstance(digit_reader, Mapping)
        )
        or (supports_expiry and not isinstance(expiry_digit_reader, Mapping))
    ):
        raise ValueError("inventory catalog must be prepared before page scanning")
    template_offset = tuple(int(value) for value in layout["template_offset_from_card"])
    card_width, card_height = (int(value) for value in layout["card_size"])
    threshold = float(layout.get("match_threshold", _DEFAULT_MATCH_THRESHOLD))
    match_image = page_image
    coordinate_scale = 1.0
    source_template_size = tuple(int(value) for value in layout["template_size"])
    if category == "materials":
        blur_kernel = tuple(int(value) for value in layout["blur_kernel"])
        blur_sigma = float(layout["blur_sigma"])
        recognition_scale = float(layout["recognition_scale"])
        blurred = cv2.GaussianBlur(
            page_image,
            blur_kernel,
            sigmaX=blur_sigma,
            sigmaY=blur_sigma,
        )
        scaled_width = max(1, int(round(page_image.shape[1] * recognition_scale)))
        scaled_height = max(1, int(round(page_image.shape[0] * recognition_scale)))
        match_image = cv2.resize(
            blurred,
            (scaled_width, scaled_height),
            interpolation=cv2.INTER_AREA,
        )
        coordinate_scale = 1.0 / recognition_scale
        source_template_size = tuple(
            int(value) for value in layout["source_template_size"]
        )

    if vision is None or not callable(getattr(vision, "find_all_templates_batch", None)):
        raise RuntimeError("framework vision service is required for inventory matching")
    batch_match_options: Dict[str, Any] = {
        "source_image": match_image,
        "template_images": template_images,
        "threshold": threshold,
        "nms_threshold": 0.5,
        "use_grayscale": False,
        "match_method": cv2.TM_CCOEFF_NORMED,
        "preprocess": "none",
    }
    if category == "items":
        preprocess = str(layout.get("preprocess") or "none").strip().lower()
        match_method_name = str(
            layout.get("match_method") or "ccoeff_normed"
        ).strip().lower()
        match_methods = {
            "ccoeff_normed": cv2.TM_CCOEFF_NORMED,
            "sqdiff": cv2.TM_SQDIFF,
        }
        if match_method_name not in match_methods:
            raise ValueError(
                f"unsupported inventory item match_method: {match_method_name}"
            )
        batch_match_options.update(
            {
                "match_method": match_methods[match_method_name],
                "preprocess": preprocess,
            }
        )
        if layout.get("score_scale") is not None:
            try:
                score_scale = float(layout["score_scale"])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "inventory item score_scale must be a number"
                ) from exc
            if score_scale <= 0:
                raise ValueError("inventory item score_scale must be greater than zero")
            batch_match_options["score_scale"] = score_scale

    batch_results = vision.find_all_templates_batch(
        **batch_match_options,
    )
    if not isinstance(batch_results, list) or len(batch_results) != len(items):
        raise _inventory_error(
            "framework vision batch result count does not match inventory templates"
        )

    candidates: List[Dict[str, Any]] = []
    for item, multi_match_result in zip(items, batch_results, strict=True):
        matches = list(getattr(multi_match_result, "matches", []) or [])
        best_confidence = max(
            (float(getattr(match, "confidence", 0.0)) for match in matches),
            default=0.0,
        )
        log_template_scan = logger.info if matches else logger.debug
        log_template_scan(
            "Inventory framework template scan: item_id=%s best_match_confidence=%.4f matches=%s",
            item.get("item_id"),
            best_confidence,
            len(matches),
        )
        for match in matches:
            confidence = float(getattr(match, "confidence", 0.0))
            if confidence < threshold:
                continue
            match_top_left = getattr(match, "top_left", None)
            if not isinstance(match_top_left, (tuple, list)) or len(match_top_left) != 2:
                continue
            match_x, match_y = (
                int(round(float(value) * coordinate_scale))
                for value in match_top_left
            )
            card_top_left = (int(match_x) - template_offset[0], int(match_y) - template_offset[1])
            if relative_roi(card_top_left, (0, 0, card_width, card_height), page_image.shape) is None:
                continue
            candidates.append(
                {
                    "top_left": (match_x, match_y),
                    "confidence": confidence,
                    "rect": (
                        match_x,
                        match_y,
                        int(source_template_size[0]),
                        int(source_template_size[1]),
                    ),
                    "item": item,
                    "card_top_left": card_top_left,
                }
            )

    observations: List[Dict[str, Any]] = []
    for candidate in _suppress_cross_template_overlaps(candidates):
        match_top_left = candidate["top_left"]
        item = candidate["item"]
        card_x, card_y = (int(value) for value in candidate["card_top_left"])
        card_image = page_image[
            card_y : card_y + card_height,
            card_x : card_x + card_width,
        ]
        count = (
            1
            if count_mode == _COUNT_MODE_CARD_INSTANCES
            else read_inventory_count(
                card_image,
                digit_reader,
                item_id=str(item["item_id"]),
            )
        )
        observation: Dict[str, Any] = {
            "item_id": str(item["item_id"]),
            "name": str(item.get("name") or item["item_id"]),
            "count": int(count),
            "confidence": float(candidate["confidence"]),
            "card_top_left": [int(candidate["card_top_left"][0]), int(candidate["card_top_left"][1])],
        }
        if (
            expiry_recognition_enabled
            and supports_expiry
            and item.get("stack_policy") == STACK_POLICY_SPLIT_BY_EXPIRY
        ):
            expiry_region = relative_roi(
                match_top_left, layout["expiry_roi_from_template"], page_image.shape
            )
            if expiry_region is None:
                continue
            expiry_x, expiry_y, expiry_width, expiry_height = expiry_region
            observation["expiry"] = read_inventory_expiry(
                page_image[expiry_y : expiry_y + expiry_height, expiry_x : expiry_x + expiry_width],
                expiry_digit_reader,
                item_id=str(item["item_id"]),
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


def _save_debug_scan_image(
    page_image: np.ndarray,
    *,
    category: str,
    page_number: int,
) -> Optional[Path]:
    """Save the exact stable grid frame used by one recognition pass when enabled."""

    raw_directory = str(os.environ.get(_DEBUG_CAPTURE_DIR_ENV) or "").strip()
    if not raw_directory:
        return None
    output_directory = Path(raw_directory).expanduser().resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    output_path = output_directory / f"{category}_page_{int(page_number):03d}.png"
    image_bgr = cv2.cvtColor(page_image, cv2.COLOR_RGB2BGR)
    if not cv2.imwrite(str(output_path), image_bgr):
        raise _inventory_error(f"unable to save inventory debug capture: {output_path}")
    logger.info("Inventory debug scan image saved: %s", output_path)
    return output_path


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

    previous_gray = cv2.cvtColor(previous_image, cv2.COLOR_RGB2GRAY)
    current_gray = cv2.cvtColor(current_image, cv2.COLOR_RGB2GRAY)
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
    *,
    expiry_recognition_enabled: bool = True,
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
        if policy == STACK_POLICY_SPLIT_BY_EXPIRY and expiry_recognition_enabled:
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


def read_inventory_category(
    app: Any,
    ocr: Any,
    vision: Any,
    *,
    category: str,
    catalog: Optional[Dict[str, Any]] = None,
    catalog_path: Optional[Path] = None,
    max_scrolls: int = _DEFAULT_MAX_SCROLLS,
) -> Dict[str, Any]:
    """Read one supported category until three scans add no new physical stacks."""

    normalized_category = str(category or "").strip()
    if normalized_category not in _CATEGORY_SPECS:
        raise ValueError("inventory category must be items, materials or equipment")
    prepared_catalog = (
        catalog
        if catalog is not None
        else prepare_inventory_catalog(
            normalized_category,
            vision,
            catalog_path=catalog_path,
        )
    )
    catalog_category = _catalog_category(prepared_catalog)
    if catalog_category != normalized_category:
        raise ValueError(
            f"inventory catalog category mismatch: expected {normalized_category}, "
            f"got {catalog_category}"
        )
    if not isinstance(prepared_catalog.get("_template_paths"), list):
        _resolve_inventory_template_paths(prepared_catalog, vision)
    layout = prepared_catalog["layout"]
    region = tuple(int(value) for value in layout["grid_region"])
    scroll_start = tuple(int(value) for value in layout.get("scroll_start", _DEFAULT_SCROLL_START))
    scroll_end = tuple(int(value) for value in layout.get("scroll_end", _DEFAULT_SCROLL_END))
    supported_ids = {str(item["item_id"]) for item in prepared_catalog["items"]}
    all_observations: List[Dict[str, Any]] = []
    previous_image: Optional[np.ndarray] = None
    previous_observations: List[Dict[str, Any]] = []
    virtual_scroll_y = 0
    scans_without_new_items = 0
    pages_scanned = 0
    completion_reason = ""
    page_image = _capture_stable_grid(app, region)

    while True:
        _save_debug_scan_image(
            page_image,
            category=normalized_category,
            page_number=pages_scanned + 1,
        )
        page_observations = scan_inventory_page(
            page_image,
            prepared_catalog,
            ocr,
            vision,
        )
        pages_scanned += 1
        if previous_image is not None:
            scroll_delta, confidence = _estimate_scroll_delta(
                previous_image, page_image, previous_observations, page_observations
            )
            if scroll_delta > 2:
                virtual_scroll_y += int(scroll_delta)
            if scroll_delta > 2 and confidence < 0.55:
                raise _inventory_error(
                    f"unable to align overlapping warehouse pages (confidence={confidence:.3f})"
                )
        unique_count_before_page = len(_dedupe_physical_observations(all_observations))
        for observation in page_observations:
            item = dict(observation)
            card_x, card_y = item["card_top_left"]
            item["_virtual_card_top_left"] = [int(card_x), int(card_y) + int(virtual_scroll_y)]
            all_observations.append(item)
        unique_count_after_page = len(_dedupe_physical_observations(all_observations))
        new_item_count = unique_count_after_page - unique_count_before_page
        if new_item_count > 0:
            scans_without_new_items = 0
        else:
            scans_without_new_items += 1
        logger.info(
            "Inventory scan progress: page=%s new_physical_items=%s "
            "consecutive_scans_without_new_items=%s",
            pages_scanned,
            new_item_count,
            scans_without_new_items,
        )
        if scans_without_new_items >= 3:
            completion_reason = "three_consecutive_scans_without_new_items"
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
            hold_before_release_sec=_SCROLL_HOLD_BEFORE_RELEASE_SEC,
        )
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
    aggregated = aggregate_inventory_observations(
        public_observations,
        prepared_catalog,
        expiry_recognition_enabled=_EXPIRY_RECOGNITION_ENABLED,
    )
    spec = _CATEGORY_SPECS[normalized_category]
    result_key = str(spec["result_key"])
    supported_key = str(spec["supported_key"])
    if normalized_category in {"materials", "equipment"}:
        public_id_key = str(spec["id_key"])
        aggregated = [
            {
                **{key: value for key, value in entry.items() if key != "item_id"},
                public_id_key: entry["item_id"],
            }
            for entry in aggregated
        ]
    if normalized_category == "equipment":
        catalog_order = {
            str(item["item_id"]): index
            for index, item in enumerate(prepared_catalog["items"])
        }
        aggregated.sort(
            key=lambda entry: catalog_order.get(str(entry.get("equipment_id") or ""), 10**9)
        )
    has_expiry_entries = _EXPIRY_RECOGNITION_ENABLED and any(
        item.get("stack_policy") == STACK_POLICY_SPLIT_BY_EXPIRY
        for item in prepared_catalog["items"]
    )
    result: Dict[str, Any] = {
        "category": normalized_category,
        "scan_scope": "catalog_only",
        "catalog_schema_version": int(prepared_catalog.get("schema_version", 1)),
        supported_key: len(supported_ids),
        "pages_scanned": pages_scanned,
        "scan_complete": True,
        "completion_reason": completion_reason,
        "consecutive_scans_without_new_items": scans_without_new_items,
        "expiry_recognition_enabled": _EXPIRY_RECOGNITION_ENABLED,
        "source": "equipment_template"
        if normalized_category == "equipment"
        else (
            "item_template+count_digit_template+expiry_digit_template"
            if has_expiry_entries
            else "item_template+count_digit_template"
        ),
        "scanned_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        result_key: aggregated,
    }
    if normalized_category == "equipment":
        result["matched_card_count"] = len(unique_observations)
        result["matched_equipment_count"] = len(aggregated)
        result.pop("expiry_recognition_enabled", None)
    else:
        result["matched_stack_count"] = len(unique_observations)
    return result


def read_inventory_items(
    app: Any,
    ocr: Any,
    vision: Any,
    *,
    catalog: Optional[Dict[str, Any]] = None,
    catalog_path: Optional[Path] = None,
    max_scrolls: int = _DEFAULT_MAX_SCROLLS,
) -> Dict[str, Any]:
    """Read supported warehouse items."""

    return read_inventory_category(
        app,
        ocr,
        vision,
        category="items",
        catalog=catalog,
        catalog_path=catalog_path,
        max_scrolls=max_scrolls,
    )


def read_inventory_materials(
    app: Any,
    ocr: Any,
    vision: Any,
    *,
    catalog: Optional[Dict[str, Any]] = None,
    catalog_path: Optional[Path] = None,
    max_scrolls: int = _DEFAULT_MAX_SCROLLS,
) -> Dict[str, Any]:
    """Read supported warehouse materials."""

    return read_inventory_category(
        app,
        ocr,
        vision,
        category="materials",
        catalog=catalog,
        catalog_path=catalog_path,
        max_scrolls=max_scrolls,
    )


def read_inventory_equipment(
    app: Any,
    ocr: Any,
    vision: Any,
    *,
    catalog: Optional[Dict[str, Any]] = None,
    catalog_path: Optional[Path] = None,
    max_scrolls: int = _DEFAULT_MAX_SCROLLS,
) -> Dict[str, Any]:
    """Read supported warehouse equipment by counting physical cards."""

    return read_inventory_category(
        app,
        ocr,
        vision,
        category="equipment",
        catalog=catalog,
        catalog_path=catalog_path,
        max_scrolls=max_scrolls,
    )
