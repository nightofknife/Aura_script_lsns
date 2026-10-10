"""Static purchase selections; no GUI, live config decoding, or import-time IO."""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path


CATALOG_PATH = Path(__file__).resolve().parents[2] / "data/meta/black_moon_local_shop_catalog.json"


@lru_cache(maxsize=1)
def _catalog() -> dict:
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or data.get("reference_client") != [1280, 720]:
        raise ValueError("Unsupported Black Moon catalogue")
    products, cities = data.get("products"), data.get("eligible_cities")
    if not isinstance(products, list) or not products or not isinstance(cities, list) or not cities:
        raise ValueError("Black Moon catalogue has no products or eligible cities")
    identifiers = [row["item_id"] for row in products]
    if any(not isinstance(value, str) or not value.strip() for value in identifiers + cities):
        raise ValueError("Black Moon catalogue contains invalid identifiers")
    if len(set(identifiers)) != len(identifiers) or len(set(cities)) != len(cities):
        raise ValueError("Black Moon catalogue identifiers must be unique")
    if not isinstance(data.get("refresh_policy"), dict):
        raise ValueError("Black Moon catalogue has no weekly refresh policy")
    return data


def load_black_moon_catalog() -> dict:
    return deepcopy(_catalog())


def normalize_selected_items(value) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) or item != item.strip() for item in value):
        raise ValueError("Black Moon selections must be a list of canonical item IDs")
    known = {row["item_id"] for row in _catalog()["products"]}
    unknown = set(value) - known
    if unknown:
        raise ValueError(f"Unknown Black Moon item IDs: {sorted(unknown)}")
    return list(dict.fromkeys(value))


def normalize_record_scope(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 128:
        raise ValueError("Black Moon record scope must be a nonempty, unpadded string of at most 128 characters")
    return value
