"""PC actions for refreshing Resonance player data from in-game OCR screens."""

from __future__ import annotations

import copy
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.utils.exceptions import StopTaskException

from .inventory_pc_actions import read_inventory_items

Region = Tuple[int, int, int, int]

_PLAN_ROOT = Path(__file__).resolve().parents[2]
_PLAYER_CACHE_ROOT = _PLAN_ROOT / "data" / "cache" / "player"
_PLAYER_LATEST_FILE = _PLAYER_CACHE_ROOT / "latest.json"

_DATA_STAGES = ("location", "profile", "currencies", "clarity", "fatigue", "inventory")
_STAGE_ORDER = (*_DATA_STAGES, "persist")
_PROFILE_PANEL_STAGES = frozenset(
    {"profile", "currencies", "clarity", "fatigue", "inventory"}
)

_scan_inventory_stage = read_inventory_items

_CLICK_PROFILE = (150, 655)
_CLICK_CURRENCY_EYE = (329, 217)
_CLICK_CONFIRM = (946, 644)
_CLICK_BACK = (82, 34)
_CLICK_PROFILE_CLOSE = (900, 150)
_CLICK_CLARITY = (190, 276)
_CLICK_FATIGUE = (385, 276)
_CLICK_INVENTORY = (165, 615)
_WAREHOUSE_ICON_OFFSET_FROM_LABEL = (0, -45)
_WAREHOUSE_ENTRY_TIMEOUT_SEC = 3.0

_MAIN_CITY_REGION: Region = (65, 105, 150, 70)
_PROFILE_REGION: Region = (90, 0, 600, 340)
_CURRENCY_POPUP_REGION: Region = (700, 245, 485, 410)
_CLARITY_PAGE_REGION: Region = (0, 0, 1280, 720)
_FATIGUE_PAGE_REGION: Region = (0, 0, 1280, 720)
_MAIN_PAGE_REGION: Region = (0, 0, 1280, 720)
_INVENTORY_PAGE_REGION: Region = (1050, 0, 230, 520)
_WAREHOUSE_ENTRY_REGION: Region = (110, 560, 140, 150)
_MAIN_PAGE_MARKERS = ("访问城市", "访问地区", "启程", "STARTENGINE")

_PROFILE_FIELD_REGIONS: Dict[str, Region] = {
    "uid": (105, 10, 180, 30),
    "level": (105, 120, 80, 35),
    "nickname": (105, 150, 385, 45),
    "iron_coins": (170, 198, 135, 40),
    "birch_stone": (420, 193, 105, 50),
    "clarity": (145, 250, 125, 45),
    "fatigue": (360, 250, 125, 45),
    "cargo": (545, 250, 125, 45),
}

_CURRENCY_FIELD_REGIONS: Dict[str, Region] = {
    "iron_coins": (1065, 305, 115, 45),
}

_CLARITY_RATIO_REGION: Region = (150, 395, 230, 80)
_FATIGUE_RATIO_REGION: Region = (85, 580, 160, 75)


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


def _enter_warehouse_page(
    app: Any,
    ocr: Any,
    *,
    timeout_sec: float = _WAREHOUSE_ENTRY_TIMEOUT_SEC,
    interval_sec: float = 0.15,
    click_interval_sec: float = 0.55,
) -> None:
    """Continuously locate, click and verify the warehouse entry for up to 3s."""

    deadline = time.monotonic() + max(float(timeout_sec), 0.1)
    next_click_at = 0.0
    last_page_text = ""
    last_entry_text = ""
    while time.monotonic() < deadline:
        page_items = _capture_ocr_items(app, ocr, _INVENTORY_PAGE_REGION)
        last_page_text = _join_text(page_items)
        if _has_all_markers(page_items, ("道具", "材料", "装备")):
            return

        now = time.monotonic()
        if now >= next_click_at:
            entry_items = _capture_ocr_items(app, ocr, _WAREHOUSE_ENTRY_REGION)
            last_entry_text = _join_text(entry_items)
            warehouse_item = _find_text_item(entry_items, "仓库")
            if warehouse_item is not None:
                center = warehouse_item.get("center") or [
                    _CLICK_INVENTORY[0],
                    _CLICK_INVENTORY[1] - _WAREHOUSE_ICON_OFFSET_FROM_LABEL[1],
                ]
                click_x = int(center[0]) + _WAREHOUSE_ICON_OFFSET_FROM_LABEL[0]
                click_y = int(center[1]) + _WAREHOUSE_ICON_OFFSET_FROM_LABEL[1]
                app.click(x=click_x, y=click_y)
                next_click_at = now + max(float(click_interval_sec), 0.1)
        time.sleep(max(float(interval_sec), 0.05))

    raise StopTaskException(
        "Player data refresh failed: warehouse page was not confirmed within "
        f"{float(timeout_sec):.1f}s. Last page OCR: {_normalize_text(last_page_text)[:120]}; "
        f"last entry OCR: {_normalize_text(last_entry_text)[:80]}",
        success=False,
    )


