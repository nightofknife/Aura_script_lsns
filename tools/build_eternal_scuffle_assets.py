"""Promote the reviewed, offline game exports to self-contained plan assets.

This development-only command consumes the explicitly supplied research root.
The runtime never imports this module or accesses game files/font renderers.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "plans/resonance_pc"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def add_equipment_name_templates(catalog: dict, research: Path, plan: Path = PLAN):
    """Render every complete equipment name, including its blank context."""
    groups = defaultdict(list)
    for entry in catalog["equipment"]:
        groups[catalog["assets"][entry["template"]]["sha256"]].append(entry)
    aliases = [rows for rows in groups.values() if len(rows) > 1]
    font_path = research / "fonts/SourceHanSansCN-Bold_origin.font"
    font = ImageFont.truetype(str(font_path), 23 * 4)
    missing_glyph = bytes(font.getmask(chr(0x10FFFF)))
    missing = sorted({char for entry in catalog["equipment"] for char in entry["name"]
                      if bytes(font.getmask(char)) == missing_glyph})
    if missing:
        raise ValueError(f"Equipment font lacks glyphs: {missing}")
    font_hash = digest(font_path)
    # UIInitChoose Txt_Name is centered, fixed 23pt, BestFit off and horizontal
    # overflow on. Preserve the longest full name rather than shrinking it.
    default_context = (138, 23)
    overflow_context = (164, 23)
    alias_ids = {row["id"]: sorted(item["id"] for item in rows) for rows in aliases for row in rows}
    for entry in catalog["equipment"]:
        box = font.getbbox(entry["name"])
        glyph = Image.new("L", (box[2] - box[0] + 16, box[3] - box[1] + 16))
        ImageDraw.Draw(glyph).text((8 - box[0], 8 - box[1]), entry["name"], font=font, fill=255)
        glyph = glyph.resize((round(glyph.width * .66 / 4), round(glyph.height * .66 / 4)), Image.Resampling.LANCZOS)
        bounds = glyph.point(lambda value: 255 if value > 20 else 0).getbbox()
        glyph = glyph.crop(bounds)
        context_size = overflow_context if glyph.width > default_context[0] else default_context
        if glyph.width > context_size[0] or glyph.height > context_size[1]:
            raise ValueError(f"Name template overflows: {entry['name']}")
        context = Image.new("L", context_size)
        context.paste(glyph, ((context_size[0] - glyph.width) // 2, (context_size[1] - glyph.height) // 2))
        path = plan / f"templates/eternal_scuffle/equipment/{entry['id']}/name.png"
        context.save(path)
        relative = path.relative_to(plan).as_posix()
        entry["name_template"] = relative
        if entry["id"] in alias_ids:
            entry["image_alias_ids"] = alias_ids[entry["id"]]
        catalog["assets"][relative] = {"sha256": digest(path), "size": list(context_size), "mode": "L", "source_font_sha256": font_hash}
    catalog.setdefault("profiles", {})["equipment_name"] = {
        "size": list(default_context), "overflow_size": list(overflow_context),
        "native_font_size": 23, "scale": .66,
        "alignment": "center", "best_fit": False, "overflow": "single_line",
        "matching": "complete_name_with_blank_context", "source_font_sha256": font_hash,
    }
    catalog["image_alias_groups"] = [{"ids": sorted(row["id"] for row in rows), "resolution": "complete_name_template"} for rows in aliases]


def add_occupied_templates(catalog: dict, research: Path, plan: Path = PLAN):
    """Stable opaque slot-border pixels distinguish occupied from unknown slots."""
    for quality in ("UR", "SSR", "SR", "R"):
        source = research / f"ui/RandomBattle_Equipment_{quality}.png"
        template = Image.open(source).convert("RGBA")
        if template.size != (90, 90):
            raise ValueError("Unexpected equipment border sprite size")
        mask = Image.new("L", template.size)
        draw = ImageDraw.Draw(mask)
        for box in ((22, 0, 67, 2), (22, 87, 67, 89), (0, 22, 2, 67), (87, 22, 89, 67)):
            draw.rectangle(box, fill=255)
        alpha = template.getchannel("A")
        for y in range(90):
            for x in range(90):
                if alpha.getpixel((x, y)) < 245:
                    mask.putpixel((x, y), 0)
        paths = {}
        for key, image, suffix in (("template", template, ""), ("mask", mask, "_mask")):
            path = plan / f"templates/eternal_scuffle/controls/occupied_{quality}{suffix}.png"
            image.save(path)
            relative = path.relative_to(plan).as_posix()
            paths[key] = relative
            catalog["assets"][relative] = {"sha256": digest(path), "size": list(image.size), "mode": image.mode, "source_sha256": digest(source)}
        catalog["controls"]["occupied_" + quality] = {
            **paths, "source": "game sprite RandomBattle_Equipment_" + quality, "source_sha256": digest(source),
            "threshold": .90, "match_method": "TM_SQDIFF_NORMED",
            "mask_description": "Opaque central perimeter only; excludes equipment art, category corner, LV label and translucent shadow."}


def add_selected_marker_mask(catalog: dict, plan: Path = PLAN):
    """SELECT text and green fill only; exclude the surrounding portrait pixels."""
    control = catalog["controls"]["D07"]
    template_path = plan / control["template"]
    with Image.open(template_path) as template:
        if template.size != (121, 27):
            raise ValueError("Unexpected SELECT marker reference size")
        mask = Image.new("L", template.size)
    ImageDraw.Draw(mask).rectangle((3, 3, 117, 23), fill=255)
    path = template_path.with_name(template_path.stem + "_mask.png")
    mask.save(path)
    relative = path.relative_to(plan).as_posix()
    control.update(mask=relative, threshold=.90,
                   mask_description="Central SELECT text and green fill; excludes 3px outer portrait/background border. No animated card corners.")
    catalog["assets"][relative] = {"sha256": digest(path), "size": [121, 27], "mode": "L",
                                  "source_sha256": digest(template_path)}


def build(research: Path, metadata_research: Path, review_v1: Path):
    spec = importlib.util.spec_from_file_location("scuffle_policy", PLAN / "src/actions/_eternal_scuffle_policy.py")
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
    target = PLAN / "templates/eternal_scuffle"
    target.mkdir(parents=True, exist_ok=True)
    assets = {}

    def register(path, source_hash=None):
        relative = path.relative_to(PLAN).as_posix()
        with Image.open(path) as im:
            assets[relative] = {"sha256": digest(path), "size": list(im.size), "mode": im.mode}
        if source_hash:
            assets[relative]["source_sha256"] = source_hash
        return relative

    def copy(source, name):
        dest = target / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        return register(dest, digest(source))

    def image_pair(image, name, source_hash):
        image = image.convert("RGBA")
        dest = target / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        image.save(dest)
        mask = image.getchannel("A").point(lambda value: 255 if value > 245 else 0)
        mask_path = dest.with_name(dest.stem + "_mask.png")
        mask.save(mask_path)
        return register(dest, source_hash), register(mask_path, source_hash)

    controls = {}
    old = read(review_v1 / "manifest.json")
    source_images = {r["source"]: r for r in old["sources"]}
    retired = {"C03", "C06", "C08", "D01", "D02", "B03", "stage_progress_one", "settlement_unopened_box"}
    for row in old["templates"]:
        if row["group"] not in {"entry", "draft", "battle", "loot", "settlement"} or row["id"] in retired or row["kind"] in {"reference", "digit"}:
            continue
        controls[row["id"]] = {"template": copy(review_v1 / row["file"], "controls/" + Path(row["file"]).name),
                               "source_box": row["box"], "client_box": row["estimated_client_box"],
                               "source_image": row["source"], "source_image_sha256": source_images[row["source"]]["sha256"],
                               "label": row["label"]}
    for key in ("choose_role_phase", "unopened_box"):
        source_id = "D01" if key == "choose_role_phase" else "settlement_unopened_box"
        original = next(row for row in old["templates"] if row["id"] == source_id)
        controls[key] = {"template": copy(research / "masks" / (key + ".png"), f"controls/{key}.png"),
                         "mask": copy(research / "masks" / (key + "_mask.png"), f"controls/{key}_mask.png"),
                         "source_box": original["box"], "client_box": original["estimated_client_box"],
                         "source_image": original["source"], "source_image_sha256": source_images[original["source"]]["sha256"]}

    role_meta = read(metadata_research / "characters/character_priority_by_id.json")
    role_names = read(research / "role_names/template_manifest.json")
    name_index = {row["id"]: row for row in role_names["characters"]}
    stand_index = {row["id"]: row for row in read(research / "role_spine/stand_manifest.json")}
    character_dates = {
        "伊卡菈": {"release_date": "2025-07-15", "release_date_source_url": "https://soli-reso.com/news/?news_id=367&type=news"},
        # The notice was posted on Sep 10; its explicitly new banner began Sep 9.
        "静流·逐夏": {"release_date": "2024-09-09", "release_date_source_url": "https://soli-reso.com/news/?news_id=135&type=notice"},
    }
    # Optional verified additions are explicit data, never guessed from IDs.
    extra_dates = metadata_research / "characters/official_release_dates.json"
    if extra_dates.exists():
        for name, row in read(extra_dates).items():
            character_dates[name] = {"release_date": row["release_date"], "release_date_source_url": row.get("source_url", row.get("release_date_source_url"))}
    characters = []
    for row in role_meta["characters"]:
        rid = row["id"]
        entry = {key: row[key] for key in ("id", "name", "rarity", "source_quality")}
        entry.update(release_date=None, release_date_source_url=None)
        entry.update(character_dates.get(entry["name"], {}))
        name = name_index[rid]
        entry["name_template"] = copy(research / "role_names" / name["context"], f"characters/{rid}/name.png")
        entry["name_template_size"] = role_names["context_size"]
        entry["stand_templates"], entry["stand_masks"] = [], []
        stand = stand_index[rid]
        for i, source in enumerate(stand["frames"]):
            source_path = research / "role_spine" / source.replace("\\", "/")
            template, mask = image_pair(Image.open(source_path), f"characters/{rid}/stand_{i:02}.png", digest(source_path))
            entry["stand_templates"].append(template)
            entry["stand_masks"].append(mask)
        entry["source"] = {"factory": "UnitFactory", "factory_sha256": role_meta["source_sha256"],
                           "res_dir": stand["resDir"], "bundle_sha256": stand["source_sha256"], "animation": "stand"}
        characters.append(entry)

    base_equipment = read(metadata_research / "equipment/equipment_priority_by_id.json")
    selected = {row["source_record_id"] for row in base_equipment["equipment"]} | {11800086}
    releases = read(metadata_research / "equipment/official_release_dates.json")
    asset_index = {}
    for bundle in read(research / "equipment/extracted_index.json"):
        for obj in bundle["objects"]:
            if obj["type"] == "Sprite" and "canvas_file" in obj:
                asset_index[(bundle["source"].lower(), obj["name"])] = (obj, bundle["sha256"])
    qualities = {"Orange": "UR", "Golden": "SSR", "Purple": "SR", "Blue": "R"}
    slots = {12600155: "attack", 12600161: "defense", 12600162: "support"}
    equipment = []
    for row in read(research / "equipment/equipment_asset_map.json"):
        rid = row["id"]
        if rid not in selected:
            continue
        entry = {"id": rid, "name": row["name"], "rarity": qualities[row["quality"]], "slot_type": slots[row["equipTagId"]],
                 "release_date": None, "release_date_source_url": None, "source": {"factory": "EquipmentFactory", "factory_sha256": base_equipment["source_sha256"], "icon_path": row["iconPath"], "tips_path": row["tipsPath"]}}
        if entry["name"] in releases:
            release = releases[entry["name"]]
            entry.update(release_date=release["release_date"], release_date_source_url=release["source_url"])
        if rid == 11800086:
            entry["source"]["candidate_pool"] = "RandomBattleFactory: 乱斗/08帝国&学会, initial main-DPS equipment"
        for profile, path_key, size in (("large", "tipsPath", 200), ("small_icon", "iconPath", 88), ("small_tips", "tipsPath", 88)):
            obj, bundle_hash = asset_index[(row[path_key + "_bundle"].lower(), row[path_key + "_sprite"])]
            source = research / "equipment" / obj["canvas_file"].replace("\\", "/")
            image = Image.open(source).convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
            template, mask = image_pair(image, f"equipment/{rid}/{profile}.png", digest(source))
            key = "template" if profile == "large" else profile + "_template"
            mask_key = "mask" if profile == "large" else profile + "_mask"
            entry[key], entry[mask_key] = template, mask
            entry["source"][profile] = {"bundle_sha256": bundle_hash, "sprite": obj["name"], "canvas_size": obj["canvas_size"], "paste_offset": obj["paste_offset"]}
        equipment.append(entry)
    characters = policy.rank_entries(characters, policy.CHARACTER_RARITIES)
    equipment = policy.rank_entries(equipment, policy.EQUIPMENT_RARITIES)
    assert len(characters) == 99 and len(equipment) == 176
    catalog = {"schema_version": 1, "ranking_version": "rarity_known_dates_in_id_slots_v1", "source_version": "local-game-export-2026-09-06",
               "reference_client": [1280, 720], "characters": characters, "equipment": equipment, "controls": controls, "assets": assets,
               "profiles": {"name": {"size": role_names["context_size"]}, "large_equipment": {"native_size": [200, 200]},
                            "small_equipment": {"native_size": [88, 88], "preserves_source_canvas": True}}}
    add_equipment_name_templates(catalog, research)
    add_occupied_templates(catalog, research)
    add_selected_marker_mask(catalog)
    out = PLAN / "data/meta/eternal_scuffle.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"characters": len(characters), "equipment": len(equipment), "controls": len(controls), "assets": len(assets)}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research-root", type=Path, required=True)
    parser.add_argument("--metadata-research-root", type=Path, required=True)
    parser.add_argument("--review-v1-root", type=Path, required=True)
    args = parser.parse_args()
    build(args.research_root, args.metadata_research_root, args.review_v1_root)
