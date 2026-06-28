"""Resonance market data service: fetch, normalize, cache, and query."""

from __future__ import annotations

import csv
import copy
import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from packages.aura_core.api import service_info
from packages.aura_core.observability.logging.core_logger import logger


class ResonanceMarketDataError(RuntimeError):
    """Structured market data service error."""

    def __init__(self, code: str, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "detail": self.detail,
        }


@service_info(
    alias="resonance_market_data",
    public=True,
    singleton=True,
    description="Fetches and normalizes Resonance market snapshots with local cache.",
)
class ResonanceMarketDataService:
    API_URL = "https://www.resonance-columba.com/api/get-prices"
    ROUTE_PAGE_URL = "https://www.resonance-columba.com/route"
    ROUTE_HOST = "https://www.resonance-columba.com"
    DEFAULT_TIMEOUT_SECONDS = 20
    DEFAULT_DEDUPE_WINDOW_SECONDS = 300
    TRAVEL_FATIGUE_SCHEMA_VERSION = "1.0.0"
    BUY_LOT_SCHEMA_VERSION = "1.0.0"
    CONSTANTS_CITIES_EXPORT = "CITIES"
    CONSTANTS_FATIGUE_EXPORT = "CITY_FATIGUES"

    def __init__(
        self,
        plan_root: Optional[Path] = None,
        api_url: str = API_URL,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        dedupe_window_seconds: int = DEFAULT_DEDUPE_WINDOW_SECONDS,
    ):
        self._lock = threading.RLock()
        self.api_url = str(api_url).strip() or self.API_URL
        self.timeout_seconds = max(int(timeout_seconds), 1)
        self.dedupe_window_seconds = max(int(dedupe_window_seconds), 1)

        self.plan_root = Path(plan_root) if plan_root else Path(__file__).resolve().parents[2]
        self.meta_dir = self.plan_root / "data" / "meta"
        self.cache_root = self.plan_root / "data" / "cache" / "market"
        self.snapshots_dir = self.cache_root / "snapshots"
        self.latest_file = self.cache_root / "latest.json"
        self.index_file = self.cache_root / "index.json"
        self.travel_fatigue_file = self.meta_dir / "city_travel_fatigue.json"
        self.buy_lot_file = self.meta_dir / "buy_lot.json"
        self.city_alias_file = self.meta_dir / "city_aliases.json"

        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.meta_dir.mkdir(parents=True, exist_ok=True)

        self.city_name_map = self._load_name_mapping(self.meta_dir / "cities.json")
        self.product_name_map = self._load_name_mapping(self.meta_dir / "products.json")
        self.city_alias_name_map = self._load_alias_mapping(self.city_alias_file)
        self._travel_fatigue_data: Optional[Dict[str, Any]] = None

    def get_travel_fatigue(self, from_city_id: str, to_city_id: str) -> int:
        from_id = str(from_city_id or "").strip()
        to_id = str(to_city_id or "").strip()
        if not from_id or not to_id:
            raise ResonanceMarketDataError(
                code="invalid_city_id",
                message="from_city_id and to_city_id are required.",
            )

        data = self._load_travel_fatigue_data()
        costs = data["costs"]
        if from_id not in costs:
            raise ResonanceMarketDataError(
                code="unknown_city_id",
                message=f"Unknown city id '{from_id}'.",
            )
        if to_id not in costs:
            raise ResonanceMarketDataError(
                code="unknown_city_id",
                message=f"Unknown city id '{to_id}'.",
            )
        return int(costs[from_id][to_id])

    def get_all_travel_fatigue(self) -> Dict[str, Any]:
        return copy.deepcopy(self._load_travel_fatigue_data())

    def refresh(self, force: bool = False) -> Dict[str, Any]:
        try:
            raw = self.fetch_raw()
            buy_lot_payload = self.resolve_buy_lot_payload()
            normalized = self.normalize(raw, buy_lot_payload=buy_lot_payload)
            persisted = self.persist(normalized, force=bool(force))
            result = copy.deepcopy(persisted)
            result["stale"] = False
            return result
        except Exception as exc:  # noqa: BLE001
            logger.warning("Resonance market refresh failed, trying stale fallback: %s", exc)
            try:
                latest = self.load_latest()
            except ResonanceMarketDataError as fallback_error:
                raise ResonanceMarketDataError(
                    code="market_refresh_failed_no_cache",
                    message="Failed to refresh market data and no cached snapshot is available.",
                    detail={"cause": str(exc), "fallback_cause": fallback_error.to_dict()},
                ) from exc

            result = copy.deepcopy(latest)
            result["stale"] = True
            result["stale_reason"] = str(exc)
            return result

    def sync_web_constants(self, sync_buy_lot: bool = True) -> Dict[str, Any]:
        constants_payload = self.fetch_route_constants_payload()
        self._write_json(self.meta_dir / "cities.json", constants_payload["cities"])
        self._write_json(self.travel_fatigue_file, constants_payload["travel_fatigue"])
        self._write_travel_fatigue_csv(constants_payload["travel_fatigue"], self.meta_dir / "city_travel_fatigue.csv")

        with self._lock:
            self.city_name_map = self._load_name_mapping(self.meta_dir / "cities.json")
            self._travel_fatigue_data = None
            self.city_alias_name_map = self._load_alias_mapping(self.city_alias_file)

        buy_lot_result: Optional[Dict[str, Any]] = None
        if sync_buy_lot:
            buy_lot_payload = self.resolve_buy_lot_payload()
            buy_lot_result = {
                "mapped_pairs": sum(len(v) for v in buy_lot_payload.get("city_product_buy_lot", {}).values()),
                "unknown_city_names": buy_lot_payload.get("unknown_city_names", []),
                "remapped_city_names": buy_lot_payload.get("remapped_city_names", []),
            }

        travel_payload = constants_payload["travel_fatigue"]
        city_count = len(constants_payload["cities"])
        edge_count = city_count * (city_count - 1) // 2
        result = {
            "status": "ok",
            "route_chunk": constants_payload.get("route_chunk"),
            "cities_count": city_count,
            "fatigue_edge_count": edge_count,
            "has_wode_city": "沃德镇" in constants_payload["cities"].values(),
        }
        if buy_lot_result is not None:
            result["buy_lot_sync"] = buy_lot_result
        result["travel_fatigue_schema_version"] = travel_payload.get("schema_version")
        return result

    def get_latest(self) -> Dict[str, Any]:
        snapshot = self.load_latest()
        result = copy.deepcopy(snapshot)
        result.setdefault("stale", False)
        return result

    def get_snapshot(self, snapshot_id: str) -> Dict[str, Any]:
        sid = str(snapshot_id or "").strip()
        if not sid:
            raise ResonanceMarketDataError(
                code="invalid_snapshot_id",
                message="snapshot_id is required.",
            )

        snapshot_file = self.snapshots_dir / f"{sid}.json"
        if not snapshot_file.is_file():
            raise ResonanceMarketDataError(
                code="snapshot_not_found",
                message=f"Snapshot '{sid}' does not exist.",
            )
        snapshot = self._read_json(snapshot_file)
        snapshot.setdefault("stale", False)
        return snapshot

    def list_snapshots(self, limit: int = 50) -> List[Dict[str, Any]]:
        max_items = max(int(limit), 1)
        index_entries = self._load_index_entries()
        return index_entries[:max_items]

    def query_products(
        self,
        scope: Optional[str] = None,
        city_id: Optional[str] = None,
        side: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        normalized_scope = str(scope).strip() if scope is not None else None
        normalized_city_id = str(city_id).strip() if city_id is not None else None
        normalized_side = str(side).strip() if side is not None else None

        if normalized_scope and normalized_scope not in {"unique", "shared", "non_buy"}:
            raise ResonanceMarketDataError(
                code="invalid_scope_filter",
                message=f"Unsupported scope '{normalized_scope}'.",
            )
        if normalized_side and normalized_side not in {"buy", "sell"}:
            raise ResonanceMarketDataError(
                code="invalid_side_filter",
                message=f"Unsupported side '{normalized_side}'.",
            )

        snapshot = self.get_latest()
        products = snapshot.get("products", {})
        if not isinstance(products, dict):
            return []

        rows: List[Dict[str, Any]] = []
        for product_id in self._sorted_ids(products.keys()):
            product = products.get(product_id, {})
            if not isinstance(product, dict):
                continue

            classification = product.get("classification", {})
            product_scope = classification.get("scope")
            if normalized_scope and product_scope != normalized_scope:
                continue

            market = product.get("market", {})
            buy_map = market.get("buy", {}) if isinstance(market, dict) else {}
            sell_map = market.get("sell", {}) if isinstance(market, dict) else {}

            if normalized_side == "buy" and (not isinstance(buy_map, dict) or not buy_map):
                continue
            if normalized_side == "sell" and (not isinstance(sell_map, dict) or not sell_map):
                continue

            if normalized_city_id:
                if normalized_side == "buy":
                    if normalized_city_id not in buy_map:
                        continue
                elif normalized_side == "sell":
                    if normalized_city_id not in sell_map:
                        continue
                else:
                    if normalized_city_id not in buy_map and normalized_city_id not in sell_map:
                        continue

            row = {
                "product_id": str(product_id),
                "name": product.get("name"),
                "market": {
                    "buy": buy_map if isinstance(buy_map, dict) else {},
                    "sell": sell_map if isinstance(sell_map, dict) else {},
                },
                "classification": classification if isinstance(classification, dict) else {},
            }
            rows.append(row)
        return rows

    def fetch_raw(self) -> Dict[str, Any]:
        req = urllib.request.Request(
            self.api_url,
            headers={"User-Agent": "Aura-Resonance/1.0 (+https://www.resonance-columba.com)"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as response:
                raw_text = response.read().decode("utf-8")
        except urllib.error.URLError as exc:
            raise ResonanceMarketDataError(
                code="market_fetch_failed",
                message="Failed to fetch market prices from remote API.",
                detail={"cause": str(exc), "url": self.api_url},
            ) from exc

        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ResonanceMarketDataError(
                code="market_payload_invalid_json",
                message="API response is not valid JSON.",
                detail={"cause": str(exc)},
            ) from exc

        if not isinstance(payload, dict):
            raise ResonanceMarketDataError(
                code="market_payload_invalid_type",
                message="API response root must be an object.",
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ResonanceMarketDataError(
                code="market_payload_missing_data",
                message="API response is missing 'data' object.",
            )
        return payload

    def normalize(self, raw: Dict[str, Any], buy_lot_payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = raw.get("data")
        if not isinstance(data, dict):
            raise ResonanceMarketDataError(
                code="normalize_invalid_payload",
                message="Expected raw payload with object field 'data'.",
            )
        buy_lot = self._extract_city_product_buy_lot(buy_lot_payload or self.load_buy_lot_payload())

        fetched_at = self._utc_now_iso()
        payload_hash = self._payload_hash(raw)

        all_seen_city_ids: set[str] = set()
        all_seen_city_ids.update(self.city_name_map.keys())

        city_to_buy_products: Dict[str, List[str]] = {}
        city_to_sell_products: Dict[str, List[str]] = {}
        unique_products: List[str] = []
        shared_products: List[str] = []
        non_buy_products: List[str] = []
        normalized_products: Dict[str, Dict[str, Any]] = {}

        for raw_product_id in self._sorted_ids(data.keys()):
            product_id = str(raw_product_id)
            product_payload = data.get(raw_product_id, {})
            if not isinstance(product_payload, dict):
                product_payload = {}

            sell_payload = product_payload.get("s") or {}
            buy_payload = product_payload.get("b") or {}

            sell_map = self._normalize_market_side(sell_payload, all_seen_city_ids)
            buy_map = self._normalize_market_side(buy_payload, all_seen_city_ids)

            for city_id in sell_map.keys():
                city_to_sell_products.setdefault(city_id, []).append(product_id)
            for city_id in buy_map.keys():
                city_to_buy_products.setdefault(city_id, []).append(product_id)

            buy_city_ids = self._sorted_ids(buy_map.keys())
            buy_city_count = len(buy_city_ids)
            if buy_city_count == 0:
                scope = "non_buy"
                non_buy_products.append(product_id)
            elif buy_city_count == 1:
                scope = "unique"
                unique_products.append(product_id)
            else:
                scope = "shared"
                shared_products.append(product_id)

            normalized_products[product_id] = {
                "name": self._resolve_product_name(product_id),
                "market": {
                    "buy": buy_map,
                    "sell": sell_map,
                },
                "buy_lot": {
                    city_id: int(buy_lot.get(city_id, {}).get(product_id, 0))
                    for city_id in buy_city_ids
                    if int(buy_lot.get(city_id, {}).get(product_id, 0)) > 0
                },
                "classification": {
                    "buy_city_ids": buy_city_ids,
                    "buy_city_count": buy_city_count,
                    "scope": scope,
                },
            }

        ordered_city_ids = self._sorted_ids(all_seen_city_ids)
        cities = {
            city_id: {"name": self._resolve_city_name(city_id)}
            for city_id in ordered_city_ids
        }

        for city_id in ordered_city_ids:
            city_to_buy_products.setdefault(city_id, [])
            city_to_sell_products.setdefault(city_id, [])

        indexes = {
            "city_to_buy_products": {
                city_id: self._sorted_ids(products)
                for city_id, products in city_to_buy_products.items()
            },
            "city_to_sell_products": {
                city_id: self._sorted_ids(products)
                for city_id, products in city_to_sell_products.items()
            },
            "unique_products": self._sorted_ids(unique_products),
            "shared_products": self._sorted_ids(shared_products),
            "non_buy_products": self._sorted_ids(non_buy_products),
        }

        snapshot_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{payload_hash[:10]}"
        return {
            "snapshot_id": snapshot_id,
            "source": self.api_url,
            "fetched_at": fetched_at,
            "raw_payload_hash": payload_hash,
            "buy_lot_schema_version": str((buy_lot_payload or {}).get("schema_version") or self.BUY_LOT_SCHEMA_VERSION),
            "cities": cities,
            "products": normalized_products,
            "indexes": indexes,
        }

    def resolve_buy_lot_payload(self) -> Dict[str, Any]:
        try:
            payload = self.fetch_buy_lot_payload()
            self._write_json(self.buy_lot_file, payload)
            return payload
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to fetch buy_lot from route webpage, using local metadata: %s", exc)
            return self.load_buy_lot_payload()

    def fetch_route_constants_payload(self) -> Dict[str, Any]:
        route_html = self._http_get_text(self.ROUTE_PAGE_URL)
        script_urls = self._extract_route_chunk_urls(route_html)
        if not script_urls:
            raise ResonanceMarketDataError(
                code="constants_fetch_failed",
                message="No route chunk script URL found on route page.",
            )

        last_error: Optional[Exception] = None
        for script_url in script_urls:
            try:
                chunk_text = self._http_get_text(script_url)
                parsed = self._extract_route_constants_from_chunk(chunk_text)
                if parsed is None:
                    continue
                city_names = parsed["cities"]
                fatigue_edges = parsed["fatigue_edges"]
                city_map = {str(index + 1): str(name) for index, name in enumerate(city_names)}
                travel_fatigue = self._build_travel_fatigue_payload(city_map, fatigue_edges)
                return {
                    "route_chunk": script_url,
                    "cities": city_map,
                    "travel_fatigue": travel_fatigue,
                }
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue
        raise ResonanceMarketDataError(
            code="constants_fetch_failed",
            message="Failed to extract route constants from route chunks.",
            detail={"cause": str(last_error) if last_error else "no_valid_chunk"},
        )

    def load_buy_lot_payload(self) -> Dict[str, Any]:
        if not self.buy_lot_file.is_file():
            raise ResonanceMarketDataError(
                code="buy_lot_missing",
                message=f"buy_lot file not found: {self.buy_lot_file}",
            )
        payload = self._read_json(self.buy_lot_file)
        if not isinstance(payload, dict):
            raise ResonanceMarketDataError(
                code="buy_lot_invalid",
                message="buy_lot payload must be an object.",
            )
        schema_version = str(payload.get("schema_version") or "").strip()
        if schema_version != self.BUY_LOT_SCHEMA_VERSION:
            raise ResonanceMarketDataError(
                code="buy_lot_invalid",
                message=(
                    f"Unsupported buy_lot schema_version '{schema_version}', "
                    f"expected '{self.BUY_LOT_SCHEMA_VERSION}'."
                ),
            )
        city_product_buy_lot = self._extract_city_product_buy_lot(payload)
        return {
            "schema_version": schema_version,
            "source": payload.get("source"),
            "default_lot": int(payload.get("default_lot") or 0),
            "city_product_buy_lot": city_product_buy_lot,
        }

    def fetch_buy_lot_payload(self) -> Dict[str, Any]:
        route_html = self._http_get_text(self.ROUTE_PAGE_URL)
        script_urls = self._extract_route_chunk_urls(route_html)
        if not script_urls:
            raise ResonanceMarketDataError(
                code="buy_lot_fetch_failed",
                message="No route chunk script URL found on route page.",
            )

        pairs: Optional[Dict[str, Dict[str, int]]] = None
        selected_script_url: Optional[str] = None
        last_error: Optional[Exception] = None

        for script_url in script_urls:
            try:
                chunk_text = self._http_get_text(script_url)
                extracted = self._extract_product_buy_lot_pairs(chunk_text)
                if extracted:
                    pairs = extracted
                    selected_script_url = script_url
                    break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue

        if pairs is None:
            raise ResonanceMarketDataError(
                code="buy_lot_fetch_failed",
                message="Failed to extract buy_lot data from route chunks.",
                detail={"cause": str(last_error) if last_error else "no_valid_chunk"},
            )

        city_name_to_id = {name: city_id for city_id, name in self.city_name_map.items()}
        product_name_to_id = {name: product_id for product_id, name in self.product_name_map.items()}
        city_product_buy_lot = {city_id: {} for city_id in self.city_name_map.keys()}
        unknown_city_names: List[str] = []
        unknown_product_names: List[str] = []
        remapped_city_names: List[Dict[str, str]] = []

        for product_name, city_lots in pairs.items():
            product_id = product_name_to_id.get(product_name)
            if not product_id:
                unknown_product_names.append(product_name)
                continue
            for city_name, lot in city_lots.items():
                city_id = city_name_to_id.get(city_name)
                if not city_id:
                    alias_target_name = self.city_alias_name_map.get(city_name)
                    if alias_target_name:
                        city_id = city_name_to_id.get(alias_target_name)
                        if city_id:
                            remapped_city_names.append(
                                {
                                    "from_city_name": city_name,
                                    "to_city_name": alias_target_name,
                                    "to_city_id": city_id,
                                }
                            )
                if not city_id:
                    unknown_city_names.append(city_name)
                    continue
                city_product_buy_lot.setdefault(city_id, {})[product_id] = int(lot)

        payload = {
            "schema_version": self.BUY_LOT_SCHEMA_VERSION,
            "source": {
                "site": self.ROUTE_PAGE_URL,
                "route_chunk": selected_script_url,
                "extracted_at": self._utc_now_iso(),
            },
            "default_lot": 0,
            "city_product_buy_lot": city_product_buy_lot,
        }
        if unknown_city_names:
            payload["unknown_city_names"] = sorted(set(unknown_city_names))
        if unknown_product_names:
            payload["unknown_product_names"] = sorted(set(unknown_product_names))
        if remapped_city_names:
            deduped = sorted(
                {json.dumps(row, ensure_ascii=False, sort_keys=True) for row in remapped_city_names}
            )
            payload["remapped_city_names"] = [json.loads(row) for row in deduped]
        return payload

    def _extract_route_constants_from_chunk(self, chunk_text: str) -> Optional[Dict[str, Any]]:
        if self.CONSTANTS_CITIES_EXPORT not in chunk_text or self.CONSTANTS_FATIGUE_EXPORT not in chunk_text:
            return None
        city_var = self._extract_export_var_name(chunk_text, self.CONSTANTS_CITIES_EXPORT)
        fatigue_var = self._extract_export_var_name(chunk_text, self.CONSTANTS_FATIGUE_EXPORT)
        if not city_var or not fatigue_var:
            return None

        city_export_idx = chunk_text.find(f"{self.CONSTANTS_CITIES_EXPORT}:()=>{city_var}")
        fatigue_export_idx = chunk_text.find(f"{self.CONSTANTS_FATIGUE_EXPORT}:()=>{fatigue_var}")
        from_index = max(city_export_idx, fatigue_export_idx, 0)

        city_expr = self._extract_assigned_array_expression(chunk_text, city_var, from_index=from_index)
        fatigue_expr = self._extract_assigned_array_expression(chunk_text, fatigue_var, from_index=from_index)
        city_names = self._parse_string_array_expression(city_expr)
        fatigue_edges = self._parse_fatigue_array_expression(fatigue_expr)
        if not city_names or not fatigue_edges:
            return None
        return {
            "cities": city_names,
            "fatigue_edges": fatigue_edges,
        }

    @staticmethod
    def _extract_export_var_name(chunk_text: str, export_name: str) -> Optional[str]:
        pattern = re.compile(rf"{re.escape(export_name)}:\(\)=>([A-Za-z_$][A-Za-z0-9_$]*)")
        match = pattern.search(chunk_text)
        if not match:
            return None
        return str(match.group(1))

    @staticmethod
    def _extract_assigned_array_expression(chunk_text: str, var_name: str, from_index: int = 0) -> str:
        assign_pattern = re.compile(rf"(?<![A-Za-z0-9_$]){re.escape(var_name)}=\[")
        match = assign_pattern.search(chunk_text, max(from_index, 0))
        if not match:
            match = assign_pattern.search(chunk_text)
        if not match:
            raise ResonanceMarketDataError(
                code="constants_parse_failed",
                message=f"Failed to locate array assignment for variable '{var_name}'.",
            )
        start = match.end() - 1
        return ResonanceMarketDataService._extract_balanced_brackets(chunk_text, start)

    @staticmethod
    def _extract_balanced_brackets(source: str, start: int) -> str:
        if start < 0 or start >= len(source) or source[start] != "[":
            raise ResonanceMarketDataError(
                code="constants_parse_failed",
                message="Invalid array expression start.",
            )

        depth = 0
        in_string: Optional[str] = None
        escaped = False
        for idx in range(start, len(source)):
            ch = source[idx]
            if in_string is not None:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == in_string:
                    in_string = None
                continue

            if ch in ('"', "'"):
                in_string = ch
                continue
            if ch == "[":
                depth += 1
                continue
            if ch == "]":
                depth -= 1
                if depth == 0:
                    return source[start : idx + 1]
        raise ResonanceMarketDataError(
            code="constants_parse_failed",
            message="Unterminated array expression in chunk content.",
        )

    @staticmethod
    def _parse_string_array_expression(array_expr: str) -> List[str]:
        try:
            loaded = json.loads(array_expr)
        except json.JSONDecodeError as exc:
            raise ResonanceMarketDataError(
                code="constants_parse_failed",
                message="Failed to parse city string array.",
                detail={"cause": str(exc)},
            ) from exc
        if not isinstance(loaded, list):
            raise ResonanceMarketDataError(
                code="constants_parse_failed",
                message="Parsed city constants payload must be a list.",
            )
        city_names: List[str] = []
        for item in loaded:
            name = str(item).strip()
            if not name:
                continue
            city_names.append(name)
        return city_names

    @staticmethod
    def _parse_fatigue_array_expression(array_expr: str) -> List[Dict[str, Any]]:
        edge_pattern = re.compile(
            r'\{\s*cities:\s*\[\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\]\s*,\s*fatigue:\s*([0-9]+)\s*\}'
        )
        edges: List[Dict[str, Any]] = []
        for match in edge_pattern.finditer(array_expr):
            a_name = str(match.group(1)).strip()
            b_name = str(match.group(2)).strip()
            fatigue = int(match.group(3))
            if not a_name or not b_name:
                continue
            edges.append({"cities": [a_name, b_name], "fatigue": fatigue})
        if not edges:
            raise ResonanceMarketDataError(
                code="constants_parse_failed",
                message="Failed to parse city fatigue edges from constants payload.",
            )
        return edges

    def _build_travel_fatigue_payload(
        self,
        city_map: Dict[str, str],
        fatigue_edges: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        name_to_city_id = {name: city_id for city_id, name in city_map.items()}
        costs: Dict[str, Dict[str, Optional[int]]] = {
            city_id: {other_id: (0 if city_id == other_id else None) for other_id in city_map.keys()}
            for city_id in city_map.keys()
        }

        for edge in fatigue_edges:
            pair = edge.get("cities") or []
            if not isinstance(pair, list) or len(pair) != 2:
                continue
            city_a_name = str(pair[0]).strip()
            city_b_name = str(pair[1]).strip()
            if city_a_name not in name_to_city_id or city_b_name not in name_to_city_id:
                continue
            city_a = name_to_city_id[city_a_name]
            city_b = name_to_city_id[city_b_name]
            value = int(edge.get("fatigue") or 0)
            costs[city_a][city_b] = value
            costs[city_b][city_a] = value

        for city_a, row in costs.items():
            for city_b, value in row.items():
                if value is None:
                    raise ResonanceMarketDataError(
                        code="constants_parse_failed",
                        message=f"Missing fatigue edge for '{city_a}->{city_b}'.",
                    )

        normalized_costs: Dict[str, Dict[str, int]] = {
            city_a: {city_b: int(row[city_b]) for city_b in city_map.keys()}
            for city_a, row in costs.items()
        }
        return {
            "schema_version": self.TRAVEL_FATIGUE_SCHEMA_VERSION,
            "cities": {city_id: city_map[city_id] for city_id in city_map.keys()},
            "costs": normalized_costs,
        }

    @staticmethod
    def _write_travel_fatigue_csv(payload: Dict[str, Any], target: Path) -> None:
        cities = payload.get("cities") or {}
        costs = payload.get("costs") or {}
        if not isinstance(cities, dict) or not isinstance(costs, dict):
            raise ResonanceMarketDataError(
                code="travel_fatigue_invalid",
                message="Cannot export CSV from invalid travel fatigue payload.",
            )
        city_ids = sorted(cities.keys(), key=lambda item: (0, int(item)) if str(item).isdigit() else (1, str(item)))
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["from_city_id", "from_city_name", "to_city_id", "to_city_name", "fatigue"],
            )
            writer.writeheader()
            for idx, city_a in enumerate(city_ids):
                for city_b in city_ids[idx + 1 :]:
                    writer.writerow(
                        {
                            "from_city_id": city_a,
                            "from_city_name": str(cities.get(city_a, "")),
                            "to_city_id": city_b,
                            "to_city_name": str(cities.get(city_b, "")),
                            "fatigue": int((costs.get(city_a) or {}).get(city_b, 0)),
                        }
                    )

    def persist(self, snapshot: Dict[str, Any], force: bool = False) -> Dict[str, Any]:
        with self._lock:
            index_entries = self._load_index_entries()
            payload_hash = str(snapshot.get("raw_payload_hash") or "").strip()
            now_epoch = int(time.time())

            if not force and payload_hash:
                for entry in index_entries:
                    if str(entry.get("raw_payload_hash") or "") != payload_hash:
                        continue
                    created_epoch = int(entry.get("created_at_epoch") or 0)
                    if now_epoch - created_epoch > self.dedupe_window_seconds:
                        continue
                    existing_id = str(entry.get("snapshot_id") or "").strip()
                    if not existing_id:
                        continue
                    existing = self._load_snapshot_by_id(existing_id)
                    if existing is None:
                        continue
                    deduped = copy.deepcopy(existing)
                    deduped["deduped"] = True
                    return deduped

            snapshot_to_save = copy.deepcopy(snapshot)
            snapshot_id = self._ensure_unique_snapshot_id(str(snapshot_to_save.get("snapshot_id") or ""))
            snapshot_to_save["snapshot_id"] = snapshot_id

            snapshot_file = self.snapshots_dir / f"{snapshot_id}.json"
            self._write_json(snapshot_file, snapshot_to_save)

            index_entry = {
                "snapshot_id": snapshot_id,
                "fetched_at": snapshot_to_save.get("fetched_at"),
                "raw_payload_hash": payload_hash,
                "product_count": len(snapshot_to_save.get("products") or {}),
                "city_count": len(snapshot_to_save.get("cities") or {}),
                "created_at_epoch": now_epoch,
            }

            merged_index = [index_entry]
            for existing in index_entries:
                if existing.get("snapshot_id") == snapshot_id:
                    continue
                merged_index.append(existing)

            self._write_json(self.index_file, merged_index)
            self._write_json(
                self.latest_file,
                {
                    "snapshot_id": snapshot_id,
                    "fetched_at": snapshot_to_save.get("fetched_at"),
                    "raw_payload_hash": payload_hash,
                    "product_count": index_entry["product_count"],
                    "city_count": index_entry["city_count"],
                },
            )

            snapshot_to_save["deduped"] = False
            return snapshot_to_save

    def load_latest(self) -> Dict[str, Any]:
        with self._lock:
            latest_ptr = self._read_json(self.latest_file) if self.latest_file.is_file() else {}
            snapshot_id = str(latest_ptr.get("snapshot_id") or "").strip() if isinstance(latest_ptr, dict) else ""
            if snapshot_id:
                latest_snapshot = self._load_snapshot_by_id(snapshot_id)
                if latest_snapshot is not None:
                    return latest_snapshot

            index_entries = self._load_index_entries()
            for entry in index_entries:
                sid = str(entry.get("snapshot_id") or "").strip()
                if not sid:
                    continue
                latest_snapshot = self._load_snapshot_by_id(sid)
                if latest_snapshot is not None:
                    return latest_snapshot

            raise ResonanceMarketDataError(
                code="snapshot_not_found",
                message="No cached market snapshot is available.",
            )

    def _normalize_market_side(self, raw_side: Any, city_accumulator: set[str]) -> Dict[str, Dict[str, Any]]:
        if not isinstance(raw_side, dict):
            return {}

        normalized: Dict[str, Dict[str, Any]] = {}
        for raw_city_id in self._sorted_ids(raw_side.keys()):
            city_id = str(raw_city_id)
            city_accumulator.add(city_id)
            quote = raw_side.get(raw_city_id, {})
            if not isinstance(quote, dict):
                quote = {}

            trend_raw = quote.get("t")
            trend = "up" if trend_raw == 1 else "down"
            variation = quote.get("v")
            price = quote.get("p")
            time_epoch = quote.get("ti")
            normalized_epoch = self._coerce_int(time_epoch)

            normalized[city_id] = {
                "price": self._coerce_number(price),
                "variation": self._coerce_number(variation),
                "trend": trend,
                "time_epoch": normalized_epoch,
                "time_iso": self._epoch_to_iso(normalized_epoch),
            }
        return normalized

    def _load_name_mapping(self, file_path: Path) -> Dict[str, str]:
        if not file_path.is_file():
            return {}
        try:
            payload = self._read_json(file_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load mapping file '%s': %s", file_path, exc)
            return {}

        if not isinstance(payload, dict):
            return {}
        normalized: Dict[str, str] = {}
        for key, value in payload.items():
            k = str(key).strip()
            if not k:
                continue
            normalized[k] = str(value)
        return normalized

    def _load_alias_mapping(self, file_path: Path) -> Dict[str, str]:
        if not file_path.is_file():
            return {}
        try:
            payload = self._read_json(file_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load alias mapping file '%s': %s", file_path, exc)
            return {}
        if not isinstance(payload, dict):
            return {}
        normalized: Dict[str, str] = {}
        for raw_source, raw_target in payload.items():
            source_name = str(raw_source).strip()
            target_name = str(raw_target).strip()
            if not source_name or not target_name:
                continue
            normalized[source_name] = target_name
        return normalized

    def _extract_city_product_buy_lot(self, payload: Dict[str, Any]) -> Dict[str, Dict[str, int]]:
        city_product_buy_lot = payload.get("city_product_buy_lot")
        if not isinstance(city_product_buy_lot, dict):
            raise ResonanceMarketDataError(
                code="buy_lot_invalid",
                message="buy_lot payload must include object field city_product_buy_lot.",
            )
        normalized: Dict[str, Dict[str, int]] = {}
        for city_id, products in city_product_buy_lot.items():
            if not isinstance(products, dict):
                continue
            ckey = str(city_id).strip()
            if not ckey:
                continue
            row: Dict[str, int] = {}
            for product_id, lot in products.items():
                pkey = str(product_id).strip()
                if not pkey:
                    continue
                try:
                    value = int(lot)
                except (TypeError, ValueError) as exc:
                    raise ResonanceMarketDataError(
                        code="buy_lot_invalid",
                        message=f"buy_lot '{ckey}->{pkey}' must be integer.",
                    ) from exc
                if value < 0:
                    raise ResonanceMarketDataError(
                        code="buy_lot_invalid",
                        message=f"buy_lot '{ckey}->{pkey}' must be >= 0.",
                    )
                row[pkey] = value
            normalized[ckey] = row
        return normalized

    def _http_get_text(self, url: str) -> str:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Aura-Resonance/1.0 (+https://www.resonance-columba.com)"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as response:
                return response.read().decode("utf-8")
        except urllib.error.URLError as exc:
            raise ResonanceMarketDataError(
                code="market_fetch_failed",
                message="Failed to fetch remote text payload.",
                detail={"cause": str(exc), "url": url},
            ) from exc

    def _extract_route_chunk_urls(self, route_html: str) -> List[str]:
        matches = re.findall(r'src="(/_next/static/chunks/[^"]+\.js)"', route_html)
        urls: List[str] = []
        for path in matches:
            url = f"{self.ROUTE_HOST}{path}"
            if url not in urls:
                urls.append(url)
        return urls

    @staticmethod
    def _extract_product_buy_lot_pairs(chunk_text: str) -> Dict[str, Dict[str, int]]:
        if "buyLot" not in chunk_text:
            return {}

        pairs: Dict[str, Dict[str, int]] = {}
        for match in re.finditer(r'\{name:"([^"]+?)",.*?buyLot:\{([^{}]*)\}', chunk_text, re.DOTALL):
            product_name = str(match.group(1)).strip()
            if not product_name:
                continue
            lot_block = str(match.group(2) or "")
            city_lots: Dict[str, int] = {}
            for city_name, lot_text in re.findall(r"([^,:{}]+):([0-9]+)", lot_block):
                cname = str(city_name).strip()
                if not cname:
                    continue
                city_lots[cname] = int(lot_text)
            pairs[product_name] = city_lots
        return pairs

    def _load_travel_fatigue_data(self) -> Dict[str, Any]:
        with self._lock:
            if self._travel_fatigue_data is not None:
                return self._travel_fatigue_data

            if not self.travel_fatigue_file.is_file():
                raise ResonanceMarketDataError(
                    code="travel_fatigue_missing",
                    message=f"Travel fatigue file not found: {self.travel_fatigue_file}",
                )
            payload = self._read_json(self.travel_fatigue_file)
            validated = self._validate_travel_fatigue_payload(payload)
            self._travel_fatigue_data = validated
            return validated

    def _validate_travel_fatigue_payload(self, payload: Any) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ResonanceMarketDataError(
                code="travel_fatigue_invalid",
                message="Travel fatigue payload must be an object.",
            )

        schema_version = str(payload.get("schema_version") or "").strip()
        if not schema_version:
            raise ResonanceMarketDataError(
                code="travel_fatigue_invalid",
                message="Travel fatigue payload is missing schema_version.",
            )
        if schema_version != self.TRAVEL_FATIGUE_SCHEMA_VERSION:
            raise ResonanceMarketDataError(
                code="travel_fatigue_invalid",
                message=(
                    f"Unsupported travel fatigue schema_version '{schema_version}', "
                    f"expected '{self.TRAVEL_FATIGUE_SCHEMA_VERSION}'."
                ),
            )

        cities = payload.get("cities")
        costs = payload.get("costs")
        if not isinstance(cities, dict) or not isinstance(costs, dict):
            raise ResonanceMarketDataError(
                code="travel_fatigue_invalid",
                message="Travel fatigue payload must include object fields cities and costs.",
            )

        city_ids = self._sorted_ids(cities.keys())
        if not city_ids:
            raise ResonanceMarketDataError(
                code="travel_fatigue_invalid",
                message="Travel fatigue payload has no cities.",
            )

        for cid in city_ids:
            if cid not in self.city_name_map:
                raise ResonanceMarketDataError(
                    code="travel_fatigue_invalid",
                    message=f"City id '{cid}' is not defined in cities.json.",
                )

        for from_id in city_ids:
            row = costs.get(from_id)
            if not isinstance(row, dict):
                raise ResonanceMarketDataError(
                    code="travel_fatigue_invalid",
                    message=f"Travel fatigue row '{from_id}' is missing or invalid.",
                )

            row_keys = self._sorted_ids(row.keys())
            if row_keys != city_ids:
                raise ResonanceMarketDataError(
                    code="travel_fatigue_invalid",
                    message=f"Travel fatigue row '{from_id}' does not cover all cities.",
                )

            for to_id in city_ids:
                value = row.get(to_id)
                if not isinstance(value, int):
                    raise ResonanceMarketDataError(
                        code="travel_fatigue_invalid",
                        message=f"Travel fatigue value '{from_id}->{to_id}' must be integer.",
                    )
                if value < 0:
                    raise ResonanceMarketDataError(
                        code="travel_fatigue_invalid",
                        message=f"Travel fatigue value '{from_id}->{to_id}' must be >= 0.",
                    )
                if from_id == to_id and value != 0:
                    raise ResonanceMarketDataError(
                        code="travel_fatigue_invalid",
                        message=f"Travel fatigue diagonal '{from_id}->{to_id}' must be 0.",
                    )

        for from_id in city_ids:
            for to_id in city_ids:
                if costs[from_id][to_id] != costs[to_id][from_id]:
                    raise ResonanceMarketDataError(
                        code="travel_fatigue_invalid",
                        message=f"Travel fatigue matrix is not symmetric at '{from_id}<->{to_id}'.",
                    )

        normalized_cities = {cid: str(cities[cid]) for cid in city_ids}
        normalized_costs = {
            from_id: {to_id: int(costs[from_id][to_id]) for to_id in city_ids}
            for from_id in city_ids
        }
        return {
            "schema_version": schema_version,
            "cities": normalized_cities,
            "costs": normalized_costs,
        }

    def _resolve_city_name(self, city_id: str) -> str:
        return self.city_name_map.get(str(city_id), f"unknown_{city_id}")

    def _resolve_product_name(self, product_id: str) -> str:
        return self.product_name_map.get(str(product_id), f"unknown_{product_id}")

    def _ensure_unique_snapshot_id(self, proposed_id: str) -> str:
        snapshot_id = proposed_id.strip()
        if not snapshot_id:
            snapshot_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        candidate = snapshot_id
        counter = 1
        while (self.snapshots_dir / f"{candidate}.json").exists():
            candidate = f"{snapshot_id}_{counter}"
            counter += 1
        return candidate

    def _load_index_entries(self) -> List[Dict[str, Any]]:
        if not self.index_file.is_file():
            return []
        payload = self._read_json(self.index_file)
        if not isinstance(payload, list):
            return []
        entries: List[Dict[str, Any]] = []
        for row in payload:
            if isinstance(row, dict):
                entries.append(dict(row))
        return entries

    def _load_snapshot_by_id(self, snapshot_id: str) -> Optional[Dict[str, Any]]:
        snapshot_file = self.snapshots_dir / f"{snapshot_id}.json"
        if not snapshot_file.is_file():
            return None
        payload = self._read_json(snapshot_file)
        if not isinstance(payload, dict):
            return None
        return payload

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    @staticmethod
    def _read_json(path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _coerce_number(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _coerce_int(value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _epoch_to_iso(epoch: Optional[int]) -> Optional[str]:
        if epoch is None:
            return None
        try:
            return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OSError, OverflowError):
            return None

    @staticmethod
    def _payload_hash(payload: Dict[str, Any]) -> str:
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _sorted_ids(values: Any) -> List[str]:
        def sort_key(raw: Any):
            token = str(raw)
            if token.isdigit():
                return (0, int(token))
            return (1, token)

        return [str(item) for item in sorted([str(v) for v in values], key=sort_key)]
