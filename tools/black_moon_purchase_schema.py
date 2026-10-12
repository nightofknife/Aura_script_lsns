"""Build the declared Black Moon input map from the authoritative item catalog."""

from __future__ import annotations

import json
from pathlib import Path

import yaml


CATALOG_PATH = (
    Path(__file__).resolve().parents[1]
    / "plans/resonance_pc/data/meta/black_moon_local_shop_catalog.json"
)


def purchase_table_schema(
    name: str = "purchase_table", *, required: bool = False,
) -> dict:
    products = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))["products"]
    item_ids = [product["item_id"] for product in products]
    if len(item_ids) != len(set(item_ids)):
        raise ValueError("Black Moon catalog contains duplicate item IDs")
    schema = {
        "name": name,
        "type": "dict",
        "required": required,
        "properties": {
            item_id: {
                "name": item_id, "type": "boolean",
                "required": False, "default": False,
            }
            for item_id in item_ids
        },
    }
    if not required:
        schema["default"] = dict.fromkeys(item_ids, False)
    return schema


def render_purchase_table_input(
    name: str = "purchase_table", *, indent: int = 6,
    list_item: bool = True, required: bool = False,
) -> str:
    """Render one YAML input without rewriting any surrounding task content."""
    schema = purchase_table_schema(name, required=required)
    value = [schema] if list_item else {name: schema}
    rendered = yaml.safe_dump(value, sort_keys=False, width=1000).rstrip()
    prefix = " " * indent
    return "\n".join(prefix + line for line in rendered.splitlines())
