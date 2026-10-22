"""Template-only rubbish recycling flow for the Resonance Windows client."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested

from ..services.city_shop_data_pc_service import ResonancePcCityShopDataService
from ._rubbish_recycling_flow import RubbishRecyclingError, run_rubbish_recycling


_PLAN_ROOT = Path(__file__).resolve().parents[2]
_CITY_CONFIG_PATH = "data/meta/rubbish_recycling_pc.json"
_LOCATION_PATH = "data/meta/location_pc.json"


def _check_cancelled() -> None:
    if is_current_task_cancel_requested():
        raise asyncio.CancelledError("Rubbish recycling cancelled")


def _raise_error(code: str, message: str, detail: Optional[Dict[str, Any]] = None) -> None:
    logger.error("Rubbish recycling failed code=%s message=%s detail=%s", code, message, detail or {})
    raise RubbishRecyclingError(code=code, message=message, detail=detail)


def _resolve_plan_path(path: str) -> Path:
    raw = Path(str(path or "").strip())
    return raw if raw.is_absolute() else _PLAN_ROOT / raw


def _load_city_config(config_file_path: str = _CITY_CONFIG_PATH) -> Dict[str, Dict[str, str]]:
    path = _resolve_plan_path(config_file_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _raise_error("rubbish_city_config_not_found", "rubbish city configuration was not found", {"path": str(path)})
    except json.JSONDecodeError as exc:
        _raise_error(
            "rubbish_city_config_invalid",
            "rubbish city configuration is not valid JSON",
            {"path": str(path), "cause": str(exc)},
        )
    cities = payload.get("eligible_cities") if isinstance(payload, dict) else None
    if not isinstance(cities, dict):
        _raise_error(
            "rubbish_city_config_invalid",
            "rubbish city configuration must contain eligible_cities",
            {"path": str(path)},
        )
    normalized: Dict[str, Dict[str, str]] = {}
    for city_id, raw_city in cities.items():
        if not isinstance(raw_city, dict):
            continue
        normalized[str(city_id)] = {
            "city_name": str(raw_city.get("city_name") or "").strip(),
            "city_key": str(raw_city.get("city_key") or "").strip(),
            "shop_name": str(raw_city.get("shop_name") or "垃圾处理中心").strip(),
        }
    return normalized


def resolve_rubbish_recycling_city(
    *,
    city_id: Any = None,
    city_name: Any = None,
    city_key: Any = None,
    config_file_path: str = _CITY_CONFIG_PATH,
) -> Optional[Dict[str, str]]:
    cities = _load_city_config(config_file_path)
    normalized_id = str(city_id or "").strip()
    if normalized_id and normalized_id in cities:
        return {"city_id": normalized_id, **cities[normalized_id]}
    normalized_name = str(city_name or "").strip()
    normalized_key = str(city_key or "").strip().lower()
    for candidate_id, candidate in cities.items():
        if normalized_name and normalized_name == candidate.get("city_name"):
            return {"city_id": candidate_id, **candidate}
        if normalized_key and normalized_key == str(candidate.get("city_key") or "").lower():
            return {"city_id": candidate_id, **candidate}
    return None


def is_rubbish_recycling_arrival(leg: Mapping[str, Any]) -> bool:
    return resolve_rubbish_recycling_city(
        city_id=leg.get("to_city_id"),
        city_name=leg.get("to_city"),
        city_key=leg.get("to_city_key"),
    ) is not None


@action_info(
    name="resonance_pc.execute_rubbish_recycling_from_city_panel",
    public=True,
    read_only=False,
    description="Enter an eligible city's rubbish station, recycle all rubbish once, and return to the city panel.",
)
@requires_services(
    app="plans/aura_base/app",
    vision="plans/aura_base/vision",
    resonance_pc_city_shop_data="resonance_pc_city_shop_data",
)
def resonance_pc_execute_rubbish_recycling_from_city_panel(
    city_name: str,
    location_file_path: str = _LOCATION_PATH,
    app: Any = None,
    vision: Any = None,
    resonance_pc_city_shop_data: ResonancePcCityShopDataService | None = None,
) -> Dict[str, Any]:
    _check_cancelled()
    if app is None or vision is None or resonance_pc_city_shop_data is None:
        raise RuntimeError("app/vision/resonance_pc_city_shop_data services are required")
    city = resolve_rubbish_recycling_city(city_name=city_name)
    if city is None:
        _raise_error(
            "rubbish_city_not_eligible",
            "the current city is not configured for rubbish recycling",
            {"city_name": city_name},
        )
    point = resonance_pc_city_shop_data.resolve_shop_point(
        city_name=city["city_name"],
        shop_name=city["shop_name"],
        location_file_path=location_file_path,
    )
    logger.info(
        "Rubbish recycling started city_id=%s city=%s station=(%s,%s)",
        city["city_id"], city["city_name"], point["x"], point["y"],
    )
    result = run_rubbish_recycling(
        city=city, station=point, app=app, vision=vision, check_cancelled=_check_cancelled,
    )
    logger.info(
        "Rubbish recycling completed city=%s status=%s initial_state=%s final_state=%s elapsed_ms=%s",
        result["city_name"], result["status"], result["initial_state"],
        result["final_state"], result["elapsed_ms"],
    )
    return result
