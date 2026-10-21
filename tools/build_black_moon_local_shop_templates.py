"""Manufacture Black Moon local-store templates from native Unity assets only."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import io
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from _trade_investment_native import configure, inspect_prefab, load_bundle


ROOT = Path(__file__).resolve().parents[1]
PLAN_ROOT = ROOT / "plans/resonance_pc"
OUTPUT = PLAN_ROOT / "templates/commerce/shop/black_moon/native"
PREFABS = ("ui/home/barstore/barstore.asset",)
LIBRARIES = ("ui/common.asset", "ui/home.asset", "ui/home/barstore.asset", "ui/store.asset",
             "ui/hometrade.asset", "ui/hometrade/update.asset", "ui/font.asset", "ui/font/originpack.asset")
STATUS = "native_generated_unvalidated"
_STAGED_OUTPUT = ContextVar("black_moon_native_staged_output", default=None)
PREPARE_NAMES = ("shop_entry", "shop_page", "local_tab_selected", "local_tab_unselected", "batch_on", "batch_off")
PREPARE_TAB_RECT = [996, 91, 160, 29]
PREPARE_BATCH_RECT = [1171, 88, 96, 33]
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


def _output_path(path):
    """Redirect producer writes to staging without changing manifest references."""
    path = Path(path).resolve()
    if not path.is_relative_to(OUTPUT.resolve()):
        raise ValueError(f"Output outside native bank: {path}")
    staged = _STAGED_OUTPUT.get()
    return staged / path.relative_to(OUTPUT.resolve()) if staged is not None else path


def _resource_path(reference):
    path = (PLAN_ROOT / reference).resolve()
    if Path(reference).is_absolute() or not path.is_relative_to((PLAN_ROOT / "templates").resolve()):
        raise ValueError(f"Resource outside plan templates: {reference}")
    if _STAGED_OUTPUT.get() is not None and path.is_relative_to(OUTPUT.resolve()):
        candidate = _output_path(path)
        if candidate.is_file():
            return candidate
    return path


@contextmanager
def staged_generation():
    # Keep temporary output in this checkout, independent of OS temp settings.
    temporary_root = ROOT / ".pytest_tmp/black-moon-native-generation"
    temporary_root.mkdir(parents=True, exist_ok=True)
    if not temporary_root.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError("Native staging directory escapes repository")
    with TemporaryDirectory(prefix="bank-", dir=temporary_root) as directory:
        stage = Path(directory)
        token = _STAGED_OUTPUT.set(stage)
        try:
            yield stage
        finally:
            _STAGED_OUTPUT.reset(token)


def save_image(file, image):
    if "/" not in file:
        file = "controls/" + file
    reference = OUTPUT / file
    path = _output_path(reference)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return reference.relative_to(PLAN_ROOT).as_posix()


def resource_files(manifest):
    """Hash declared resources only; neighbouring functions do not belong to this bank."""
    cards = manifest.get("cards", {})
    entries = list(manifest["templates"])
    entries.extend(cards.get("items", []))
    references = {row[key] for row in entries for key in ("file", "mask_file") if row.get(key)}
    files = {}
    for reference in sorted(references):
        path = _resource_path(reference)
        files[reference] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return files


def obsolete_resources(previous, current):
    """Resolve the producer's old outputs without enrolling neighbouring banks."""
    roots = (OUTPUT.resolve(), (PLAN_ROOT / "templates/commerce/shop/black_moon/cards").resolve())
    retired_identity_mask = (PLAN_ROOT / "templates/commerce/shop/black_moon/masks/item_identity_120x119.png").resolve()
    for reference in set(previous.get("files", {})) - set(current.get("files", {})):
        path = (PLAN_ROOT / reference).resolve()
        if Path(reference).is_absolute() or not (any(path.is_relative_to(root) for root in roots)
                                                or path == retired_identity_mask):
            continue
        if path.is_file():
            yield path


def cleanup_previous_resources(previous, current):
    """Remove only resources previously declared by this producer, after success."""
    for path in obsolete_resources(previous, current):
        path.unlink()


