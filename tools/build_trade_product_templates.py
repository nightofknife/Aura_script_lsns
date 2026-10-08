"""Build opaque trade-icon crops from an installed game, offline only.

Run with the separate devtools virtualenv (UnityPy is not a runtime dependency).
Only generated PNGs and the JSON catalog are consumed by the plan.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "plans/resonance_pc"
ICON_SIZE = (96, 96)
CROP = (8, 24, 80, 56)
FONT_IDS = {"plus": 6136794397608061395, "label": -2237384480076949585}
RENDERING = {
    "plus": {"text": "+", "font_size": 64, "center": (88.125, 69.25)},
    "label": {"text": "\u53ef\u8865\u5145", "font_size": 34, "center": (89.0625, 131.375)},
}


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def product_sources(records: list[dict], requested_names: set[str] | None = None) -> dict[str, str]:
    """Join exact configured names, rejecting ambiguous sources rather than guessing."""
    result = {}
    for record in records:
        fields = {field["name"]: field.get("value") for field in record["fields"]}
        name, source = fields.get("name"), fields.get("imagePath")
        if (fields.get("isInformalData") or not name or not source
                or "\u6682\u65e0" in str(fields.get("idCN", "")) or "\u6682\u65e0" in name):
            continue
        if requested_names is not None and name not in requested_names:
            continue
        if name in result and result[name] != source:
            raise ValueError(f"Conflicting configured icon paths for {name!r}")
        result[name] = source
    return result


def render_states(icon: Image.Image, background: Image.Image, frame: Image.Image,
                  fonts: dict[str, bytes]) -> dict[str, Image.Image]:
    """Reproduce the reviewed native 180px composition, then take one fixed crop."""
    if icon.size not in ((180, 180), (180, 179)):
        raise ValueError(f"Unexpected small commodity icon size: {icon.size}")
    available = Image.new("RGBA", (180, 180), (0, 0, 0, 255))
    available.alpha_composite(background.convert("RGBA"), (-8, 0))
    available.alpha_composite(icon.convert("RGBA"))
    available.alpha_composite(frame.convert("RGBA"))
    selected = Image.fromarray(
        np.rint(np.asarray(available.convert("RGB"), dtype=float) * .4).astype(np.uint8)
    ).convert("RGBA")
    draw = ImageDraw.Draw(selected)
    for key, parameters in RENDERING.items():
        font = ImageFont.truetype(BytesIO(fonts[key]), parameters["font_size"])
        text = parameters["text"]
        left, top, right, bottom = font.getbbox(text)
        cx, cy = parameters["center"]
        draw.text((cx - (left + right) / 2, cy - (top + bottom) / 2),
                  text, font=font, fill="white")
    x, y, width, height = CROP
    return {
        state: image.convert("RGB").resize(ICON_SIZE, Image.Resampling.BILINEAR)
        .crop((x, y, x + width, y + height))
        for state, image in (("available", available), ("selected", selected))
    }


def build(game_data: Path, devtools_src: Path, plan: Path = PLAN) -> dict:
    sys.path.insert(0, str(devtools_src.resolve()))
    from aura_resonance_devtools.full_config_export import decode_binary_config
    from aura_resonance_devtools.unity_bundle import _logical_sprite_image, load_bundle

    metadata = game_data / "il2cpp_data/Metadata/global-metadata.dat"
    config = game_data / "Patch/BinaryConfig/HomeGoodsFactory.bin"
    products = json.loads((plan / "data/meta/products.json").read_text(encoding="utf-8"))
    mapping = product_sources(decode_binary_config(config)["records"], set(products.values()))
    source_paths = [metadata, config, plan / "data/meta/products.json"]

    def sprites(relative: str) -> dict[str, Image.Image]:
        path = game_data / "Patch/Asset" / relative
        source_paths.append(path)
        environment = load_bundle(path, metadata)
        result = {}
        for obj in environment.objects:
            if obj.type.name != "Sprite":
                continue
            sprite = obj.read()
            if sprite.m_Name in result:
                raise ValueError(f"Duplicate sprite {sprite.m_Name!r} in {relative}")
            result[sprite.m_Name] = _logical_sprite_image(sprite, obj.version)
        return result

    libraries = {"small": sprites("item/goods/small.asset"),
                 "wulinyuan": sprites("item/goods/wulinyuan.asset")}
    common = sprites("ui/common.asset")
    font_path = game_data / "Patch/Asset/ui/font/originpack.asset"
    source_paths.append(font_path)
    fonts = {}
    for obj in load_bundle(font_path, metadata).objects:
        if obj.type.name == "Font" and obj.path_id in FONT_IDS.values():
            key = next(key for key, value in FONT_IDS.items() if value == obj.path_id)
            fonts[key] = bytes(obj.read_typetree()["m_FontData"])
    if fonts.keys() != FONT_IDS.keys():
        raise ValueError("The required original game fonts were not found")

    folder = plan / "templates/trade_products"
    folder.mkdir(parents=True, exist_ok=True)
    entries = {}
    assets = {}
    for product_id, name in products.items():
        source = mapping.get(name)
        entry = {"name": name, "supported": False}
        if not source:
            entry["reason"] = "exact_product_name_not_in_game_config"
        else:
            parts = source.split("/")
            icon = libraries.get(parts[-2].lower(), {}).get(parts[-1])
            entry["source"] = source
            if icon is None:
                entry["reason"] = "configured_sprite_not_found"
            else:
                states = render_states(icon, common["common_bottom_rarity01"],
                                       common["common_mask_rarity01"], fonts)
                entry["supported"] = True
                for state, image in states.items():
                    target = folder / f"{product_id}_{state}.png"
                    image.save(target)
                    relative = target.relative_to(plan).as_posix()
                    entry[state] = relative
                    assets[relative] = {"sha256": digest(target), "size": list(image.size), "mode": image.mode}
        entries[product_id] = entry

    catalog = {
        "schema_version": 1,
        "match": {"method": "TM_SQDIFF_NORMED", "threshold": .92,
                  "use_grayscale": False, "preprocess": "none"},
        "icon_size": list(ICON_SIZE), "crop": list(CROP),
        "template_size": [CROP[2], CROP[3]],
        "rendering": {"native_size": [180, 180], "background_offset": [-8, 0],
                      "selected_brightness": .4, "glyphs": RENDERING},
        "source_hashes": {
            (path.relative_to(game_data).as_posix() if path.is_relative_to(game_data)
             else "plan/products.json"): digest(path) for path in source_paths
        },
        "products": entries, "assets": assets,
    }
    output = plan / "data/meta/trade_product_templates.json"
    output.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return catalog


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-data", type=Path, required=True)
    parser.add_argument("--devtools-src", type=Path,
                        default=ROOT.parent / "Aura_script_lsns_devtools/src")
    parser.add_argument("--plan", type=Path, default=PLAN)
    args = parser.parse_args()
    catalog = build(args.game_data.resolve(), args.devtools_src, args.plan.resolve())
    unsupported = {key: item for key, item in catalog["products"].items() if not item["supported"]}
    print(json.dumps({"products": len(catalog["products"]), "templates": len(catalog["assets"]),
                      "unsupported": unsupported}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
