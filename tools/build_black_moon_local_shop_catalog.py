"""Generate the static Black Moon local-shop catalog from native BinaryConfig.

Build-time decoding uses aura_resonance_devtools from --devtools-src. No game,
asset bundles, fonts, screenshots, or research-directory exports are required.
Only --output is written; runtime consumers need only that JSON file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
META_ROOT = REPO_ROOT / "plans/resonance_pc/data/meta"
EQUIPMENT_BOX_POOL = 80301264
PUBLIC_REFRESH_SOURCES = [
    {
        "url": "https://www.taptap.cn/moment/720046623463311231",
        "claim": "Black Moon local shops refresh Monday at 05:00.",
        "kind": "community_guide",
        "provenance": "public_community_corroboration",
    },
    {
        "url": "https://www.taptap.cn/moment/711009545115144144",
        "claim": "Local shops refresh Monday at 05:00.",
        "kind": "community_guide",
        "provenance": "public_community_corroboration",
    },
]


def decode_fields(raw: bytes, strings: dict[int, str], wanted=None) -> dict:
    """Decode framed fields, including the nested record-list wire format."""
    result = {}
    cursor = 0
    while cursor < len(raw):
        if cursor + 8 > len(raw):
            raise ValueError("Truncated native field header")
        size, key_offset = struct.unpack_from("<II", raw, cursor)
        end = cursor + 4 + size
        if size < 5 or end > len(raw):
            raise ValueError("Invalid native field bounds")
        key = strings[key_offset]
        value = raw[cursor + 8:end]
        cursor = end
        if wanted is not None and key not in wanted:
            continue
        tag = value[0]
        if tag in (4, 5, 9, 11, 12, 13, 14):
            reference = struct.unpack_from("<I", value, 1)[0]
            decoded = None if reference == 0xFFFFFFFF else strings[reference]
        elif tag in (0, 6):
            decoded = struct.unpack_from("<i", value, 1)[0]
        elif tag == 1:
            decoded = struct.unpack_from("<d", value, 1)[0]
        elif tag == 2:
            decoded = struct.unpack_from("<q", value, 1)[0]
        elif tag == 3:
            decoded = bool(value[1])
        elif tag == 7:
            count = struct.unpack_from("<I", value, 6)[0]
            position = 10
            decoded = []
            for _ in range(count):
                if position + 8 > len(value):
                    raise ValueError("Truncated native list element")
                length, field_count = struct.unpack_from("<II", value, position)
                element_end = position + 4 + length
                if length < 4 or element_end > len(value):
                    raise ValueError("Invalid native list bounds")
                element = decode_fields(value[position + 8:element_end], strings)
                if len(element) != field_count:
                    raise ValueError("Native list field count mismatch")
                decoded.append(element)
                position = element_end
            if position != len(value):
                raise ValueError("Trailing native list data")
        else:
            raise ValueError(f"Unsupported native tag {tag} in {key}")
        if key in result:
            raise ValueError(f"Duplicate native field {key}")
        result[key] = decoded
    return result


class NativeConfigs:
    def __init__(self, root: Path, devtools_src: Path):
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(devtools_src.resolve()))
        from aura_resonance_devtools.binary_config import PooledBinaryConfig
        from aura_resonance_devtools.role_catalog import IndexedConfigReader

        self.root = root
        self.config_type = PooledBinaryConfig
        self.reader_type = IndexedConfigReader
        self.sources = {}

    def load(self, name: str, fields: set[str]) -> dict[int, dict]:
        path = self.root / f"{name}.bin"
        config = self.config_type.load(path)
        reader = self.reader_type(config, path.name)
        if any(issue.severity == "error" for issue in reader.issues):
            raise ValueError(f"Cannot decode indexed records in {path.name}")
        self.sources[path.name] = {
            "sha256": hashlib.sha256(config.data).hexdigest(),
            "content_hash": config.content_hash,
        }
        return {
            record.record_id: decode_fields(
                config.data[record.start:record.end], config.strings, fields
            )
            for record in reader.records
        }


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def normalized_icon(resource_url: str) -> str:
    return "/".join(part for part in resource_url.replace("\\", "/").split("/") if part).casefold()


def canonical_records(meta_root: Path, sources: dict, box_source_ids: set[int],
                      required_source_ids: set[int]) -> dict[tuple[str, int], dict]:
    records = {}
    groups = {}
    aliases = {}
    required_visuals = {
        (kind, (source.get("name") or "").strip(), normalized_icon(source.get("iconPath") or ""), source.get("quality"))
        for kind, native_rows in sources.items() for source_id, source in native_rows.items()
        if source_id in required_source_ids
    }
    for filename, collection, id_field, kind, category in (
        ("inventory_items.json", "items", "item_id", "ItemFactory", "item"),
        ("inventory_materials.json", "materials", "material_id", "SourceMaterialFactory", "material"),
        ("inventory_equipment.json", "equipment", "equipment_id", "EquipmentFactory", "equipment"),
    ):
        for row in read_json(meta_root / filename)[collection]:
            source_ids = row.get("source_record_ids")
            if source_ids is None:
                source_ids = [row["source_record_id"]]
            source_ids = sorted(set(source_ids))
            native_rows = [sources[kind][source_id] for source_id in source_ids]
            quality = row.get("quality") or native_rows[0]["quality"]
            icon = native_rows[0]["iconPath"].replace("\\", "/")
            icon_key = normalized_icon(icon)
            names = {row["name"].strip()} | {native_row["name"].strip() for native_row in native_rows}
            # Unrelated inventory groups may drift independently of this shop.
            # Retain both directly referenced groups and exact alias anchors.
            if not required_source_ids.intersection(source_ids) and not any(
                    (kind, name, icon_key, quality) in required_visuals for name in names):
                continue
            product_category = category
            if kind == "ItemFactory" and box_source_ids.intersection(source_ids):
                product_category = "equipment_box"
            if not row["name"] or not icon_key or quality not in ("White", "Blue", "Purple", "Golden", "Orange"):
                raise ValueError(f"Incomplete canonical native metadata for {row[id_field]}")
            if any(native_row["quality"] != quality or normalized_icon(native_row["iconPath"]) != icon_key
                   for native_row in native_rows):
                raise ValueError(f"Canonical visual metadata disagrees for {row[id_field]}")
            group_key = (kind, product_category, row["name"].strip(), icon_key, quality)
            entry = {
                "item_id": row[id_field],
                "name": row["name"],
                "category": product_category,
                "source_kind": kind,
                "source_record_ids": set(source_ids),
                "quality": quality,
                "icon_resource_url": icon,
                "_metadata_source_ids": set(source_ids),
                "_canonical_item_ids": {row[id_field]},
            }
            if group_key in groups:
                entry = groups[group_key]
                entry["source_record_ids"].update(source_ids)
                entry["_metadata_source_ids"].update(source_ids)
                entry["_canonical_item_ids"].add(row[id_field])
            else:
                groups[group_key] = entry
            # Existing canonical names and their explicitly mapped native names
            # may differ; both anchor exact name/icon/quality alias resolution.
            for name in names:
                alias_key = (kind, product_category, name, icon_key, quality)
                previous = aliases.get(alias_key)
                if previous is not None and previous is not entry:
                    raise ValueError(f"Ambiguous canonical native alias for {row[id_field]}")
                aliases[alias_key] = entry
            for source_id in source_ids:
                source_key = (kind, source_id)
                if source_key in records and records[source_key] is not entry:
                    raise ValueError(f"Ambiguous canonical source ID {source_id}")
                records[source_key] = entry

    for kind, native_rows in sources.items():
        category = {"ItemFactory": "item", "SourceMaterialFactory": "material", "EquipmentFactory": "equipment"}[kind]
        for source_id, source in sorted(native_rows.items()):
            source_key = (kind, source_id)
            if source_key in records or source.get("isInformalData"):
                continue
            name = (source.get("name") or "").strip()
            icon_key = normalized_icon(source.get("iconPath") or "")
            quality = source.get("quality")
            if not name or not icon_key:
                continue
            product_category = category
            if kind == "ItemFactory":
                box_key = (kind, "equipment_box", name, icon_key, quality)
                if source_id in box_source_ids or box_key in aliases:
                    product_category = "equipment_box"
            entry = aliases.get((kind, product_category, name, icon_key, quality))
            if entry is not None:
                records[source_key] = entry
                entry["source_record_ids"].add(source_id)

    for entry in groups.values():
        entry["source_record_ids"] = sorted(entry["source_record_ids"])
    return records


def reachable_commodities(references, lists, commodities) -> set[int]:
    found = set()
    visited = set()
    active = set()

    def follow(record_id):
        if record_id in commodities:
            found.add(record_id)
            return
        if record_id in active:
            raise ValueError(f"Cyclic shop pool {record_id}")
        if record_id in visited:
            return
        pool = lists[record_id]
        if pool.get("isInformalData"):
            return
        active.add(record_id)
        for entry in pool["shopList"]:
            if entry.get("weight", 1) > 0:
                follow(entry["id"])
        active.remove(record_id)
        visited.add(record_id)

    for entry in references:
        if entry.get("weight", 1) > 0:
            follow(entry["id"])
    return found


def build_catalog(binary_config: Path, devtools_src: Path, meta_root: Path) -> dict:
    native = NativeConfigs(binary_config, devtools_src)
    stores = native.load("StoreFactory", {
        "showUI", "isInformalData", "shopList", "commodityFixedList",
        "specialShopList", "refreshType", "refreshTimeList", "maxShopNum",
    })
    stores = {rid: row for rid, row in stores.items()
              if row.get("showUI") == "Group_LocalStore" and not row.get("isInformalData")}
    if not stores:
        raise ValueError("No native Group_LocalStore records found")
    lists = native.load("ListFactory", {"shopList", "isInformalData"})
    commodities = native.load("CommodityFactory", {
        "purchase", "isInformalData", "commodityItemList", "commodityNum",
    })
    stations = native.load("HomeStationFactory", {"name", "barStoreList", "isInformalData"})
    sources = {
        kind: native.load(kind, {"name", "quality", "iconPath", "isInformalData"})
        for kind in ("ItemFactory", "SourceMaterialFactory", "EquipmentFactory")
    }
    city_keys = {row["city_name"]: row["city_key"]
                 for row in read_json(meta_root / "city_identity_templates.json")["cities"]}
    eligible_cities = set()
    city_links = []
    linked_stores = set()
    for station_id, station in sorted(stations.items()):
        if station.get("isInformalData"):
            continue
        matches = sorted({entry["id"] for entry in station.get("barStoreList", [])
                          if entry["id"] in stores})
        if matches:
            city_key = city_keys[station["name"]]
            eligible_cities.add(city_key)
            linked_stores.update(matches)
            city_links.append({"city_key": city_key, "station_id": station_id, "store_ids": matches})
    if linked_stores != set(stores):
        raise ValueError("Local stores cannot all be linked to canonical cities")

    references = []
    store_sources = []
    for store_id, store in sorted(stores.items()):
        if store["refreshType"] != "Weekly" or store["refreshTimeList"] != [{"refreshTime": "05:00:00"}]:
            raise ValueError(f"Unsupported local shop reset in store {store_id}")
        for field in ("commodityFixedList", "shopList", "specialShopList"):
            references.extend(store[field])
        store_sources.append({
            "store_id": store_id,
            "commodityFixedList": [entry["id"] for entry in store["commodityFixedList"]],
            "shopList_count": len(store["shopList"]),
            "specialShopList": [entry["id"] for entry in store["specialShopList"]],
            "maxShopNum": store["maxShopNum"],
        })
    candidates = reachable_commodities(references, lists, commodities)
    box_candidates = reachable_commodities([{"id": EQUIPMENT_BOX_POOL}], lists, commodities)
    box_source_ids = {entry["id"] for commodity_id in box_candidates
                      for entry in commodities[commodity_id]["commodityItemList"]}
    required_source_ids = {entry["id"] for commodity_id in candidates
                           if commodities[commodity_id]["purchase"] and not commodities[commodity_id].get("isInformalData")
                           for entry in commodities[commodity_id]["commodityItemList"]}
    canonical = canonical_records(meta_root, sources, box_source_ids, required_source_ids)
    source_kinds = {}
    for kind, native_rows in sources.items():
        for source_id in native_rows:
            if source_id in source_kinds:
                raise ValueError(f"Ambiguous native source kind for record {source_id}")
            source_kinds[source_id] = kind
    products = {}
    used_identities = {}
    for commodity_id in sorted(candidates):
        commodity = commodities[commodity_id]
        if not commodity["purchase"] or commodity.get("isInformalData"):
            continue
        contents = commodity["commodityItemList"]
        if commodity["commodityNum"] <= 0 or not contents or any(entry["num"] <= 0 for entry in contents):
            continue
        resolved = []
        for content in contents:
            source_id = content["id"]
            kind = source_kinds.get(source_id)
            identity = canonical.get((kind, source_id))
            if identity is None:
                raise ValueError(f"No exact canonical native identity for commodity {commodity_id}, {kind} record {source_id}")
            resolved.append(identity)
        identities = {entry["item_id"] for entry in resolved}
        if len(identities) != 1:
            raise ValueError(f"Multi-product commodity {commodity_id} needs an explicit catalog contract")
        identity = resolved[0]
        item_id = identity["item_id"]
        if item_id in used_identities and used_identities[item_id] is not identity:
            raise ValueError(f"Canonical ID {item_id} spans incompatible native identities")
        if item_id not in products:
            products[item_id] = {
                **{key: value for key, value in identity.items() if not key.startswith("_")},
                "commodity_ids": [],
            }
            used_identities[item_id] = identity
        products[item_id]["commodity_ids"].append(commodity_id)

    return {
        "schema_version": 1,
        "reference_client": [1280, 720],
        "refresh_policy": {
            "weekday": 0,
            "time": "05:00:00",
            "timezone": "Asia/Shanghai",
            "source": {
                "native": {
                    "file": "StoreFactory.bin",
                    "store_ids": sorted(stores),
                    "refreshType": "Weekly",
                    "refreshTimeList": [{"refreshTime": "05:00:00"}],
                    "proves": "Weekly refresh at 05:00; no weekday or timezone is encoded in these fields.",
                },
                "public_corroboration": PUBLIC_REFRESH_SOURCES,
                "weekday_basis": "Public community sources, not the native Weekly enum.",
                "timezone_basis": "Asia/Shanghai policy for the Chinese client.",
            },
        },
        "eligible_cities": sorted(eligible_cities),
        "products": sorted(products.values(), key=lambda row: (row["category"], row["item_id"])),
        "source": {
            "native_files": dict(sorted(native.sources.items())),
            "city_links": city_links,
            "stores": store_sources,
            "reachable_commodity_count": len(candidates),
            "canonical_metadata": ["inventory_items.json", "inventory_materials.json", "inventory_equipment.json"],
            "grouping": {
                "identity_fields": ["source_kind", "category", "canonical_name", "normalized_icon_resource_url", "quality"],
                "native_aliases_added": {
                    kind: sum(len(set(entry["source_record_ids"]) - entry["_metadata_source_ids"])
                              for entry in used_identities.values() if entry["source_kind"] == kind)
                    for kind in sources
                },
                "canonical_item_id_aliases": {
                    alias: entry["item_id"] for entry in used_identities.values()
                    for alias in sorted(entry["_canonical_item_ids"]) if alias != entry["item_id"]
                },
            },
            "stock_count_policy": {
                "native_field": "StoreFactory.maxShopNum",
                "interpretation": "capacity_only",
                "does_not_prove_exact_total": True,
                "reason": "Configured capacity does not establish exact generated or sold-out card counts.",
            },
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary-config", type=Path, required=True)
    parser.add_argument("--devtools-src", type=Path, required=True)
    parser.add_argument("--meta-root", type=Path, default=META_ROOT)
    parser.add_argument("--output", type=Path, default=META_ROOT / "black_moon_local_shop_catalog.json")
    args = parser.parse_args()
    catalog = build_catalog(args.binary_config, args.devtools_src, args.meta_root)
    args.output.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "products": len(catalog["products"]),
        "commodity_variants": sum(len(row["commodity_ids"]) for row in catalog["products"]),
        "eligible_cities": len(catalog["eligible_cities"]),
        "native_aliases_added": catalog["source"]["grouping"]["native_aliases_added"],
        "grouped_equipment_products": sum(row["category"] == "equipment" and len(row["source_record_ids"]) > 1
                                          for row in catalog["products"]),
        "native_maxShopNum": sorted({row["maxShopNum"] for row in catalog["source"]["stores"]}),
        "does_not_prove_exact_total": True,
    }))


if __name__ == "__main__":
    main()
