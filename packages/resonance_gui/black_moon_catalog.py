"""Read-only GUI catalog; never import game actions or recognition services."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from .paths import resolve_application_root


CATEGORY_NAMES = {
    "equipment": "装备", "equipment_box": "装备箱", "item": "道具", "material": "材料",
}
QUALITY_NAMES = {"Golden": "金色", "Purple": "紫色", "Blue": "蓝色", "White": "白色", "Orange": "橙色"}
EXECUTION_KEYS = (
    "auto_black_moon_local_purchase", "black_moon_purchase_table", "black_moon_purchase_record_scope",
)


class BlackMoonCatalogError(ValueError):
    """The authoritative catalog is unavailable or invalid."""


@dataclass(frozen=True)
class BlackMoonProduct:
    item_id: str
    name: str
    category: str
    quality: str
    icon_path: Path | None = None


@dataclass(frozen=True)
class BlackMoonCatalog:
    products: tuple[BlackMoonProduct, ...]

    @property
    def item_ids(self) -> tuple[str, ...]:
        return tuple(product.item_id for product in self.products)


def load_black_moon_catalog(root: Path | None = None, *, include_icons: bool = True) -> BlackMoonCatalog:
    root = Path(root) if root is not None else resolve_application_root()
    plan = root / "plans/resonance_pc"
    path = plan / "data/meta/black_moon_local_shop_catalog.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        rows = raw["products"]
        if not isinstance(rows, list) or not rows:
            raise ValueError("商品目录为空")
        products = []
        seen: set[str] = set()
        for row in rows:
            item_id, name, category, quality = (row[key] for key in ("item_id", "name", "category", "quality"))
            if (not all(isinstance(value, str) and value.strip() for value in (item_id, name, category, quality))
                    or item_id in seen or category not in CATEGORY_NAMES):
                raise ValueError("商品 ID 重复或商品字段无效")
            seen.add(item_id)
            products.append(BlackMoonProduct(item_id, name, category, quality))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BlackMoonCatalogError(f"黑月商品目录不可用：{exc}") from exc

    if not include_icons:
        return BlackMoonCatalog(tuple(products))

    # Icons are optional. Resource names in the catalog are not HTTP URLs.
    bank = plan / "templates/commerce/shop/black_moon/calibrated"
    icons: dict[str, Path] = {}
    try:
        manifest = json.loads((bank / "manifest.json").read_text(encoding="utf-8"))
        for row in manifest["items"]:
            candidate = (bank / row["file"]).resolve()
            if candidate.is_relative_to(bank.resolve()) and candidate.is_file():
                icons[str(row["item_id"])] = candidate
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return BlackMoonCatalog(tuple(
        BlackMoonProduct(product.item_id, product.name, product.category, product.quality, icons.get(product.item_id))
        for product in products
    ))


def normalize_purchase_table(values: Any, catalog: BlackMoonCatalog | None = None) -> dict[str, bool]:
    """Fill missing known IDs with false; never infer selection from old lists."""
    if not isinstance(values, Mapping):
        raise ValueError("黑月购买表必须为商品 ID 到布尔值的映射")
    if any(not isinstance(key, str) or not key or type(value) is not bool for key, value in values.items()):
        raise ValueError("黑月购买表的商品 ID 必须为字符串，选择值必须为布尔值")
    if catalog is None:
        # Preserve settings when assets are absent, rather than resetting unrelated freight inputs.
        return dict(values)
    unknown = set(values) - set(catalog.item_ids)
    if unknown:
        raise ValueError(f"黑月购买表包含未知商品：{', '.join(sorted(unknown))}")
    return {item_id: values.get(item_id, False) for item_id in catalog.item_ids}


def normalize_black_moon_settings(values: Mapping[str, Any], *, require_catalog: bool = False) -> dict[str, Any]:
    enabled = values.get("auto_black_moon_local_purchase", False)
    if type(enabled) is not bool:
        raise ValueError("自动黑月本地采购必须为布尔值")
    scope = values.get("black_moon_purchase_record_scope", "default")
    if not isinstance(scope, str) or not scope or scope != scope.strip() or len(scope) > 128:
        raise ValueError("黑月采购记录范围必须为非空字符串，不含首尾空格，且不超过 128 个字符")
    try:
        catalog = load_black_moon_catalog(include_icons=False)
    except BlackMoonCatalogError:
        if enabled and require_catalog:
            raise
        catalog = None
    return {
        "auto_black_moon_local_purchase": enabled,
        "black_moon_purchase_table": normalize_purchase_table(values.get("black_moon_purchase_table", {}), catalog),
        "black_moon_purchase_record_scope": scope,
    }
