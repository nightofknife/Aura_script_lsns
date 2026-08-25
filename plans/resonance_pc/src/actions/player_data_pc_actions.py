"""PC actions for refreshing Resonance player data from in-game OCR screens."""

from __future__ import annotations

import copy
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import cv2

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.utils.exceptions import StopTaskException

from .character_pc_actions import (
    enter_character_page,
    load_character_catalog,
    read_player_characters,
)
from .inventory_pc_actions import (
    prepare_inventory_catalog,
    read_inventory_equipment,
    read_inventory_items,
    read_inventory_materials,
)

Region = Tuple[int, int, int, int]

_PLAN_ROOT = Path(__file__).resolve().parents[2]
_PLAYER_CACHE_ROOT = _PLAN_ROOT / "data" / "cache" / "player"
_PLAYER_LATEST_FILE = _PLAYER_CACHE_ROOT / "latest.json"

_DATA_STAGES = ("location", "profile", "inventory", "characters")
_STAGE_ORDER = _DATA_STAGES
_PROFILE_PANEL_STAGES = frozenset({"profile", "inventory", "characters"})

_INVENTORY_CATEGORY_ORDER = ("items", "materials", "equipment")
_CURRENCY_ITEM_IDS = {
    "iron_alliance_coin": "iron_coins",
    "birch_crystal": "birch_stone",
}


def _scan_inventory_stage(
    app: Any,
    ocr: Any,
    vision: Any,
    *,
    category: str,
    catalog: Dict[str, Any],
) -> Dict[str, Any]:
    if category == "items":
        return read_inventory_items(app, ocr, vision, catalog=catalog)
    if category == "materials":
        return read_inventory_materials(app, ocr, vision, catalog=catalog)
    if category == "equipment":
        return read_inventory_equipment(app, ocr, vision, catalog=catalog)
    raise ValueError(f"unsupported inventory category: {category}")

_CLICK_PROFILE = (150, 655)
_CLICK_BACK = (82, 34)
_CLICK_PROFILE_CLOSE = (900, 150)
_CLICK_INVENTORY = (165, 615)
_CLICK_INVENTORY_CATEGORY = {
    "items": (1205, 51),
    "materials": (1205, 127),
    "equipment": (1205, 203),
}
_WAREHOUSE_ENTRY_TIMEOUT_SEC = 3.0
_WAREHOUSE_ENTRY_TEMPLATE = _PLAN_ROOT / "templates" / "player_data_warehouse_entry.png"
_WAREHOUSE_ENTRY_TEMPLATE_THRESHOLD = 0.82
_INVENTORY_CATEGORY_TIMEOUT_SEC = 3.0

_MAIN_CITY_REGION: Region = (65, 105, 150, 70)
_PROFILE_REGION: Region = (90, 0, 600, 340)
_MAIN_PAGE_REGION: Region = (0, 0, 1280, 720)
_INVENTORY_PAGE_REGION: Region = (1050, 0, 230, 520)
_WAREHOUSE_ENTRY_REGION: Region = (110, 560, 140, 150)
_INVENTORY_CATEGORY_REGIONS: Dict[str, Region] = {
    "items": (1110, 22, 170, 58),
    "materials": (1110, 98, 170, 58),
    "equipment": (1110, 174, 170, 58),
}
_MAIN_PAGE_MARKERS = ("访问城市", "访问地区", "启程", "STARTENGINE")

_PROFILE_FIELD_REGIONS: Dict[str, Region] = {
    "uid": (105, 10, 180, 30),
    "level": (105, 120, 80, 35),
    "nickname": (105, 150, 385, 45),
    "clarity": (145, 250, 125, 45),
    "fatigue": (360, 250, 125, 45),
    "cargo": (545, 250, 125, 45),
}


def _normalize_text(text: str) -> str:
    return re.sub(r"[\s:：,，.。/\\|_-]+", "", str(text or "")).upper()


def _text_of(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("text", "") or "")
    return str(getattr(item, "text", "") or "")


