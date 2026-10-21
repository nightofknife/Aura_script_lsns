"""Build the offline affix-refresh catalog from native client BinaryConfig.

This is a build-time importer. The shipped JSON has no game-install dependency.
No screenshots, input operations, or running game process are used.
"""
from __future__ import annotations

import argparse
import json
import re
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path

from build_black_moon_local_shop_catalog import NativeConfigs, REPO_ROOT


def decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def display_number(row: dict, field: str) -> Decimal:
    # SN values have four fixed-point decimals. The client renders server.value
    # * CommonNum, then GetPreciseDecimalFloor(floatNum), including base attrs.
    value = Decimal(str(row.get(field, 0))) * Decimal(str(row.get("CommonNum", 1))) / 10000
    precision = Decimal(1).scaleb(-int(row.get("floatNum", 0)))
    return value.quantize(precision, rounding=ROUND_FLOOR)


def clean_rich_text(text: str) -> str:
    return re.sub(r"<[^>]*>", "", str(text)).strip()


def build(binary_root: Path, devtools_src: Path) -> dict:
    configs = NativeConfigs(binary_root, devtools_src)
    equipment = configs.load("EquipmentFactory", {
        "idCN", "mod", "isInformalData", "name", "iconPath", "equipTagId",
        "campTagId", "skillList", "randomSkillList", "disappearSkillList",
    })
    skills = configs.load("SkillFactory", {
        "description", "isInformalData", "minAttrSN", "maxAttrSN", "digitSN",
        "isBaseAttr", "CommonNum", "floatNum", "campList", "partList",
    })
    pools = configs.load("ListFactory", {"EquipmentEntryList", "isInformalData"})
    tags = configs.load("TagFactory", {"typeName"})
    texts = configs.load("TextFactory", {"text"})

    def reachable(record_id: int, active: frozenset[int] = frozenset()) -> set[int]:
        if record_id in active:
            raise ValueError(f"Cyclic affix pool: {record_id}")
        if record_id in skills:
            return set() if skills[record_id].get("isInformalData") else {record_id}
        row = pools[record_id]
        if row.get("isInformalData"):
            return set()
        result = set()
        for child in row.get("EquipmentEntryList", []):
            if child.get("weight", 1) > 0:
                result.update(reachable(child["id"], active | {record_id}))
        return result

    rows, referenced = [], set()
    for record_id, row in sorted(equipment.items()):
        if (row.get("isInformalData") or row.get("mod") != "基础装备"
                or row.get("idCN", "").startswith("00")
                or not row.get("name") or not row.get("iconPath")):
            continue
        allowed = set()
        for child in row.get("randomSkillList", []):
            if child.get("weight", 1) > 0:
                allowed.update(reachable(child["skillId"]))
        for excluded in row.get("disappearSkillList", []):
            allowed.difference_update(reachable(excluded["skillId"]))
        camp_id, part_id = row["campTagId"], row["equipTagId"]
        allowed = {
            key for key in allowed
            if (not skills[key].get("campList") or camp_id in {v["name"] for v in skills[key]["campList"]})
            and (not skills[key].get("partList") or part_id in {v["name"] for v in skills[key]["partList"]})
        }
        fixed = []
        for child in row.get("skillList", []):
            skill = skills[child["skillId"]]
            description = clean_rich_text(skill.get("description", ""))
            if "%s" in description:
                description = description % decimal_text(display_number(skill, "maxAttrSN"))
            else:
                description = description.replace("%%", "%")
            fixed.append(description)
        rows.append({"id": record_id, "name": row["name"], "camp_id": camp_id,
                     "part_id": part_id, "affix_ids": sorted(allowed),
                     "base_random_slots": int(tags[part_id]["typeName"]),
                     "fixed_descriptions": fixed})
        referenced.update(allowed)

    affixes = []
    for record_id in sorted(referenced):
        row = skills[record_id]
        description = clean_rich_text(row.get("description", ""))
        minimum, maximum = display_number(row, "minAttrSN"), display_number(row, "maxAttrSN")
        step = display_number(row, "digitSN")
        if "%s" not in description:
            minimum = maximum = Decimal(0)
            step = Decimal(1)
        if description.count("%s") > 1 or minimum > maximum or (minimum < maximum and step <= 0):
            raise ValueError(f"Unsupported native affix numeric contract: {record_id}")
        affixes.append({"id": record_id, "description": description,
                        "min_value": decimal_text(minimum), "max_value": decimal_text(maximum),
                        "step": decimal_text(step if step > 0 else Decimal(1)),
                        "unit": "%" if "%s%%" in description else "",
                        "camp_ids": sorted({v["name"] for v in row.get("campList", [])}),
                        "part_ids": sorted({v["name"] for v in row.get("partList", [])})})
    return {"schema_version": 1, "reference_client": [1280, 720],
            "source_files": configs.sources, "affixes": affixes, "equipment": rows,
            "dialog_prompts": {
                "refresh_confirm": clean_rich_text(texts[80601577]["text"]),
                "replace_confirm": clean_rich_text(texts[80601578]["text"]),
            }}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary-root", type=Path, required=True)
    parser.add_argument("--devtools-src", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "plans/resonance_pc/data/meta/affix_refresh.json")
    args = parser.parse_args()
    result = build(args.binary_root, args.devtools_src)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(result['equipment'])} equipment records and {len(result['affixes'])} affix types to {args.output}")


if __name__ == "__main__":
    main()
