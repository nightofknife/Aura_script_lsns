"""Manufacture Black Moon local-store templates from native Unity assets only."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

from _trade_investment_native import configure, inspect_prefab, load_bundle


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "plans/resonance_pc/templates/black_moon_local_shop"
PREFABS = ("ui/home/barstore/barstore.asset", "ui/common/showitem.asset", "ui/mainui/mainui.asset")
LIBRARIES = ("ui/common.asset", "ui/home.asset", "ui/home/barstore.asset", "ui/store.asset",
             "ui/hometrade.asset", "ui/hometrade/update.asset", "ui/font.asset", "ui/font/originpack.asset",
             "ui/mainui.asset")
STATUS = "native_generated_unvalidated"
STORE_PREFAB = "ui/home/barstore/barstore.asset"
STORE = "/BarStore/Group_LocalStore/Group_StoreList"
CARD = STORE + "/ScrollGrid_Commodity/Viewport/Grid/Group_Item_000"
SCROLL_FIELDS = ("m_Padding", "m_CellSize", "m_Spacing", "m_Constraint", "m_ConstraintCount",
                 "m_ChildAlignment", "m_StartCorner", "m_StartAxis", "m_Content", "m_Viewport",
                 "m_Vertical", "m_Horizontal", "m_MovementType", "m_Inertia", "m_Elasticity",
                 "m_DecelerationRate", "m_ScrollSensitivity", "m_VerticalScrollbar",
                 "m_HorizontalScrollbar", "m_VerticalScrollbarVisibility", "m_HorizontalScrollbarVisibility",
                 "m_VerticalScrollbarSpacing", "m_HorizontalScrollbarSpacing",
                 "m_HorizontalFit", "m_VerticalFit", "Padding", "CellSize", "Spacing",
                 "ScrollDirect", "StartCorner", "IsRevert", "IsSort", "DataCount", "Cells", "Cell")


def box(rect):
    x, y, w, h = rect
    return x, y, x + w, y + h


def relative_rect(rect, origin):
    return [rect[0] - origin[0], rect[1] - origin[1], *rect[2:]]


class Native:
    """Keep native sprites and fonts in memory; persist only provenance and PNGs."""

    def __init__(self, assets, metadata, records):
        from PIL import Image

        self.Image = Image
        self.assets, self.metadata, self.records = assets, metadata, records
        self.objects, self.names, self.hashes, self.images = {}, {}, {}, {}
        self.used_nodes, self.used_objects = {}, {}
        self.bundle_cache = {}
        self.blockers = []
        for relative in LIBRARIES:
            self.library(relative)
        from aura_resonance_devtools.binary_config import PooledBinaryConfig

        self.text_path = assets.parent / "BinaryConfig/TextFactory.bin"
        self.text_pool = PooledBinaryConfig.load(self.text_path)
        self.text_hash = hashlib.sha256(self.text_path.read_bytes()).hexdigest()

    def library(self, relative):
        if relative not in self.bundle_cache:
            path = self.assets / relative
            env = load_bundle(path, self.metadata)
            self.bundle_cache[relative] = env
            self.hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
            for obj in env.objects:
                if obj.type.name in ("Sprite", "Font"):
                    item = obj.read()
                    key = (relative, obj.path_id)
                    self.objects[key] = obj
                    self.names[(relative, item.m_Name.casefold())] = key
        return self.bundle_cache[relative]

    def object(self, pid):
        matches = [key for key in self.objects if key[1] == pid]
        if not matches:
            # Resolve only the requested external sprite, without retaining unrelated atlases.
            for path in sorted((self.assets / "ui").glob("*.asset")):
                relative = path.relative_to(self.assets).as_posix()
                if relative in self.bundle_cache:
                    continue
                env = load_bundle(path, self.metadata)
                found = [obj for obj in env.objects if obj.path_id == pid and obj.type.name in ("Sprite", "Font")]
                if found:
                    self.library(relative)
                    matches = [(relative, pid)]
                    break
        if len(matches) != 1:
            raise ValueError(f"Native object {pid}: expected one source, found {len(matches)}")
        return matches[0], self.objects[matches[0]]

    def named(self, relative, name):
        self.library(relative)
        key = self.names[(relative, name.casefold())]
        return key, self.objects[key]

    def provenance(self, key, obj):
        source = {"bundle": key[0], "bundle_sha256": self.hashes[key[0]],
                  "path_id": obj.path_id, "name": obj.read().m_Name, "type": obj.type.name}
        self.used_objects[f"{key[0]}:{key[1]}"] = source
        return source

    def image(self, key, obj):
        from aura_resonance_devtools.unity_bundle import _logical_sprite_image

        if key not in self.images:
            self.images[key] = _logical_sprite_image(obj.read(), obj.version).convert("RGBA")
        return self.images[key].copy()

    def nodes(self, path, prefab=STORE_PREFAB):
        result = [node for node in self.records[prefab]["nodes"] if node["path"] == path]
        for node in result:
            self.used_nodes[f"{prefab}:{node['transform_id']}"] = (prefab, node)
        return result

    def node(self, path, prefab=STORE_PREFAB):
        nodes = self.nodes(path, prefab)
        if len(nodes) != 1:
            raise ValueError(f"Ambiguous native node {path}; use transform ID for repeated sibling names")
        return nodes[0]

    @staticmethod
    def component(node, field):
        return next(c["tree"] for c in node["components"] if field in c["tree"])

    def sprite(self, node):
        tree = self.component(node, "m_Sprite")
        key, obj = self.object(tree["m_Sprite"]["m_PathID"])
        image = self.image(key, obj)
        w, h = node["rect_client"][2:]
        if not w or not h:
            raise ValueError(f"Unresolved automatic layout on {node['path']}")
        if tree.get("m_Type", 0) != 0:
            raise ValueError(f"Unsupported non-simple native Image on {node['path']}")
        if tree.get("m_PreserveAspect"):
            ratio = min(w / image.width, h / image.height)
            resized = image.resize((round(image.width * ratio), round(image.height * ratio)), self.Image.Resampling.BILINEAR)
            image = self.Image.new("RGBA", (w, h))
            image.alpha_composite(resized, ((w - resized.width) // 2, (h - resized.height) // 2))
        else:
            image = image.resize((w, h), self.Image.Resampling.BILINEAR)
        tint = tree["m_Color"]
        image = self.Image.merge("RGBA", tuple(channel.point(
            lambda value, factor=tint[channel_name]: round(value * factor))
            for channel, channel_name in zip(image.split(), ("r", "g", "b", "a"))))
        source = self.provenance(key, obj)
        source.update({"node": node["path"], "transform_id": node["transform_id"],
                       "rect": node["rect_client"], "native_tint": tint,
                       "native_image_type": tree.get("m_Type", 0)})
        return image, source

    def text(self, node):
        from PIL import ImageDraw, ImageFont

        tree = self.component(node, "m_FontData")
        style = tree["m_FontData"]
        key, obj = self.object(style["m_Font"]["m_PathID"])
        font_bytes = bytes(obj.read().m_FontData)
        w, h = node["rect_client"][2:]
        native_w, native_h = round(w * 1.5), round(h * 1.5)
        size = style["m_FontSize"]
        text = tree["m_Text"]
        localized = {STORE + "/Txt_": "\u9ed1\u6708\u5546\u5e97"}
        translation = None
        if node["path"] in localized:
            text = localized[node["path"]]
            pool_offset = self.text_pool.string_offset(text)
            text = self.text_pool.strings[pool_offset]
            translation = {"file": "Patch/BinaryConfig/TextFactory.bin", "sha256": self.text_hash,
                           "pool_offset": pool_offset, "prefab_text": tree["m_Text"],
                           "selection": "exact native string observed in user-provided static screenshot; not an ID join"}
        font = ImageFont.truetype(io.BytesIO(font_bytes), size)
        if style.get("m_BestFit"):
            while size > max(1, style.get("m_MinSize", 1)) and font.getlength(text) > native_w:
                size -= 1
                font = ImageFont.truetype(io.BytesIO(font_bytes), size)
        left, top, right, bottom = font.getbbox(text)
        glyph = self.Image.new("L", (max(1, right - left + 4), max(1, bottom - top + 4)))
        ImageDraw.Draw(glyph).text((2 - left, 2 - top), text, font=font, fill=255,
                                  stroke_width=int(style.get("m_FontStyle", 0) in (1, 3)))
        glyph = glyph.resize((round(glyph.width * 2 / 3), round(glyph.height * 2 / 3)), self.Image.Resampling.BILINEAR)
        alignment = style.get("m_Alignment", 4)
        x = (0, (w - glyph.width) // 2, w - glyph.width)[alignment % 3]
        y = (0, (h - glyph.height) // 2, h - glyph.height)[alignment // 3]
        tint = tree["m_Color"]
        rgba = self.Image.new("RGBA", glyph.size, tuple(round(tint[k] * 255) for k in ("r", "g", "b")) + (0,))
        rgba.putalpha(glyph.point(lambda value: round(value * tint["a"])))
        image = self.Image.new("RGBA", (w, h))
        image.alpha_composite(rgba, (x, y))
        source = self.provenance(key, obj)
        source.update({"node": node["path"], "transform_id": node["transform_id"],
                       "rect": node["rect_client"], "text": text, "native_font_size": style["m_FontSize"],
                       "rendered_native_font_size": size, "font_sha256": hashlib.sha256(font_bytes).hexdigest(),
                       "native_style": style, "native_tint": tint,
                       "font_renderer": "Pillow native font memory; Unity rasterization unvalidated"})
        if translation:
            source["localization"] = translation
        return image, source

    def layer(self, canvas, node, origin=(0, 0)):
        if any("m_Sprite" in c["tree"] for c in node["components"]):
            image, source = self.sprite(node)
        else:
            image, source = self.text(node)
        canvas.alpha_composite(image, (node["rect_client"][0] - origin[0], node["rect_client"][1] - origin[1]))
        return source


def save_image(file, image):
    path = OUTPUT / file
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return file


def manufacturing(assets, metadata, records, catalog_path):
    from PIL import Image, ImageDraw

    native = Native(assets, metadata, records)
    manifest = {"schema_version": 1, "reference_client": [1280, 720], "status": STATUS,
                "state_minimum_margin": .035,
                "templates": [], "source": {"prefab": STORE_PREFAB,
                "prefab_sha256": records[STORE_PREFAB]["bundle_sha256"],
                "layout": "1920x1080 native RectTransforms composed at 1280x720; not inventory geometry"},
                "limitations": ["No tests, matching, GUI, game or screenshot validation performed",
                                 "Thresholds are manufacturing defaults, not calibrated scores",
                                 "Native font memory rendered by Pillow, not Unity's text rasterizer"],
                "blockers": native.blockers}

    def entry(name, canvas, rect, roi=None, sources=None, threshold=.86, mask=None, **extra):
        image = canvas.crop(box(rect))
        file = save_image(f"{name}.png", image.convert("RGB"))
        if mask is None:
            mask = image.getchannel("A").point(lambda v: 255 if v >= 240 else 0)
        else:
            mask = mask.crop(box(rect))
        row = {"name": name, "file": file, "mask_file": save_image(f"{name}.mask.png", mask),
               "roi": roi or rect, "preprocess": "rgb", "method": "1-TM_SQDIFF_NORMED",
               "threshold": threshold, "status": STATUS,
               "source": {"prefab": STORE_PREFAB, "nodes": sources or [], "crop": rect}, **extra}
        manifest["templates"].append(row)
        return row

    def compose(paths, prefab=STORE_PREFAB, background=None):
        canvas = background.copy() if background is not None else Image.new("RGBA", (1280, 720))
        sources = []
        for path in paths:
            for node in native.nodes(path, prefab):
                sources.append(native.layer(canvas, node))
        return canvas, sources

    # Invariant menu and page navigation, with dynamic NPC/currency regions absent.
    for name, parent, rect in (
        ("rest_menu", "/BarStore/Group_Main/Btn_Talk", [742, 470, 201, 41]),
        ("shop_entry", "/BarStore/Group_Main/Btn_Store", [742, 386, 201, 42]),
    ):
        canvas, sources = compose([parent, parent + "/Img_", parent + "/Txt_"])
        entry(name, canvas, rect, sources=sources, threshold=.88)
    for name, path in (("page_back", "/BarStore/Group_CommonTopLeft/Btn_Return"),
                       ("page_home", "/BarStore/Group_CommonTopLeft/Btn_Home")):
        node = native.node(path)
        canvas, sources = compose([path])
        entry(name, canvas, node["rect_client"], sources=sources, threshold=.9)
    main_prefab = "ui/mainui/mainui.asset"
    canvas, sources = compose(["/MainUI/Btn_Map", "/MainUI/Btn_Map/Img_P", "/MainUI/Btn_Map/Txt_Name"], main_prefab)
    row = entry("main_nav", canvas, [1083, 402, 150, 48], sources=sources)
    row["source"]["prefab"] = main_prefab

    panel, panel_sources = compose([STORE + "/Img_BG"])
    canvas, sources = compose([STORE + "/Img_Icon", STORE + "/Txt_"], background=panel)
    entry("shop_page", canvas, [604, 74, 212, 54], sources=panel_sources + sources, threshold=.87)

    tab_rect = [990, 85, 181, 44]
    for state, group in (("selected", "Group_On/Img_1"), ("unselected", "Group_Off")):
        paths = [STORE + "/Group_Tab/Img_"]
        prefix = STORE + "/Group_Tab/Group_Local/" + group
        if state == "selected":
            paths.append(prefix)
        paths += [prefix + "/Img_Select"]
        canvas, sources = compose(paths, background=panel)
        tab_mask = Image.new("L", (1280, 720))
        ImageDraw.Draw(tab_mask).rectangle(box(tab_rect), fill=255)
        ImageDraw.Draw(tab_mask).rectangle((1064, 94, 1150, 122), fill=0)
        entry("local_tab_" + state, canvas, tab_rect, sources=panel_sources + sources,
              threshold=.88, mask=tab_mask, color_mae_limit=24, comparison_group="local_tab",
              same_position=True, minimum_state_margin=.035,
              text_excluded="Serialized tab text is outdated; current localization absent from native TextFactory string pool")
    batch_rect = [1171, 88, 96, 33]
    for state in ("on", "off"):
        prefix = STORE + "/Btn_PL/Group_" + state.title()
        canvas, sources = compose([prefix + "/Txt_", prefix + "/Img_"])
        entry("batch_" + state, canvas, batch_rect, sources=sources,
              threshold=.88, color_mae_limit=24, comparison_group="batch",
              same_position=True, minimum_state_margin=.035)
    canvas, sources = compose([STORE + "/Group_PL/Img_", STORE + "/Group_PL/Btn_Buy",
                               STORE + "/Group_PL/Btn_Buy/Txt_"], background=panel)
    entry("confirm_enabled", canvas, [1139, 651, 120, 36], sources=panel_sources + sources,
          threshold=.9, color_mae_limit=24)

    reward_prefab = "ui/common/showitem.asset"
    canvas, sources = compose(["/ShowItem/Group_580/Group_Title/Img_Title",
                               "/ShowItem/Group_580/Group_Title/Txt_Title"], reward_prefab)
    row = entry("reward", canvas, [523, 93, 234, 67], roi=[480, 0, 320, 320], sources=sources)
    row["source"]["prefab"] = reward_prefab

    frame_node = native.node(CARD)
    logical_origin = frame_node["rect_client"][:2]
    logical_size = frame_node["rect_client"][2:]
    logical_frame = Image.new("RGBA", tuple(logical_size))
    frame_sources = [native.layer(logical_frame, native.node(CARD + "/Group_Item/Btn_Item"), logical_origin)]
    alpha = logical_frame.getchannel("A")
    artwork_bounds = alpha.getbbox()
    core_bounds = alpha.point(lambda value: 255 if value >= 128 else 0).getbbox()
    inset = list(core_bounds[:2])
    origin = [logical_origin[i] + inset[i] for i in (0, 1)]
    size = [core_bounds[2] - core_bounds[0], core_bounds[3] - core_bounds[1]]
    frame_base = logical_frame.crop(core_bounds)
    raw_viewport = native.node(STORE + "/ScrollGrid_Commodity/Viewport")["rect_client"]
    viewport = [*raw_viewport[:3], 643 - raw_viewport[1]]
    native_icon_rect = relative_rect(native.node(CARD + "/Group_Item/Btn_Item/Img_ItemBG/Img_Item")["rect_client"], origin)
    icon_rect = [*native_icon_rect[:2], min(native_icon_rect[2], size[0] - native_icon_rect[0]),
                 min(native_icon_rect[3], size[1] - native_icon_rect[1])]
    selected_roi = relative_rect([217, 4, 115, 27], inset)
    sold_out_roi = relative_rect([117, 29, 107, 65], inset)
    logical_columns = [native.node(STORE + "/ScrollGrid_Commodity/Viewport/Grid/Group_Item_00" + str(i))["rect_client"][0] for i in (0, 1)]
    columns = [x + inset[0] for x in logical_columns]
    cards = {"viewport": viewport, "raw_native_viewport": raw_viewport, "size": size, "columns": columns,
             "column_offsets": [x - viewport[0] for x in columns],
             "icon_rect": icon_rect, "native_icon_rect": native_icon_rect,
             "selected_roi": selected_roi, "sold_out_roi": sold_out_roi,
             "anchors": [], "frame_variants": [], "items": [], "minimum_item_margin": .04,
             "fingerprint_box": [32, 32, size[0] - 44, size[1] - 40],
             "fingerprint_exclude": [selected_roi], "status": STATUS,
             "row_pitch_native": 196, "row_pitch_client": 196 * 2 / 3,
             "fixed_slot_mapping": False, "partial_cards": "recover origin from top/bottom anchor offset; constrain x to columns",
             "viewport_batch_occlusion": [592, 643, 679, 52],
             "source": {"prefab": STORE_PREFAB, "card_node": CARD,
                        "card_transform_id": frame_node["transform_id"], "viewport_node": STORE + "/ScrollGrid_Commodity/Viewport"}}
    manifest["cards"] = cards
    cards["scroll_layout"] = scroll_layout(native, raw_viewport, viewport, logical_size, inset, size)
    cards["artwork_alpha_bbox"] = relative_rect(
        [artwork_bounds[0], artwork_bounds[1], artwork_bounds[2] - artwork_bounds[0], artwork_bounds[3] - artwork_bounds[1]], inset)
    cards["artwork_rect"] = [0, 0, *size]
    cards["artwork_core_rect"] = cards["artwork_rect"].copy()
    cards["origin"] = origin.copy()
    cards["logical_size"] = logical_size
    cards["logical_origin"] = logical_origin
    cards["logical_columns"] = logical_columns
    cards["native_artwork_rect"] = [*inset, *size]
    cards["logical_to_artwork_offset"] = inset
    cards["artwork_source"] = {"nodes": frame_sources, "alpha_threshold": 128,
                               "full_alpha_bbox_threshold": 1, "coordinates": "card-relative",
                               "partial_policy": "require translated artwork_rect fully inside effective viewport; low-alpha fringe outside visible core is excluded",
                               "limitation": "Visible-core threshold is explicit manufacturing policy, not screenshot-calibrated; nonzero alpha fringe extends across full logical rectangle"}
    cards["frames_determine_state"] = False
    cards["state_evidence"] = {"selected": "selected", "sold_out": "sold_out",
                               "policy": "markers only; native dark-overlay frame appearances can be identical"}
    logical_excluded = [
        [20, 0, 139, 130],  # Quality/icon/name overlap and pack count.
        [141, 23, 178, 49], [130, 57, 193, 56], [217, 4, 115, 27],
        [263, 0, 74, 27],  # Dynamic residue count protrudes across the card edge.
    ]
    excluded = [relative_rect(rect, inset) for rect in logical_excluded]
    frame_mask = Image.new("L", tuple(size), 255)
    draw = ImageDraw.Draw(frame_mask)
    for rect in excluded:
        x, y, w, h = rect
        draw.rectangle((x, y, x + w - 1, y + h - 1), fill=0)
    import numpy as np

    frame_mask = Image.fromarray(np.minimum(np.asarray(frame_mask), np.asarray(frame_base.getchannel("A"))))
    selected_frame_mask = frame_mask.copy()
    selected_excluded = [[0, 0, 25, size[1]], [size[0] - 130, 0, 130, 32],
                         [0, 0, size[0], 4], [0, size[1] - 4, size[0], 4],
                         [0, 0, 4, size[1]], [size[0] - 4, 0, 4, size[1]]]
    selected_draw = ImageDraw.Draw(selected_frame_mask)
    for x, y, w, h in selected_excluded:
        selected_draw.rectangle((x, y, x + w - 1, y + h - 1), fill=0)
    frames = {"normal": frame_base}
    state_sources = {"normal": frame_sources}
    for state, suffix in (("selected", None),
                          ("soldout", "/Group_Item/Btn_Item/Img_Sold")):
        canvas = frame_base.copy()
        sources = frame_sources.copy()
        if state == "selected":
            sources.append(native.layer(canvas, native.node(CARD + "/Group_Item/Btn_Item/Img_Sold"), origin))
        if suffix:
            for node in native.nodes(CARD + suffix):
                sources.append(native.layer(canvas, node, origin))
        frames[state] = canvas
        state_sources[state] = sources
    for state, image in frames.items():
        file = save_image(f"cards/{state}.png", image.convert("RGB"))
        geometry_mask = selected_frame_mask if state == "selected" else frame_mask
        mask_file = save_image(f"cards/{state}.mask.png", geometry_mask)
        appearance = "normal" if state == "normal" else "native_dark_overlay"
        variant = {"file": file, "mask_file": mask_file, "appearance": appearance,
                   "role": "geometry_only", "preprocess": "rgb",
                   "threshold": .86, "status": STATUS, "source": state_sources[state]}
        cards["frame_variants"].append(variant)
        anchor_rows = (("top", 6, 12), ("bottom", size[1] - 10, 6)) if state == "selected" else (
            ("top", 0, 12), ("bottom", size[1] - 12, 12))
        for edge, y, height in anchor_rows:
            offset = [0, y]
            rect = [*offset, size[0], height]
            cards["anchors"].append({"file": save_image(f"cards/{state}_{edge}.png", image.crop(box(rect)).convert("RGB")),
                                     "mask_file": save_image(f"cards/{state}_{edge}.mask.png", geometry_mask.crop(box(rect))),
                                     "offset": offset, "threshold": .86, "preprocess": "rgb",
                                     "appearance": appearance, "role": "geometry_only",
                                     "status": STATUS, "source": {"frame": file, "crop": rect}})
    cards["frame_exclude"] = excluded
    cards["selected_frame_exclude"] = selected_excluded
    cards["selected_darkening"] = {"source_node": CARD + "/Group_Item/Btn_Item/Img_Sold",
                                    "sprite_id": 8735325811154869955,
                                    "kind": "native_dark_overlay_reuse",
                                    "limitation": "Darkening observed statically; selected runtime alpha/tint unavailable in serialized prefab. Native sold dark overlay reused, not calibrated."}
    manifest["limitations"].append(cards["selected_darkening"]["limitation"])
    native.blockers.append({"kind": "selected_state_sprite_unavailable", "sprite_id": 5556224950374819097,
                            "prefab_node": CARD + "/Group_Select/Img_",
                            "reason": "Serialized selected banner reference absent in current UI atlas; serialized full-card sprite di_select is tab-style, not the observed yellow frame",
                            "fallback": "Native-font dark selected label and native dark-overlay frame/item variants; yellow border/banner not manufactured"})
    manifest["limitations"].append("Local tab text omitted/masked: serialized label is outdated and current label absent from native TextFactory string pool")

    selected = Image.new("RGBA", (1280, 720))
    selected_sources = []
    for path in (CARD + "/Group_Select/Txt_",):
        for node in native.nodes(path):
            selected_sources.append(native.layer(selected, node))
    text_rect = native.node(CARD + "/Group_Select/Txt_")["rect_client"]
    row = entry("selected", selected, text_rect, roi=viewport, sources=selected_sources,
                threshold=.86, card_relative_roi=selected_roi)
    glyph_alpha = selected.crop(box(text_rect)).getchannel("A")
    save_image(row["file"], glyph_alpha.point(lambda value: 255 if value >= 128 else 0))
    save_image(row["mask_file"], Image.new("L", tuple(text_rect[2:]), 255))
    row["preprocess"] = "dark"
    row["source"]["prepared_pixels"] = {"mode": "L", "foreground": 255, "background": 0,
                                         "native_font_alpha_threshold": 128,
                                         "mask": "full text rectangle, including negative background evidence"}
    sold, sold_sources = compose([CARD + "/Group_Item/Btn_Item/Img_Sold",
                                 CARD + "/Group_Item/Btn_Item/Img_Sold/Img_",
                                 CARD + "/Group_Item/Btn_Item/Img_Sold/Txt_"])
    entry("sold_out", sold, [origin[0] + sold_out_roi[0], origin[1] + sold_out_roi[1], *sold_out_roi[2:]],
          roi=viewport, sources=sold_sources, threshold=.86, card_relative_roi=sold_out_roi)
    build_items(native, catalog_path, cards, frame_base, origin)
    manifest["catalog"] = {"path": catalog_path.relative_to(ROOT).as_posix() if catalog_path.is_relative_to(ROOT) else str(catalog_path),
                           "present": catalog_path.exists(), "manufactured": len(cards["items"])}
    if catalog_path.exists():
        manifest["catalog"]["sha256"] = hashlib.sha256(catalog_path.read_bytes()).hexdigest()
    manifest["source"]["bundle_sha256"] = native.hashes
    manifest["source"]["prefab_hashes"] = {path: record["bundle_sha256"] for path, record in records.items()}
    manifest["state_comparisons"] = {
        "local_tab": {"on": "local_tab_selected", "off": "local_tab_unselected", "roi": tab_rect,
                      "preprocess": "rgb", "method": "1-TM_SQDIFF_NORMED", "minimum_margin": .035,
                      "color_mae_limit": 24, "ties": "unconfirmed; no purchase"},
        "batch": {"on": "batch_on", "off": "batch_off", "roi": batch_rect,
                  "preprocess": "rgb", "method": "1-TM_SQDIFF_NORMED", "minimum_margin": .035,
                  "color_mae_limit": 24, "ties": "unconfirmed"}}
    manifest["files"] = {path.relative_to(OUTPUT).as_posix(): {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                         for path in sorted(OUTPUT.rglob("*.png"))}
    write_json(OUTPUT / "manifest.json", manifest)
    evidence(native)
    print(json.dumps({"templates": len(manifest["templates"]), "items": len(cards["items"]),
                      "cards": {k: cards[k] for k in ("viewport", "size", "columns", "icon_rect", "artwork_rect", "artwork_core_rect")},
                      "scroll_layout": cards["scroll_layout"], "blockers": native.blockers}, ensure_ascii=False))
    return manifest


def build_items(native, catalog_path, cards, frame_base, origin):
    from PIL import Image, ImageDraw
    from aura_resonance_devtools.item_catalog import QUALITY_NUMBERS

    if not catalog_path.exists():
        native.blockers.append({"kind": "catalog_absent", "path": str(catalog_path),
                                "resolution": "Rerun this builder after canonical catalog arrives; never infer slot-to-product mappings"})
        return
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    products = catalog.get("products", [])
    if isinstance(products, dict):
        products = [{"item_id": item_id, **product} for item_id, product in products.items()]
    cards["catalog_products"] = len(products)
    cards["quality"] = {}
    bg_node = native.node(CARD + "/Group_Item/Btn_Item/Img_ItemBG")
    border_node = native.node(CARD + "/Group_Item/Btn_Item/Img_Mask")
    icon_rect = cards["icon_rect"]
    native_icon_rect = cards["native_icon_rect"]
    for product in products:
        item_id = product["item_id"]
        try:
            quality_name = product["quality"]
            quality = QUALITY_NUMBERS[quality_name]
            if quality not in range(1, 6):
                raise ValueError(f"Unsupported native quality {quality}")
            resource = product["icon_resource_url"].replace("\\", "/").strip("/")
            parts = resource.split("/")
            if parts[0].casefold() in ("asset", "assets"):
                parts = parts[1:]
            relative = "/".join(parts[:-1]).lower() + ".asset"
            key, obj = native.named(relative, parts[-1])
            icon = native.image(key, obj).resize(tuple(native_icon_rect[2:]), Image.Resampling.BILINEAR)
            background_key, background_obj = native.named("ui/common.asset", f"common_bottom_rarity{quality:02}")
            border_key, border_obj = native.named("ui/common.asset", f"common_mask_rarity{quality:02}")
            canvas = frame_base.copy()
            bg_rect = relative_rect(bg_node["rect_client"], origin)
            border_rect = relative_rect(border_node["rect_client"], origin)
            background = native.image(background_key, background_obj).resize(tuple(bg_rect[2:]), Image.Resampling.BILINEAR)
            border = native.image(border_key, border_obj).resize(tuple(border_rect[2:]), Image.Resampling.BILINEAR)
            canvas.alpha_composite(background, tuple(bg_rect[:2]))
            canvas.alpha_composite(icon, tuple(native_icon_rect[:2]))
            canvas.alpha_composite(border, tuple(border_rect[:2]))
            image = canvas.crop(box(icon_rect)).convert("RGB")
            mask = Image.new("L", image.size, 255)
            draw = ImageDraw.Draw(mask)
            draw.rectangle((0, 0, 32, 27), fill=0)  # Optional equipment type marker.
            draw.rectangle((11, 81, image.width - 1, image.height - 1), fill=0)  # Dynamic pack quantity.
            source = {"catalog_product": product, "icon": native.provenance(key, obj),
                      "quality_background": native.provenance(background_key, background_obj),
                      "quality_border": native.provenance(border_key, border_obj),
                      "prefab": STORE_PREFAB, "icon_node": CARD + "/Group_Item/Btn_Item/Img_ItemBG/Img_Item",
                      "icon_rect": icon_rect, "native_icon_rect": native_icon_rect,
                      "background_rect": bg_rect, "border_rect": border_rect}
            selected = canvas.copy()
            dark_source = native.layer(selected, native.node(CARD + "/Group_Item/Btn_Item/Img_Sold"), origin)
            for state, state_canvas in (("normal", canvas), ("selected", selected)):
                file = save_image(f"items/{item_id}_{state}.png", state_canvas.crop(box(icon_rect)).convert("RGB"))
                mask_file = save_image(f"items/{item_id}_{state}.mask.png", mask)
                state_source = {**source, "selected_overlay": dark_source} if state == "selected" else source
                cards["items"].append({"item_id": item_id, "name": product["name"], "quality": quality_name,
                                       "quality_number": quality, "state": state,
                                       "category": product["category"], "file": file, "mask_file": mask_file,
                                       "threshold": .86, "preprocess": "rgb", "status": STATUS, "source": state_source})
            cards["quality"][str(quality)] = {"background": source["quality_background"], "border": source["quality_border"]}
        except (KeyError, ValueError, FileNotFoundError) as exc:
            native.blockers.append({"kind": "item_extraction", "item_id": item_id,
                                    "icon_resource_url": product.get("icon_resource_url"), "reason": str(exc)})


def scroll_layout(native, raw_viewport, effective_viewport, logical_size, inset, artwork_size):
    nodes = []
    for suffix in ("", "/Viewport", "/Viewport/Grid"):
        node = native.node(STORE + "/ScrollGrid_Commodity" + suffix)
        components = []
        for component in node["components"]:
            tree = component["tree"]
            fields = {key: tree[key] for key in SCROLL_FIELDS if key in tree}
            references = {key: value for key, value in tree.items()
                          if isinstance(value, dict) and "m_PathID" in value
                          and key not in ("m_GameObject", "m_Script", "m_Material", "m_Sprite")}
            components.append({"path_id": component["path_id"], "fields": fields,
                               "references": references, "serialized_field_names": sorted(tree)})
        nodes.append({"node": node["path"], "transform_id": node["transform_id"],
                      "game_object_id": node["game_object_id"], "rect_native": node["rect_native"],
                      "rect_client": node["rect_client"], "components": components})
    padding_explanation = {"status": "serialized_bottom_padding_absent"}
    for node in nodes:
        for component in node["components"]:
            fields = component["fields"]
            padding = fields.get("Padding", fields.get("m_Padding"))
            if padding is None:
                continue
            bottom_native = padding["m_Bottom"]
            bottom_client = bottom_native * 2 / 3
            fringe = logical_size[1] - inset[1] - artwork_size[1]
            padding_explanation = {
                "status": "conditional_source_explanation_not_runtime_proof",
                "source_node": node["node"], "source_component_id": component["path_id"],
                "bottom_padding_native": bottom_native, "bottom_padding_client": bottom_client,
                "rounded_logical_card_bottom_fringe_client": fringe,
                "conditional_final_art_bottom_client": raw_viewport[1] + raw_viewport[3] - bottom_client - fringe,
                "assumption": "Runtime custom grid applies serialized bottom padding and aligns content bottom to raw viewport bottom. Actual runtime content height/scroll-end alignment is not serialized."}
    return {"source": {"prefab": STORE_PREFAB, "sha256": native.records[STORE_PREFAB]["bundle_sha256"]},
            "native_to_client_scale": 2 / 3, "nodes": nodes,
            "raw_viewport_bottom_client": raw_viewport[1] + raw_viewport[3],
            "effective_viewport_bottom_client": effective_viewport[1] + effective_viewport[3],
            "runtime_scroll_end_fit": "not_established_by_serialized_geometry",
            "padding_explanation": padding_explanation,
            "limitation": "Serialized content geometry/references and padding alone do not establish runtime content height, scroll-end position, or toolbar-driven viewport changes. Static screenshot final art ending at643 is user-provided evidence, not inferred runtime configuration. Safe footer remains643."}


def evidence(native):
    fields = ("m_Sprite", "m_Color", "m_Type", "m_PreserveAspect", "m_FontData", "m_Text",
              "m_Colors", "m_Interactable", "m_Alpha", "_txtId", *SCROLL_FIELDS)
    rows = []
    for prefab, node in native.used_nodes.values():
        rows.append({"prefab": prefab, "path": node["path"], "transform_id": node["transform_id"],
                     "active": node["active"], "rect_native": node["rect_native"], "rect_client": node["rect_client"],
                     "transform": {key: node["transform"][key] for key in
                                   ("m_AnchorMin", "m_AnchorMax", "m_Pivot", "m_AnchoredPosition", "m_SizeDelta", "m_LocalScale")},
                     "components": [{"path_id": component["path_id"],
                                     **{key: component["tree"][key] for key in fields if key in component["tree"]}}
                                    for component in node["components"] if any(key in component["tree"] for key in fields)]})
    write_json(OUTPUT / "sources/native_ui.json", {"reference_client": [1280, 720], "status": STATUS,
              "prefabs": {path: {"sha256": record["bundle_sha256"],
                                  "excluded_non_ui_transform_count": len(record.get("excluded_transform_ids", []))}
                          for path, record in native.records.items()}, "nodes": rows})
    write_json(OUTPUT / "sources/native_assets.json", {"bundle_hashes": native.hashes, "objects": list(native.used_objects.values())})


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def extract(assets: Path, metadata: Path) -> dict:
    records = {}
    for relative in PREFABS:
        path = assets / relative
        if not path.exists():
            continue
        env = load_bundle(path, metadata)
        try:
            record = inspect_prefab(env)
        except KeyError as exc:
            transforms = {obj.path_id: obj.read_typetree() for obj in env.objects if obj.type.name == "RectTransform"}
            excluded = set()
            for pid, tree in transforms.items():
                parent = tree["m_Father"]["m_PathID"]
                while parent in transforms:
                    parent = transforms[parent]["m_Father"]["m_PathID"]
                if parent:
                    excluded.add(pid)
            record = inspect_prefab(SimpleNamespace(
                objects=[obj for obj in env.objects if obj.path_id not in excluded], assets=env.assets))
            record["excluded_transform_ids"] = sorted(excluded)
            record["extraction_blocker"] = f"Excluded non-UI transform branches with non-RectTransform parent: {exc}"
        record["bundle"] = relative
        record["bundle_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        record["native_objects"] = [
            {"path_id": obj.path_id, "type": obj.type.name,
             "name": obj.read().m_Name}
            for obj in env.objects if obj.type.name in ("Sprite", "Font")
        ]
        records[relative] = record
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-data", type=Path, default=Path("D:/software/soli/Resonance/雷索纳斯_Data"))
    parser.add_argument("--pythonlibs", type=Path, default=Path("D:/project/aura_s_lsns/.codex_tmp/item_quality_research/pythonlibs"))
    parser.add_argument("--devtools-src", type=Path, default=Path("D:/project/Aura_script_lsns_devtools/src"))
    parser.add_argument("--catalog", type=Path, default=ROOT / "plans/resonance_pc/data/meta/black_moon_local_shop_catalog.json")
    parser.add_argument("--extract-only", action="store_true")
    args = parser.parse_args()
    configure(args.pythonlibs, args.devtools_src)
    assets = args.game_data / "Patch/Asset"
    metadata = args.game_data / "il2cpp_data/Metadata/global-metadata.dat"
    records = extract(assets, metadata)
    if args.extract_only:
        paths = (CARD, STORE + "/ScrollGrid_Commodity/Viewport",
                 CARD + "/Group_Item/Btn_Item/Img_ItemBG/Img_Item", STORE + "/Btn_PL", STORE + "/Group_PL/Btn_Buy")
        native = Native(assets, metadata, records)
        for path in paths:
            node = native.node(path)
            print(json.dumps({"node": path, "rect_client": node["rect_client"]}))
        evidence(native)
    else:
        manufacturing(assets, metadata, records, args.catalog.resolve())
    for relative in ("sources/native_prefabs.json", "native_extraction.log"):
        obsolete = OUTPUT / relative
        if obsolete.exists():
            obsolete.unlink()


if __name__ == "__main__":
    main()
