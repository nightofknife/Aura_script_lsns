"""City/shop lookup data service for ResonancePc UI flows."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional

from packages.aura_core.api import service_info


class CityShopDataError(RuntimeError):
    """Structured lookup error for city/shop data."""

    def __init__(self, code: str, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.detail = detail or {}

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


_CITY_ALIAS_TO_KEY: Dict[str, str] = {
    "阿妮塔能源研究所": "anita_energy_research_institute",
    "7号自由港": "freeport",
    "七号自由港": "freeport",
    "7号自由电港": "freeport",
    "7号直电港": "freeport",
    "7号直由港": "freeport",
    "7号自电港": "freeport",
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

_SHOP_NAME_TO_KEY: Dict[str, str] = {
    "交易所": "exchange",
    "交易中心": "exchange",
    "休息区": "rest",
    "休息": "rest",
    "作战": "battle",
    "作战区": "battle",
    "商会": "commerce",
    "科伦巴商会": "commerce",
}


def _normalize_text(text: Any) -> str:
    return re.sub(r"[\s\u3000\|:：,，。.!！?？（）()\[\]【】<>《》'\"`~\-]+", "", str(text)).lower()


@service_info(
    alias="resonance_pc_city_shop_data",
    public=True,
    singleton=True,
    description="Resolve ResonancePc city names and city shop coordinates from static data.",
)
class ResonancePcCityShopDataService:
    """Resolve city/shop identifiers from local metadata."""

    def __init__(self, plan_root: Optional[Path] = None):
        self.plan_root = Path(plan_root) if plan_root is not None else Path(__file__).resolve().parents[2]

    def resolve_city(self, city_text: str, location_file_path: str = "data/meta/location_pc.json") -> Dict[str, str]:
        city_table = self._load_city_table(location_file_path)
        city_key = self._resolve_city_key(city_text, city_table)
        return {
            "city_key": city_key,
            "city_name": _CITY_KEY_DISPLAY_NAME.get(city_key, city_key),
        }

    def resolve_shop_point(
        self,
        city_name: str,
        shop_name: str,
        location_file_path: str = "data/meta/location_pc.json",
    ) -> Dict[str, Any]:
        city_table = self._load_city_table(location_file_path)
        city_key = self._resolve_city_key(city_name, city_table)
        shop_key = self._resolve_shop_key(shop_name)
        city_data = city_table.get(city_key)
        if not isinstance(city_data, dict):
            self._raise(
                "city_not_found_in_location",
                f"City '{city_key}' not found in location data.",
                {"city_key": city_key},
            )
        point = city_data.get(shop_key)
        if not isinstance(point, list) or len(point) != 2:
            available = sorted(k for k, value in city_data.items() if isinstance(value, list) and len(value) == 2)
            self._raise(
                "shop_not_found_in_city",
                f"Shop '{shop_name}' not found in city '{city_key}'.",
                {"city_key": city_key, "shop_name": shop_name, "shop_key": shop_key, "available_shop_keys": available},
            )
        try:
            x = int(point[0])
            y = int(point[1])
        except (TypeError, ValueError):
            self._raise(
                "shop_point_invalid",
                f"Shop point for '{city_key}.{shop_key}' must contain numeric x/y.",
                {"city_key": city_key, "shop_key": shop_key, "point": point},
            )
        return {
            "city_key": city_key,
            "city_name": _CITY_KEY_DISPLAY_NAME.get(city_key, city_key),
            "shop_key": shop_key,
            "shop_name": str(shop_name or "").strip(),
            "x": x,
            "y": y,
        }

    def _load_city_table(self, location_file_path: str) -> Dict[str, Any]:
        file_path = self._resolve_path(location_file_path)
        if not file_path.is_file():
            self._raise(
                "location_file_not_found",
                f"Location file not found: {file_path}",
                {"location_file_path": location_file_path, "resolved_location_file_path": str(file_path)},
            )
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            self._raise(
                "location_json_invalid",
                "Location file is not valid JSON.",
                {"location_file_path": str(file_path), "cause": str(exc)},
            )
        if not isinstance(payload, dict) or not isinstance(payload.get("city"), dict):
            self._raise(
                "location_json_invalid",
                "Location file must include object field 'city'.",
                {"location_file_path": str(file_path)},
            )
        return payload["city"]

    def _resolve_path(self, location_file_path: str) -> Path:
        raw = Path(str(location_file_path or "").strip())
        if raw.is_absolute():
            return raw
        if raw.is_file():
            return raw.resolve()
        return (self.plan_root / raw).resolve()

    def _resolve_city_key(self, city_text: str, city_table: Dict[str, Any]) -> str:
        raw = str(city_text or "").strip()
        if not raw:
            self._raise("city_not_resolved", "city_name/city_text is required.")
        if raw in city_table:
            return raw

        normalized = _normalize_text(raw)
        lookup: Dict[str, str] = {}
        for city_key in city_table.keys():
            lookup[_normalize_text(city_key)] = city_key
            display = _CITY_KEY_DISPLAY_NAME.get(city_key)
            if display:
                lookup[_normalize_text(display)] = city_key
        for alias, city_key in _CITY_ALIAS_TO_KEY.items():
            if city_key in city_table:
                lookup[_normalize_text(alias)] = city_key

        if normalized in lookup:
            return lookup[normalized]
        for alias_norm in sorted(lookup.keys(), key=len, reverse=True):
            if alias_norm and alias_norm in normalized:
                return lookup[alias_norm]
        self._raise(
            "city_not_resolved",
            f"Unable to resolve city from '{raw}'.",
            {"city_text": raw, "available_city_keys": sorted(city_table.keys())},
        )
        return ""

    def _resolve_shop_key(self, shop_name: str) -> str:
        raw = str(shop_name or "").strip()
        if not raw:
            self._raise("shop_not_resolved", "shop_name is required.")
        normalized = _normalize_text(raw)
        by_norm = {_normalize_text(name): key for name, key in _SHOP_NAME_TO_KEY.items()}
        if raw in set(_SHOP_NAME_TO_KEY.values()):
            return raw
        if normalized in by_norm:
            return by_norm[normalized]
        self._raise(
            "shop_not_resolved",
            f"Unable to resolve shop from '{raw}'.",
            {"shop_name": raw, "available_shop_names": sorted(_SHOP_NAME_TO_KEY.keys())},
        )
        return ""

    def _raise(self, code: str, message: str, detail: Optional[Dict[str, Any]] = None) -> None:
        raise CityShopDataError(code=code, message=message, detail=detail)