def _item_to_dict(item: Any, *, offset_x: int = 0, offset_y: int = 0, scale: float = 1.0) -> Dict[str, Any]:
    center = getattr(item, "center_point", None) or (0, 0)
    rect = getattr(item, "rect", None)
    divisor = scale if scale and scale > 0 else 1.0
    return {
        "text": _text_of(item),
        "center": [int(center[0] / divisor) + offset_x, int(center[1] / divisor) + offset_y],
        "rect": [
            int(rect[0] / divisor) + offset_x,
            int(rect[1] / divisor) + offset_y,
            int(rect[2] / divisor),
            int(rect[3] / divisor),
        ]
        if rect
        else None,
        "confidence": float(getattr(item, "confidence", 0.0) or 0.0),
    }


def _join_text(items: Iterable[Any]) -> str:
    return " ".join(text for text in (_text_of(item).strip() for item in items) if text)


def _extract_ints(text: str) -> List[int]:
    return [int(match) for match in re.findall(r"\d+", str(text or ""))]


def _extract_first_int(text: str, default: int = 0) -> int:
    ints = _extract_ints(text)
    return ints[0] if ints else default


def _extract_uid(text: str) -> str:
    match = re.search(r"UID\s*[:：]?\s*(\d{4,})", str(text or ""), re.IGNORECASE)
    if match:
        return match.group(1)[:10]
    match = re.search(r"\d{6,}", str(text or ""))
    return match.group(0)[:10] if match else ""


def _extract_nickname(text: str) -> str:
    cleaned = re.sub(r"(?<!\S)\d+(?!\S)", " ", str(text or "")).strip()
    cjk_runs = re.findall(r"[\u4e00-\u9fff][\u4e00-\u9fffA-Za-z0-9_·-]*", cleaned)
    if cjk_runs:
        return max(cjk_runs, key=len)
    return cleaned


def _extract_ratio(text: str) -> Dict[str, int]:
    match = re.search(r"(\d+)\s*/\s*(\d+)", str(text or ""))
    if match:
        return {"current": int(match.group(1)), "max": int(match.group(2))}
    ints = _extract_ints(text)
    if len(ints) >= 2:
        return {"current": ints[0], "max": ints[1]}
    return {"current": 0, "max": 0}


def _capture_ocr_items(app: Any, ocr: Any, region: Optional[Region] = None, *, scale: float = 1.0) -> List[Dict[str, Any]]:
    capture = app.capture(rect=region)
    if not capture.success:
        raise StopTaskException(f"Player data refresh failed: capture failed for region {region}.", success=False)
    image = capture.image
    if scale and scale != 1.0:
        import cv2

        image = cv2.resize(image, None, fx=float(scale), fy=float(scale), interpolation=cv2.INTER_CUBIC)
    multi = ocr.recognize_all(source_image=image)
    offset_x = int(region[0]) if region else 0
    offset_y = int(region[1]) if region else 0
    return [
        _item_to_dict(item, offset_x=offset_x, offset_y=offset_y, scale=float(scale or 1.0))
        for item in getattr(multi, "results", [])
    ]


def _wait_for_any_marker(
    app: Any,
    ocr: Any,
    *,
    markers: Iterable[str],
    region: Optional[Region] = None,
    timeout_sec: float = 8.0,
    interval_sec: float = 0.5,
    label: str = "page",
) -> List[Dict[str, Any]]:
    normalized_markers = [_normalize_text(marker) for marker in markers]
    deadline = time.time() + max(float(timeout_sec), 0.1)
    last_text = ""
    while time.time() < deadline:
        items = _capture_ocr_items(app, ocr, region)
        last_text = _normalize_text(_join_text(items))
        if any(marker and marker in last_text for marker in normalized_markers):
            return items
        time.sleep(max(float(interval_sec), 0.05))
    raise StopTaskException(
        f"Player data refresh failed: expected {label} markers were not found. Last OCR text: {last_text[:160]}",
        success=False,
    )


def _find_text_item(items: Iterable[Mapping[str, Any]], marker: str) -> Optional[Dict[str, Any]]:
    normalized_marker = _normalize_text(marker)
    for item in items:
        if normalized_marker and normalized_marker in _normalize_text(_text_of(item)):
            return dict(item)
    return None


