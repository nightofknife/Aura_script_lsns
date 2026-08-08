"""Presentation catalog for city-grouped Resonance PC unlockable products."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
from typing import Any

from .config_repository import PC_TRADE_CITY_OPTIONS


@dataclass(frozen=True)
class TradeProduct:
    product_id: str
    name: str


@dataclass(frozen=True)
class TradeProductGroup:
    city_id: str
    city_name: str
    products: tuple[TradeProduct, ...]


def _meta_candidates(filename: str) -> list[Path]:
    relative = Path("plans") / "resonance_pc" / "data" / "meta" / filename
    candidates: list[Path] = []
    base_path = str(os.environ.get("AURA_BASE_PATH") or "").strip()
    if base_path:
        candidates.append(Path(base_path) / relative)
    candidates.append(Path.cwd() / relative)
    candidates.append(Path(__file__).resolve().parents[2] / relative)
    executable = Path(sys.executable).resolve()
    candidates.append(executable.parent.parent / relative)
    return list(dict.fromkeys(path.resolve() for path in candidates))


def trade_meta_path(filename: str) -> Path:
    for path in _meta_candidates(filename):
        if path.is_file():
            return path
    searched = "\n".join(str(path) for path in _meta_candidates(filename))
    raise FileNotFoundError(f"找不到跑商元数据 {filename}，已搜索：\n{searched}")


def load_trade_product_groups() -> tuple[TradeProductGroup, ...]:
    product_payload = _load_json_object(trade_meta_path("products.json"))
    unlock_payload = _load_json_object(trade_meta_path("product_unlocks.json"))
    city_unlocks = unlock_payload.get("city_product_unlocks")
    if not isinstance(city_unlocks, dict):
        raise ValueError("product_unlocks.city_product_unlocks 必须是字典")

    groups: list[TradeProductGroup] = []
    for city_id, city_name in PC_TRADE_CITY_OPTIONS:
        if city_id not in city_unlocks:
            continue
        raw_products = city_unlocks.get(city_id) or []
        if not isinstance(raw_products, list):
            raise ValueError(f"product_unlocks city '{city_id}' 必须是列表")
        products: list[TradeProduct] = []
        for product_id in (str(value) for value in raw_products):
            product_name = str(product_payload.get(product_id) or "").strip()
            if not product_name:
                raise ValueError(f"products.json 缺少商品 ID '{product_id}'")
            products.append(TradeProduct(product_id=product_id, name=product_name))
        groups.append(
            TradeProductGroup(
                city_id=city_id,
                city_name=city_name,
                products=tuple(products),
            )
        )
    return tuple(groups)


def trade_product_ids(groups: tuple[TradeProductGroup, ...]) -> tuple[str, ...]:
    values = {
        product.product_id
        for group in groups
        for product in group.products
    }
    return tuple(sorted(values, key=_numeric_sort_key))


def _load_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} 顶层必须是字典")
    return payload


def _numeric_sort_key(value: str) -> tuple[int, str]:
    text = str(value)
    return (int(text), text) if text.isdigit() else (2**31 - 1, text)