def _read_region_text(app: Any, ocr: Any, region: Region, *, scale: float = 1.0) -> str:
    return _join_text(_capture_ocr_items(app, ocr, region, scale=scale))


def _read_int_region(app: Any, ocr: Any, region: Region) -> int:
    return _extract_first_int(_read_region_text(app, ocr, region))


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
    return {
        "profile": {
            "uid": _extract_uid(uid_text),
            "nickname": nickname,
            "level": _extract_first_int(level_text),
        },
        "cargo": cargo,
    }


def _read_currencies_stage(app: Any, ocr: Any) -> Dict[str, int]:
    return {
        "iron_coins": _read_int_region(app, ocr, _PROFILE_FIELD_REGIONS["iron_coins"]),
        "birch_stone": _read_int_region(app, ocr, _PROFILE_FIELD_REGIONS["birch_stone"]),
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
    if not requested.intersection(_DATA_STAGES):
        raise ValueError("persist cannot run without at least one data stage")
    return tuple(stage for stage in _STAGE_ORDER if stage in requested)


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
        status["cargo"] = copy.deepcopy(fresh["status"]["cargo"])
    if "currencies" in section_updated_at:
        merged["currencies"] = copy.deepcopy(fresh["currencies"])

    for stage in ("clarity", "fatigue"):
        if stage not in section_updated_at:
            continue
        status = merged.get("status")
        if not isinstance(status, dict):
            status = {}
            merged["status"] = status
        status[stage] = copy.deepcopy(fresh["status"][stage])

    if "inventory" in section_updated_at:
        merged["inventory"] = copy.deepcopy(fresh["inventory"])

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
    metadata.update(
        {
            "source": "ocr",
            "updated_at": updated_at,
            "section_updated_at": previous_section_times,
        }
    )
    merged["metadata"] = metadata
    return merged


def _persist_latest(
    fresh: Dict[str, Any],
    *,
    section_updated_at: Dict[str, str],
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
    )
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_file.with_suffix(cache_file.suffix + ".tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(cache_file)
    return merged


def _best_effort_return_to_main(app: Any, ocr: Any, page: str) -> None:
    try:
        if page == "currency":
            app.click(x=_CLICK_CONFIRM[0], y=_CLICK_CONFIRM[1])
            page = "profile"
        elif page in {"clarity", "fatigue", "inventory"}:
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
    timeout=300,
    description="Selectively refresh and optionally persist Resonance PC player data.",
)
@requires_services(
    app="plans/aura_base/app",
    ocr="plans/aura_base/ocr",
)
def resonance_pc_player_data_refresh(
    stages: Any = None,
    app: Any = None,
    ocr: Any = None,
) -> Dict[str, Any]:
    if app is None or ocr is None:
        raise RuntimeError("app/ocr service is required")

    selected_stages = _normalize_stages(stages)
    selected = set(selected_stages)
    section_updated_at: Dict[str, str] = {}
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
                result.setdefault("status", {})["cargo"] = profile_data["cargo"]
                section_updated_at["profile"] = _utc_now_iso()

            if "currencies" in selected:
                currencies = _read_currencies_stage(app, ocr)
                app.click(x=_CLICK_CURRENCY_EYE[0], y=_CLICK_CURRENCY_EYE[1])
                current_page = "unknown"
                _wait_for_any_marker(
                    app,
                    ocr,
                    markers=("所有货币",),
                    region=_CURRENCY_POPUP_REGION,
                    label="currency popup",
                )
                current_page = "currency"
                currencies["iron_coins"] = _read_int_region(
                    app,
                    ocr,
                    _CURRENCY_FIELD_REGIONS["iron_coins"],
                )
                result["currencies"] = currencies
                app.click(x=_CLICK_CONFIRM[0], y=_CLICK_CONFIRM[1])
                current_page = "unknown"
                _wait_for_any_marker(
                    app,
                    ocr,
                    markers=("UID", "资产", "查看更多信息"),
                    region=_PROFILE_REGION,
                    label="profile panel after currency popup",
                )
                current_page = "profile"
                section_updated_at["currencies"] = _utc_now_iso()

            if "clarity" in selected:
                app.click(x=_CLICK_CLARITY[0], y=_CLICK_CLARITY[1])
                current_page = "unknown"
                _wait_for_any_marker(
                    app,
                    ocr,
                    markers=("澄明度", "CLARITY", "请选择恢复方式"),
                    region=_CLARITY_PAGE_REGION,
                    label="clarity page",
                )
                current_page = "clarity"
                time.sleep(0.5)
                clarity = _read_ratio_region(app, ocr, _CLARITY_RATIO_REGION)
                result.setdefault("status", {})["clarity"] = clarity
                app.click(x=_CLICK_BACK[0], y=_CLICK_BACK[1])
                current_page = "unknown"
                _wait_for_any_marker(
                    app,
                    ocr,
                    markers=("UID", "资产", "查看更多信息"),
                    region=_PROFILE_REGION,
                    label="profile panel after clarity page",
                )
                current_page = "profile"
                section_updated_at["clarity"] = _utc_now_iso()

            if "fatigue" in selected:
                app.click(x=_CLICK_FATIGUE[0], y=_CLICK_FATIGUE[1])
                current_page = "unknown"
                _wait_for_any_marker(
                    app,
                    ocr,
                    markers=("FATIGUE", "疲劳值", "请选择恢复疲劳值方式"),
                    region=_FATIGUE_PAGE_REGION,
                    label="fatigue page",
                )
                current_page = "fatigue"
                time.sleep(0.5)
                fatigue = _read_ratio_region(app, ocr, _FATIGUE_RATIO_REGION)
                result.setdefault("status", {})["fatigue"] = fatigue
                app.click(x=_CLICK_BACK[0], y=_CLICK_BACK[1])
                current_page = "unknown"
                _wait_for_any_marker(
                    app,
                    ocr,
                    markers=("UID", "资产", "查看更多信息"),
                    region=_PROFILE_REGION,
                    label="profile panel after fatigue page",
                )
                current_page = "profile"
                section_updated_at["fatigue"] = _utc_now_iso()

            if "inventory" in selected:
                current_page = "inventory"
                _enter_warehouse_page(app, ocr)
                time.sleep(0.5)
                result["inventory"] = _scan_inventory_stage(app, ocr)
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

            current_page = "unknown"
            _close_profile_panel_to_main(app, ocr)
            current_page = "main"
        except Exception:
            _best_effort_return_to_main(app, ocr, current_page)
            raise

    persisted = False
    if "persist" in selected:
        _persist_latest(result, section_updated_at=section_updated_at)
        persisted = True

    result["metadata"] = {
        "refreshed_at": _utc_now_iso(),
        "source": "ocr",
        "executed_stages": list(selected_stages),
        "skipped_stages": [stage for stage in _STAGE_ORDER if stage not in selected],
        "persisted": persisted,
        "section_updated_at": copy.deepcopy(section_updated_at),
    }
    return copy.deepcopy(result)


@action_info(
    name="resonance_pc.player_data_get_latest",
    public=True,
    read_only=True,
    description="Get latest cached Resonance PC player data.",
)
def resonance_pc_player_data_get_latest() -> Dict[str, Any]:
    return copy.deepcopy(_load_latest())
