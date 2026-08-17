"""Read-only passenger route metadata for the Resonance GUI."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from .trade_catalog import trade_meta_path


@dataclass(frozen=True)
class PassengerCity:
    city_id: str
    name: str


@dataclass(frozen=True)
class PassengerRouteEstimate:
    city_a: PassengerCity
    city_b: PassengerCity
    trip_fatigue: int

class PassengerRouteCatalog:
    def __init__(self, payload: dict[str, Any]) -> None:
        cities = payload.get("cities")
        costs = payload.get("costs")
        if not isinstance(cities, dict) or not isinstance(costs, dict):
            raise ValueError("city_travel_fatigue.json 缺少 cities/costs")
        self._cities = {
            str(city_id): PassengerCity(str(city_id), str(city_name))
            for city_id, city_name in cities.items()
            if str(city_id).strip() and str(city_name).strip()
        }
        self._costs = {
            str(from_city_id): {
                str(to_city_id): int(value)
                for to_city_id, value in dict(row or {}).items()
            }
            for from_city_id, row in costs.items()
            if isinstance(row, dict)
        }

    @property
    def cities(self) -> tuple[PassengerCity, ...]:
        return tuple(sorted(self._cities.values(), key=lambda city: _city_sort_key(city.city_id)))

    def city(self, city_id: str) -> PassengerCity:
        normalized = str(city_id or "").strip()
        if normalized not in self._cities:
            raise ValueError(f"未知客运城市 ID：{normalized or '<empty>'}")
        return self._cities[normalized]

    def estimate(self, city_a_id: str, city_b_id: str) -> PassengerRouteEstimate:
        city_a = self.city(city_a_id)
        city_b = self.city(city_b_id)
        if city_a.city_id == city_b.city_id:
            raise ValueError("客运线路的两个城市不能相同")
        try:
            trip_fatigue = int(self._costs[city_a.city_id][city_b.city_id])
        except KeyError as exc:
            raise ValueError(
                f"缺少 {city_a.name} 与 {city_b.name} 的疲劳数据"
            ) from exc
        return PassengerRouteEstimate(
            city_a=city_a,
            city_b=city_b,
            trip_fatigue=trip_fatigue,
        )


def load_passenger_route_catalog() -> PassengerRouteCatalog:
    path = trade_meta_path("city_travel_fatigue.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("city_travel_fatigue.json 顶层必须是字典")
    return PassengerRouteCatalog(payload)


def _city_sort_key(city_id: str) -> tuple[int, str]:
    return (int(city_id), city_id) if city_id.isdigit() else (2**31 - 1, city_id)


__all__ = [
    "PassengerCity",
    "PassengerRouteCatalog",
    "PassengerRouteEstimate",
    "load_passenger_route_catalog",
]