def _has_all_markers(items: Iterable[Any], markers: Iterable[str]) -> bool:
    normalized_text = _normalize_text(_join_text(items))
    return all(
        normalized_marker and normalized_marker in normalized_text
        for normalized_marker in (_normalize_text(marker) for marker in markers)
    )


def _match_warehouse_entry(app: Any) -> Dict[str, Any]:
    capture = app.capture(rect=_WAREHOUSE_ENTRY_REGION)
    if not getattr(capture, "success", False):
        return {"found": False, "confidence": 0.0, "reason": "capture_failed"}

    source = getattr(capture, "image", None)
    template = cv2.imread(str(_WAREHOUSE_ENTRY_TEMPLATE), cv2.IMREAD_GRAYSCALE)
    if source is None:
        return {"found": False, "confidence": 0.0, "reason": "capture_empty"}
    if template is None:
        raise StopTaskException(
            f"Player data refresh failed: warehouse entry template is unavailable: {_WAREHOUSE_ENTRY_TEMPLATE}",
            success=False,
        )

    if source.ndim == 2:
        source_gray = source
    elif source.shape[2] == 4:
        source_gray = cv2.cvtColor(source, cv2.COLOR_BGRA2GRAY)
    else:
        source_gray = cv2.cvtColor(source, cv2.COLOR_BGR2GRAY)
    template_height, template_width = template.shape[:2]
    source_height, source_width = source_gray.shape[:2]
    if source_width < template_width or source_height < template_height:
        return {"found": False, "confidence": 0.0, "reason": "capture_too_small"}

    score_map = cv2.matchTemplate(source_gray, template, cv2.TM_CCOEFF_NORMED)
    _, confidence, _, top_left = cv2.minMaxLoc(score_map)
    center = [
        int(_WAREHOUSE_ENTRY_REGION[0] + top_left[0] + template_width // 2),
        int(_WAREHOUSE_ENTRY_REGION[1] + top_left[1] + template_height // 2),
    ]
    return {
        "found": float(confidence) >= _WAREHOUSE_ENTRY_TEMPLATE_THRESHOLD,
        "confidence": float(confidence),
        "center": center,
    }


def _enter_warehouse_page(
    app: Any,
    ocr: Any,
    *,
    timeout_sec: float = _WAREHOUSE_ENTRY_TIMEOUT_SEC,
    interval_sec: float = 0.15,
    click_interval_sec: float = 0.55,
) -> None:
    """Continuously template-match, click and verify the warehouse entry for up to 3s."""

    deadline = time.monotonic() + max(float(timeout_sec), 0.1)
    next_click_at = 0.0
    last_page_text = ""
    last_entry_match: Dict[str, Any] = {"found": False, "confidence": 0.0}
    while time.monotonic() < deadline:
        now = time.monotonic()
        if now >= next_click_at:
            last_entry_match = _match_warehouse_entry(app)
            if last_entry_match.get("found"):
                center = last_entry_match["center"]
                app.click(x=int(center[0]), y=int(center[1]))
                next_click_at = now + max(float(click_interval_sec), 0.1)

        page_items = _capture_ocr_items(app, ocr, _INVENTORY_PAGE_REGION)
        last_page_text = _join_text(page_items)
        if _has_all_markers(page_items, ("道具", "材料", "装备")):
            return
        time.sleep(max(float(interval_sec), 0.05))

    raise StopTaskException(
        "Player data refresh failed: warehouse page was not confirmed within "
        f"{float(timeout_sec):.1f}s. Last page OCR: {_normalize_text(last_page_text)[:120]}; "
        f"last entry template confidence: {float(last_entry_match.get('confidence') or 0.0):.3f}",
        success=False,
    )


def _inventory_category_brightness(app: Any, category: str) -> float:
    region = _INVENTORY_CATEGORY_REGIONS[category]
    capture = app.capture(rect=region)
    if not getattr(capture, "success", False) or getattr(capture, "image", None) is None:
        return 0.0
    image = capture.image
    if image.ndim == 2:
        gray = image
    elif image.shape[2] == 4:
        gray = cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
    else:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return float(gray.mean())


def _select_inventory_category(
    app: Any,
    category: str,
    *,
    timeout_sec: float = _INVENTORY_CATEGORY_TIMEOUT_SEC,
    interval_sec: float = 0.15,
    click_interval_sec: float = 0.55,
) -> None:
    """Select a warehouse category and verify its bright selected button without OCR."""

    if category not in _INVENTORY_CATEGORY_ORDER:
        raise ValueError(f"unsupported inventory category: {category}")
    other_categories = tuple(
        candidate for candidate in _INVENTORY_CATEGORY_ORDER if candidate != category
    )
    deadline = time.monotonic() + max(float(timeout_sec), 0.1)
    next_click_at = 0.0
    target_brightness = 0.0
    other_brightness: Dict[str, float] = {}
    while time.monotonic() < deadline:
        now = time.monotonic()
        if now >= next_click_at:
            point = _CLICK_INVENTORY_CATEGORY[category]
            app.click(x=point[0], y=point[1])
            next_click_at = now + max(float(click_interval_sec), 0.1)
        target_brightness = _inventory_category_brightness(app, category)
        other_brightness = {
            candidate: _inventory_category_brightness(app, candidate)
            for candidate in other_categories
        }
        brightest_other = max(other_brightness.values(), default=0.0)
        if target_brightness >= 145.0 and target_brightness - brightest_other >= 35.0:
            return
        time.sleep(max(float(interval_sec), 0.05))
    raise StopTaskException(
        "Player data refresh failed: warehouse category was not confirmed within "
        f"{float(timeout_sec):.1f}s; category={category}; "
        f"target_brightness={target_brightness:.1f}; "
        f"other_brightness={other_brightness}",
        success=False,
    )


def _read_region_text(app: Any, ocr: Any, region: Region, *, scale: float = 1.0) -> str:
    return _join_text(_capture_ocr_items(app, ocr, region, scale=scale))


def _read_ratio_region(app: Any, ocr: Any, region: Region) -> Dict[str, int]:
    return _extract_ratio(_read_region_text(app, ocr, region))


def _parse_city_name(items: List[Dict[str, Any]]) -> str:
    candidates = []
    for item in items:
        text = _text_of(item).strip()
        if not text:
            continue
        compact = _normalize_text(text)
        if any(marker in compact for marker in ("访问城市", "访问地区", "STARTENGINE", "启程")):
            continue
        if any(token in text for token in ("城", "站", "局", "港", "矿")):
            candidates.append(text)
    if candidates:
        return max(candidates, key=len)
    return _join_text(items).strip()


def _read_profile_stage(app: Any, ocr: Any) -> Dict[str, Any]:
    uid_text = _read_region_text(app, ocr, (95, 8, 160, 35), scale=4.0)
    nickname = _extract_nickname(_read_region_text(app, ocr, _PROFILE_FIELD_REGIONS["nickname"]))
    level_text = _read_region_text(app, ocr, _PROFILE_FIELD_REGIONS["level"])
    cargo = _read_ratio_region(app, ocr, _PROFILE_FIELD_REGIONS["cargo"])
    clarity = _read_ratio_region(app, ocr, _PROFILE_FIELD_REGIONS["clarity"])
    fatigue = _read_ratio_region(app, ocr, _PROFILE_FIELD_REGIONS["fatigue"])
    return {
        "profile": {
            "uid": _extract_uid(uid_text),
            "nickname": nickname,
            "level": _extract_first_int(level_text),
        },
        "cargo": cargo,
        "clarity": clarity,
        "fatigue": fatigue,
    }


def _close_profile_panel_to_main(app: Any, ocr: Any) -> None:
    app.click(x=_CLICK_PROFILE_CLOSE[0], y=_CLICK_PROFILE_CLOSE[1])
    _wait_for_any_marker(
        app,
        ocr,
        markers=_MAIN_PAGE_MARKERS,
        region=_MAIN_PAGE_REGION,
        timeout_sec=8.0,
        label="main page after player data refresh",
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _normalize_stages(stages: Any = None) -> Tuple[str, ...]:
    if stages is None:
        return _STAGE_ORDER
    if not isinstance(stages, list):
        raise ValueError("stages must be a list of stage names")

    requested: set[str] = set()
    for stage in stages:
        if not isinstance(stage, str) or stage not in _STAGE_ORDER:
            raise ValueError(
                "stages contains an unsupported value; supported stages are: "
                + ", ".join(_STAGE_ORDER)
            )
        requested.add(stage)

    if not requested:
        raise ValueError("stages must select at least one data stage")
    return tuple(stage for stage in _STAGE_ORDER if stage in requested)


def _normalize_inventory_categories(categories: Any = None) -> Tuple[str, ...]:
    if categories is None:
        return ("items",)
    if not isinstance(categories, list):
        raise ValueError("inventory_categories must be a list")
    requested: set[str] = set()
    for category in categories:
        if not isinstance(category, str) or category not in _INVENTORY_CATEGORY_ORDER:
            raise ValueError(
                "inventory_categories contains an unsupported value; supported values are: "
                + ", ".join(_INVENTORY_CATEGORY_ORDER)
            )
        requested.add(category)
    if not requested:
        raise ValueError("inventory_categories must select at least one category")
    return tuple(category for category in _INVENTORY_CATEGORY_ORDER if category in requested)


def _inventory_categories(payload: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(payload, Mapping):
        return {}
    raw_categories = payload.get("categories")
    if isinstance(raw_categories, Mapping):
        return {
            category: copy.deepcopy(dict(raw_categories[category]))
            for category in _INVENTORY_CATEGORY_ORDER
            if isinstance(raw_categories.get(category), Mapping)
        }
    category = str(payload.get("category") or "").strip()
    if category in _INVENTORY_CATEGORY_ORDER:
        return {category: copy.deepcopy(dict(payload))}
    if isinstance(payload.get("items"), list):
        return {"items": copy.deepcopy(dict(payload))}
    return {}


def _currencies_from_inventory(payload: Any) -> Dict[str, int]:
    items_payload = _inventory_categories(payload).get("items")
    if not isinstance(items_payload, Mapping):
        return {}
    raw_items = items_payload.get("items")
    if not isinstance(raw_items, list):
        return {}

    currencies: Dict[str, int] = {}
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            continue
        currency_key = _CURRENCY_ITEM_IDS.get(str(raw_item.get("item_id") or ""))
        count = raw_item.get("count")
        if currency_key is None or isinstance(count, bool) or not isinstance(count, (int, float)):
            continue
        currencies[currency_key] = int(count)
    return currencies


def _load_latest(*, cache_file: Optional[Path] = None) -> Dict[str, Any]:
    cache_file = Path(cache_file or _PLAYER_LATEST_FILE)
    if not cache_file.is_file():
        raise RuntimeError("No cached Resonance PC player data is available.")
    try:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Cached Resonance PC player data is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Cached Resonance PC player data must be a JSON object.")
    return payload


def _merge_latest(
    existing: Dict[str, Any],
    fresh: Dict[str, Any],
    *,
    section_updated_at: Dict[str, str],
    updated_at: str,
    inventory_category_updated_at: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    merged = copy.deepcopy(existing)

    if "location" in section_updated_at:
        merged["location"] = copy.deepcopy(fresh["location"])
    if "profile" in section_updated_at:
        merged["profile"] = copy.deepcopy(fresh["profile"])
        status = merged.get("status")
        if not isinstance(status, dict):
            status = {}
            merged["status"] = status
        for status_key in ("cargo", "clarity", "fatigue"):
            status[status_key] = copy.deepcopy(fresh["status"][status_key])

    if "inventory" in section_updated_at:
        fresh_inventory = fresh["inventory"]
        fresh_categories = _inventory_categories(fresh_inventory)
        if fresh_categories:
            categories = _inventory_categories(merged.get("inventory"))
            categories.update(fresh_categories)
            merged["inventory"] = {
                "schema_version": 2,
                "categories": categories,
            }
        else:
            merged["inventory"] = copy.deepcopy(fresh_inventory)
        fresh_currencies = fresh.get("currencies")
        if isinstance(fresh_currencies, Mapping) and fresh_currencies:
            currencies = merged.get("currencies")
            if not isinstance(currencies, dict):
                currencies = {}
                merged["currencies"] = currencies
            currencies.update(copy.deepcopy(dict(fresh_currencies)))

    if "characters" in section_updated_at:
        merged["characters"] = copy.deepcopy(fresh["characters"])

    metadata = merged.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    else:
        metadata = copy.deepcopy(metadata)
    previous_section_times = metadata.get("section_updated_at")
    if not isinstance(previous_section_times, dict):
        previous_section_times = {}
    else:
        previous_section_times = copy.deepcopy(previous_section_times)
    previous_section_times.update(section_updated_at)
    previous_section_times = {
        stage: previous_section_times[stage]
        for stage in _DATA_STAGES
        if stage in previous_section_times
    }
    previous_category_times = metadata.get("inventory_category_updated_at")
    if not isinstance(previous_category_times, dict):
        previous_category_times = {}
    else:
        previous_category_times = copy.deepcopy(previous_category_times)
    if inventory_category_updated_at:
        previous_category_times.update(inventory_category_updated_at)
    metadata.update(
        {
            "source": "ocr",
            "updated_at": updated_at,
            "section_updated_at": previous_section_times,
        }
    )
    if previous_category_times:
        metadata["inventory_category_updated_at"] = previous_category_times
    merged["metadata"] = metadata
    return merged


def _persist_latest(
    fresh: Dict[str, Any],
    *,
    section_updated_at: Dict[str, str],
    inventory_category_updated_at: Optional[Dict[str, str]] = None,
    cache_file: Optional[Path] = None,
) -> Dict[str, Any]:
    cache_file = Path(cache_file or _PLAYER_LATEST_FILE)
    existing = _load_latest(cache_file=cache_file) if cache_file.is_file() else {}
    updated_at = _utc_now_iso()
    merged = _merge_latest(
        existing,
        fresh,
        section_updated_at=section_updated_at,
        updated_at=updated_at,
        inventory_category_updated_at=inventory_category_updated_at,
    )
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_file.with_suffix(cache_file.suffix + ".tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(cache_file)
    return merged


def _best_effort_return_to_main(app: Any, ocr: Any, page: str) -> None:
    try:
        if page in {"inventory", "characters"}:
            app.click(x=_CLICK_BACK[0], y=_CLICK_BACK[1])
            page = "profile"
        if page == "profile":
            app.click(x=_CLICK_PROFILE_CLOSE[0], y=_CLICK_PROFILE_CLOSE[1])
        _wait_for_any_marker(
            app,
            ocr,
            markers=_MAIN_PAGE_MARKERS,
            region=_MAIN_PAGE_REGION,
            timeout_sec=3.0,
            label="main page during player data cleanup",
        )
    except Exception:
        return


@action_info(
    name="resonance_pc.player_data_refresh",
    public=True,
    read_only=False,
    timeout=900,
    description="Selectively refresh and automatically persist four Resonance PC player-data sections.",
)
@requires_services(
    app="plans/aura_base/app",
    ocr="plans/aura_base/ocr",
    vision="plans/aura_base/vision",
)
def resonance_pc_player_data_refresh(
    stages: Any = None,
    inventory_categories: Any = None,
    app: Any = None,
    ocr: Any = None,
    vision: Any = None,
) -> Dict[str, Any]:
    if app is None or ocr is None:
        raise RuntimeError("app/ocr service is required")

    selected_stages = _normalize_stages(stages)
    selected_inventory_categories = _normalize_inventory_categories(inventory_categories)
    selected = set(selected_stages)
    character_catalog: Optional[Dict[str, Any]] = None
    if "characters" in selected:
        if vision is None:
            raise RuntimeError("vision service is required for character refresh")
        character_catalog = load_character_catalog(vision=vision)
    prepared_inventory_catalogs: Dict[str, Dict[str, Any]] = {}
    if "inventory" in selected:
        if vision is None:
            raise RuntimeError("vision service is required for inventory refresh")
        prepared_inventory_catalogs = {
            category: prepare_inventory_catalog(category, vision)
            for category in selected_inventory_categories
        }
    section_updated_at: Dict[str, str] = {}
    inventory_category_updated_at: Dict[str, str] = {}
    result: Dict[str, Any] = {}

    _wait_for_any_marker(
        app,
        ocr,
        markers=_MAIN_PAGE_MARKERS,
        region=_MAIN_PAGE_REGION,
        label="main page before player data refresh",
    )

    if "location" in selected:
        main_items = _capture_ocr_items(app, ocr, _MAIN_CITY_REGION)
        result["location"] = {"current_city": _parse_city_name(main_items)}
        section_updated_at["location"] = _utc_now_iso()

    panel_required = bool(selected.intersection(_PROFILE_PANEL_STAGES))
    current_page = "main"
    if panel_required:
        try:
            app.click(x=_CLICK_PROFILE[0], y=_CLICK_PROFILE[1])
            current_page = "unknown"
            _wait_for_any_marker(
                app,
                ocr,
                markers=("UID", "资产", "查看更多信息"),
                region=_PROFILE_REGION,
                label="profile panel",
            )
            current_page = "profile"

            if "profile" in selected:
                profile_data = _read_profile_stage(app, ocr)
                result["profile"] = profile_data["profile"]
                result["status"] = {
                    "cargo": profile_data["cargo"],
                    "clarity": profile_data["clarity"],
                    "fatigue": profile_data["fatigue"],
                }
                section_updated_at["profile"] = _utc_now_iso()

            if "inventory" in selected:
                current_page = "inventory"
                _enter_warehouse_page(app, ocr)
                category_results: Dict[str, Dict[str, Any]] = {}
                for category in selected_inventory_categories:
                    _select_inventory_category(app, category)
                    time.sleep(0.5)
                    category_results[category] = _scan_inventory_stage(
                        app,
                        ocr,
                        vision,
                        category=category,
                        catalog=prepared_inventory_catalogs[category],
                    )
                    inventory_category_updated_at[category] = _utc_now_iso()
                result["inventory"] = {
                    "schema_version": 2,
                    "categories": category_results,
                }
                currencies = _currencies_from_inventory(result["inventory"])
                if currencies:
                    result["currencies"] = currencies
                app.click(x=_CLICK_BACK[0], y=_CLICK_BACK[1])
                _wait_for_any_marker(
                    app,
                    ocr,
                    markers=("UID", "资产", "查看更多信息"),
                    region=_PROFILE_REGION,
                    label="profile panel after warehouse item page",
                )
                current_page = "profile"
                section_updated_at["inventory"] = _utc_now_iso()

            if "characters" in selected:
                if character_catalog is None:
                    raise RuntimeError("character catalog was not loaded")
                current_page = "characters"
                first_page_image = enter_character_page(
                    app,
                    vision,
                    character_catalog,
                )
                result["characters"] = read_player_characters(
                    app,
                    vision,
                    character_catalog,
                    first_page_image=first_page_image,
                )
                app.click(x=_CLICK_BACK[0], y=_CLICK_BACK[1])
                _wait_for_any_marker(
                    app,
                    ocr,
                    markers=("UID", "资产", "查看更多信息"),
                    region=_PROFILE_REGION,
                    label="profile panel after character page",
                )
                current_page = "profile"
                section_updated_at["characters"] = _utc_now_iso()

            current_page = "unknown"
            _close_profile_panel_to_main(app, ocr)
            current_page = "main"
        except Exception:
            _best_effort_return_to_main(app, ocr, current_page)
            raise

    _persist_latest(
        result,
        section_updated_at=section_updated_at,
        inventory_category_updated_at=inventory_category_updated_at,
    )
    persisted = True

    result["metadata"] = {
        "refreshed_at": _utc_now_iso(),
        "source": "ocr",
        "executed_stages": list(selected_stages),
        "skipped_stages": [stage for stage in _STAGE_ORDER if stage not in selected],
        "persisted": persisted,
        "section_updated_at": copy.deepcopy(section_updated_at),
    }
    if inventory_category_updated_at:
        result["metadata"]["inventory_category_updated_at"] = copy.deepcopy(
            inventory_category_updated_at
        )
    return copy.deepcopy(result)


@action_info(
    name="resonance_pc.player_data_get_latest",
    public=True,
    read_only=True,
    description="Get latest cached Resonance PC player data.",
)
def resonance_pc_player_data_get_latest() -> Dict[str, Any]:
    return copy.deepcopy(_load_latest())
