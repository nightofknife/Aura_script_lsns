"""Build investment templates exclusively from native game UI assets and fonts."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from _trade_investment_native import configure, inspect_prefab, load_bundle


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "plans/resonance_pc/templates/trade_goods_investment"
LEVEL_ROIS = {"current": [899, 217, 81, 48], "preview": [1030, 217, 80, 48]}
SLOT_ORIGINS = [[61 + col * 127, 143 + row * 132] for row in range(3) for col in range(3)]
MAX_PREFIX = "/HomeTradeUpgrade/Group_Right/Group_Max/"
SUCCESS_OCCLUSION_ROI = [0, 327, 1280, 66]


def crop(image: np.ndarray, box: list[int]) -> np.ndarray:
    x, y, width, height = box
    return image[y:y + height, x:x + width].copy()


def bright_mask(image: np.ndarray) -> np.ndarray:
    minimum = image.min(axis=2)
    spread = image.max(axis=2).astype(int) - minimum.astype(int)
    return np.where((minimum >= 205) & (spread < 38), 255, 0).astype(np.uint8)


def yellow_mask(image: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(image, cv2.COLOR_RGB2HSV)
    return cv2.inRange(hsv, np.array([16, 160, 170]), np.array([40, 255, 255]))


def dark_mask(image: np.ndarray) -> np.ndarray:
    return np.where(image.max(axis=2) < 90, 255, 0).astype(np.uint8)


def tight_token(mask: np.ndarray, canvas=(84, 36)) -> tuple[np.ndarray, list[int]]:
    points = cv2.findNonZero(mask)
    if points is None:
        raise ValueError("No foreground in text ROI")
    x, y, width, height = cv2.boundingRect(points)
    if width > canvas[0] - 2 or height > canvas[1] - 2:
        raise ValueError(f"Unexpected token size: {width}x{height}, canvas={canvas}")
    target = np.zeros((canvas[1], canvas[0]), dtype=np.uint8)
    ox, oy = (canvas[0] - width) // 2, (canvas[1] - height) // 2
    target[oy:oy + height, ox:ox + width] = mask[y:y + height, x:x + width]
    return target, [x, y, width, height]


def digit_token(mask: np.ndarray) -> np.ndarray:
    x, y, width, height = cv2.boundingRect(cv2.findNonZero(mask))
    glyph = cv2.resize(mask[y:y + height, x:x + width], (14, 26), interpolation=cv2.INTER_AREA)
    return np.pad(np.where(glyph >= 128, 255, 0).astype(np.uint8), 3)


def render_text(text: str, font_bytes: bytes, native_size: int, variant: str = "native", stroke: int = 0) -> Image.Image:
    size = native_size if variant == "native" else round(native_size * 2/3)
    font = ImageFont.truetype(io.BytesIO(font_bytes), size)
    canvas = Image.new("L", (max(500, round(font.getlength(text)) + 40), size * 3), 0)
    ImageDraw.Draw(canvas).text((15, size), text, font=font, fill=255, anchor="lt", stroke_width=stroke, stroke_fill=255)
    if variant == "native":
        canvas = canvas.resize((round(canvas.width * 2/3), round(canvas.height * 2/3)), Image.Resampling.BILINEAR)
    return canvas


def foreground_text(text: str, font_bytes: bytes, native_size: int, variant="native", stroke=0) -> np.ndarray:
    mask = np.where(np.asarray(render_text(text, font_bytes, native_size, variant, stroke)) >= 160, 255, 0).astype(np.uint8)
    x, y, width, height = cv2.boundingRect(cv2.findNonZero(mask))
    return np.pad(mask[y:y + height, x:x + width], 2)


def sprite_pixels(image: Image.Image, size: list[int], tint=None) -> np.ndarray:
    rgba = np.array(image.convert("RGBA").resize(tuple(size), Image.Resampling.BILINEAR))
    if tint:
        rgba = np.clip(rgba.astype(float) * np.array(tint), 0, 255).astype(np.uint8)
    alpha = rgba[:, :, 3:4].astype(float) / 255
    return (rgba[:, :, :3] * alpha).astype(np.uint8)


def save(name: str, pixels: np.ndarray) -> None:
    path = TEMPLATES / name
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(pixels).save(path)


def add_entry_availability(assets: Path, metadata: Path, manifest: dict) -> None:
    """Compose comparable native RGB icon/label templates for both entry states."""
    from aura_resonance_devtools.unity_bundle import _logical_sprite_image

    prefab_rel = "ui/hometrade/hometrade.asset"
    nodes = {node["path"]: node for node in inspect_prefab(load_bundle(assets / prefab_rel, metadata))["nodes"]}
    prefix = "/HomeTrade/Group_Main/Btn_invest"
    objects, hashes = {}, {}
    for relative in ("ui/common.asset", "ui/hometrade.asset", "ui/font/originpack.asset"):
        bundle = assets / relative
        hashes[relative] = hashlib.sha256(bundle.read_bytes()).hexdigest()
        for obj in load_bundle(bundle, metadata).objects:
            if obj.type.name in ("Sprite", "Font"):
                objects[obj.path_id] = (obj, relative)
    crop_box = (734, 456, 903, 523)
    size = [crop_box[2] - crop_box[0], crop_box[3] - crop_box[1]]
    roi = [crop_box[0] - 3, crop_box[1] - 3, size[0] + 6, size[1] + 6]
    names = {"entry_available", "entry_unavailable", "entry_restriction", "entry_lock"}
    manifest["templates"] = [entry for entry in manifest["templates"] if entry["name"] not in names]

    def component(path, field):
        return next(item["tree"] for item in nodes[path]["components"] if field in item["tree"])

    def sprite(path):
        tree = component(path, "m_Sprite")
        obj, relative = objects[tree["m_Sprite"]["m_PathID"]]
        rect = nodes[path]["rect_client"]
        image = _logical_sprite_image(obj.read(), obj.version).convert("RGBA").resize(
            tuple(rect[2:]), Image.Resampling.BILINEAR)
        image = Image.merge("RGBA", tuple(channel.point(
            lambda value, factor=tree["m_Color"][key]: round(value * factor))
            for channel, key in zip(image.split(), ("r", "g", "b", "a"))))
        return image, {"node": path, "sprite_path_id": obj.path_id, "bundle": relative, "rect": rect}

    pairs = []
    for name, parent, expected_id in (("entry_available", prefix, -4532753186095738395),
                                      ("entry_unavailable", prefix + "/Img_Rep", -7361993374187006141)):
        canvas = Image.new("RGBA", (1280, 720))
        background, background_source = sprite(parent)
        if background_source["sprite_path_id"] != expected_id:
            raise ValueError(f"Native investment entry sprite changed: {parent}")
        canvas.alpha_composite(background, tuple(nodes[parent]["rect_client"][:2]))
        icon, icon_source = sprite(parent + "/Img_Icon")
        canvas.alpha_composite(icon, tuple(nodes[parent + "/Img_Icon"]["rect_client"][:2]))
        text_node = parent + "/Txt_Name"
        tree = component(text_node, "m_FontData")
        style = tree["m_FontData"]
        if style["m_FontStyle"] != 0:
            raise ValueError("Entry text requires the native normal font style")
        font_obj, font_bundle = objects[style["m_Font"]["m_PathID"]]
        font_bytes = bytes(font_obj.read().m_FontData)
        alpha = render_text(tree["m_Text"], font_bytes, style["m_FontSize"])
        alpha = alpha.crop(alpha.getbbox())
        color = tuple(round(tree["m_Color"][key] * 255) for key in ("r", "g", "b"))
        glyph = Image.new("RGBA", alpha.size, color + (0,))
        glyph.putalpha(alpha)
        canvas.alpha_composite(glyph, tuple(nodes[text_node]["rect_client"][:2]))
        image = canvas.crop(crop_box)
        pairs.append((name, image))
        save(f"{name}.png", np.array(image.convert("RGB")))
        manifest["templates"].append({
            "name": name, "file": f"{name}.png", "mask_file": "entry_pair_mask.png",
            "kind": "native_sprite_font_composite", "background": background_source, "icon": icon_source,
            "text": {"node": text_node, "font_path_id": font_obj.path_id, "bundle": font_bundle,
                     "font_sha256": hashlib.sha256(font_bytes).hexdigest(), "native_size": style["m_FontSize"]},
            "bundle_sha256": hashes, "prefab_node": parent,
            "prefab_sha256": hashlib.sha256((assets / prefab_rel).read_bytes()).hexdigest(),
            "client_crop_box": list(crop_box), "client_node_size": size, "roi": roi,
            "preprocess": "rgb", "threshold": 0.82,
            "method": "1 - TM_SQDIFF_NORMED", "status": "native_generated_unvalidated",
        })
    alpha = np.minimum(*(np.array(image.getchannel("A")) for _, image in pairs))
    save("entry_pair_mask.png", np.where(alpha >= 250, 255, 0).astype(np.uint8))
    manifest["entry_availability"] = {
        "available": "entry_available", "unavailable": "entry_unavailable",
        "decision": "available_score > unavailable_score; ties are unavailable",
        "method": "1 - TM_SQDIFF_NORMED", "shared_mask": "entry_pair_mask.png",
        "status": "native_generated_unvalidated", "stable_frames": 2,
        "limitations": "Visual preview approved; no pair-score or live-flow validation was run.",
    }


def add_maximum_cards(assets: Path, metadata: Path, nodes: dict, manifest: dict,
                      normal_pixels=None, normal_header_pixels=None) -> None:
    from aura_resonance_devtools.unity_bundle import _logical_sprite_image

    cards = manifest["cards"]
    prefix = "/HomeTradeUpgrade/Group_Left/ScrollGrid_Left/Viewport/Grid/Group_Item_000/"
    node = prefix + "Group_Max/Img_"
    tree = next(c["tree"] for c in nodes[node]["components"] if "m_Sprite" in c["tree"])
    sprite_id = tree["m_Sprite"]["m_PathID"]
    if sprite_id != 9077236014113405044:
        raise ValueError("Native maximum-card sprite has changed")
    frame_rect = nodes[prefix + "Img_BG"]["rect_client"]
    if nodes[node]["rect_client"] != frame_rect:
        raise ValueError("Maximum-card overlay no longer matches the whole native frame")
    rel = "ui/hometrade/update.asset"
    env = load_bundle(assets / rel, metadata)
    sprite_objects = {obj.path_id: obj for obj in env.objects if obj.type.name == "Sprite"}

    def sprite_image(pid):
        obj = sprite_objects[pid]
        return _logical_sprite_image(obj.read(), obj.version).convert("RGBA")

    size = tuple(cards["size"])
    if normal_pixels is None:
        background = sprite_image(cards["normal_sprite_id"]).resize(size, Image.Resampling.BILINEAR)
        bottom_rect = nodes[prefix + "Img_Bottom"]["rect_client"]
        common = load_bundle(assets / "ui/common.asset", metadata)
        obj = next(o for o in common.objects if o.type.name == "Sprite" and o.path_id == cards["bottom_sprite_id"])
        bottom = _logical_sprite_image(obj.read(), obj.version).convert("RGBA").resize(tuple(bottom_rect[2:]), Image.Resampling.BILINEAR)
        dx, dy = frame_rect[0] - bottom_rect[0], frame_rect[1] - bottom_rect[1]
        bottom.alpha_composite(background, (dx, dy))
        layer = bottom.crop((dx, dy, dx + size[0], dy + size[1]))
        panel_rect = nodes["/HomeTradeUpgrade/Group_Left/Img_BG"]["rect_client"]
        panel = sprite_image(cards["panel_sprite_id"]).resize(tuple(panel_rect[2:]), Image.Resampling.BILINEAR)
        px, py = frame_rect[0] - panel_rect[0], frame_rect[1] - panel_rect[1]
        backdrop = Image.new("RGBA", size, tuple(cards["backdrop_color"]))
        panel_patch = Image.alpha_composite(backdrop, panel.crop((px, py, px + size[0], py + size[1])))
        normal_pixels = np.array(Image.alpha_composite(panel_patch, layer).convert("RGB"))
        normal_header_pixels = np.array(Image.alpha_composite(backdrop, background).convert("RGB"))
    tint = [tree["m_Color"][key] for key in ("r", "g", "b", "a")]
    overlay = np.array(sprite_image(sprite_id).resize(size, Image.Resampling.BILINEAR))
    overlay = Image.fromarray(np.clip(overlay.astype(float) * np.array(tint), 0, 255).astype(np.uint8))

    def compose(pixels):
        return np.array(Image.alpha_composite(Image.fromarray(pixels).convert("RGBA"), overlay).convert("RGB"))

    pixels, header = compose(normal_pixels), compose(normal_header_pixels)
    group_rect = nodes[prefix + "Group_Max"]["rect_client"]
    label_rect = nodes[prefix + "Group_Max/Txt_"]["rect_client"]
    badge_box = [group_rect[0] - frame_rect[0], group_rect[1] - frame_rect[1],
                 group_rect[2], label_rect[1] - group_rect[1] - 1]
    if badge_box[3] <= 0:
        raise ValueError("Maximum-card badge has no text-free native region")
    save("cards/maximum_badge.png", cv2.GaussianBlur(crop(pixels, badge_box), (3, 3), 0))
    badge_entry = {"name": "maximum_card_badge", "file": "cards/maximum_badge.png",
                   "kind": "native_sprite_composite", "sprite_path_id": sprite_id,
                   "prefab_node": node, "roi": cards["viewport"], "preprocess": "rgb_blur",
                   "threshold": 0.90}
    manifest["templates"] = [row for row in manifest["templates"]
                             if row["name"] != "maximum_card_badge"] + [badge_entry]
    mask = np.array(Image.open(TEMPLATES / cards["frame_mask"]).convert("L"))
    excluded = []
    for path in (prefix + "Group_Max", prefix + "Group_Max/Txt_"):
        x, y, w, h = nodes[path]["rect_client"]
        x, y = x - frame_rect[0], y - frame_rect[1]
        excluded.append([x, y, w, h])
        mask[max(0, y - 1):min(size[1], y + h + 1), max(0, x - 1):min(size[0], x + w + 1)] = 0
    save("cards/maximum.png", cv2.GaussianBlur(pixels, (3, 3), 0))
    save("cards/maximum_header.png", cv2.GaussianBlur(crop(header, cards["header_box"]), (3, 3), 0))
    sizes = sorted({tuple(row["size"]) for row in cards["frame_variants"]})
    variants = []
    for width, height in sizes:
        filename = f"cards/maximum_{width}x{height}.png"
        mask_file = f"cards/maximum_frame_mask_{width}x{height}.png"
        save(filename, cv2.GaussianBlur(cv2.resize(pixels, (width, height), interpolation=cv2.INTER_AREA), (3, 3), 0))
        save(mask_file, cv2.resize(mask, (width, height), interpolation=cv2.INTER_NEAREST))
        variants.append({"file": filename, "mask": mask_file, "size": [width, height], "state": "maximum"})
    cards["frame_variants"] = [row for row in cards["frame_variants"] if row["state"] != "maximum"] + variants
    for key, filename in (("variants", "cards/maximum.png"), ("header_variants", "cards/maximum_header.png")):
        if filename not in cards[key]:
            cards[key].append(filename)
    cards["maximum"] = {"prefab_node": node, "sprite_path_id": sprite_id, "sprite": "itemLvMax",
                         "bundle": rel, "bundle_sha256": hashlib.sha256((assets / rel).read_bytes()).hexdigest(),
                         "client_node_rect": frame_rect, "native_tint": tint,
                         "group_initially_active": nodes[prefix + "Group_Max"]["active"],
                         "badge_box": badge_box,
                         "excluded_interior_rects": excluded, "status": "native_generated_unvalidated"}
    mark_runtime_metadata(manifest)


def mark_runtime_metadata(manifest: dict) -> None:
    manifest["status"] = "template_runtime_with_unvalidated_states"
    obsolete = "Disabled confirm state and maximum-level page require native state composition"
    manifest["limitations"] = [item for item in manifest.get("limitations", []) if item != obsolete]
    for item in ("No live or temporal verification",
                 "Maximum-level layout and maximum-card state have no real screenshot verification",
                 "Disabled confirm state has no real screenshot verification; runtime uses enabled-template color consistency"):
        if item not in manifest["limitations"]:
            manifest["limitations"].append(item)


def add_max_level(assets: Path, metadata: Path, nodes: dict, manifest: dict, *, bounded: bool = False) -> None:
    from aura_resonance_devtools.unity_bundle import _logical_sprite_image

    def component(path, field):
        return next(c["tree"] for c in nodes[path]["components"] if field in c["tree"])

    def save_max_asset(filename, pixels):
        if bounded and (TEMPLATES / filename).exists():
            raise FileExistsError(f"Refusing to overwrite existing native asset: {filename}")
        save(filename, pixels)

    level_node = MAX_PREFIX + "Txt_Lv2"
    level_tree = component(level_node, "m_FontData")
    style = level_tree["m_FontData"]
    base_style = component("/HomeTradeUpgrade/Group_Right/Group_NotMax/Txt_Lv1", "m_FontData")["m_FontData"]
    font_id, size = style["m_Font"]["m_PathID"], style["m_FontSize"]
    if style["m_FontStyle"] != 0:
        raise ValueError("Max-level font style requires a native renderer not supported by this builder")
    font_rel = "ui/font.asset"
    font_obj = next(obj for obj in load_bundle(assets / font_rel, metadata).objects
                    if obj.type.name == "Font" and obj.path_id == font_id)
    font = font_obj.read()
    font_bytes = bytes(font.m_FontData)
    font_sha = hashlib.sha256(font_bytes).hexdigest()
    reuse = (style == base_style and bool(manifest["levels"]) and bool(manifest["digits"])
             and all(row["font_path_id"] == font_id and row["native_font_size"] == size
                     and row["font_sha256"] == font_sha for row in manifest["levels"])
             and all(row["font_path_id"] == font_id and row["native_font_size"] == size
                     for row in manifest["digits"]))
    if not reuse:
        levels, digits = [], []
        for level in range(21):
            for variant in ("native", "client"):
                mask = np.where(np.asarray(render_text(f"Lv.{level}", font_bytes, size, variant)) >= 180,
                                255, 0).astype(np.uint8)
                token, foreground = tight_token(mask)
                filename = f"max_levels/{level:02d}_{variant}.png"
                save_max_asset(filename, token)
                levels.append({"level": level, "file": filename, "kind": "native_font_composite",
                               "font": font.m_Name, "font_path_id": font_id, "font_bundle": font_rel,
                               "font_sha256": font_sha, "native_font_size": size,
                               "render_variant": variant, "foreground": foreground, "prefab_node": level_node})
        for digit in range(10):
            for variant in ("native", "client"):
                mask = np.where(np.asarray(render_text(str(digit), font_bytes, size, variant)) >= 180,
                                255, 0).astype(np.uint8)
                filename = f"max_digits/{digit}_{variant}.png"
                save_max_asset(filename, digit_token(mask))
                digits.append({"digit": digit, "file": filename, "kind": "native_font_composite",
                               "font_path_id": font_id, "native_font_size": size, "variant": variant})
        manifest["max_levels"], manifest["max_digits"] = levels, digits

    image_node = MAX_PREFIX + "Img_LvMax"
    image_tree = component(image_node, "m_Sprite")
    sprite_id = image_tree["m_Sprite"]["m_PathID"]
    if sprite_id != -1763122843846654301:
        raise ValueError("Native maximum-level badge sprite has changed")
    if not any(row["name"] == "max_layout" for row in manifest["templates"]):
        rel = "ui/hometrade/update.asset"
        obj = next(obj for obj in load_bundle(assets / rel, metadata).objects
                   if obj.type.name == "Sprite" and obj.path_id == sprite_id)
        sprite = obj.read()
        raw = _logical_sprite_image(sprite, obj.version)
        rect = nodes[image_node]["rect_client"]
        text_rect = nodes[MAX_PREFIX + "Txt_LvMax"]["rect_client"]
        # Exclude the localized child label and a one-pixel rasterization fringe.
        bottom = text_rect[1] + text_rect[3] - rect[1] + 1
        native_crop = [0, bottom, rect[2], rect[3] - bottom]
        if native_crop[3] <= 0:
            raise ValueError("Native maximum-level badge has no text-free bottom band")
        tint = [image_tree["m_Color"][key] for key in ("r", "g", "b", "a")]
        pixels = crop(sprite_pixels(raw, rect[2:], tint), native_crop)
        save_max_asset("max_layout.png", pixels)
        manifest["templates"].append({
            "name": "max_layout", "file": "max_layout.png", "kind": "native_sprite",
            "sprite": sprite.m_Name, "sprite_path_id": sprite_id, "bundle": rel,
            "bundle_sha256": hashlib.sha256((assets / rel).read_bytes()).hexdigest(),
            "prefab_node": image_node, "native_sprite_size": list(raw.size),
            "client_node_size": rect[2:], "crop_on_client_sprite": native_crop,
            "roi": rect, "preprocess": "rgb", "threshold": 0.85,
            "excluded_text_node": MAX_PREFIX + "Txt_LvMax", "excluded_text_roi": text_rect,
            "status": "native_generated_unvalidated",
        })
    manifest["max_level_roi"] = nodes[level_node]["rect_client"]
    manifest["max_level_style"] = {
        "prefab_node": level_node, "font": font.m_Name, "font_path_id": font_id,
        "font_bundle": font_rel, "font_sha256": font_sha, "native_font_size": size,
        "native_font_style": style["m_FontStyle"],
        "native_color": [level_tree["m_Color"][key] for key in ("r", "g", "b", "a")],
        "preprocess": "yellow", "levels_key": "levels" if reuse else "max_levels",
        "digits_key": "digits" if reuse else "max_digits", "reuses_existing_templates": reuse,
        "prefab_sha256": hashlib.sha256((assets / "ui/hometrade/hometradeupgrade.asset").read_bytes()).hexdigest(),
        "status": "native_generated_unvalidated",
    }
    manifest["cards"]["success_occlusion_roi"] = SUCCESS_OCCLUSION_ROI.copy()
    old_limitation = "Disabled confirm state and maximum-level page require native state composition"
    manifest["limitations"] = [item for item in manifest.get("limitations", []) if item != old_limitation]
    for item in ("Disabled confirm state still requires native state composition",
                 "Maximum-level badge uses a text-free native sprite band; matching threshold is not screenshot-calibrated",
                 "Maximum-level yellow text and layout have no live, screenshot, or temporal verification"):
        if item not in manifest["limitations"]:
            manifest["limitations"].append(item)
    mark_runtime_metadata(manifest)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-data", type=Path, required=True)
    parser.add_argument("--pythonlibs", type=Path, required=True)
    parser.add_argument("--devtools-src", type=Path, required=True)
    parser.add_argument("--max-only", action="store_true",
                        help="Add only native maximum-level assets and metadata to an existing manifest")
    parser.add_argument("--maximum-cards-only", action="store_true",
                        help="Add only native maximum-card localization assets to an existing manifest")
    parser.add_argument("--entry-only", action="store_true",
                        help="Add only native open and restricted exchange-entry assets")
    args = parser.parse_args()
    configure(args.pythonlibs, args.devtools_src)
    from aura_resonance_devtools.unity_bundle import _logical_sprite_image

    TEMPLATES.mkdir(parents=True, exist_ok=True)
    metadata = args.game_data / "il2cpp_data/Metadata/global-metadata.dat"
    assets = args.game_data / "Patch/Asset"
    manifest_path = TEMPLATES / "manifest.json"
    if args.entry_only:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        add_entry_availability(assets, metadata, manifest)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"mode": "entry_only", "status": manifest["entry_availability"]["status"]}))
        return
    pref_path = assets / "ui/hometrade/hometradeupgrade.asset"
    nodes = {node["path"]: node for node in inspect_prefab(load_bundle(pref_path, metadata))["nodes"]}
    if args.maximum_cards_only:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        add_maximum_cards(assets, metadata, nodes, manifest)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"mode": "maximum_cards_only", "state": "maximum", "status": manifest["status"]}))
        return
    if args.max_only:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["schema_version"] != 2 or manifest["reference_client"] != [1280, 720]:
            raise ValueError("Bounded maximum-level generation requires a 1280x720 schema-v2 manifest")
        add_max_level(assets, metadata, nodes, manifest, bounded=True)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"mode": "max_only", "max_layout": next(row for row in manifest["templates"]
                                                                 if row["name"] == "max_layout"),
                          "max_level_roi": manifest["max_level_roi"],
                          "max_level_style": manifest["max_level_style"],
                          "success_occlusion_roi": manifest["cards"]["success_occlusion_roi"]}))
        return
    sprites, fonts, provenance = {}, {}, {}
    for rel in ("ui/font.asset", "ui/font/originpack.asset", "ui/hometrade/update.asset", "ui/common.asset"):
        bundle = assets / rel
        env = load_bundle(bundle, metadata)
        provenance[rel] = hashlib.sha256(bundle.read_bytes()).hexdigest()
        for obj in env.objects:
            if obj.type.name == "Font":
                font = obj.read()
                fonts[obj.path_id] = (bytes(font.m_FontData), font.m_Name, rel)
            elif obj.type.name == "Sprite":
                if rel == "ui/common.asset" and obj.path_id != 8083724852411651157:
                    continue
                sprite = obj.read()
                sprites[obj.path_id] = (_logical_sprite_image(sprite, obj.version), sprite.m_Name, rel)

    def component(path, field):
        return next(c["tree"] for c in nodes[path]["components"] if field in c["tree"])

    templates = []
    def add_text(name, text, font_id, size, roi, variant="native", node=None, preprocess="bright", stroke=0):
        data, font_name, rel = fonts[font_id]
        pixels = foreground_text(text, data, size, variant, stroke)
        filename = f"{name}.png"
        save(filename, pixels)
        templates.append({"name": name, "file": filename, "kind": "native_font_composite",
                          "font": font_name, "font_path_id": font_id, "font_bundle": rel,
                          "font_sha256": hashlib.sha256(data).hexdigest(), "text": text,
                          "native_font_size": size, "render_variant": variant, "native_stroke": stroke,
                          "prefab_node": node, "roi": roi, "preprocess": preprocess, "threshold": 0.82})

    prefix = "/HomeTradeUpgrade/Group_Right/Group_NotMax/"
    level_style = component(prefix + "Txt_Lv1", "m_FontData")["m_FontData"]
    level_font_id, level_size = level_style["m_Font"]["m_PathID"], level_style["m_FontSize"]
    levels, digits = [], []
    for level in range(21):
        for variant in ("native", "client"):
            data, name, rel = fonts[level_font_id]
            mask = np.where(np.asarray(render_text(f"Lv.{level}", data, level_size, variant)) >= 180, 255, 0).astype(np.uint8)
            token, foreground = tight_token(mask)
            filename = f"levels/{level:02d}_{variant}.png"
            save(filename, token)
            levels.append({"level": level, "file": filename, "kind": "native_font_composite",
                           "font": name, "font_path_id": level_font_id, "font_bundle": rel,
                           "font_sha256": hashlib.sha256(data).hexdigest(), "native_font_size": level_size,
                           "render_variant": variant, "foreground": foreground,
                           "prefab_node": prefix + "Txt_Lv1"})
    for digit in range(10):
        for variant in ("native", "client"):
            mask = np.where(np.asarray(render_text(str(digit), fonts[level_font_id][0], level_size, variant)) >= 180, 255, 0).astype(np.uint8)
            filename = f"digits/{digit}_{variant}.png"
            save(filename, digit_token(mask))
            digits.append({"digit": digit, "file": filename, "kind": "native_font_composite",
                           "font_path_id": level_font_id, "native_font_size": level_size, "variant": variant})

    title_node = "/HomeTradeUpgrade/Group_Left/Txt_"
    title_style = component(title_node, "m_FontData")["m_FontData"]
    add_text("page_title", "\u4ea4\u6613\u54c1\u6295\u8d44", title_style["m_Font"]["m_PathID"],
             title_style["m_FontSize"], [200, 85, 180, 55], node=title_node, stroke=1)
    shop_prefab = assets / "ui/hometrade/hometrade.asset"
    shop_nodes = {node["path"]: node for node in inspect_prefab(load_bundle(shop_prefab, metadata))["nodes"]}
    entry_node = "/HomeTrade/Group_Main/Btn_invest/Txt_Name"
    entry_style = next(c["tree"]["m_FontData"] for c in shop_nodes[entry_node]["components"] if "m_FontData" in c["tree"])
    add_text("entry", "\u4ea4\u6613\u54c1\u6295\u8d44", entry_style["m_Font"]["m_PathID"], entry_style["m_FontSize"],
             [780, 462, 220, 50], preprocess="dark_blur", node=entry_node)
    save("entry.png", cv2.GaussianBlur(np.array(Image.open(TEMPLATES / "entry.png")), (3, 3), 0))
    toast = inspect_prefab(load_bundle(assets / "ui/common/tips.asset", metadata))
    toast_node = next(n for n in toast["nodes"] if n["path"] == "/Tips/Txt_Tips")
    toast_style = next(c["tree"]["m_FontData"] for c in toast_node["components"] if "m_FontData" in c["tree"])
    add_text("success", "\u5347\u7ea7\u6210\u529f", toast_style["m_Font"]["m_PathID"],
             toast_style["m_FontSize"], [480, 335, 330, 65], node="/Tips/Txt_Tips")

    for name, node, roi, operation in [
        ("plus", prefix + "Btn_Up", [1110, 200, 80, 80], "rgb"),
        ("minus", prefix + "Btn_Down", [815, 200, 80, 80], "rgb"),
        ("confirm_enabled", prefix + "Btn_confirm", [810, 520, 390, 90], "rgb"),
        ("lock", "/HomeTradeUpgrade/Group_Left/ScrollGrid_Left/Viewport/Grid/Group_Item_000/Img_lock", [50, 140, 385, 450], "bright"),
        ("selected_corner", "/HomeTradeUpgrade/Group_Left/ScrollGrid_Left/Viewport/Grid/Group_Item_000/Img_OnMask", [50, 140, 385, 450], "yellow"),
        ("page_anchor", "/HomeTradeUpgrade/Group_Left/Img_icon", [160, 90, 70, 65], "bright"),
    ]:
        tree = component(node, "m_Sprite")
        sprite_id = tree["m_Sprite"]["m_PathID"]
        raw, sprite_name, rel = sprites[sprite_id]
        size = nodes[node]["rect_client"][2:]
        pixels = sprite_pixels(raw, size)
        if name == "confirm_enabled":
            text_style = component(prefix + "Txt_confirm", "m_FontData")["m_FontData"]
            text_layer = render_text("\u786e\u8ba4", fonts[text_style["m_Font"]["m_PathID"]][0], text_style["m_FontSize"])
            text_layer = text_layer.crop(text_layer.getbbox())
            x, y = round((size[0] - text_layer.width) / 2), round((size[1] - text_layer.height) / 2)
            alpha = np.asarray(text_layer).astype(float)[:, :, None] / 255
            pixels[y:y + text_layer.height, x:x + text_layer.width] = (pixels[y:y + text_layer.height, x:x + text_layer.width] * (1 - alpha)).astype(np.uint8)
        native_crop = None
        if operation == "bright":
            mask = bright_mask(pixels)
            points = cv2.findNonZero(mask)
            if points is None:
                raise ValueError(f"Empty native foreground: {name}")
            x, y, width, height = cv2.boundingRect(points)
            native_crop = [x - 2, y - 2, width + 4, height + 4]
            pixels = np.pad(mask, 2)[y:y + height + 4, x:x + width + 4]
            if name in ("lock", "page_anchor"):
                pixels = cv2.GaussianBlur(pixels, (3, 3), 0)
                operation = "bright_blur"
        elif operation == "yellow":
            pixels = yellow_mask(pixels)[3:25, :18]
            native_crop = [0, 3, 18, 22]
        else:
            alpha = np.array(raw.resize(tuple(size), Image.Resampling.BILINEAR))[:, :, 3]
            x, y, width, height = cv2.boundingRect(cv2.findNonZero(np.where(alpha > 240, 255, 0).astype(np.uint8)))
            native_crop = [x, y, width, height]
            pixels = crop(pixels, native_crop)
            if name == "confirm_enabled":
                pixels = cv2.GaussianBlur(pixels, (3, 3), 0)
                operation = "rgb_blur"
        save(f"{name}.png", pixels)
        if pixels.shape[0] < 10 or pixels.shape[1] < 10 or pixels.std() < 10:
            raise ValueError(f"Degenerate native template: {name}, shape={pixels.shape}")
        templates.append({"name": name, "file": f"{name}.png", "kind": "native_sprite",
                          "sprite": sprite_name, "sprite_path_id": sprite_id, "bundle": rel,
                          "prefab_node": node, "native_sprite_size": list(raw.size),
                          "client_node_size": size, "crop_on_client_sprite": native_crop,
                          "roi": roi, "preprocess": operation, "threshold": 0.85})

    card_node = "/HomeTradeUpgrade/Group_Left/ScrollGrid_Left/Viewport/Grid/Group_Item_000/"
    background_id = component(card_node + "Img_BG", "m_Sprite")["m_Sprite"]["m_PathID"]
    selected_id = component(card_node + "Img_OnMask", "m_Sprite")["m_Sprite"]["m_PathID"]
    card_size = nodes[card_node + "Img_BG"]["rect_client"][2:]
    background = sprites[background_id][0].convert("RGBA").resize(tuple(card_size), Image.Resampling.BILINEAR)
    selected_layer = sprites[selected_id][0].convert("RGBA").resize(tuple(card_size), Image.Resampling.BILINEAR)
    bottom_id = component(card_node + "Img_Bottom", "m_Sprite")["m_Sprite"]["m_PathID"]
    bottom_rect = nodes[card_node + "Img_Bottom"]["rect_client"]
    frame_rect = nodes[card_node + "Img_BG"]["rect_client"]
    bottom = sprites[bottom_id][0].convert("RGBA").resize(tuple(bottom_rect[2:]), Image.Resampling.BILINEAR)
    dx, dy = frame_rect[0] - bottom_rect[0], frame_rect[1] - bottom_rect[1]
    bottom.alpha_composite(background, (dx, dy))
    normal_layer = bottom.crop((dx, dy, dx + card_size[0], dy + card_size[1]))
    panel_node = "/HomeTradeUpgrade/Group_Left/Img_BG"
    panel_id = component(panel_node, "m_Sprite")["m_Sprite"]["m_PathID"]
    panel_rect = nodes[panel_node]["rect_client"]
    panel = sprites[panel_id][0].convert("RGBA").resize(tuple(panel_rect[2:]), Image.Resampling.BILINEAR)
    px, py = frame_rect[0] - panel_rect[0], frame_rect[1] - panel_rect[1]
    panel_patch = panel.crop((px, py, px + card_size[0], py + card_size[1]))
    backdrop_color = component("/HomeTradeUpgrade/Img_Bg", "m_Color")["m_Color"]
    backdrop_rgba = tuple(round(backdrop_color[key] * 255) for key in ("r", "g", "b", "a"))
    panel_patch = Image.alpha_composite(Image.new("RGBA", tuple(card_size), backdrop_rgba), panel_patch)
    normal_layer = Image.alpha_composite(panel_patch, normal_layer)
    lock_id = component(card_node + "Img_lock", "m_Sprite")["m_Sprite"]["m_PathID"]
    lock_layer = sprites[lock_id][0].convert("RGBA").resize(tuple(card_size), Image.Resampling.BILINEAR)
    normal_pixels = sprite_pixels(normal_layer, card_size)
    locked_pixels = sprite_pixels(Image.alpha_composite(normal_layer, lock_layer), card_size)
    selected_pixels = sprite_pixels(Image.alpha_composite(normal_layer, selected_layer), card_size)
    # The hanging hole exposes the backdrop, not the commodity's underlay.
    header_base = Image.alpha_composite(Image.new("RGBA", tuple(card_size), backdrop_rgba), background)
    header_pixels = {"normal": sprite_pixels(header_base, card_size),
                     "locked": sprite_pixels(Image.alpha_composite(header_base, lock_layer), card_size),
                     "selected": sprite_pixels(Image.alpha_composite(header_base, selected_layer), card_size)}
    card_mask = np.zeros((card_size[1], card_size[0]), dtype=np.uint8)
    card_mask[:15] = 255
    card_mask[15:-9, :4] = 255
    card_mask[15:-9, -4:] = 255
    card_mask[-9:] = 255
    card_mask[np.array(background)[:, :, 3] < 220] = 0
    header_box = [47, 0, 22, 14]
    frame_variants = []
    for name, pixels in (("normal", normal_pixels), ("locked", locked_pixels), ("selected", selected_pixels)):
        save(f"cards/{name}.png", cv2.GaussianBlur(pixels, (3, 3), 0))
        save(f"cards/{name}_header.png", cv2.GaussianBlur(crop(header_pixels[name], header_box), (3, 3), 0))
        for width, height in ((115, 129), (114, 129), (115, 128), (114, 128)):
            filename = f"cards/{name}_{width}x{height}.png"
            mask_filename = f"cards/frame_mask_{width}x{height}.png"
            resized = cv2.resize(pixels, (width, height), interpolation=cv2.INTER_AREA)
            save(filename, cv2.GaussianBlur(resized, (3, 3), 0))
            save(mask_filename, cv2.resize(card_mask, (width, height), interpolation=cv2.INTER_NEAREST))
            frame_variants.append({"file": filename, "mask": mask_filename, "size": [width, height], "state": name})
    save("cards/frame_mask.png", card_mask)
    save("cards/header_mask.png", np.full((header_box[3], header_box[2]), 255, dtype=np.uint8))
    cards = {"source": "native_game_assets_only", "bundle": sprites[background_id][2],
             "normal_sprite": sprites[background_id][1], "normal_sprite_id": background_id,
             "selected_sprite": sprites[selected_id][1], "selected_sprite_id": selected_id,
             "size": card_size, "header_box": header_box, "header_mask": "cards/header_mask.png",
             "bottom_sprite_id": bottom_id, "lock_sprite_id": lock_id, "panel_sprite_id": panel_id,
             "backdrop_node": "/HomeTradeUpgrade/Img_Bg", "backdrop_color": list(backdrop_rgba),
             "frame_mask": "cards/frame_mask.png", "variants": ["cards/normal.png", "cards/locked.png", "cards/selected.png"],
             "header_variants": ["cards/normal_header.png", "cards/locked_header.png", "cards/selected_header.png"],
             "frame_variants": frame_variants,
             "viewport": nodes["/HomeTradeUpgrade/Group_Left/ScrollGrid_Left/Viewport"]["rect_client"],
             "header_threshold": 0.88, "frame_threshold": 0.80,
             "method": "TM_CCOEFF_NORMED", "preprocess": "rgb_blur", "fixed_slot_origins_used": False}

    manifest = {"schema_version": 2, "reference_client": [1280, 720], "native_canvas": [1920, 1080],
                "station": "Farstar Bridge", "status": "offline_review_only", "source": "native_game_assets_only",
                "reset_list_to_top": False, "read_investment_amount": False, "use_ocr": False,
                "bundle_sha256": {**provenance, "ui/hometrade/hometradeupgrade.asset": hashlib.sha256(pref_path.read_bytes()).hexdigest(),
                                  "ui/hometrade/hometrade.asset": hashlib.sha256(shop_prefab.read_bytes()).hexdigest(),
                                  "ui/common/tips.asset": hashlib.sha256((assets / "ui/common/tips.asset").read_bytes()).hexdigest()},
                "templates": templates, "levels": levels, "digits": digits, "cards": cards, "level_rois": LEVEL_ROIS,
                "level_matching": {"method": "TM_CCOEFF_NORMED", "threshold": 0.77,
                                   "minimum_margin": 0.02, "require_full_token": True, "canvas": [84, 36],
                                   "digit_verification": True, "digit_threshold": 0.80, "digit_margin": 0.04},
                "rasterization": {"font_cutoff": 180, "foreground_min_rgb": 205,
                                  "foreground_max_chroma_spread": 38, "digit_normalized_size": [20, 32],
                                  "scale": "1920x1080 -> 1280x720; native and client-size glyph variants"},
                "slot_origins": SLOT_ORIGINS, "slot_size": [115, 129],
                "limitations": ["Disabled confirm state and maximum-level page require native state composition",
                                "No live or temporal verification", "Unseen levels generated but not screenshot-validated"]}
    add_max_level(assets, metadata, nodes, manifest)
    add_maximum_cards(assets, metadata, nodes, manifest, normal_pixels, header_pixels["normal"])
    add_entry_availability(assets, metadata, manifest)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"templates": len(templates), "native_levels": len(levels), "font": fonts[level_font_id][1]}))


if __name__ == "__main__":
    main()