def publish_generation(stage, previous, manifest):
    """Publish validated files and retire old outputs as one rollback scope."""
    staged = sorted((path for path in stage.rglob("*") if path.is_file()),
                    key=lambda path: (path.name == "manifest.json", path.as_posix()))
    replacements = [(path, OUTPUT / path.relative_to(stage)) for path in staged]
    retired = list(obsolete_resources(previous, manifest))
    targets = [*(target for _, target in replacements), *retired]
    # Read every backup before the first mutation. A failed backup is harmless.
    backups = {path: path.read_bytes() if path.exists() else None for path in targets}
    try:
        for source, target in replacements:
            target.parent.mkdir(parents=True, exist_ok=True)
            source.replace(target)
        cleanup_previous_resources(previous, manifest)
    except BaseException:
        for path, content in reversed(tuple(backups.items())):
            if content is None:
                path.unlink(missing_ok=True)
            elif not path.is_file() or path.read_bytes() != content:
                # An unchanged read-only target may have caused publication to
                # fail. Do not rewrite it and interrupt the remaining rollback.
                path.write_bytes(content)
        raise


def catalog_products(path):
    catalog = json.loads(path.read_text(encoding="utf-8"))
    products = catalog.get("products", [])
    if isinstance(products, dict):
        products = [{"item_id": item_id, **product} for item_id, product in products.items()]
    if not isinstance(products, list) or not products:
        raise ValueError("Native generation requires a nonempty product catalog")
    ids = []
    for product in products:
        item_id = product.get("item_id") if isinstance(product, dict) else None
        if not isinstance(item_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", item_id):
            raise ValueError(f"Invalid catalog item ID: {item_id!r}")
        ids.append(item_id)
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate catalog item IDs")
    return products


def validate_generation(manifest, expected_ids, *, prepare_only=False):
    from PIL import Image

    if manifest.get("blockers"):
        raise ValueError(f"Native generation blockers: {manifest['blockers']}")
    controls = manifest["templates"]
    if (len(controls) != len(PREPARE_NAMES)
            or {row["name"] for row in controls} != set(PREPARE_NAMES)):
        raise ValueError("Native preparation controls are incomplete")
    items = manifest.get("cards", {}).get("items", [])
    if (not expected_ids or len(expected_ids) != len(set(expected_ids))
            or len(items) != len(expected_ids) or {row["item_id"] for row in items} != set(expected_ids)
            or any(row.get("state") != "normal" or "mask_file" in row for row in items)):
        raise ValueError("Native product coverage is incomplete")

    def dimensions(rect, label):
        if (not isinstance(rect, (list, tuple)) or len(rect) != 4
                or any(type(value) is not int for value in rect)
                or min(rect[:2]) < 0 or min(rect[2:]) <= 0):
            raise ValueError(f"Invalid native {label} geometry")
        return tuple(rect[2:])

    item_size = dimensions(manifest.get("cards", {}).get("icon_rect"), "item crop")
    image_owners, mask_owners = {}, {}
    for row in [*controls, *items]:
        is_control = "item_id" not in row
        identity = row["name"] if is_control else row["item_id"]
        size = (dimensions(row.get("source", {}).get("crop"), f"control {identity} crop")
                if is_control else item_size)
        if not is_control and row.get("source", {}).get("icon_rect") != manifest["cards"]["icon_rect"]:
            raise ValueError(f"Native item crop metadata disagrees: {identity}")
        path = _resource_path(row["file"]).resolve()
        if path in image_owners:
            raise ValueError(f"Duplicate native image output: {identity} and {image_owners[path]}")
        image_owners[path] = identity
        with Image.open(path) as image:
            image.load()
            if image.mode != "RGB" or image.size != size:
                raise ValueError(f"Invalid native image size/mode: {identity}")
        if row.get("mask_file"):
            mask_path = _resource_path(row["mask_file"]).resolve()
            mask_owners.setdefault(mask_path, []).append(identity)
            with Image.open(mask_path) as mask:
                mask.load()
                if mask.mode != "L" or mask.size != size or mask.getbbox() is None:
                    raise ValueError(f"Invalid native mask size/mode/content: {identity}")
    if set(image_owners) & set(mask_owners):
        raise ValueError("Native image output cannot also be a mask")
    for identities in mask_owners.values():
        if len(identities) > 1 and set(identities) != {"local_tab_selected", "local_tab_unselected"}:
            raise ValueError(f"Unexpected shared native mask: {identities}")
    generated = set()
    for row in [*controls, *([] if prepare_only else items)]:
        for key in ("file", "mask_file"):
            if row.get(key):
                path = _output_path(PLAN_ROOT / row[key])
                if not path.is_file():
                    raise ValueError(f"Generated resource missing from staging: {row[key]}")
                generated.add(path)
    if generated != set(_STAGED_OUTPUT.get().rglob("*.png")):
        raise ValueError("Unexpected resources in native staging bank")
    for reference in manifest["files"]:
        with Image.open(_resource_path(reference)) as image:
            image.verify()
    if manifest["files"] != resource_files(manifest):
        raise ValueError("Generated resource hashes do not match")


def share_prepare_masks(entries):
    """Share tab geometry only when both generated masks have equal pixels."""
    from PIL import Image

    tabs = [row for row in entries if row["name"].startswith("local_tab_")]
    if len(tabs) != 2:
        return
    paths = [_resource_path(row["mask_file"]) for row in tabs]
    with Image.open(paths[0]) as left, Image.open(paths[1]) as right:
        equal = (left.mode, left.size, left.tobytes()) == (right.mode, right.size, right.tobytes())
        shared = save_image("local_tab.mask.png", left) if equal else None
    if shared:
        for row, path in zip(tabs, paths):
            row["mask_file"] = shared
            if path.resolve() != _resource_path(shared).resolve():
                path.unlink()


def manufacture_prepare_controls(native):
    """Bounded control-only recipe calibrated on the four supplied client frames."""
    from PIL import Image, ImageDraw

    entries = []

    def compose(paths, background=None):
        canvas = background.copy() if background is not None else Image.new("RGBA", (1280, 720))
        sources = [native.layer(canvas, node) for path in paths for node in native.nodes(path)]
        return canvas, sources

    def entry(name, canvas, rect, sources, mask=None, opaque=False):
        image = canvas.crop(box(rect))
        row = {"name": name, "file": save_image(f"{name}.png", image.convert("RGB")),
               "roi": [max(0, rect[0] - 8), max(0, rect[1] - 8),
                       min(1280, rect[0] + rect[2] + 8) - max(0, rect[0] - 8),
                       min(720, rect[1] + rect[3] + 8) - max(0, rect[1] - 8)],
               "preprocess": "rgb", "method": "1-TM_SQDIFF_NORMED", "threshold": .90,
               "status": "supplied_screenshots_validated_no_live_clicks",
               "source": {"prefab": STORE_PREFAB, "nodes": sources, "crop": rect,
                          "position_tolerance": 8, "opaque_matching": opaque}}
        if not opaque:
            mask = mask.crop(box(rect)) if mask is not None else image.getchannel("A").point(lambda value: 255 if value >= 240 else 0)
            row["mask_file"] = save_image(f"{name}.mask.png", mask)
        if name.startswith(("local_tab", "batch_")):
            row["color_mae_limit"] = 24
        entries.append(row)

    parent = "/BarStore/Group_Main/Btn_Store"
    canvas, sources = compose([parent, parent + "/Img_", parent + "/Txt_"])
    entry("shop_entry", canvas, [742, 386, 201, 42], sources, opaque=True)
    panel, panel_sources = compose([STORE + "/Img_BG"])
    canvas, sources = compose([STORE + "/Img_Icon"], panel)
    entry("shop_page", canvas, native.node(STORE + "/Img_Icon")["rect_client"], panel_sources + sources)
    for state, group in (("selected", "Group_On/Img_1"), ("unselected", "Group_Off")):
        prefix = STORE + "/Group_Tab/Group_Local/" + group
        paths = [STORE + "/Group_Tab/Img_"]
        if state == "selected":
            paths.append(prefix)
        canvas, sources = compose(paths + [prefix + "/Img_Select"], panel)
        mask = Image.new("L", (1280, 720))
        ImageDraw.Draw(mask).rectangle(box(PREPARE_TAB_RECT), fill=255)
        ImageDraw.Draw(mask).rectangle((1064, 91, 1156, 122), fill=0)
        entry("local_tab_" + state, canvas, PREPARE_TAB_RECT, panel_sources + sources, mask)
    for state in ("on", "off"):
        prefix = STORE + "/Btn_PL/Group_" + state.title()
        canvas, sources = compose([prefix + "/Txt_", prefix + "/Img_"])
        entry("batch_" + state, canvas, PREPARE_BATCH_RECT, sources)
    share_prepare_masks(entries)
    return entries


def update_prepare_controls(assets, metadata, records):
    previous = json.loads((OUTPUT / "manifest.json").read_text(encoding="utf-8"))
    expected_ids = [row["item_id"] for row in normal_item_catalog(previous.get("cards", {}))["items"]]
    with staged_generation() as stage:
        manifest = _update_prepare_controls(assets, metadata, records)
        validate_generation(manifest, expected_ids, prepare_only=True)
        publish_generation(stage, previous, manifest)
    print(json.dumps({"mode": "prepare_only", "templates": list(PREPARE_NAMES)}))


def _update_prepare_controls(assets, metadata, records):
    manifest_path = OUTPUT / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    native = Native(assets, metadata, records)
    entries = manufacture_prepare_controls(native)
    manifest["templates"] = entries
    manifest["cards"] = normal_item_catalog(manifest.get("cards", {}))
    if "catalog" in manifest:
        manifest["catalog"]["manufactured"] = len(manifest["cards"]["items"])
    for group, rect in (("local_tab", PREPARE_TAB_RECT), ("batch", PREPARE_BATCH_RECT)):
        manifest["state_comparisons"][group]["roi"] = list(rect)
    manifest["prepare_controls"] = {"names": list(PREPARE_NAMES), "threshold": .90,
        "position_tolerance": 8, "reference_client": [1280, 720],
        "validation": "four supplied screenshots, 24 positive/negative control checks; no live input",
        "fixture": "tests/fixtures/black_moon_prepare/manifest.json",
        "source_prefab_sha256": records[STORE_PREFAB]["bundle_sha256"]}
    note = "Only six preparation controls have four real-frame ROI checks; stock, purchase and live transitions remain unvalidated."
    if note not in manifest["limitations"]:
        manifest["limitations"].append(note)
    manifest["files"] = resource_files(manifest)
    if getattr(native, "blockers", []):
        raise ValueError(f"Native generation blockers: {native.blockers}")
    write_json(manifest_path, manifest)
    return manifest


def normal_item_catalog(cards):
    """Keep native artwork inputs used by the calibrated item producer only."""
    result = {key: cards[key] for key in ("icon_rect", "native_icon_rect", "quality", "catalog_products")
              if key in cards}
    result["items"] = [{key: value for key, value in row.items() if key != "mask_file"}
                       for row in cards.get("items", []) if row["state"] == "normal"]
    return result


def manufacturing(assets, metadata, records, catalog_path):
    products = catalog_products(catalog_path)
    previous_path = OUTPUT / "manifest.json"
    previous = json.loads(previous_path.read_text(encoding="utf-8")) if previous_path.is_file() else {}
    with staged_generation() as stage:
        manifest = _manufacturing(assets, metadata, records, catalog_path)
        validate_generation(manifest, [row["item_id"] for row in products])
        for name in ("native_ui.json", "native_assets.json"):
            if not (stage / "sources" / name).is_file():
                raise ValueError(f"Native provenance missing from staging: {name}")
        publish_generation(stage, previous, manifest)
    cards = manifest["cards"]
    print(json.dumps({"templates": len(manifest["templates"]), "items": len(cards["items"]),
                      "icon_rect": cards["icon_rect"], "native_icon_rect": cards["native_icon_rect"],
                      "blockers": manifest["blockers"]}, ensure_ascii=False))
    return manifest


def _manufacturing(assets, metadata, records, catalog_path):
    from PIL import Image

    native = Native(assets, metadata, records)
    manifest = {"schema_version": 2, "reference_client": [1280, 720], "status": STATUS,
                "state_minimum_margin": .035,
                "templates": manufacture_prepare_controls(native), "source": {"prefab": STORE_PREFAB,
                "prefab_sha256": records[STORE_PREFAB]["bundle_sha256"],
                "layout": "1920x1080 native RectTransforms composed at 1280x720; not inventory geometry"},
                "limitations": ["No tests, matching, GUI, game or screenshot validation performed",
                                 "Thresholds are manufacturing defaults, not calibrated scores",
                                 "Native font memory rendered by Pillow, not Unity's text rasterizer"],
                "blockers": native.blockers}

    # The visible core and native icon crop are unchanged. Geometry is a local
    # manufacturing input; the calibrated scanner owns runtime card recognition.
    frame_node = native.node(CARD)
    logical_origin = frame_node["rect_client"][:2]
    logical_frame = Image.new("RGBA", tuple(frame_node["rect_client"][2:]))
    native.layer(logical_frame, native.node(CARD + "/Group_Item/Btn_Item"), logical_origin)
    core_bounds = logical_frame.getchannel("A").point(lambda value: 255 if value >= 128 else 0).getbbox()
    inset = list(core_bounds[:2])
    origin = [logical_origin[i] + inset[i] for i in (0, 1)]
    size = [core_bounds[2] - core_bounds[0], core_bounds[3] - core_bounds[1]]
    frame_base = logical_frame.crop(core_bounds)
    native_icon_rect = relative_rect(native.node(CARD + "/Group_Item/Btn_Item/Img_ItemBG/Img_Item")["rect_client"], origin)
    icon_rect = [*native_icon_rect[:2], min(native_icon_rect[2], size[0] - native_icon_rect[0]),
                 min(native_icon_rect[3], size[1] - native_icon_rect[1])]
    cards = {"items": [], "icon_rect": icon_rect, "native_icon_rect": native_icon_rect}
    manifest["cards"] = cards
    build_items(native, catalog_path, cards, frame_base, origin)
    manifest["catalog"] = {"path": catalog_path.relative_to(ROOT).as_posix() if catalog_path.is_relative_to(ROOT) else str(catalog_path),
                           "present": catalog_path.exists(), "manufactured": len(cards["items"])}
    if catalog_path.exists():
        manifest["catalog"]["sha256"] = hashlib.sha256(catalog_path.read_bytes()).hexdigest()
    manifest["source"]["bundle_sha256"] = native.hashes
    manifest["source"]["prefab_hashes"] = {path: record["bundle_sha256"] for path, record in records.items()}
    manifest["state_comparisons"] = {
        "local_tab": {"on": "local_tab_selected", "off": "local_tab_unselected", "roi": PREPARE_TAB_RECT,
                      "preprocess": "rgb", "method": "1-TM_SQDIFF_NORMED", "minimum_margin": .035,
                      "color_mae_limit": 24, "ties": "unconfirmed; no purchase"},
        "batch": {"on": "batch_on", "off": "batch_off", "roi": PREPARE_BATCH_RECT,
                  "preprocess": "rgb", "method": "1-TM_SQDIFF_NORMED", "minimum_margin": .035,
                  "color_mae_limit": 24, "ties": "unconfirmed"}}
    manifest["files"] = resource_files(manifest)
    write_json(OUTPUT / "manifest.json", manifest)
    evidence(native)
    return manifest


def build_items(native, catalog_path, cards, frame_base, origin):
    from PIL import Image
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
            source = {"catalog_product": product, "icon": native.provenance(key, obj),
                      "quality_background": native.provenance(background_key, background_obj),
                      "quality_border": native.provenance(border_key, border_obj),
                      "prefab": STORE_PREFAB, "icon_node": CARD + "/Group_Item/Btn_Item/Img_ItemBG/Img_Item",
                      "icon_rect": icon_rect, "native_icon_rect": native_icon_rect,
                      "background_rect": bg_rect, "border_rect": border_rect}
            file = save_image(f"items/{item_id}_normal.png", canvas.crop(box(icon_rect)).convert("RGB"))
            cards["items"].append({"item_id": item_id, "name": product["name"], "quality": quality_name,
                                   "quality_number": quality, "state": "normal",
                                   "category": product["category"], "file": file,
                                   "threshold": .86, "preprocess": "rgb", "status": STATUS, "source": source})
            cards["quality"][str(quality)] = {"background": source["quality_background"], "border": source["quality_border"]}
        except (KeyError, ValueError, FileNotFoundError) as exc:
            native.blockers.append({"kind": "item_extraction", "item_id": item_id,
                                    "icon_resource_url": product.get("icon_resource_url"), "reason": str(exc)})


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
    path = _output_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def extract(assets: Path, metadata: Path, prefabs=PREFABS) -> dict:
    records = {}
    for relative in prefabs:
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
    parser.add_argument("--prepare-only", action="store_true", help="Regenerate only the six calibrated preparation controls")
    args = parser.parse_args()
    configure(args.pythonlibs, args.devtools_src)
    assets = args.game_data / "Patch/Asset"
    metadata = args.game_data / "il2cpp_data/Metadata/global-metadata.dat"
    if args.prepare_only:
        records = extract(assets, metadata, prefabs=(STORE_PREFAB,))
        update_prepare_controls(assets, metadata, records)
        return
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
