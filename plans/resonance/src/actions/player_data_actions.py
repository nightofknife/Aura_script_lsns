"""Actions for refreshing Resonance player data from in-game OCR screens."""

from __future__ import annotations

import copy
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.utils.exceptions import StopTaskException

from .startup_actions import resonance_enter_main


_PLAN_ROOT = Path(__file__).resolve().parents[2]
_PLAYER_CACHE_ROOT = _PLAN_ROOT / "data" / "cache" / "player"
_PLAYER_LATEST_FILE = _PLAYER_CACHE_ROOT / "latest.json"

Region = Tuple[int, int, int, int]

_CLICK_PROFILE = (150, 655)
_CLICK_CURRENCY_EYE = (329, 217)
_CLICK_CONFIRM = (946, 644)
_CLICK_BACK = (82, 34)
_CLICK_CLARITY = (190, 276)
_CLICK_FATIGUE = (385, 276)

_MAIN_CITY_REGION: Region = (65, 105, 150, 70)
_PROFILE_REGION: Region = (90, 0, 600, 340)
_CURRENCY_POPUP_REGION: Region = (700, 245, 485, 410)
_CLARITY_PAGE_REGION: Region = (0, 0, 1280, 720)
_FATIGUE_PAGE_REGION: Region = (0, 0, 1280, 720)
_MAIN_PAGE_REGION: Region = (0, 0, 1280, 720)
_MAIN_PAGE_MARKERS = ("访问城市", "访问地区", "启程", "STARTENGINE")

_PROFILE_FIELD_REGIONS: Dict[str, Region] = {
    "uid": (105, 10, 180, 30),
    "level": (105, 120, 80, 35),
    "nickname": (105, 150, 385, 45),
    "iron_coins": (170, 198, 135, 40),
    "birch_stone": (430, 198, 80, 40),
    "clarity": (145, 250, 125, 45),
    "fatigue": (360, 250, 125, 45),
    "cargo": (545, 250, 125, 45),
}

_CURRENCY_FIELD_REGIONS: Dict[str, Region] = {
    "iron_coins": (1065, 305, 115, 45),
}

_CLARITY_RATIO_REGION: Region = (150, 395, 230, 80)
_FATIGUE_RATIO_REGION: Region = (90, 585, 130, 65)

_CLARITY_OPTIONS = [
    {
        "name": "仙人掌能量棒棒糖",
        "delta": 40,
        "slot_region": (55, 535, 285, 120),
        "count_region": (55, 610, 70, 45),
    },
    {
        "name": "仙人掌跳跳卷",
        "delta": 60,
        "slot_region": (355, 535, 300, 120),
        "count_region": (360, 610, 80, 45),
    },
    {
        "name": "仙人掌能量跳糖",
        "delta": 180,
        "slot_region": (660, 535, 300, 120),
        "count_region": (660, 610, 80, 45),
    },
    {
        "name": "桦石",
        "delta": 100,
        "slot_region": (960, 515, 270, 140),
        "limit_region": (955, 515, 120, 35),
    },
]

_FATIGUE_OPTIONS = [
    {
        "name": "提神棒棒糖",
        "delta": -60,
        "slot_region": (575, 155, 170, 205),
        "count_region": (720, 280, 28, 40),
    },
    {
        "name": "提神口香糖",
        "delta": -100,
        "slot_region": (815, 155, 175, 205),
        "count_region": (944, 280, 45, 40),
    },
    {
        "name": "仙人掌提神跳糖",
        "delta": -900,
        "slot_region": (575, 410, 175, 205),
        "count_region": (690, 525, 80, 70),
    },
    {
        "name": "桦石",
        "delta": -150,
        "slot_region": (815, 410, 175, 205),
        "limit_region": (815, 410, 140, 40),
    },
]


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


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


