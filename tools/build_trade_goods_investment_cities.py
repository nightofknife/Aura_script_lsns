"""Build city investment bounds from installed native exchange catalogs, offline.

The native sellList determines the set and count, not investment display order.
Runtime recognition must continue following screen order. This tool uses the
separate devtools decoder; generated JSON is the only runtime dependency.
"""
from __future__ import annotations

import argparse
import ast
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "plans/resonance_pc"
NATIVE_FILES = ("HomeStationFactory.bin", "HomeGoodsQuotationFactory.bin", "HomeGoodsFactory.bin")


def _literal_tables(path: Path) -> dict:
    """Read existing city aliases without importing plan runtime dependencies."""
    result = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id in ("_CITY_ALIAS_TO_KEY", "_CITY_KEY_DISPLAY_NAME"):
                result[node.target.id] = ast.literal_eval(node.value)
    if len(result) != 2:
        raise ValueError("City alias and display tables are missing")
    return result


def build(game_data: Path, devtools_src: Path, plan: Path = PLAN) -> dict:
    sys.path.insert(0, str(devtools_src.resolve()))
    from aura_resonance_devtools.full_config_export import decode_binary_config

    native_root = game_data / "Patch/BinaryConfig"

    def records(filename):
        result = []
        for row in decode_binary_config(native_root / filename)["records"]:
            fields = {field["name"]: field.get("value") for field in row["fields"]}
            if not fields.get("isInformalData"):
                result.append(fields)
        return result

    stations = records(NATIVE_FILES[0])
    quotations = {row["id"]: row for row in records(NATIVE_FILES[1])}
    goods = {row["id"]: row for row in records(NATIVE_FILES[2])}
    cities_path = plan / "data/meta/cities.json"
    products_path = plan / "data/meta/products.json"
    alias_path = plan / "src/services/city_shop_data_pc_service.py"
    cities = json.loads(cities_path.read_text(encoding="utf-8"))
    products = json.loads(products_path.read_text(encoding="utf-8"))
    products_by_name = {name: pid for pid, name in products.items()}
    tables = _literal_tables(alias_path)
    alias_to_key = tables["_CITY_ALIAS_TO_KEY"]
    key_display = tables["_CITY_KEY_DISPLAY_NAME"]
    result = {
        "schema_version": 1,
        "source": {
            "kind": "native_game_config",
            "mapping": "HomeStationFactory.sellList -> HomeGoodsQuotationFactory.goodsId -> HomeGoodsFactory",
            "product_order": "native_sell_list_order; runtime uses screen order",
            "sha256": {f"Patch/BinaryConfig/{name}": sha256((native_root / name).read_bytes()).hexdigest()
                       for name in NATIVE_FILES},
            "plan_sha256": {path.relative_to(plan).as_posix(): sha256(path.read_bytes()).hexdigest()
                            for path in (cities_path, products_path, alias_path)},
        },
        "cities": {},
    }
    for city_id, name in cities.items():
        city_key = alias_to_key.get(name)
        if city_key is None or city_key not in key_display:
            raise ValueError(f"City '{name}' has no existing canonical key")
        # Prefer exact native name, then its existing canonical display name.
        exact = [row for row in stations if row.get("name") == name]
        matches = exact or [row for row in stations if row.get("name") == key_display[city_key]]
        if len(matches) != 1:
            raise ValueError(f"City '{name}' has {len(matches)} native station matches; catalog is unknown")
        station = matches[0]
        sell_list = station.get("sellList")
        if not isinstance(sell_list, list):
            raise ValueError(f"Native sellList is missing for '{name}'; catalog is unknown")
        entries, seen_goods = [], set()
        for raw in sell_list:
            quotation_id = next(field["value"] for field in raw if field["name"] == "id")
            quote = quotations[quotation_id]
            item = goods[quote["goodsId"]]
            if item["id"] in seen_goods:
                raise ValueError(f"Duplicate native goods '{item['id']}' in '{name}'")
            seen_goods.add(item["id"])
            entry = {"name": item["name"], "native_goods_id": item["id"],
                     "native_quotation_id": quotation_id}
            if item["name"] in products_by_name:
                entry["product_id"] = products_by_name[item["name"]]
            entries.append(entry)
        aliases = list(dict.fromkeys([name, city_key, key_display[city_key], station["name"],
                                     *(alias for alias, key in alias_to_key.items() if key == city_key)]))
        result["cities"][city_id] = {"name": name, "aliases": aliases,
                                    "native_station_id": station["id"],
                                    "status": "known" if entries else "empty",
                                    "products": entries}
    output = plan / "data/meta/trade_goods_investment_cities.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-data", type=Path, required=True)
    parser.add_argument("--devtools-src", type=Path, default=ROOT.parent / "Aura_script_lsns_devtools/src")
    parser.add_argument("--plan", type=Path, default=PLAN)
    args = parser.parse_args()
    result = build(args.game_data.resolve(), args.devtools_src, args.plan.resolve())
    print(json.dumps({"cities": len(result["cities"]),
                      "counts": {key: len(row["products"]) for key, row in result["cities"].items()}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
