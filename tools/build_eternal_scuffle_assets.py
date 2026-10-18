"""Promote the reviewed, offline game exports to self-contained plan assets.

This development-only command consumes the explicitly supplied research root.
The runtime never imports this module or accesses game files/font renderers.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import importlib.util
import io
import os
import tempfile
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "plans/resonance_pc"

CONTROL_FILENAMES = {
    "H01": "home_title", "H02": "play_button", "C01": "coin_dialog_title",
    "C02": "coin_min_button", "C04": "coin_plus_button", "C05": "coin_max_button",
    "C07": "coin_confirm_button", "D03": "initial_equipment_prompt",
    "D04": "equipment_select_button", "D05": "captain_prompt",
    "D06": "captain_confirm_button", "D07": "character_selected_banner",
    "D08": "selected_corner_tl", "D09": "empty_slot_border", "D10": "empty_slot_diagonal",
    "B01": "start_button", "B02": "abandon_button", "B04": "auto_battle_marker",
    "B05": "victory_marker", "B06": "victory_next_button",
}


class FunctionalAssetWriter:
    """Deduplicate within a view; publish only a complete validated build.

    ``deferred`` keeps candidate bytes in memory until finalize. Existing owned
    files are immutable until the catalog changes, so failed/interrupted builds
    leave the previous catalog readable. Only previous ``assets`` own outputs;
    unrelated producers sharing a functional directory are never glob-deleted.
    """

    def __init__(self, plan: Path, assets: dict, *, previous_assets=None, deferred=True):
        self.plan, self.assets, self.seen = plan.resolve(), assets, {}
        self.previous_assets = previous_assets or {}
        self.deferred = deferred
        self.pending = {}
        for relative in self.previous_assets:
            self._path(relative)

    def _path(self, relative):
        path = (self.plan / relative).resolve()
        if not relative.startswith("templates/") or not path.is_relative_to(self.plan / "templates") or path.suffix != ".png":
            raise ValueError(f"Functional asset escapes plan templates: {relative}")
        return path

    def encoded(self, relative):
        return self.pending[relative] if relative in self.pending else self._path(relative).read_bytes()

    def image(self, relative):
        return Image.open(io.BytesIO(self.encoded(relative)))

    def _store(self, payload, relative, metadata):
        path = self._path(relative)
        checksum = hashlib.sha256(payload).hexdigest()
        if self.deferred:
            # A changed image must not invalidate the still-published old index.
            variant = path.with_name(path.stem + "." + checksum + path.suffix)
            if path.exists() and digest(path) != checksum:
                if relative not in self.previous_assets:
                    raise ValueError(f"Refusing to replace another producer's asset: {relative}")
                relative = variant.relative_to(self.plan).as_posix()
            elif not path.exists() and variant.exists():
                # Keep names stable on repeated builds following a changed image.
                relative = variant.relative_to(self.plan).as_posix()
            if relative in self.pending and self.pending[relative] != payload:
                raise ValueError(f"Conflicting generated output: {relative}")
            self.pending[relative] = payload
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        self.assets[relative] = {"sha256": checksum, **metadata}
        return relative

    def write_encoded(self, payload: bytes, relative: str, metadata=None):
        with Image.open(io.BytesIO(payload)) as image:
            return self._store(payload, relative, {"size": list(image.size), "mode": image.mode, **(metadata or {})})

    def write(self, image: Image.Image, relative: str, view: str, metadata: dict) -> str:
        self._path(relative)
        key = (view, image.mode, image.size, hashlib.sha256(image.tobytes()).hexdigest())
        shared = self.seen.get(key)
        if shared is None:
            encoded = io.BytesIO()
            image.save(encoded, format="PNG")
            shared = self._store(encoded.getvalue(), relative, {"size": list(image.size), "mode": image.mode, **metadata})
            self.seen[key] = shared
        record = {"sha256": self.assets[shared]["sha256"], "size": list(image.size), "mode": image.mode, **metadata}
        asset = self.assets[shared]
        original = {key: value for key, value in asset.items() if key != "source_variants"}
        if record != original:
            sources = asset.setdefault("source_variants", [original])
            if record not in sources:
                sources.append(record)
        return shared

    @staticmethod
    def _atomic_write(path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            Path(temporary).replace(path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def finalize(self, catalog: dict, catalog_path: Path):
        """Validate every reference before publishing; prune only prior owned files.

        New files are written first, the catalog is replaced atomically, and
        stale images are removed last. No source/generation failure reaches this
        boundary. A failure before index replacement leaves previous bytes valid.
        """
        catalog_path = catalog_path.resolve()
        if not catalog_path.is_relative_to(self.plan / "data/meta"):
            raise ValueError("Catalog escapes plan metadata")
        if catalog.get("assets") != self.assets:
            raise ValueError("Incomplete asset index")
        def check_references(value):
            if isinstance(value, str) and value.startswith("templates/") and value.endswith(".png"):
                if value not in self.assets:
                    raise ValueError(f"Unindexed generated reference: {value}")
            elif isinstance(value, dict):
                for item in value.values():
                    check_references(item)
            elif isinstance(value, list):
                for item in value:
                    check_references(item)
        check_references({key: value for key, value in catalog.items() if key != "assets"})
        for relative, metadata in self.assets.items():
            payload = self.encoded(relative)
            if hashlib.sha256(payload).hexdigest() != metadata["sha256"]:
                raise ValueError(f"Generated asset hash mismatch: {relative}")
            with Image.open(io.BytesIO(payload)) as image:
                if list(image.size) != metadata["size"] or image.mode != metadata["mode"]:
                    raise ValueError(f"Generated asset shape mismatch: {relative}")
        stale = set(self.previous_assets) - set(self.assets)
        for relative in stale:
            path = self._path(relative)
            if path.exists() and digest(path) != self.previous_assets[relative]["sha256"]:
                raise ValueError(f"Previously owned asset changed externally: {relative}")
        payload = (json.dumps(catalog, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        created = []
        try:
            for relative, encoded in self.pending.items():
                path = self._path(relative)
                if path.exists():
                    if digest(path) != hashlib.sha256(encoded).hexdigest():
                        raise ValueError(f"Generated destination changed externally: {relative}")
                else:
                    self._atomic_write(path, encoded)
                    created.append(path)
            self._atomic_write(catalog_path, payload)
        except BaseException:
            for path in created:
                path.unlink(missing_ok=True)
            raise
        for relative in stale:
            path = self._path(relative)
            path.unlink(missing_ok=True)
            parent = path.parent
            while parent != self.plan / "templates" and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
        self.pending.clear()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_ordered_entries(entries: list[dict], source_rows: list[dict]) -> list[dict]:
    """Restore logical input order after sorted physical asset emission."""
    positions = {row["id"]: index for index, row in enumerate(source_rows)}
    return sorted(entries, key=lambda row: positions[row["id"]])


def add_equipment_name_templates(catalog: dict, research: Path, plan: Path = PLAN, *, writer=None):
    """Render every complete equipment name, including its blank context."""
    groups = defaultdict(list)
    for entry in sorted(catalog["equipment"], key=lambda row: row["id"]):
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
    writer = writer or FunctionalAssetWriter(plan, catalog["assets"], deferred=False)
    for entry in sorted(catalog["equipment"], key=lambda row: row["id"]):
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
        relative = writer.write(context, f"templates/inventory/equipment/art/{entry['id']}/name.png",
                                "equipment/name/image", {"source_font_sha256": font_hash})
        entry["name_template"] = relative
        if entry["id"] in alias_ids:
            entry["image_alias_ids"] = alias_ids[entry["id"]]
    catalog.setdefault("profiles", {})["equipment_name"] = {
        "size": list(default_context), "overflow_size": list(overflow_context),
        "native_font_size": 23, "scale": .66,
        "alignment": "center", "best_fit": False, "overflow": "single_line",
        "matching": "complete_name_with_blank_context", "source_font_sha256": font_hash,
    }
    catalog["image_alias_groups"] = [{"ids": sorted(row["id"] for row in rows), "resolution": "complete_name_template"} for rows in aliases]


def add_occupied_templates(catalog: dict, research: Path, plan: Path = PLAN, *, writer=None):
    """Stable opaque slot-border pixels distinguish occupied from unknown slots."""
    writer = writer or FunctionalAssetWriter(plan, catalog["assets"], deferred=False)
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
            name = "occupied_mask.png" if key == "mask" and quality == "UR" else f"occupied_{quality}{suffix}.png"
            relative = writer.write(image, f"templates/inventory/equipment/frames/scuffle/{name}",
                                    "occupied/mask" if key == "mask" else name, {"source_sha256": digest(source)})
            paths[key] = relative
        catalog["controls"]["occupied_" + quality] = {
            **paths, "source": "game sprite RandomBattle_Equipment_" + quality, "source_sha256": digest(source),
            "threshold": .90, "match_method": "TM_SQDIFF_NORMED",
            "mask_description": "Opaque central perimeter only; excludes equipment art, category corner, LV label and translucent shadow."}


def add_selected_marker_mask(catalog: dict, plan: Path = PLAN, *, writer=None):
    """SELECT text and green fill only; exclude the surrounding portrait pixels."""
    control = catalog["controls"]["D07"]
    template_path = plan / control["template"]
    with (writer.image(control["template"]) if writer else Image.open(template_path)) as template:
        if template.size != (121, 27):
            raise ValueError("Unexpected SELECT marker reference size")
        mask = Image.new("L", template.size)
    ImageDraw.Draw(mask).rectangle((3, 3, 117, 23), fill=255)
    path = template_path.with_name(template_path.stem + "_mask.png")
    relative = path.relative_to(plan).as_posix()
    writer = writer or FunctionalAssetWriter(plan, catalog["assets"], deferred=False)
    relative = writer.write(mask, relative, "control/" + relative, {"source_sha256": hashlib.sha256(writer.encoded(control["template"])).hexdigest()})
    control.update(mask=relative, threshold=.90,
                   mask_description="Central SELECT text and green fill; excludes 3px outer portrait/background border. No animated card corners.")


def add_defeat_title_mask(catalog: dict, plan: Path = PLAN, *, writer=None):
    """Reconstruct the reference's orthogonal YOU LOSE strokes, not its backdrop."""
    control = catalog["controls"]["battle_lose_title"]
    template_path = plan / control["template"]
    with (writer.image(control["template"]) if writer else Image.open(template_path)) as original:
        if original.size != (634, 96):
            raise ValueError("Unexpected defeat title reference size")
        mask = Image.new("L", original.size)
    # Inclusive rectangles traced from the reference's straight pixel-font edges.
    # Each group is one glyph; adjoining bands preserve the stepped corners and
    # O counters without accepting backdrop pixels or threshold-induced holes.
    # The outermost glow/partially covered row is omitted at y=4 and y=90.
    strokes = (
        # Y
        ((6, 5, 30, 41), (67, 5, 79, 41), (18, 41, 67, 53), (30, 53, 54, 89)),
        # O
        ((103, 5, 152, 16), (91, 17, 115, 78),
         (152, 17, 164, 78), (103, 78, 152, 89)),
        # U
        ((176, 5, 201, 78), (237, 5, 249, 78), (188, 78, 237, 89)),
        # L
        ((298, 5, 323, 89), (324, 78, 371, 89)),
        # O
        ((396, 5, 445, 16), (384, 17, 408, 78),
         (445, 17, 457, 78), (396, 78, 445, 89)),
        # S
        ((481, 5, 530, 16), (469, 17, 493, 41), (530, 17, 542, 29),
         (481, 41, 530, 53), (518, 53, 542, 78),
         (469, 65, 481, 78), (481, 78, 530, 89)),
        # E
        ((555, 5, 579, 89), (580, 5, 628, 16),
         (580, 41, 615, 53), (580, 78, 628, 89)),
    )
    draw = ImageDraw.Draw(mask)
    for glyph in strokes:
        for bounds in glyph:
            draw.rectangle(bounds, fill=255)
    path = template_path.with_name(template_path.stem + "_mask.png")
    relative = path.relative_to(plan).as_posix()
    writer = writer or FunctionalAssetWriter(plan, catalog["assets"], deferred=False)
    relative = writer.write(mask, relative, "control/" + relative, {"source_sha256": hashlib.sha256(writer.encoded(control["template"])).hexdigest()})
    control.update(mask=relative, threshold=.90,
                   mask_description="Geometric YOU LOSE pixel-font strokes traced as inclusive axis-aligned rectangles; preserves stepped corners and O counters, excludes backdrop and outer glow. No RGB thresholding.")