def _extract_count_int(text: str, default: int = 0) -> int:
    compact = re.sub(r"\s+", "", str(text or "")).upper()
    if compact in {"T", "I", "L", "市", "丨"}:
        return 1
    if compact and re.fullmatch(r"[0-9A-Z:：]+", compact):
        compact = compact.translate(str.maketrans({"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "T": "1", "B": "3"}))
        ints = _extract_ints(compact)
        if ints:
            return ints[0]
    return _extract_first_int(text, default)


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


def _extract_daily_limit(text: str) -> Optional[str]:
    compact = re.sub(r"[\s:：,，.。\\|_-]+", "", str(text or ""))
    match = re.search(r"每日限购(\d+)/(\d+)", compact)
    if not match:
        match = re.search(r"(\d+)/(\d+)", compact)
    if not match:
        return None
    return f"{int(match.group(1))}/{int(match.group(2))}"


def _looks_unavailable(text: str) -> bool:
    compact = _normalize_text(text)
    return "获取途径" in compact or "獲取途徑" in compact


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


def _parse_profile_panel(app: Any, ocr: Any) -> Dict[str, Any]:
    uid_text = _read_region_text(app, ocr, (95, 8, 160, 35), scale=4.0)
    nickname = _extract_nickname(_read_region_text(app, ocr, _PROFILE_FIELD_REGIONS["nickname"]))
    level_text = _read_region_text(app, ocr, _PROFILE_FIELD_REGIONS["level"])
    clarity = _read_ratio_region(app, ocr, _PROFILE_FIELD_REGIONS["clarity"])
    fatigue = _read_ratio_region(app, ocr, _PROFILE_FIELD_REGIONS["fatigue"])
    cargo = _read_ratio_region(app, ocr, _PROFILE_FIELD_REGIONS["cargo"])
    return {
        "profile": {
            "uid": _extract_uid(uid_text),
            "nickname": nickname,
            "level": _extract_first_int(level_text),
        },
        "currencies": {
            "iron_coins": _read_int_region(app, ocr, _PROFILE_FIELD_REGIONS["iron_coins"]),
            "birch_stone": _read_int_region(app, ocr, _PROFILE_FIELD_REGIONS["birch_stone"]),
        },
        "status": {
            "clarity": clarity,
            "fatigue": fatigue,
            "cargo": cargo,
        },
    }


def _parse_recovery_option(
    *,
    name: str,
    delta: int,
    slot_text: str,
    count_text: str = "",
    limit_text: str = "",
) -> Dict[str, Any]:
    daily_limit = _extract_daily_limit(" ".join([limit_text, slot_text]))
    unavailable = _looks_unavailable(slot_text)
    result: Dict[str, Any] = {
        "name": name,
        "delta": int(delta),
    }
    if daily_limit is not None:
        result["daily_limit"] = daily_limit
        current, max_count = [int(part) for part in daily_limit.split("/", 1)]
        result["available"] = current > 0 and max_count > 0
        return result

    count = _extract_count_int(count_text, default=0)
    if count == 0 and not unavailable:
        ints = [value for value in _extract_ints(slot_text) if value != abs(int(delta))]
        count = ints[-1] if ints else 0
    result["count"] = int(count)
    result["available"] = bool(count > 0 and not unavailable)
    return result


def _read_recovery_options(app: Any, ocr: Any, specs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    options: List[Dict[str, Any]] = []
    for spec in specs:
        slot_text = _read_region_text(app, ocr, spec["slot_region"])
        count_text = _read_region_text(app, ocr, spec["count_region"]) if spec.get("count_region") else ""
        limit_text = _read_region_text(app, ocr, spec["limit_region"]) if spec.get("limit_region") else ""
        options.append(
            _parse_recovery_option(
                name=str(spec["name"]),
                delta=int(spec["delta"]),
                slot_text=slot_text,
                count_text=count_text,
                limit_text=limit_text,
            )
        )
    return options


def _close_profile_panel_to_main(app: Any, ocr: Any) -> None:
    app.press_key("back")
    _wait_for_any_marker(
        app,
        ocr,
        markers=_MAIN_PAGE_MARKERS,
        region=_MAIN_PAGE_REGION,
        timeout_sec=8.0,
        label="main page after player data refresh",
    )


def _persist_latest(payload: Dict[str, Any], *, cache_file: Path = _PLAYER_LATEST_FILE) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_latest(*, cache_file: Path = _PLAYER_LATEST_FILE) -> Dict[str, Any]:
    if not cache_file.is_file():
        raise RuntimeError("No cached Resonance player data is available.")
    payload = json.loads(cache_file.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("Cached Resonance player data must be a JSON object.")
    return payload


@action_info(
    name="resonance.player_data_refresh",
    public=True,
    read_only=False,
    timeout=180,
    description="Refresh Resonance profile, currencies, clarity, fatigue, cargo and recovery options.",
)
@requires_services(
    app="plans/aura_base/app",
    ocr="plans/aura_base/ocr",
)
def resonance_player_data_refresh(
    persist: bool = True,
    enter_main_first: bool = True,
    app: Any = None,
    ocr: Any = None,
) -> Dict[str, Any]:
    if app is None or ocr is None:
        raise RuntimeError("app/ocr service is required")

    if _coerce_bool(enter_main_first):
        resonance_enter_main(app=app, ocr=ocr, launch_from_home=True, max_settle_rounds=300)

    main_items = _capture_ocr_items(app, ocr, _MAIN_CITY_REGION)
    current_city = _parse_city_name(main_items)

    app.click(x=_CLICK_PROFILE[0], y=_CLICK_PROFILE[1])
    _wait_for_any_marker(
        app,
        ocr,
        markers=("UID", "资产", "查看更多信息"),
        region=_PROFILE_REGION,
        label="profile panel",
    )

    parsed = _parse_profile_panel(app, ocr)

    app.click(x=_CLICK_CURRENCY_EYE[0], y=_CLICK_CURRENCY_EYE[1])
    _wait_for_any_marker(
        app,
        ocr,
        markers=("所有货币",),
        region=_CURRENCY_POPUP_REGION,
        label="currency popup",
    )
    popup_iron_coins = _read_int_region(app, ocr, _CURRENCY_FIELD_REGIONS["iron_coins"])
    if popup_iron_coins:
        parsed["currencies"]["iron_coins"] = popup_iron_coins

    app.click(x=_CLICK_CONFIRM[0], y=_CLICK_CONFIRM[1])
    _wait_for_any_marker(
        app,
        ocr,
        markers=("UID", "资产", "查看更多信息"),
        region=_PROFILE_REGION,
        label="profile panel after currency popup",
    )

    app.click(x=_CLICK_CLARITY[0], y=_CLICK_CLARITY[1])
    _wait_for_any_marker(
        app,
        ocr,
        markers=("澄明度", "CLARITY", "请选择恢复方式"),
        region=_CLARITY_PAGE_REGION,
        label="clarity page",
    )
    time.sleep(0.5)
    clarity = _read_ratio_region(app, ocr, _CLARITY_RATIO_REGION)
    clarity["recovery_options"] = _read_recovery_options(app, ocr, _CLARITY_OPTIONS)

    app.click(x=_CLICK_BACK[0], y=_CLICK_BACK[1])
    _wait_for_any_marker(
        app,
        ocr,
        markers=("UID", "资产", "查看更多信息"),
        region=_PROFILE_REGION,
        label="profile panel after clarity page",
    )

    app.click(x=_CLICK_FATIGUE[0], y=_CLICK_FATIGUE[1])
    _wait_for_any_marker(
        app,
        ocr,
        markers=("FATIGUE", "疲劳值", "请选择恢复疲劳值方式"),
        region=_FATIGUE_PAGE_REGION,
        label="fatigue page",
    )
    time.sleep(0.5)
    fatigue = _read_ratio_region(app, ocr, _FATIGUE_RATIO_REGION)
    fatigue["recovery_options"] = _read_recovery_options(app, ocr, _FATIGUE_OPTIONS)

    app.click(x=_CLICK_BACK[0], y=_CLICK_BACK[1])
    _wait_for_any_marker(
        app,
        ocr,
        markers=("UID", "资产", "查看更多信息"),
        region=_PROFILE_REGION,
        label="profile panel after fatigue page",
    )

    _close_profile_panel_to_main(app, ocr)

    result = {
        "profile": parsed["profile"],
        "location": {"current_city": current_city},
        "currencies": parsed["currencies"],
        "status": {
            "clarity": clarity,
            "fatigue": fatigue,
            "cargo": parsed["status"]["cargo"],
        },
        "metadata": {
            "refreshed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "source": "ocr",
        },
    }

    if _coerce_bool(persist):
        _persist_latest(result)
    return copy.deepcopy(result)


@action_info(
    name="resonance.player_data_get_latest",
    public=True,
    read_only=True,
    description="Get latest cached Resonance player data.",
)
def resonance_player_data_get_latest() -> Dict[str, Any]:
    return copy.deepcopy(_load_latest())
