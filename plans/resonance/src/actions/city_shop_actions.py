"""Actions for city shop navigation in Resonance."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict

from packages.aura_core.api import action_info


class CityShopResolveError(RuntimeError):
    """Structured error for city shop resolution."""

    def __init__(self, code: str, message: str, detail: Dict[str, Any] | None = None):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.detail = detail or {}

    def __str__(self) -> str:  # pragma: no cover - trivial formatting
        return f"{self.code}: {self.message}"

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


_CITY_ALIAS_TO_KEY: Dict[str, str] = {
    "阿妮塔能源研究所": "anita_energy_research_institute",
    "7号自由港": "freeport",
    "七号自由港": "freeport",
    "7号自由电港": "freeport",
    "澄明数据中心": "clarity_data_center_administration_bureau",
    "修格里城": "shoggolith_city",
    "修格果城": "shoggolith_city",
    "修格男城": "shoggolith_city",
    "铁盟哨站": "brcl_outpost",
    "荒原站": "wilderness_station",
    "曼德矿场": "mander_mine",
    "淘金乐园": "onederland",
    "阿妮塔战备工厂": "anita_weapon_research_institute",
    "阿妮塔发射中心": "anita_rocket_base",
    "海角城": "cape_city",
    "汇流塔": "confluence_tower",
    "云岫桥基地": "confluence_tower",
    "沃德镇": "confluence_tower",
    "格罗努城": "gronru_city",
}

_CITY_KEY_DISPLAY_NAME: Dict[str, str] = {
    "anita_energy_research_institute": "阿妮塔能源研究所",
    "freeport": "7号自由港",
    "clarity_data_center_administration_bureau": "澄明数据中心",
    "shoggolith_city": "修格里城",
    "brcl_outpost": "铁盟哨站",
    "wilderness_station": "荒原站",
    "mander_mine": "曼德矿场",
    "onederland": "淘金乐园",
    "anita_weapon_research_institute": "阿妮塔战备工厂",
    "anita_rocket_base": "阿妮塔发射中心",
    "gronru_city": "格罗努城",
    "cape_city": "海角城",
    "confluence_tower": "汇流塔",
}

_PLAN_ROOT = Path(__file__).resolve().parents[2]


def _normalize_text(text: str) -> str:
    return re.sub(r"[\s\u3000\|:：,，。.!！?？（）()\[\]【】<>《》'\"`~\-]+", "", str(text))


def _resolve_location_file_path(location_file_path: str) -> Path:
    raw_path = Path(str(location_file_path or "").strip())
    if raw_path.is_absolute():
        return raw_path
    if raw_path.is_file():
        return raw_path.resolve()
    return (_PLAN_ROOT / raw_path).resolve()


def _raise_error(code: str, message: str, detail: Dict[str, Any] | None = None) -> None:
    raise CityShopResolveError(code=code, message=message, detail=detail)


def _resolve_city_key(ocr_city_text: str, available_city_keys: set[str]) -> str:
    normalized_text = _normalize_text(ocr_city_text)
    if not normalized_text:
        _raise_error(
            code="city_not_resolved_from_ocr",
            message="OCR city text is empty after normalization.",
            detail={"ocr_city_text": ocr_city_text},
        )

    # 1) Direct hit by key token (debug / fallback path).
    for city_key in sorted(available_city_keys, key=len, reverse=True):
        if _normalize_text(city_key) and _normalize_text(city_key) in normalized_text:
            return city_key

    # 2) Alias hit by Chinese city name.
    alias_items = sorted(_CITY_ALIAS_TO_KEY.items(), key=lambda kv: len(kv[0]), reverse=True)
    for alias_name, city_key in alias_items:
        if _normalize_text(alias_name) in normalized_text:
            return city_key

    _raise_error(
        code="city_not_resolved_from_ocr",
        message="Unable to resolve city from OCR text.",
        detail={
            "ocr_city_text": ocr_city_text,
            "available_city_keys": sorted(available_city_keys),
        },
    )


@action_info(
    name="resonance.resolve_city_shop_point",
    public=True,
    read_only=True,
    description="Resolve city+shop point from OCR text and location.json.",
)
def resonance_resolve_city_shop_point(
    ocr_city_text: str,
    shop_type: str,
    location_file_path: str,
) -> Dict[str, Any]:
    file_path = _resolve_location_file_path(location_file_path)
    if not file_path.is_file():
        _raise_error(
            code="location_file_not_found",
            message=f"Location file not found: {file_path}",
            detail={
                "location_file_path": str(location_file_path),
                "resolved_location_file_path": str(file_path),
            },
        )

    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _raise_error(
            code="location_json_invalid",
            message="location.json is not valid JSON.",
            detail={"location_file_path": str(file_path), "cause": str(exc)},
        )

    if not isinstance(payload, dict) or not isinstance(payload.get("city"), dict):
        _raise_error(
            code="location_json_invalid",
            message="location.json must include object field 'city'.",
            detail={"location_file_path": str(file_path)},
        )

    city_table: Dict[str, Any] = payload["city"]
    city_key = _resolve_city_key(str(ocr_city_text), set(city_table.keys()))

    city_data = city_table.get(city_key)
    if not isinstance(city_data, dict):
        _raise_error(
            code="city_not_found_in_location",
            message=f"City '{city_key}' not found in location.json.",
            detail={"city_key": city_key, "location_file_path": str(file_path)},
        )

    raw_shop_type = str(shop_type)
    shop_point = city_data.get(raw_shop_type)
    if shop_point is None:
        available_shop_types = sorted([k for k, v in city_data.items() if isinstance(v, list) and len(v) == 2])
        _raise_error(
            code="shop_type_not_found_in_city",
            message=f"Shop type '{raw_shop_type}' not found in city '{city_key}'.",
            detail={
                "city_key": city_key,
                "shop_type": raw_shop_type,
                "available_shop_types": available_shop_types,
            },
        )

    if not isinstance(shop_point, list) or len(shop_point) != 2:
        _raise_error(
            code="shop_point_invalid",
            message=f"Shop point for '{city_key}.{raw_shop_type}' must be [x, y].",
            detail={"city_key": city_key, "shop_type": raw_shop_type, "shop_point": shop_point},
        )

    try:
        x = int(shop_point[0])
        y = int(shop_point[1])
    except (TypeError, ValueError):
        _raise_error(
            code="shop_point_invalid",
            message=f"Shop point for '{city_key}.{raw_shop_type}' must contain numeric x/y.",
            detail={"city_key": city_key, "shop_type": raw_shop_type, "shop_point": shop_point},
        )
    return {
        "city_key": city_key,
        "city_name": _CITY_KEY_DISPLAY_NAME.get(city_key, city_key),
        "shop_type": raw_shop_type,
        "x": x,
        "y": y,
    }