def build(research: Path, metadata_research: Path, review_v1: Path):
    spec = importlib.util.spec_from_file_location("scuffle_policy", PLAN / "src/actions/_eternal_scuffle_policy.py")
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
    target = PLAN / "templates/battle/scuffle"
    out = PLAN / "data/meta/eternal_scuffle.json"
    previous = read(out)["assets"] if out.exists() else {}
    assets = {}
    writer = FunctionalAssetWriter(PLAN, assets, previous_assets=previous, deferred=True)

    def copy(source, name):
        relative = (target / name).relative_to(PLAN).as_posix()
        return writer.write_encoded(source.read_bytes(), relative, {"source_sha256": digest(source)})

    def image_pair(image, name, source_hash, view):
        image = image.convert("RGBA")
        relative = writer.write(image, name, view + "/image", {"source_sha256": source_hash})
        mask = image.getchannel("A").point(lambda value: 255 if value > 245 else 0)
        path = Path(name)
        mask_relative = writer.write(mask, path.with_name(path.stem + "_mask.png").as_posix(),
                                     view + "/mask", {"source_sha256": source_hash})
        return relative, mask_relative

    controls = {}
    old = read(review_v1 / "manifest.json")
    source_images = {r["source"]: r for r in old["sources"]}
    retired = {"C03", "C06", "C08", "D01", "D02", "B03", "stage_progress_one", "settlement_unopened_box"}
    for row in old["templates"]:
        if row["group"] not in {"entry", "draft", "battle", "loot", "settlement"} or row["id"] in retired or row["kind"] in {"reference", "digit"}:
            continue
        controls[row["id"]] = {"template": copy(review_v1 / row["file"], "controls/" + CONTROL_FILENAMES.get(row["id"], row["id"]) + ".png"),
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
    for row in sorted(role_meta["characters"], key=lambda row: row["id"]):
        rid = row["id"]
        entry = {key: row[key] for key in ("id", "name", "rarity", "source_quality")}
        entry.update(release_date=None, release_date_source_url=None)
        entry.update(character_dates.get(entry["name"], {}))
        name = name_index[rid]
        name_source = research / "role_names" / name["context"]
        with Image.open(name_source) as name_image:
            entry["name_template"] = writer.write(name_image, f"templates/characters/nameplates/scuffle/{rid}.png",
                                                  "character_names", {"source_sha256": digest(name_source)})
        entry["name_template_size"] = role_names["context_size"]
        entry["stand_templates"], entry["stand_masks"], entry["stand_sources"] = [], [], []
        stand = stand_index[rid]
        for i, source in enumerate(stand["frames"]):
            source_path = research / "role_spine" / source.replace("\\", "/")
            template, mask = image_pair(Image.open(source_path), f"templates/characters/stand/scuffle/{rid}/frame_{i:02}.png", digest(source_path), f"stand/{rid}")
            entry["stand_templates"].append(template)
            entry["stand_masks"].append(mask)
            entry["stand_sources"].append({"source_sha256": digest(source_path)})
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
    equipment_rows = read(research / "equipment/equipment_asset_map.json")
    for row in sorted(equipment_rows, key=lambda row: row["id"]):
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
            functional = {"large": "detail_200", "small_icon": "icon_88", "small_tips": "tips_88"}[profile]
            template, mask = image_pair(image, f"templates/inventory/equipment/art/{rid}/{functional}.png", digest(source), f"equipment/{profile}")
            key = "template" if profile == "large" else profile + "_template"
            mask_key = "mask" if profile == "large" else profile + "_mask"
            entry[key], entry[mask_key] = template, mask
            entry["source"][profile] = {"bundle_sha256": bundle_hash, "sprite": obj["name"], "canvas_size": obj["canvas_size"], "paste_offset": obj["paste_offset"]}
        equipment.append(entry)
    characters = policy.rank_entries(source_ordered_entries(characters, role_meta["characters"]), policy.CHARACTER_RARITIES)
    equipment = policy.rank_entries(source_ordered_entries(equipment, equipment_rows), policy.EQUIPMENT_RARITIES)
    assert len(characters) == 99 and len(equipment) == 176
    catalog = {"schema_version": 1, "ranking_version": "rarity_known_dates_in_id_slots_v1", "source_version": "local-game-export-2026-09-06",
               "reference_client": [1280, 720], "characters": characters, "equipment": equipment, "controls": controls, "assets": assets,
               "profiles": {"name": {"size": role_names["context_size"]}, "large_equipment": {"native_size": [200, 200]},
                            "small_equipment": {"native_size": [88, 88], "preserves_source_canvas": True}}}
    add_equipment_name_templates(catalog, research, PLAN, writer=writer)
    add_occupied_templates(catalog, research, PLAN, writer=writer)
    add_selected_marker_mask(catalog, PLAN, writer=writer)
    add_defeat_title_mask(catalog, PLAN, writer=writer)
    writer.finalize(catalog, out)
    print(json.dumps({"characters": len(characters), "equipment": len(equipment), "controls": len(controls), "assets": len(assets)}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research-root", type=Path, required=True)
    parser.add_argument("--metadata-research-root", type=Path, required=True)
    parser.add_argument("--review-v1-root", type=Path, required=True)
    args = parser.parse_args()
    build(args.research_root, args.metadata_research_root, args.review_v1_root)
