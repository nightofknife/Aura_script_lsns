"""Rebuild the four current main-UI templates from installed native resources.

Run from the repository root. UnityPy, native-resource helpers, and font bytes
are development dependencies; fonts stay in memory. This generator neither
captures the game nor reads existing template pixels or screenshots.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from hashlib import sha256
import io
import json
from pathlib import Path
import sys

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "plans/resonance_pc"
CATALOG = "data/meta/mainui_control_templates.json"
CLIENT_SCALE = 2 / 3
CONTROL_NODES = {
    "depart": "/MainUI/Btn_Launch",
    "train_maintenance": "/MainUI/Group_OutSide/Group_Station/Btn_Build",
    "passenger_management": "/MainUI/Group_OutSide/Group_Station/Btn_HandleG",
    "horn": "/MainUI/Group_OutSide/Group_Running/Btn_Horn",
}


def rooted(root: Path, reference: str) -> Path:
    """Resolve catalog/resource references without escaping their owning root."""
    path = (root / reference).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"Reference escapes its root: {reference}")
    return path


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def opaque_crop(image: Image.Image, bounds: list[int]) -> Image.Image:
    if (len(bounds) != 4 or any(type(value) is not int for value in bounds)
            or not 0 <= bounds[0] < bounds[2] <= image.width
            or not 0 <= bounds[1] < bounds[3] <= image.height):
        raise ValueError(f"Invalid native-render crop: {bounds}")
    crop = image.crop(tuple(bounds)).convert("RGBA")
    if crop.getchannel("A").getextrema() != (255, 255):
        raise ValueError("Crop transparency changed; review the artwork and mask contract")
    # Stable opaque background keeps the CCOEFF templates nonconstant, and
    # avoids depending on scene pixels beyond the native button artwork.
    return crop.convert("RGB")


def build(game_data: Path, catalog_root: Path) -> tuple[dict[str, bytes], dict[str, Image.Image]]:
    from _trade_investment_native import inspect_prefab, load_bundle
    from aura_resonance_devtools.unity_bundle import _logical_sprite_image

    catalog = deepcopy(json.loads(rooted(catalog_root, CATALOG).read_text(encoding="utf-8")))
    controls = catalog["controls"]
    if catalog.get("reference_client") != [1280, 720] or set(controls) != set(CONTROL_NODES):
        raise ValueError("Expected exactly the four current 1280x720 main-UI controls")
    depart_reference = controls["depart"]["catalog"]
    depart = deepcopy(json.loads(rooted(catalog_root, depart_reference).read_text(encoding="utf-8")))
    assets = game_data / "Patch/Asset"
    metadata = game_data / "il2cpp_data/Metadata/global-metadata.dat"
    sprite_path = assets / "ui/mainui.asset"
    prefab_path = assets / "ui/mainui/mainui.asset"
    prefab = inspect_prefab(load_bundle(prefab_path, metadata))
    by_path = {node["path"]: node for node in prefab["nodes"]}
    by_id = {node["transform_id"]: node for node in prefab["nodes"]}
    scale_refs = next(c["tree"]["initScaleTrans"] for c in by_path["/MainUI"]["components"]
                      if "initScaleTrans" in c["tree"])
    scale_targets = {reference["m_PathID"] for reference in scale_refs}
    sprite_env = load_bundle(sprite_path, metadata)
    sprite_objects = {obj.path_id: obj for obj in sprite_env.objects if obj.type.name == "Sprite"}
    source_hashes = {"ui/mainui.asset": digest(sprite_path),
                     "ui/mainui/mainui.asset": digest(prefab_path)}
    font_envs = {}
    outputs: dict[str, bytes] = {}
    images: dict[str, Image.Image] = {}

    for key, node_path in CONTROL_NODES.items():
        node = by_path[node_path]
        if node["transform_id"] not in scale_targets:
            raise ValueError(f"Control no longer belongs to MainUI initScaleTrans: {node_path}")
        chain = []
        ancestor = node
        while ancestor:
            scale = ancestor["transform"]["m_LocalScale"]
            if any(abs(float(scale[axis]) - 1) > 1e-6 for axis in ("x", "y", "z")):
                raise ValueError(f"Review changed prefab scale: {ancestor['path']}")
            chain.append({"path": ancestor["path"], "local_scale": scale})
            ancestor = by_id.get(ancestor["transform"]["m_Father"]["m_PathID"])
        component = next(c["tree"] for c in node["components"] if "m_Sprite" in c["tree"])
        if any(abs(float(component["m_Color"][channel]) - 1) > 1e-6
               for channel in ("r", "g", "b", "a")):
            raise ValueError(f"Review changed native image tint: {node_path}")
        sprite_id = component["m_Sprite"]["m_PathID"]
        sprite_obj = sprite_objects[sprite_id]
        sprite = sprite_obj.read()
        size = node["transform"]["m_SizeDelta"]
        native_size = (round(size["x"]), round(size["y"]))
        native = (_logical_sprite_image(sprite, sprite_obj.version) if key == "depart"
                  else sprite.image).convert("RGBA")
        expected_size = (310, 160) if key == "depart" else (110, 110)
        if native.size != native_size or native_size != expected_size:
            raise ValueError(f"Review changed native dimensions: {node_path} {native.size}/{native_size}")
        render_size = tuple(round(value * CLIENT_SCALE) for value in native_size)
        if key == "depart":
            rendering = depart["rendering"]
            if int(rendering["background_sprite_id"]) != sprite_id:
                raise ValueError("Departure sprite provenance no longer matches the native prefab")
            if rendering["resize"] != list(render_size):
                raise ValueError("Departure recipe must use the current 207x107 native render")
            fonts = []
            for language in ("chinese", "english"):
                spec = rendering[language]
                resource, font_id = spec["font"].rsplit(":", 1)
                font_path = rooted(assets, resource)
                if resource not in font_envs:
                    font_envs[resource] = load_bundle(font_path, metadata)
                obj = next(obj for obj in font_envs[resource].objects if obj.path_id == int(font_id))
                font_source = obj.read()
                font_data = bytes(font_source.m_FontData)
                font = ImageFont.truetype(io.BytesIO(font_data), spec["font_size"])
                draw = ImageDraw.Draw(native)
                left, top, right, bottom = draw.textbbox((0, 0), spec["text"], font=font)
                cx, cy = spec["ink_center"]
                draw.text((cx - (left + right) / 2, cy - (top + bottom) / 2),
                          spec["text"], font=font, fill=(0, 0, 0, 255))
                source_hashes[resource] = digest(font_path)
                fonts.append({"language": language, "resource": resource, "font_id": font_id,
                              "name": font_source.m_Name, "font_size": spec["font_size"],
                              "font_sha256": sha256(font_data).hexdigest(),
                              "bundle_sha256": source_hashes[resource],
                              "rendering": "Pillow native-font ink-centered reconstruction; not Unity rendered pixels"})
            bounds = rendering["crop_bounds"]
            if bounds != [37, 26, 167, 77]:
                raise ValueError("Review a changed departure crop before replacing current artwork")
            entry = depart
            rendering.update(native_size=list(native_size), resize=list(render_size),
                             ui_local_scale=1.0, canvas_scale="720/1080")
            depart["fonts"] = fonts
            depart["source_node"] = node_path
            depart["ancestor_scale_chain"] = chain
            depart["calibration"] = {
                "kind": "native_rebuilt_not_runtime_exercised", "reference_client": [1280, 720],
                "old_scale_compatibility": False,
                "note": "Reconstructed from native resources; no match score claimed for the new artwork.",
            }
        else:
            entry = controls[key]
            source = entry["source"]
            if (source["source_node"] != node_path or source["source_sprite_path_id"] != sprite_id
                    or source["native_control_size"] != list(native_size)):
                raise ValueError(f"Control provenance no longer matches the native prefab: {key}")
            bounds = source["crop_bounds_xyxy"]
            source.update(source_sprite_name=sprite.m_Name, client_control_size=list(render_size),
                          ancestor_scale_chain=chain)
            entry["rendering"].update(canvas_scale="720/1080", ui_local_scale=1.0,
                                      label_policy="No text or scenery reconstructed; native central icon only")
        image = opaque_crop(native.resize(render_size, Image.Resampling.BILINEAR), bounds)
        encoded = png_bytes(image)
        reference = entry["template"]
        if not reference.startswith("templates/"):
            raise ValueError(f"Template output must stay under templates/: {reference}")
        entry.update(size=list(image.size), sha256=sha256(encoded).hexdigest(), mode="RGB")
        # The existing entry['match'] dictionary is intentionally untouched.
        outputs[reference] = encoded
        images[key] = image

    depart["source_hashes"] = dict(source_hashes)
    catalog["source_hashes"] = dict(source_hashes)
    for reference, value in ((depart_reference, depart), (CATALOG, catalog)):
        outputs[reference] = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return outputs, images


def save_preview(images: dict[str, Image.Image], path: Path) -> None:
    zoom = 4
    row_height = max(image.height for image in images.values()) * zoom + 35
    width = max(image.width for image in images.values()) * zoom + 24
    preview = Image.new("RGB", (width, row_height * len(images) + 12), "#eeeeee")
    draw = ImageDraw.Draw(preview)
    for index, (key, image) in enumerate(images.items()):
        top = 8 + index * row_height
        draw.text((12, top), f"{key}: {image.width} x {image.height}", fill="#222222")
        preview.paste(image.resize((image.width * zoom, image.height * zoom),
                                   Image.Resampling.NEAREST), (12, top + 22))
    path.parent.mkdir(parents=True, exist_ok=True)
    preview.save(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-data", type=Path, required=True, help="Installed game *_Data directory")
    parser.add_argument("--pythonlibs", type=Path, required=True, help="Development dependency directory containing UnityPy")
    parser.add_argument("--devtools-src", type=Path, required=True, help="Directory containing aura_resonance_devtools")
    parser.add_argument("--catalog-root", type=Path, default=PLAN, help="Plan root supplying the current JSON recipes and matching configuration")
    parser.add_argument("--output", type=Path, default=PLAN, help="Destination plan root; writes four PNGs and two metadata catalogs")
    parser.add_argument("--preview", type=Path, help="Optional new-artwork-only preview image path")
    args = parser.parse_args()
    if Path.cwd().resolve() != ROOT:
        parser.error("Run from the repository root")
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(args.pythonlibs.resolve()), str(args.devtools_src.resolve()), str(ROOT / "tools")]
    outputs, images = build(args.game_data.resolve(), args.catalog_root.resolve())
    destinations = [(rooted(args.output, reference), content) for reference, content in outputs.items()]
    for destination, content in destinations:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    if args.preview:
        save_preview(images, args.preview.resolve())
    print(f"Rebuilt four current main-UI templates and metadata under {args.output.resolve()}")


if __name__ == "__main__":
    main()
