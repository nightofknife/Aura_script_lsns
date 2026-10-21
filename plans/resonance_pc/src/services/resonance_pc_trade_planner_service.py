"""ResonancePc trading planner service (V1)."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional

from packages.aura_core.api import service_info

from ._buy_lot_normalization import normalize_city_product_buy_lot
from .resonance_pc_market_data_service import (
    ResonancePcMarketDataError,
    ResonancePcMarketDataService,
)
from .resonance_pc_trade_exact_solver import ResonancePcExactTradeSolver


DEFAULT_BOOK_PROFIT_THRESHOLD = 500000


class ResonancePcTradePlannerError(RuntimeError):
    """Structured planner error."""

    def __init__(self, code: str, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


@service_info(
    alias="resonance_pc_trade_planner",
    public=True,
    singleton=True,
    description="ResonancePc exact full-budget trade planner.",
    deps={"resonance_pc_market_data": "resonance_pc_market_data"},
)
class ResonancePcTradePlannerService:
    BUY_LOT_SCHEMA_VERSION = "1.0.0"
    PRODUCT_UNLOCKS_SCHEMA_VERSION = "1.0.0"
    TRADE_CONSTRAINTS_SCHEMA_VERSION = "1.0.0"
    TRADE_RULES_SCHEMA_VERSION = "2.0.0"
    OPTIMAL_ROUTE_CACHE_SIZE = 8
    DEFAULT_ALLOWED_CITY_IDS = [str(city_id) for city_id in range(1, 22)]
    DEFAULT_AVAILABLE_CITY_IDS = [
        "1",
        "2",
        "3",
        "4",
        "5",
        "6",
        "7",
        "8",
        "9",
        "10",
        "11",
        "12",
        "13",
        "15",
        "16",
        "18",
        "20",
        "21",
    ]
    DEFAULT_CITY_ID_TO_KEY = {
        "1": "shoggolith_city",
        "2": "brcl_outpost",
        "3": "freeport",
        "4": "clarity_data_center_administration_bureau",
        "5": "anita_weapon_research_institute",
        "6": "anita_energy_research_institute",
        "7": "wilderness_station",
        "8": "mander_mine",
        "9": "onederland",
        "10": "anita_rocket_base",
        "11": "cape_city",
        "12": "yunxiuqiao_base",
        "13": "confluence_tower",
        "14": "farstar_bridge",
        "15": "lanxin_city",
        "16": "qiyu_station",
        "17": "tatu_station",
        "18": "black_moon_amusement_park",
        "19": "gronru_city",
        "20": "vitilin_forest",
        "21": "wulin_source",
    }
    KNOWN_CITY_KEY_TO_ID = {
        **{value: key for key, value in DEFAULT_CITY_ID_TO_KEY.items()},
        "clarity_data_center": "4",
        "anita_launch_center": "10",
        "seacorner_city": "11",
        "gonglu_city": "19",
    }

    def __init__(
        self,
        resonance_pc_market_data: ResonancePcMarketDataService,
        plan_root: Optional[Path] = None,
    ):
        self.market_data = resonance_pc_market_data
        self.plan_root = Path(plan_root) if plan_root else Path(__file__).resolve().parents[2]
        self.meta_dir = self.plan_root / "data" / "meta"
        self.buy_lot_file = self.meta_dir / "buy_lot.json"
        self.product_unlocks_file = self.meta_dir / "product_unlocks.json"
        self.trade_constraints_file = self.meta_dir / "trade_constraints.json"
        self.trade_rules_file = self.meta_dir / "trade_rules.json"
        self._buy_lot_payload: Optional[Dict[str, Any]] = None
        self._product_unlocks_payload: Optional[Dict[str, Any]] = None
        self._trade_constraints_payload: Optional[Dict[str, Any]] = None
        self._trade_rules_payload: Optional[Dict[str, Any]] = None
        self._optimal_route_cache: OrderedDict[str, Dict[str, Any]] = (
            OrderedDict()
        )
        self._optimal_route_cache_lock = threading.RLock()

    def plan_optimal_route(
        self,
        *,
        fatigue_budget: int = 700,
        cargo_capacity: int = 750,
        book_budget: Optional[int] = 0,
        trade_mode: str = "free",
        book_policy: str = "profit",
        book_waste_limit: int = 0,
        negotiation_policy: str = "auto",
        fixed_route_city_ids: Optional[List[str]] = None,
        reposition_to_route: bool = False,
        target_profit: Any = None,
        book_profit_threshold: Any = DEFAULT_BOOK_PROFIT_THRESHOLD,
        negotiation_budget: Optional[int] = None,
        bargain_success_rates_bps: Optional[List[Any]] = None,
        bargain_step_bps: Optional[Any] = 1000,
        raise_success_rates_bps: Optional[List[Any]] = None,
        raise_step_bps: Optional[Any] = 1000,
        available_city_ids: Optional[List[str]] = None,
        required_end_city_ids: Optional[List[str]] = None,
        city_prestige: Optional[Dict[str, Any]] = None,
        city_tax_rates_bps: Optional[Dict[str, Any]] = None,
        product_unlocks: Optional[Dict[str, Any]] = None,
        current_city_key: Optional[str] = None,
        current_city_id: Optional[str] = None,
        current_city: Optional[str] = None,
        snapshot_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return the exact best complete route for one frozen market snapshot."""

        mode = str(trade_mode).strip().lower()
        if mode not in {"free", "fixed"}:
            raise ResonancePcTradePlannerError(code="invalid_optimal_route_input",
                                              message="trade_mode must be free or fixed")
        city_tax_rates_bps = {} if city_tax_rates_bps is None else city_tax_rates_bps
        if (not isinstance(city_tax_rates_bps, dict)
                or any(not isinstance(key, str) or not key.strip() or type(value) is not int
                       or not 0 <= value <= 10000 for key, value in city_tax_rates_bps.items())):
            raise ResonancePcTradePlannerError(code="invalid_optimal_route_input",
                message="city_tax_rates_bps must map city IDs to integer basis points from 0 to 10000")
        if type(book_budget) is int and book_budget == 0:
            book_policy, book_profit_threshold = "profit", DEFAULT_BOOK_PROFIT_THRESHOLD
        if book_policy != "fill":
            book_waste_limit = 0
        if mode == "fixed":
            if not isinstance(fixed_route_city_ids, list) or len(fixed_route_city_ids) < 2:
                raise ResonancePcTradePlannerError(code="invalid_optimal_route_input",
                                                  message="fixed_route_city_ids must be an ordered list of at least two cities")
            # Preserve order and repeats in the actual request; available cities
            # constrain free-route modes only.
            available_city_ids = list(dict.fromkeys(str(city) for city in (fixed_route_city_ids or [])))
            required_end_city_ids = None
        else:
            fixed_route_city_ids, reposition_to_route = None, False
        if mode == "fixed":
            target_profit = None

        constraints = self._load_trade_constraints_payload()
        if snapshot_id:
            snapshot = self.market_data.get_snapshot(snapshot_id=str(snapshot_id))
        else:
            snapshot = self.market_data.get_latest()
        if not isinstance(snapshot, dict):
            raise ResonancePcTradePlannerError(
                code="invalid_snapshot",
                message="Market snapshot payload is invalid.",
            )

        fatigue_payload = self.market_data.get_all_travel_fatigue()
        self._validate_fatigue_payload(fatigue_payload)
        resolved_city_id = self._resolve_current_city_id(
            current_city_id=current_city_id,
            current_city_key=current_city_key,
            current_city=current_city,
            fatigue_payload=fatigue_payload,
            city_key_to_id=constraints["key_to_city_id"],
        )
        if resolved_city_id is None:
            raise ResonancePcTradePlannerError(
                code="current_city_required",
                message="One current city input is required for optimal route planning.",
            )

        trade_rules_payload = self._load_trade_rules_payload()
        city_rules = (trade_rules_payload.get("tax") or {}).get("city_rules") or {}
        unsupported_currency_ids = {
            str(city) for city, rule in city_rules.items() if rule.get("supported_currency") is False
        }
        explicit_cities = set(str(city) for city in (available_city_ids or []))
        unsupported_requested = sorted((explicit_cities | {resolved_city_id}) & unsupported_currency_ids)
        if unsupported_requested:
            raise ResonancePcTradePlannerError(code="unsupported_trade_currency",
                message="武林源等异币城市尚不支持统一货运收益计算，请移出线路或参与城市。",
                detail={"unsupported_city_ids": unsupported_requested})
        unknown_tax_cities = sorted(set(city_tax_rates_bps) - set(fatigue_payload.get("costs") or {}))
        if unknown_tax_cities:
            raise ResonancePcTradePlannerError(code="invalid_optimal_route_input",
                message=f"city_tax_rates_bps contains unknown city IDs: {unknown_tax_cities}")
        fatigue_costs = fatigue_payload.get("costs") or {}
        supported_city_ids = [
            str(city_id)
            for city_id in fatigue_costs
            if str(city_id).strip()
        ]
        if available_city_ids is None:
            default_available_city_ids = constraints["default_available_city_ids"]
            allowed_city_ids = [
                city_id
                for city_id in supported_city_ids
                if city_id in default_available_city_ids and city_id not in unsupported_currency_ids
            ]
        else:
            if not isinstance(available_city_ids, list):
                raise ResonancePcTradePlannerError(
                    code="invalid_available_city_ids",
                    message="available_city_ids must be a list.",
                )
            requested_city_ids = list(
                dict.fromkeys(str(city_id).strip() for city_id in available_city_ids if str(city_id).strip())
            )
            unsupported_city_ids = [
                city_id for city_id in requested_city_ids if city_id not in supported_city_ids
            ]
            if unsupported_city_ids:
                raise ResonancePcTradePlannerError(
                    code="unsupported_selected_cities",
                    message="Some selected cities are outside the travel-fatigue graph.",
                    detail={
                        "unsupported_city_ids": unsupported_city_ids,
                        "supported_city_ids": supported_city_ids,
                    },
                )
            allowed_city_ids = [
                city_id for city_id in supported_city_ids if city_id in requested_city_ids
            ]
            if len(allowed_city_ids) < 2:
                raise ResonancePcTradePlannerError(
                    code="insufficient_selected_cities",
                    message="At least two available_city_ids are required.",
                    detail={"available_city_ids": allowed_city_ids},
                )
        if mode == "fixed" and resolved_city_id in supported_city_ids and resolved_city_id not in allowed_city_ids:
            allowed_city_ids.append(resolved_city_id)
        if resolved_city_id not in allowed_city_ids:
            raise ResonancePcTradePlannerError(
                code=(
                    "current_city_not_selected"
                    if resolved_city_id in supported_city_ids
                    else "unsupported_city"
                ),
                message=(
                    f"Current city '{resolved_city_id}' is not selected for planning."
                    if resolved_city_id in supported_city_ids
                    else f"Current city '{resolved_city_id}' is outside the travel-fatigue graph."
                ),
                detail={"allowed_city_ids": allowed_city_ids},
            )
        normalized_end_city_ids: Optional[List[str]] = None
        if required_end_city_ids is not None:
            if not isinstance(required_end_city_ids, list):
                raise ResonancePcTradePlannerError(
                    code="invalid_required_end_city_ids",
                    message="required_end_city_ids must be a list.",
                )
            normalized_end_city_ids = list(
                dict.fromkeys(
                    str(city_id).strip()
                    for city_id in required_end_city_ids
                    if str(city_id).strip()
                )
            )
            invalid_end_city_ids = [
                city_id for city_id in normalized_end_city_ids if city_id not in allowed_city_ids
            ]
            if not normalized_end_city_ids or invalid_end_city_ids:
                raise ResonancePcTradePlannerError(
                    code="invalid_required_end_city_ids",
                    message="Required end cities must be selected planning cities.",
                    detail={
                        "required_end_city_ids": normalized_end_city_ids,
                        "invalid_end_city_ids": invalid_end_city_ids,
                        "allowed_city_ids": allowed_city_ids,
                    },
                )

        buy_lot_payload = self._load_buy_lot_payload()
        product_unlocks_payload = self._load_product_unlocks_payload()
        planning_inputs = {
            "start_city_id": resolved_city_id, "required_end_city_ids": normalized_end_city_ids,
            "fatigue_budget": fatigue_budget, "cargo_capacity": cargo_capacity, "book_budget": book_budget,
            "trade_mode": mode, "book_policy": book_policy, "book_waste_limit": book_waste_limit,
            "negotiation_policy": negotiation_policy,
            "fixed_route_city_ids": fixed_route_city_ids,
            "reposition_to_route": reposition_to_route, "target_profit": target_profit,
            "book_profit_threshold": 0 if book_policy == "fill" else book_profit_threshold,
            "negotiation_budget": negotiation_budget,
            "bargain_success_rates_bps": bargain_success_rates_bps, "bargain_step_bps": bargain_step_bps,
            "raise_success_rates_bps": raise_success_rates_bps, "raise_step_bps": raise_step_bps,
            "city_prestige": city_prestige, "city_tax_rates_bps": city_tax_rates_bps,
            "product_unlocks": product_unlocks,
        }
        cache_key = self._optimal_route_cache_key(
            snapshot=snapshot, fatigue_payload=fatigue_payload, buy_lot_payload=buy_lot_payload,
            product_unlocks_payload=product_unlocks_payload, trade_rules_payload=trade_rules_payload,
            allowed_city_ids=allowed_city_ids, planning_inputs=planning_inputs,
        )
        # Default scope reports excluded currencies; an explicit valid scope does not.
        cache_key += ":default_scope" if available_city_ids is None else ":explicit_scope"
        with self._optimal_route_cache_lock:
            cached = self._optimal_route_cache.get(cache_key)
            if cached is not None:
                self._optimal_route_cache.move_to_end(cache_key)
                return copy.deepcopy(cached)

        solver = ResonancePcExactTradeSolver(
            snapshot=snapshot,
            fatigue_payload=fatigue_payload,
            buy_lot=buy_lot_payload["city_product_buy_lot"],
            trade_rules=trade_rules_payload,
            allowed_city_ids=allowed_city_ids,
            unlockable_product_ids=product_unlocks_payload["product_ids"],
        )
        try:
            result = solver.solve(**planning_inputs)
        except (TypeError, ValueError) as exc:
            raise ResonancePcTradePlannerError(
                code="invalid_optimal_route_input",
                message=str(exc),
            ) from exc
        if available_city_ids is None and unsupported_currency_ids:
            result["pricing_excluded_city_ids"] = sorted(unsupported_currency_ids)
            result.setdefault("warnings", []).append("默认规划已排除尚不支持统一计价的异币城市。")
        with self._optimal_route_cache_lock:
            self._optimal_route_cache[cache_key] = copy.deepcopy(result)
            self._optimal_route_cache.move_to_end(cache_key)
            while (
                len(self._optimal_route_cache)
                > self.OPTIMAL_ROUTE_CACHE_SIZE
            ):
                self._optimal_route_cache.popitem(last=False)
        return result

    @staticmethod
    def _optimal_route_cache_key(
        *, snapshot: Dict[str, Any], fatigue_payload: Dict[str, Any],
        buy_lot_payload: Dict[str, Any], product_unlocks_payload: Dict[str, Any],
        trade_rules_payload: Dict[str, Any], allowed_city_ids: List[str], planning_inputs: Dict[str, Any],
    ) -> str:
        payload = {
            "snapshot": snapshot, "fatigue_payload": fatigue_payload,
            "buy_lot_payload": buy_lot_payload, "product_unlocks_payload": product_unlocks_payload,
            "trade_rules_payload": trade_rules_payload, "allowed_city_ids": list(allowed_city_ids),
            "planning_inputs": planning_inputs,
        }
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":"), default=str).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()


    def get_trade_constraints(self) -> Dict[str, Any]:
        return copy.deepcopy(self._load_trade_constraints_payload())

    def assert_allowed_city(
        self,
        *,
        city_key: Optional[str] = None,
        city_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        constraints = self._load_trade_constraints_payload()
        normalized_city_key = str(city_key or "").strip()
        normalized_city_id = str(city_id or "").strip()
        if not normalized_city_key and not normalized_city_id:
            raise ResonancePcTradePlannerError(
                code="invalid_city",
                message="city_key or city_id is required.",
            )

        if normalized_city_key and normalized_city_key not in constraints["allowed_city_keys"]:
            raise ResonancePcTradePlannerError(
                code="unsupported_city",
                message=f"City '{normalized_city_key}' is not allowed in current trade constraints.",
                detail={
                    "city_key": normalized_city_key,
                    "allowed_city_ids": constraints["allowed_city_ids"],
                    "allowed_city_keys": constraints["allowed_city_keys"],
                },
            )
        if normalized_city_id and normalized_city_id not in constraints["allowed_city_ids"]:
            raise ResonancePcTradePlannerError(
                code="unsupported_city",
                message=f"City id '{normalized_city_id}' is not allowed in current trade constraints.",
                detail={
                    "city_id": normalized_city_id,
                    "allowed_city_ids": constraints["allowed_city_ids"],
                    "allowed_city_keys": constraints["allowed_city_keys"],
                },
            )

        if normalized_city_key and normalized_city_id:
            mapped_city_id = constraints["key_to_city_id"].get(normalized_city_key)
            if mapped_city_id != normalized_city_id:
                raise ResonancePcTradePlannerError(
                    code="city_mapping_conflict",
                    message="city_key and city_id do not refer to the same city.",
                    detail={
                        "city_key": normalized_city_key,
                        "city_id": normalized_city_id,
                        "mapped_city_id": mapped_city_id,
                    },
                )

        resolved_city_key = normalized_city_key or constraints["city_id_to_key"][normalized_city_id]
        resolved_city_id = normalized_city_id or constraints["key_to_city_id"][normalized_city_key]
        return {
            "ok": True,
            "city_key": resolved_city_key,
            "city_id": resolved_city_id,
            "allowed_city_ids": constraints["allowed_city_ids"],
            "allowed_city_keys": constraints["allowed_city_keys"],
        }


    def _resolve_current_city_id(
        self,
        *,
        current_city_id: Optional[str],
        current_city_key: Optional[str],
        current_city: Optional[str],
        fatigue_payload: Dict[str, Any],
        city_key_to_id: Optional[Dict[str, str]],
    ) -> Optional[str]:
        resolved: List[Tuple[str, str]] = []
        provided: List[Tuple[str, str]] = []
        for label, token in (
            ("current_city_id", current_city_id),
            ("current_city_key", current_city_key),
            ("current_city", current_city),
        ):
            raw_value = str(token or "").strip()
            if raw_value:
                provided.append((label, raw_value))
            city_id = self._resolve_city_id_token(
                token,
                fatigue_payload=fatigue_payload,
                city_key_to_id=city_key_to_id,
            )
            if city_id is not None:
                resolved.append((label, city_id))

        if not resolved:
            if provided:
                raise ResonancePcTradePlannerError(
                    code="current_city_not_resolved",
                    message="Unable to resolve current city.",
                    detail={label: value for label, value in provided},
                )
            return None
        first_city_id = resolved[0][1]
        conflicts = [(label, city_id) for label, city_id in resolved if city_id != first_city_id]
        if conflicts:
            raise ResonancePcTradePlannerError(
                code="current_city_conflict",
                message="Current city inputs do not resolve to the same city.",
                detail={
                    "resolved": [{"field": label, "city_id": city_id} for label, city_id in resolved],
                },
            )
        return first_city_id

    def _resolve_city_id_token(
        self,
        token: Optional[str],
        *,
        fatigue_payload: Dict[str, Any],
        city_key_to_id: Optional[Dict[str, str]],
    ) -> Optional[str]:
        value = str(token or "").strip()
        if not value:
            return None

        fatigue_costs = fatigue_payload.get("costs") or {}
        if isinstance(fatigue_costs, dict) and value in fatigue_costs:
            return value

        key_maps: List[Dict[str, str]] = []
        if isinstance(city_key_to_id, dict):
            key_maps.append({str(k): str(v) for k, v in city_key_to_id.items()})
        key_maps.append(self.KNOWN_CITY_KEY_TO_ID)
        for key_map in key_maps:
            city_id = key_map.get(value)
            if city_id and (not isinstance(fatigue_costs, dict) or city_id in fatigue_costs):
                return str(city_id)

        city_names = fatigue_payload.get("cities") or {}
        if isinstance(city_names, dict):
            alias_payload = self._load_city_name_aliases()
            aliases = {str(k): str(v) for k, v in alias_payload.items()}
            lookup_values = [value]
            if value in aliases:
                lookup_values.append(aliases[value])

            normalized_lookup = {self._normalize_city_lookup_text(item) for item in lookup_values}
            for raw_city_id, raw_name in city_names.items():
                city_id = str(raw_city_id)
                city_name = str(raw_name or "").strip()
                if not city_name:
                    continue
                if value == city_name or self._normalize_city_lookup_text(city_name) in normalized_lookup:
                    return city_id
        return None

    def _load_city_name_aliases(self) -> Dict[str, str]:
        alias_file = self.meta_dir / "city_aliases.json"
        if not alias_file.is_file():
            return {}
        try:
            payload = json.loads(alias_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        if not isinstance(payload, dict):
            return {}
        return {str(k): str(v) for k, v in payload.items() if str(k).strip() and str(v).strip()}

    @staticmethod
    def _normalize_city_lookup_text(raw: Any) -> str:
        return "".join(ch for ch in str(raw).lower() if ch.isalnum())


    def _load_buy_lot_payload(self) -> Dict[str, Any]:
        if self._buy_lot_payload is not None:
            return self._buy_lot_payload
        if not self.buy_lot_file.is_file():
            raise ResonancePcTradePlannerError(
                code="buy_lot_missing",
                message=f"buy_lot metadata file not found: {self.buy_lot_file}",
            )
        payload = json.loads(self.buy_lot_file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ResonancePcTradePlannerError(
                code="buy_lot_invalid",
                message="buy_lot payload must be an object.",
            )
        schema_version = str(payload.get("schema_version") or "").strip()
        if schema_version != self.BUY_LOT_SCHEMA_VERSION:
            raise ResonancePcTradePlannerError(
                code="buy_lot_invalid",
                message=(
                    f"Unsupported buy_lot schema_version '{schema_version}', "
                    f"expected '{self.BUY_LOT_SCHEMA_VERSION}'."
                ),
            )
        normalized = normalize_city_product_buy_lot(
            payload.get("city_product_buy_lot"), error_factory=ResonancePcTradePlannerError
        )

        self._buy_lot_payload = {
            "schema_version": schema_version,
            "city_product_buy_lot": normalized,
        }
        return self._buy_lot_payload

    def _load_product_unlocks_payload(self) -> Dict[str, Any]:
        if self._product_unlocks_payload is not None:
            return self._product_unlocks_payload
        if not self.product_unlocks_file.is_file():
            raise ResonancePcTradePlannerError(
                code="product_unlocks_missing",
                message=f"product unlock metadata file not found: {self.product_unlocks_file}",
            )
        try:
            payload = json.loads(self.product_unlocks_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ResonancePcTradePlannerError(
                code="product_unlocks_invalid",
                message="product unlock metadata is not valid JSON.",
            ) from exc
        if not isinstance(payload, dict):
            raise ResonancePcTradePlannerError(
                code="product_unlocks_invalid",
                message="product unlock metadata must be an object.",
            )
        schema_version = str(payload.get("schema_version") or "").strip()
        if schema_version != self.PRODUCT_UNLOCKS_SCHEMA_VERSION:
            raise ResonancePcTradePlannerError(
                code="product_unlocks_invalid",
                message=(
                    f"Unsupported product unlock schema_version '{schema_version}', "
                    f"expected '{self.PRODUCT_UNLOCKS_SCHEMA_VERSION}'."
                ),
            )
        city_product_unlocks = payload.get("city_product_unlocks")
        if not isinstance(city_product_unlocks, dict):
            raise ResonancePcTradePlannerError(
                code="product_unlocks_invalid",
                message="product unlock metadata must include city_product_unlocks.",
            )
        normalized: Dict[str, List[str]] = {}
        all_product_ids: set[str] = set()
        for city_id, raw_product_ids in city_product_unlocks.items():
            if not isinstance(raw_product_ids, list):
                raise ResonancePcTradePlannerError(
                    code="product_unlocks_invalid",
                    message=f"product unlock city '{city_id}' must be a list.",
                )
            values = list(
                dict.fromkeys(
                    str(product_id).strip()
                    for product_id in raw_product_ids
                    if str(product_id).strip()
                )
            )
            normalized[str(city_id)] = values
            all_product_ids.update(values)
        self._product_unlocks_payload = {
            "schema_version": schema_version,
            "city_product_unlocks": normalized,
            "product_ids": sorted(
                all_product_ids,
                key=lambda value: (int(value), value)
                if value.isdigit()
                else (2**31 - 1, value),
            ),
        }
        return self._product_unlocks_payload

    def _load_trade_rules_payload(self) -> Dict[str, Any]:
        if self._trade_rules_payload is not None:
            return self._trade_rules_payload
        if not self.trade_rules_file.is_file():
            raise ResonancePcTradePlannerError(
                code="trade_rules_missing",
                message=f"trade rules metadata file not found: {self.trade_rules_file}",
            )
        try:
            payload = json.loads(self.trade_rules_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ResonancePcTradePlannerError(
                code="trade_rules_invalid",
                message="trade rules metadata is not valid JSON.",
            ) from exc
        if not isinstance(payload, dict):
            raise ResonancePcTradePlannerError(
                code="trade_rules_invalid",
                message="trade rules payload must be an object.",
            )
        schema_version = str(payload.get("schema_version") or "").strip()
        if schema_version != self.TRADE_RULES_SCHEMA_VERSION:
            raise ResonancePcTradePlannerError(
                code="trade_rules_invalid",
                message=(
                    f"Unsupported trade rules schema_version '{schema_version}', "
                    f"expected '{self.TRADE_RULES_SCHEMA_VERSION}'."
                ),
            )
        prestige_levels = payload.get("prestige_levels")
        if not isinstance(prestige_levels, dict) or set(prestige_levels) != {
            str(level) for level in range(1, 21)
        }:
            raise ResonancePcTradePlannerError(
                code="trade_rules_invalid",
                message="trade rules must define every prestige level from 1 through 20.",
            )
        negotiation = payload.get("negotiation")
        if not isinstance(negotiation, dict):
            raise ResonancePcTradePlannerError(
                code="trade_rules_invalid",
                message="trade rules must define negotiation rules.",
            )
        if str(negotiation.get("model") or "") != "binary_to_cap_expected_fatigue":
            raise ResonancePcTradePlannerError(
                code="trade_rules_invalid",
                message="trade rules must use the binary_to_cap_expected_fatigue model.",
            )
        max_adjustment_bps = negotiation.get("max_adjustment_bps")
        attempt_fatigue = negotiation.get("attempt_fatigue")
        if (
            isinstance(max_adjustment_bps, bool)
            or not isinstance(max_adjustment_bps, int)
            or max_adjustment_bps <= 0
            or max_adjustment_bps > 10_000
        ):
            raise ResonancePcTradePlannerError(
                code="trade_rules_invalid",
                message="trade rules must define max_adjustment_bps between 1 and 10000.",
            )
        if (
            isinstance(attempt_fatigue, bool)
            or not isinstance(attempt_fatigue, int)
            or attempt_fatigue <= 0
        ):
            raise ResonancePcTradePlannerError(
                code="trade_rules_invalid",
                message="trade rules must define a positive negotiation attempt fatigue cost.",
            )
        defaults = negotiation.get("defaults")
        if not isinstance(defaults, dict):
            raise ResonancePcTradePlannerError(
                code="trade_rules_invalid",
                message="trade rules must define negotiation defaults.",
            )
        for key in ("bargain_success_rates_bps", "raise_success_rates_bps"):
            values = defaults.get(key)
            if (
                not isinstance(values, list)
                or not values
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 0
                    or value > 10_000
                    for value in values
                )
            ):
                raise ResonancePcTradePlannerError(
                    code="trade_rules_invalid",
                    message=f"trade rules default '{key}' must be a non-empty 0..10000 integer list.",
                )
        for key in ("bargain_step_bps", "raise_step_bps"):
            value = defaults.get(key)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
                or value > 2000
            ):
                raise ResonancePcTradePlannerError(
                    code="trade_rules_invalid",
                    message=f"trade rules default '{key}' must be between 1 and 2000.",
                )
        self._trade_rules_payload = payload
        return self._trade_rules_payload

    def _load_trade_constraints_payload(self) -> Dict[str, Any]:
        if self._trade_constraints_payload is not None:
            return self._trade_constraints_payload

        if self.trade_constraints_file.is_file():
            payload = json.loads(self.trade_constraints_file.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ResonancePcTradePlannerError(
                    code="trade_constraints_invalid",
                    message="trade_constraints payload must be an object.",
                )
            schema_version = str(payload.get("schema_version") or "").strip()
            if schema_version != self.TRADE_CONSTRAINTS_SCHEMA_VERSION:
                raise ResonancePcTradePlannerError(
                    code="trade_constraints_invalid",
                    message=(
                        f"Unsupported trade_constraints schema_version '{schema_version}', "
                        f"expected '{self.TRADE_CONSTRAINTS_SCHEMA_VERSION}'."
                    ),
                )
            raw_allowed_ids = payload.get("allowed_city_ids")
            raw_default_available_ids = payload.get(
                "default_available_city_ids",
                raw_allowed_ids,
            )
            raw_city_id_to_key = payload.get("city_id_to_key")
        else:
            raw_allowed_ids = list(self.DEFAULT_ALLOWED_CITY_IDS)
            raw_default_available_ids = list(self.DEFAULT_AVAILABLE_CITY_IDS)
            raw_city_id_to_key = dict(self.DEFAULT_CITY_ID_TO_KEY)

        if not isinstance(raw_allowed_ids, list):
            raise ResonancePcTradePlannerError(
                code="trade_constraints_invalid",
                message="trade_constraints.allowed_city_ids must be a list.",
            )
        if not isinstance(raw_default_available_ids, list):
            raise ResonancePcTradePlannerError(
                code="trade_constraints_invalid",
                message="trade_constraints.default_available_city_ids must be a list.",
            )
        if not isinstance(raw_city_id_to_key, dict):
            raise ResonancePcTradePlannerError(
                code="trade_constraints_invalid",
                message="trade_constraints.city_id_to_key must be an object.",
            )

        allowed_city_ids: List[str] = []
        seen_ids: set[str] = set()
        for raw_city_id in raw_allowed_ids:
            city_id = str(raw_city_id).strip()
            if not city_id or city_id in seen_ids:
                continue
            if city_id not in raw_city_id_to_key:
                raise ResonancePcTradePlannerError(
                    code="trade_constraints_invalid",
                    message=f"city_id_to_key is missing mapping for city_id '{city_id}'.",
                )
            seen_ids.add(city_id)
            allowed_city_ids.append(city_id)

        city_id_to_key: Dict[str, str] = {}
        key_to_city_id: Dict[str, str] = {}
        for city_id in allowed_city_ids:
            city_key = str(raw_city_id_to_key.get(city_id) or "").strip()
            if not city_key:
                raise ResonancePcTradePlannerError(
                    code="trade_constraints_invalid",
                    message=f"city_id_to_key for city_id '{city_id}' must be non-empty string.",
                )
            if city_key in key_to_city_id and key_to_city_id[city_key] != city_id:
                raise ResonancePcTradePlannerError(
                    code="trade_constraints_invalid",
                    message=f"Duplicate city_key mapping in trade constraints: '{city_key}'.",
                )
            city_id_to_key[city_id] = city_key
            key_to_city_id[city_key] = city_id

        if len(allowed_city_ids) < 2:
            raise ResonancePcTradePlannerError(
                code="trade_constraints_invalid",
                message="trade_constraints must include at least two allowed_city_ids.",
            )

        default_available_city_ids: List[str] = []
        seen_default_ids: set[str] = set()
        for raw_city_id in raw_default_available_ids:
            city_id = str(raw_city_id).strip()
            if not city_id or city_id in seen_default_ids:
                continue
            if city_id not in city_id_to_key:
                raise ResonancePcTradePlannerError(
                    code="trade_constraints_invalid",
                    message=(
                        "trade_constraints.default_available_city_ids must be "
                        f"a subset of allowed_city_ids; unexpected city_id '{city_id}'."
                    ),
                )
            seen_default_ids.add(city_id)
            default_available_city_ids.append(city_id)
        if len(default_available_city_ids) < 2:
            raise ResonancePcTradePlannerError(
                code="trade_constraints_invalid",
                message=(
                    "trade_constraints must include at least two "
                    "default_available_city_ids."
                ),
            )

        self._trade_constraints_payload = {
            "schema_version": self.TRADE_CONSTRAINTS_SCHEMA_VERSION,
            "allowed_city_ids": allowed_city_ids,
            "default_available_city_ids": default_available_city_ids,
            "allowed_city_keys": [city_id_to_key[city_id] for city_id in allowed_city_ids],
            "city_id_to_key": city_id_to_key,
            "key_to_city_id": key_to_city_id,
        }
        return self._trade_constraints_payload


    def _validate_fatigue_payload(self, payload: Dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise ResonancePcTradePlannerError(
                code="invalid_fatigue_payload",
                message="Travel fatigue payload must be an object.",
            )
        costs = payload.get("costs")
        if not isinstance(costs, dict):
            raise ResonancePcTradePlannerError(
                code="invalid_fatigue_payload",
                message="Travel fatigue payload must include costs object.",
            )
