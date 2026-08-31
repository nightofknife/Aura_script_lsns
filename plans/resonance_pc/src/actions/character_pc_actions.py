"""Character roster recognition helpers for Resonance PC player data."""

from __future__ import annotations

import copy
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import cv2
import numpy as np

from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.utils.exceptions import StopTaskException


Region = Tuple[int, int, int, int]

_PLAN_ROOT = Path(__file__).resolve().parents[2]
_CHARACTER_CONFIG_FILE = _PLAN_ROOT / "data" / "meta" / "player_characters.json"
_PLAN_KEY = "resonance_pc"
_DEBUG_CAPTURE_DIR_ENV = "AURA_CHARACTER_DEBUG_CAPTURE_DIR"


def _character_error(message: str) -> StopTaskException:
    return StopTaskException(f"Character refresh failed: {message}", success=False)


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _coerce_int_sequence(value: Any, *, length: int, label: str) -> Tuple[int, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValueError(f"character config {label} must contain {length} integers")
    try:
        return tuple(int(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"character config {label} must contain integers") from exc


def _coerce_float(value: Any, *, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"character config {label} must be numeric") from exc
    if not math.isfinite(result):
        raise ValueError(f"character config {label} must be finite")
    return result


def _resolve_template_path(
    reference: str,
    *,
    plan_root: Path,
    vision: Any = None,
) -> Path:
    normalized = str(reference or "").strip()
    if not normalized:
        raise ValueError("character template reference must not be empty")
    if vision is not None and callable(getattr(vision, "resolve_template", None)):
        resolved = Path(
            vision.resolve_template(_PLAN_KEY, normalized, plan_root)
        ).resolve()
    else:
        resolved = (plan_root / normalized).resolve()
    if not _path_is_within(resolved, plan_root) or not resolved.is_file():
        raise ValueError(f"character template is unavailable: {resolved}")
    return resolved


def _read_image_file(path: Path, flags: int = cv2.IMREAD_UNCHANGED) -> np.ndarray:
    """Read an image from a Unicode-safe Windows path."""

    try:
        encoded = np.fromfile(str(path), dtype=np.uint8)
    except OSError as exc:
        raise ValueError(f"unable to read image bytes: {path}") from exc
    image = cv2.imdecode(encoded, flags)
    if image is None:
        raise ValueError(f"unable to decode image: {path}")
    return image


def _identity_template_rgb(path: Path) -> np.ndarray:
    image = _read_image_file(path)
    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
    if image.ndim == 3 and image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2RGB)
    if image.ndim == 3 and image.shape[2] == 3:
        return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    raise ValueError(f"unsupported character template shape {image.shape}: {path}")


def _validate_image_size(path: Path, expected: Tuple[int, int], *, label: str) -> np.ndarray:
    image = _read_image_file(path)
    height, width = image.shape[:2]
    if (width, height) != expected:
        raise ValueError(
            f"{label} must be exactly {expected[0]}x{expected[1]}: "
            f"{path} is {width}x{height}"
        )
    return image


def load_character_catalog(
    config_path: Optional[Path] = None,
    *,
    plan_root: Optional[Path] = None,
    vision: Any = None,
) -> Dict[str, Any]:
    """Load layout config and discover templates from named character folders.

    Named folders without PNG files are catalog placeholders for characters whose
    templates have not been collected yet. They are intentionally ignored until a
    template is added.
    """

    root = Path(plan_root or _PLAN_ROOT).resolve()
    path = Path(config_path or _CHARACTER_CONFIG_FILE)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to load character config: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("character config must be a JSON object")
    if int(payload.get("schema_version", 0)) != 1:
        raise ValueError("character config schema_version must be 1")

    raw_layout = payload.get("layout")
    raw_templates = payload.get("templates")
    if not isinstance(raw_layout, Mapping) or not isinstance(raw_templates, Mapping):
        raise ValueError("character config must contain layout and templates objects")

    reference_client = _coerce_int_sequence(
        raw_layout.get("reference_client"), length=2, label="reference_client"
    )
    grid_region = _coerce_int_sequence(
        raw_layout.get("grid_region"), length=4, label="grid_region"
    )
    entry_region = _coerce_int_sequence(
        raw_layout.get("entry_region"), length=4, label="entry_region"
    )
    card_size = _coerce_int_sequence(
        raw_layout.get("card_size"), length=2, label="card_size"
    )
    template_size = _coerce_int_sequence(
        raw_layout.get("template_size"), length=2, label="template_size"
    )
    template_offset = _coerce_int_sequence(
        raw_layout.get("template_offset_from_card"),
        length=2,
        label="template_offset_from_card",
    )
    star_template_size = _coerce_int_sequence(
        raw_layout.get("star_template_size"), length=2, label="star_template_size"
    )
    star_row_from_card = _coerce_int_sequence(
        raw_layout.get("star_row_from_card"), length=4, label="star_row_from_card"
    )
    scroll_start = _coerce_int_sequence(
        raw_layout.get("scroll_start"), length=2, label="scroll_start"
    )
    scroll_end = _coerce_int_sequence(
        raw_layout.get("scroll_end"), length=2, label="scroll_end"
    )

    if reference_client != (1280, 720):
        raise ValueError("character recognition currently requires a 1280x720 client")
    if template_size != (140, 140):
        raise ValueError("character identity templates must be exactly 140x140")
    if star_template_size != (24, 24):
        raise ValueError("character star template must be exactly 24x24")
    if any(value <= 0 for value in (*card_size, *template_size, *star_template_size)):
        raise ValueError("character card and template dimensions must be positive")
    if grid_region[2] <= 0 or grid_region[3] <= 0:
        raise ValueError("character grid region must have positive dimensions")
    if entry_region[2] <= 0 or entry_region[3] <= 0:
        raise ValueError("character entry region must have positive dimensions")
    if (
        star_row_from_card[2] < star_template_size[0]
        or star_row_from_card[3] < star_template_size[1]
    ):
        raise ValueError("character star row must be at least as large as the star template")

    entry_threshold = _coerce_float(
        raw_layout.get("entry_match_threshold"), label="entry_match_threshold"
    )
    character_threshold = _coerce_float(
        raw_layout.get("character_match_threshold"),
        label="character_match_threshold",
    )
    star_match_threshold = _coerce_float(
        raw_layout.get("star_match_threshold"), label="star_match_threshold"
    )
    if not 0.0 <= entry_threshold <= 1.0 or not 0.0 <= character_threshold <= 1.0:
        raise ValueError("character template thresholds must be between 0 and 1")
    if not 0.0 <= star_match_threshold <= 1.0:
        raise ValueError("character star match threshold must be between 0 and 1")

    star_peak_window_size = int(raw_layout.get("star_peak_window_size", 5))
    star_min_horizontal_distance = int(raw_layout.get("star_min_horizontal_distance", 16))
    max_stars = int(raw_layout.get("max_stars", 5))
    if star_peak_window_size <= 0 or star_peak_window_size % 2 == 0:
        raise ValueError("character star peak window size must be a positive odd integer")
    if star_min_horizontal_distance <= 0:
        raise ValueError("character star minimum horizontal distance must be positive")
    if max_stars <= 0:
        raise ValueError("character maximum star count must be positive")

    no_new_limit = int(raw_layout.get("no_new_scan_limit", 3))
    max_scrolls = int(raw_layout.get("max_scrolls", 30))
    stable_attempts = int(raw_layout.get("stable_capture_attempts", 5))
    if no_new_limit <= 0 or max_scrolls < 0 or stable_attempts <= 0:
        raise ValueError("character scan limits are invalid")

    character_root_ref = str(raw_templates.get("character_root") or "").strip()
    character_root = (root / character_root_ref).resolve()
    if not _path_is_within(character_root, root) or not character_root.is_dir():
        raise ValueError(f"character template root is unavailable: {character_root}")

    characters: List[Dict[str, Any]] = []
    flattened: List[Dict[str, Any]] = []
    directories = sorted(
        (item for item in character_root.iterdir() if item.is_dir()),
        key=lambda item: item.name,
    )
    if not directories:
        raise ValueError("character template root must contain at least one named folder")
    for directory in directories:
        character_name = directory.name.strip()
        if not character_name or character_name != directory.name:
            raise ValueError(f"invalid character template directory name: {directory.name!r}")
        template_files = sorted(
            (
                item
                for item in directory.iterdir()
                if item.is_file() and item.suffix.lower() == ".png"
            ),
            key=lambda item: item.name,
        )
        if not template_files:
            continue
        template_paths: List[str] = []
        template_refs: List[str] = []
        for template_file in template_files:
            resolved_file = template_file.resolve()
            if not _path_is_within(resolved_file, directory.resolve()):
                raise ValueError(f"character template escapes its directory: {template_file}")
            template_ref = resolved_file.relative_to(root).as_posix()
            resolved = _resolve_template_path(
                template_ref,
                plan_root=root,
                vision=vision,
            )
            _validate_image_size(resolved, template_size, label="character identity template")
            template_image = _identity_template_rgb(resolved)
            template_refs.append(template_ref)
            template_paths.append(str(resolved))
            flattened.append(
                {
                    "character_id": character_name,
                    "name": character_name,
                    "template_ref": template_ref,
                    "template_path": str(resolved),
                    "template_image": template_image,
                }
            )
        characters.append(
            {
                "character_id": character_name,
                "name": character_name,
                "template_refs": template_refs,
                "template_paths": template_paths,
            }
        )

    if not flattened:
        raise ValueError("character template root must contain at least one PNG template")

    entry_template = _resolve_template_path(
        str(raw_templates.get("entry") or ""),
        plan_root=root,
        vision=vision,
    )
    lit_star_template = _resolve_template_path(
        str(raw_templates.get("lit_star") or ""),
        plan_root=root,
        vision=vision,
    )
    lit_star_mask = _resolve_template_path(
        str(raw_templates.get("lit_star_mask") or ""),
        plan_root=root,
        vision=vision,
    )
    _validate_image_size(lit_star_template, star_template_size, label="lit star template")
    _validate_image_size(lit_star_mask, star_template_size, label="lit star mask")
    lit_star_template_image = _identity_template_rgb(lit_star_template)
    lit_star_mask_image = _read_image_file(lit_star_mask, cv2.IMREAD_GRAYSCALE)
    entry_image = _read_image_file(entry_template)
    if entry_image is None or entry_image.size == 0:
        raise ValueError(f"unable to read character entry template: {entry_template}")

    layout = {
        "reference_client": reference_client,
        "grid_region": grid_region,
        "entry_region": entry_region,
        "card_size": card_size,
        "template_size": template_size,
        "template_offset_from_card": template_offset,
        "star_template_size": star_template_size,
        "star_row_from_card": star_row_from_card,
        "entry_match_threshold": entry_threshold,
        "character_match_threshold": character_threshold,
        "star_match_threshold": star_match_threshold,
        "star_peak_window_size": star_peak_window_size,
        "star_min_horizontal_distance": star_min_horizontal_distance,
        "max_stars": max_stars,
        "no_new_scan_limit": no_new_limit,
        "max_scrolls": max_scrolls,
        "scroll_start": scroll_start,
        "scroll_end": scroll_end,
        "scroll_duration_sec": _coerce_float(
            raw_layout.get("scroll_duration_sec"), label="scroll_duration_sec"
        ),
        "scroll_hold_before_release_sec": _coerce_float(
            raw_layout.get("scroll_hold_before_release_sec"),
            label="scroll_hold_before_release_sec",
        ),
        "stable_capture_attempts": stable_attempts,
        "stable_capture_interval_sec": _coerce_float(
            raw_layout.get("stable_capture_interval_sec"),
            label="stable_capture_interval_sec",
        ),
        "stable_capture_max_mean_diff": _coerce_float(
            raw_layout.get("stable_capture_max_mean_diff"),
            label="stable_capture_max_mean_diff",
        ),
    }
    return {
        "schema_version": 1,
        "layout": layout,
        "characters": characters,
        "templates": flattened,
        "entry_template_path": str(entry_template),
        "lit_star_template_path": str(lit_star_template),
        "lit_star_mask_path": str(lit_star_mask),
        "lit_star_template_image": lit_star_template_image,
        "lit_star_mask_image": lit_star_mask_image,
    }


def _relative_roi(
    top_left: Sequence[int],
    offset: Sequence[int],
    size: Sequence[int],
    image_shape: Sequence[int],
) -> Optional[Region]:
    x = int(top_left[0]) + int(offset[0])
    y = int(top_left[1]) + int(offset[1])
    width, height = int(size[0]), int(size[1])
    image_height, image_width = int(image_shape[0]), int(image_shape[1])
    if x < 0 or y < 0 or width <= 0 or height <= 0:
        return None
    if x + width > image_width or y + height > image_height:
        return None
    return (x, y, width, height)


def _suppress_overlapping_cards(
    candidates: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    kept: List[Dict[str, Any]] = []
    for candidate in sorted(
        candidates,
        key=lambda item: float(item["confidence"]),
        reverse=True,
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


def _match_character_candidates(
    page_image: np.ndarray,
    catalog: Mapping[str, Any],
    vision: Any,
) -> List[Dict[str, Any]]:
    if page_image is None or not isinstance(page_image, np.ndarray) or page_image.size == 0:
        raise _character_error("character grid capture is empty")
    layout = catalog.get("layout")
    templates = catalog.get("templates")
    if not isinstance(layout, Mapping) or not isinstance(templates, list) or not templates:
        raise ValueError("character catalog has not been loaded")
    if vision is None or not callable(getattr(vision, "find_all_templates_batch", None)):
        raise RuntimeError("framework vision service is required for character matching")

    template_images = [item["template_image"] for item in templates]
    batch_results = vision.find_all_templates_batch(
        source_image=page_image,
        template_images=template_images,
        threshold=float(layout["character_match_threshold"]),
        nms_threshold=0.5,
        use_grayscale=False,
        match_method=cv2.TM_CCOEFF_NORMED,
        preprocess="none",
    )
    if not isinstance(batch_results, list) or len(batch_results) != len(templates):
        raise _character_error(
            "framework vision batch result count does not match character templates"
        )

    template_offset = tuple(int(value) for value in layout["template_offset_from_card"])
    card_size = tuple(int(value) for value in layout["card_size"])
    candidates: List[Dict[str, Any]] = []
    for template, multi_match_result in zip(templates, batch_results, strict=True):
        matches = list(getattr(multi_match_result, "matches", []) or [])
        for match in matches:
            top_left = getattr(match, "top_left", None)
            if not isinstance(top_left, (tuple, list)) or len(top_left) != 2:
                continue
            card_top_left = (
                int(top_left[0]) - template_offset[0],
                int(top_left[1]) - template_offset[1],
            )
            if _relative_roi(card_top_left, (0, 0), card_size, page_image.shape) is None:
                continue
            candidates.append(
                {
                    "character_id": str(template["character_id"]),
                    "name": str(template["name"]),
                    "template_ref": str(template["template_ref"]),
                    "confidence": float(getattr(match, "confidence", 0.0) or 0.0),
                    "card_top_left": card_top_left,
                }
            )
    return _suppress_overlapping_cards(candidates)


def _find_star_row_peaks(
    star_row_image: np.ndarray,
    template_image: np.ndarray,
    mask_image: np.ndarray,
    *,
    threshold: float,
    peak_window_size: int,
    min_horizontal_distance: int,
) -> List[Dict[str, Any]]:
    """Find unique white-star peaks across one complete character star row."""

    if (
        star_row_image is None
        or not isinstance(star_row_image, np.ndarray)
        or star_row_image.size == 0
    ):
        raise ValueError("character star row image must be a non-empty numpy array")
    if (
        template_image is None
        or not isinstance(template_image, np.ndarray)
        or template_image.size == 0
    ):
        raise ValueError("character lit-star template must be a non-empty numpy array")
    if (
        mask_image is None
        or not isinstance(mask_image, np.ndarray)
        or mask_image.size == 0
    ):
        raise ValueError("character lit-star mask must be a non-empty numpy array")
    if (
        star_row_image.shape[0] < template_image.shape[0]
        or star_row_image.shape[1] < template_image.shape[1]
    ):
        raise ValueError(
            "character star row image must be at least as large as the lit-star template"
        )
    if mask_image.shape[:2] != template_image.shape[:2]:
        raise ValueError("character lit-star mask and template sizes must match")

    difference_map = cv2.matchTemplate(
        star_row_image,
        template_image,
        cv2.TM_SQDIFF_NORMED,
        mask=mask_image,
    )
    confidence_map = np.nan_to_num(
        1.0 - difference_map,
        nan=-1.0,
        posinf=-1.0,
        neginf=-1.0,
    ).astype(np.float32, copy=False)
    local_maxima = cv2.dilate(
        confidence_map,
        np.ones((int(peak_window_size), int(peak_window_size)), dtype=np.float32),
    )
    ys, xs = np.where(
        (confidence_map >= float(threshold))
        & (confidence_map >= local_maxima - 1e-7)
    )
    candidates = sorted(
        (
            {
                "confidence": float(confidence_map[y, x]),
                "top_left": (int(x), int(y)),
            }
            for y, x in zip(ys, xs, strict=True)
        ),
        key=lambda item: float(item["confidence"]),
        reverse=True,
    )

    kept: List[Dict[str, Any]] = []
    for candidate in candidates:
        candidate_x = int(candidate["top_left"][0])
        if any(
            abs(candidate_x - int(other["top_left"][0])) < int(min_horizontal_distance)
            for other in kept
        ):
            continue
        kept.append(candidate)
    return sorted(kept, key=lambda item: int(item["top_left"][0]))


def read_character_stars(
    page_image: np.ndarray,
    card_top_left: Sequence[int],
    catalog: Mapping[str, Any],
    *,
    character_name: str,
) -> int:
    """Count lit stars by matching across the character's complete star row."""

    layout = catalog["layout"]
    star_row_region = tuple(int(value) for value in layout["star_row_from_card"])
    roi = _relative_roi(
        card_top_left,
        star_row_region[:2],
        star_row_region[2:],
        page_image.shape,
    )
    if roi is None:
        raise _character_error(f"star row is outside the captured card for {character_name}")
    x, y, width, height = roi
    star_row_image = page_image[y : y + height, x : x + width]
    peaks = _find_star_row_peaks(
        star_row_image,
        catalog["lit_star_template_image"],
        catalog["lit_star_mask_image"],
        threshold=float(layout["star_match_threshold"]),
        peak_window_size=int(layout["star_peak_window_size"]),
        min_horizontal_distance=int(layout["star_min_horizontal_distance"]),
    )
    max_stars = int(layout["max_stars"])
    if len(peaks) > max_stars:
        raise _character_error(
            f"star row produced {len(peaks)} unique white-star matches for "
            f"{character_name}; maximum is {max_stars}"
        )
    logger.info(
        "Character star recognition: name=%s stars=%s peaks=%s",
        character_name,
        len(peaks),
        [
            {
                "top_left": tuple(int(value) for value in peak["top_left"]),
                "confidence": round(float(peak["confidence"]), 4),
            }
            for peak in peaks
        ],
    )
    return len(peaks)


def scan_character_page(
    page_image: np.ndarray,
    catalog: Mapping[str, Any],
    vision: Any,
) -> List[Dict[str, Any]]:
    """Recognize all fully visible supported character cards in one frame."""

    observations: Dict[str, Dict[str, Any]] = {}
    for candidate in _match_character_candidates(page_image, catalog, vision):
        character_id = str(candidate["character_id"])
        stars = read_character_stars(
            page_image,
            candidate["card_top_left"],
            catalog,
            character_name=str(candidate["name"]),
        )
        observation = {
            **candidate,
            "stars": int(stars),
        }
        previous = observations.get(character_id)
        if previous is not None:
            if int(previous["stars"]) != int(stars):
                raise _character_error(
                    f"duplicate matches disagree on stars for {character_id}: "
                    f"{previous['stars']} vs {stars}"
                )
            if float(previous["confidence"]) >= float(candidate["confidence"]):
                continue
        observations[character_id] = observation
        logger.info(
            "Character observation: name=%s stars=%s confidence=%.4f anchor=%s",
            character_id,
            stars,
            float(candidate["confidence"]),
            candidate["card_top_left"],
        )
    return list(observations.values())


def _capture_region(app: Any, region: Region, *, label: str) -> np.ndarray:
    capture = app.capture(rect=region)
    if not getattr(capture, "success", False) or getattr(capture, "image", None) is None:
        raise _character_error(f"capture failed for {label}: {region}")
    return capture.image


def _capture_stable_grid(app: Any, catalog: Mapping[str, Any]) -> np.ndarray:
    layout = catalog["layout"]
    region = tuple(int(value) for value in layout["grid_region"])
    attempts = int(layout["stable_capture_attempts"])
    interval = float(layout["stable_capture_interval_sec"])
    max_diff = float(layout["stable_capture_max_mean_diff"])
    previous = _capture_region(app, region, label="character grid")
    for _ in range(max(attempts, 1)):
        time.sleep(max(interval, 0.01))
        current = _capture_region(app, region, label="character grid")
        if previous.shape == current.shape and float(cv2.absdiff(previous, current).mean()) <= max_diff:
            return current
        previous = current
    return previous


def _save_debug_scan_image(page_image: np.ndarray, page_number: int) -> Optional[Path]:
    raw_directory = str(os.environ.get(_DEBUG_CAPTURE_DIR_ENV) or "").strip()
    if not raw_directory:
        return None
    output_directory = Path(raw_directory).expanduser().resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    output_path = output_directory / f"characters_page_{int(page_number):03d}.png"
    image_bgr = cv2.cvtColor(page_image, cv2.COLOR_RGB2BGR)
    if not cv2.imwrite(str(output_path), image_bgr):
        raise _character_error(f"unable to save character debug capture: {output_path}")
    logger.info("Character debug scan image saved: %s", output_path)
    return output_path


def enter_character_page(
    app: Any,
    vision: Any,
    catalog: Mapping[str, Any],
    *,
    timeout_sec: float = 3.0,
    interval_sec: float = 0.15,
    click_interval_sec: float = 0.55,
) -> np.ndarray:
    """Continuously match and click the crew icon until known cards are visible."""

    layout = catalog["layout"]
    entry_region = tuple(int(value) for value in layout["entry_region"])
    deadline = time.monotonic() + max(float(timeout_sec), 0.1)
    next_click_at = 0.0
    next_confirm_at = 0.0
    clicked = False
    last_confidence = 0.0
    while time.monotonic() < deadline:
        now = time.monotonic()
        if now >= next_click_at:
            entry_image = _capture_region(app, entry_region, label="character entry")
            entry_match = vision.find_template(
                source_image=entry_image,
                template_image=str(catalog["entry_template_path"]),
                threshold=float(layout["entry_match_threshold"]),
                use_grayscale=True,
                match_method=cv2.TM_CCOEFF_NORMED,
                preprocess="none",
            )
            last_confidence = float(getattr(entry_match, "confidence", 0.0) or 0.0)
            center = getattr(entry_match, "center_point", None)
            if bool(getattr(entry_match, "found", False)) and center is not None:
                app.click(
                    x=int(entry_region[0] + center[0]),
                    y=int(entry_region[1] + center[1]),
                )
                clicked = True
                next_confirm_at = now + 0.3
            next_click_at = now + max(float(click_interval_sec), 0.1)

        if clicked and now >= next_confirm_at:
            page_image = _capture_stable_grid(app, catalog)
            if _match_character_candidates(page_image, catalog, vision):
                return page_image
            next_confirm_at = now + max(float(click_interval_sec), 0.1)
        time.sleep(max(float(interval_sec), 0.05))

    raise _character_error(
        "character page was not confirmed within "
        f"{float(timeout_sec):.1f}s; last entry confidence={last_confidence:.3f}"
    )


def read_player_characters(
    app: Any,
    vision: Any,
    catalog: Mapping[str, Any],
    *,
    first_page_image: Optional[np.ndarray] = None,
) -> Dict[str, Any]:
    """Scan character pages until three consecutive frames add no new identities."""

    layout = catalog["layout"]
    known: Dict[str, Dict[str, Any]] = {}
    scans_without_new = 0
    pages_scanned = 0
    completion_reason = ""
    page_image = (
        first_page_image
        if isinstance(first_page_image, np.ndarray) and first_page_image.size > 0
        else _capture_stable_grid(app, catalog)
    )

    while True:
        _save_debug_scan_image(page_image, pages_scanned + 1)
        observations = scan_character_page(page_image, catalog, vision)
        pages_scanned += 1
        new_count = 0
        for observation in observations:
            character_id = str(observation["character_id"])
            previous = known.get(character_id)
            if previous is not None:
                if int(previous["stars"]) != int(observation["stars"]):
                    raise _character_error(
                        f"repeated scans disagree on stars for {character_id}: "
                        f"{previous['stars']} vs {observation['stars']}"
                    )
                if float(observation["confidence"]) > float(previous["confidence"]):
                    known[character_id] = copy.deepcopy(observation)
                continue
            known[character_id] = copy.deepcopy(observation)
            new_count += 1

        if new_count > 0:
            scans_without_new = 0
        else:
            scans_without_new += 1
        logger.info(
            "Character scan progress: page=%s new_characters=%s "
            "consecutive_scans_without_new_characters=%s",
            pages_scanned,
            new_count,
            scans_without_new,
        )
        if scans_without_new >= int(layout["no_new_scan_limit"]):
            completion_reason = "three_consecutive_scans_without_new_characters"
            break
        if pages_scanned > int(layout["max_scrolls"]):
            raise _character_error("character scan exceeded maximum scroll count")

        scroll_start = layout["scroll_start"]
        scroll_end = layout["scroll_end"]
        app.drag(
            int(scroll_start[0]),
            int(scroll_start[1]),
            int(scroll_end[0]),
            int(scroll_end[1]),
            duration=float(layout["scroll_duration_sec"]),
            hold_before_release_sec=float(layout["scroll_hold_before_release_sec"]),
        )
        page_image = _capture_stable_grid(app, catalog)

    if not known:
        raise _character_error("no supported character was recognized")

    entries = [
        {
            "character_id": character_id,
            "name": str(known[character_id]["name"]),
            "stars": int(known[character_id]["stars"]),
        }
        for character_id in sorted(known)
    ]
    characters = catalog.get("characters")
    templates = catalog.get("templates")
    return {
        "schema_version": 1,
        "scan_scope": "template_directories",
        "supported_character_count": len(characters) if isinstance(characters, list) else 0,
        "supported_template_count": len(templates) if isinstance(templates, list) else 0,
        "matched_character_count": len(entries),
        "pages_scanned": pages_scanned,
        "scan_complete": True,
        "completion_reason": completion_reason,
        "consecutive_scans_without_new_characters": scans_without_new,
        "source": "character_template+masked_lit_star_template",
        "scanned_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "entries": entries,
    }


__all__ = [
    "enter_character_page",
    "load_character_catalog",
    "read_character_stars",
    "read_player_characters",
    "scan_character_page",
]
