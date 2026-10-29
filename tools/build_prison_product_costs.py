"""Build base direct production costs from native recipes, without game input.

External ingredients use the native normal price, as requested. Operating
overhead, bonus output and historical batch costs remain explicitly unknown.
"""
from __future__ import annotations

import argparse
from fractions import Fraction
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "plans/resonance_pc"
CURRENCY_ID = 11400001


def normalize_fields(item):
    if isinstance(item, list):
        if item and all(isinstance(row, dict) and "name" in row and "value" in row for row in item):
            return {row["name"]: normalize_fields(row["value"]) for row in item}
        return [normalize_fields(row) for row in item]
    return item


def estimate_costs(recipes, native_goods, product_names):
    by_product = {row["product"]: row for row in recipes}
    if len(by_product) != len(recipes):
        raise ValueError("Multiple recipes for one product need an explicit selection policy")
    name_ids = {}
    for identity, name in product_names.items():
        name_ids.setdefault(name, []).append(identity)
    external = {}
    memo = {}

    def unit_cost(identity, visiting=()):
        if identity == CURRENCY_ID:
            return Fraction(1)
        if identity in visiting:
            raise ValueError("Cyclic production recipe")
        if identity in memo:
            return memo[identity]
        recipe = by_product.get(identity)
        if recipe is None:
            item = native_goods.get(identity, {})
            price = item.get("price")
            if type(price) is not int or price <= 0:
                raise ValueError(f"Missing normal external ingredient price: {identity}")
            external[str(identity)] = {"goods": item["name"], "unit_price": price,
                                       "basis": "native_normal_price_excluding_tax"}
            return Fraction(price)
        count = recipe["perNum"]
        if type(count) is not int or count <= 0 or not recipe.get("costList"):
            raise ValueError("Invalid production yield or empty material costs")
        total = Fraction(0)
        for row in recipe["costList"]:
            if type(row["num"]) is not int or row["num"] <= 0:
                raise ValueError("Invalid recipe input quantity")
            total += row["num"] * unit_cost(row["id"], (*visiting, identity))
        memo[identity] = total / count
        return memo[identity]

    products = {}
    for recipe in recipes:
        identity = recipe["product"]
        name = native_goods[identity]["name"]
        ids = name_ids.get(name, [])
        if len(ids) != 1:
            raise ValueError(f"Ambiguous or missing runtime product identity: {name}")
        cost = unit_cost(identity)
        products[ids[0]] = {
            "name": name, "native_goods_id": identity, "recipe_id": recipe["id"],
            "recipe_name": recipe["name"], "base_output": recipe["perNum"],
            "period_minutes": recipe["perTime"],
            "unit_direct_cost": (2 * cost.numerator + cost.denominator) // (2 * cost.denominator),
            "unit_direct_cost_exact": str(cost),
            "inputs": [{"native_goods_id": row["id"],
                        "name": "铁盟币" if row["id"] == CURRENCY_ID else native_goods[row["id"]]["name"],
                        "quantity": row["num"], "unit_direct_cost_exact": str(unit_cost(row["id"]))}
                       for row in recipe["costList"]],
        }
    return products, external


def build(game_data, devtools_src):
    sys.path.insert(0, str(devtools_src.resolve()))
    from aura_resonance_devtools.full_config_export import decode_binary_config

    binary = game_data / "Patch/BinaryConfig"
    hashes = {}

    def records(name):
        path = binary / (name + ".bin")
        hashes["Patch/BinaryConfig/" + path.name] = sha256(path.read_bytes()).hexdigest()
        return [{f["name"]: normalize_fields(f.get("value")) for f in row["fields"]}
                for row in decode_binary_config(path)["records"]]

    recipes = [row for row in records("ProductionFactory") if row.get("mod") == "监狱配方"
               and not row.get("isInformalData") and row.get("product") and row.get("perNum")]
    goods = {row["id"]: row for row in records("HomeGoodsFactory")}
    names = json.loads((PLAN / "data/meta/products.json").read_text(encoding="utf-8"))
    products, external = estimate_costs(recipes, goods, names)
    return {"schema_version": 1, "model": "native_recipe_base_direct_cost",
            "currency": "iron_alliance_coin", "source_sha256": hashes,
            "assumptions": {
                "unit_cost_rounding": "nearest_half_up_after_exact_material_recursion",
                "material_source": "prison_production_when_recipe_exists_otherwise_native_normal_price",
                "external_purchase_tax_included": False, "bonus_output_included": False,
                "operating_overhead_included": False, "operating_overhead": None,
                "actual_historical_batch_cost": None, "game_inventory_tax_basis": None,
                "fixed_assets_and_unlocks_amortized": False,
            }, "external_ingredients": external, "products": products}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-data", type=Path, default=Path("D:/game/Resonance/雷索纳斯_Data"))
    parser.add_argument("--devtools-src", type=Path, default=ROOT.parent / "Aura_script_lsns_devtools/src")
    parser.add_argument("--output", type=Path, default=PLAN / "data/meta/prison_product_costs_pc.json")
    args = parser.parse_args()
    result = build(args.game_data, args.devtools_src)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Estimated {len(result['products'])} products; {len(result['external_ingredients'])} external ingredients")


if __name__ == "__main__":
    main()
