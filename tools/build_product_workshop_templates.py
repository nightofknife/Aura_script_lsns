"""Generate production-editor templates from its exact native font bindings.

Asset generation only: no OCR, screen capture, game input or matching checks.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from _trade_investment_native import configure, load_bundle

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "plans/resonance_pc"
MEDIUM = 6136794397608061395
BOLD = -2237384480076949585
CONTROLS = {
    "quick": ("快速编队", MEDIUM, 26, [25, 460, 190, 80], ["light", "dark"]),
    "apply_marker": ("应用", MEDIUM, 26, [1100, 455, 170, 90], ["light"]),
    "save_message": ("布局已变更，是否保存？", MEDIUM, 34, [240, 260, 800, 200], ["light"]),
    "save_yes": ("确认", BOLD, 34, [642, 468, 631, 74], ["light"]),
    "save_no": ("取消", BOLD, 34, [7, 468, 631, 74], ["light"]),
}


def glyphs(raw, text, size, scale):
    font = ImageFont.truetype(io.BytesIO(raw), size)
    box = font.getbbox(text)
    image = Image.new("L", (box[2]-box[0]+12, box[3]-box[1]+12))
    ImageDraw.Draw(image).text((6-box[0], 6-box[1]), text, font=font, fill=255)
    if scale != 1:
        image = image.resize((round(image.width*scale), round(image.height*scale)), Image.Resampling.BILINEAR)
    image = image.point(lambda value: 255 if value >= 160 else 0)
    return image.crop(image.getbbox()).convert("L")


def build(game_data, pythonlibs):
    configure(pythonlibs, ROOT / "tools")
    assets = game_data / "Patch/Asset"
    bundle = assets / "ui/font/originpack.asset"
    env = load_bundle(bundle, game_data / "il2cpp_data/Metadata/global-metadata.dat")
    fonts = {obj.path_id: bytes(obj.read().m_FontData) for obj in env.objects
             if obj.type.name == "Font" and obj.path_id in {MEDIUM, BOLD}}
    if set(fonts) != {MEDIUM, BOLD} or not all(fonts.values()):
        raise ValueError("Exact production-editor native fonts are required; no system-font fallback")
    output = PLAN / "templates/reforming_center/product_workshop"
    output.mkdir(parents=True, exist_ok=True)
    controls = {}
    for name, (text, font_id, native_size, region, polarity) in CONTROLS.items():
        variants = []
        sizes = [("native", native_size, 2/3),
                 ("client_floor", int(native_size*2/3), 1),
                 ("client_ceil", int(native_size*2/3)+1, 1)]
        for variant, size, scale in sizes:
            image = glyphs(fonts[font_id], text, size, scale)
            destination = output / f"{name}_{variant}.png"
            image.save(destination)
            variants.append({"image": destination.relative_to(PLAN).as_posix(),
                             "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()})
        controls[name] = {"text": text, "font_path_id": font_id, "native_size": native_size,
                          "region": region, "polarity": polarity, "threshold": .88, "variants": variants}
    metadata = {
        "schema_version": 1, "reference_client": [1280, 720],
        "status": "native_generated_unvalidated", "method": "native_font_glyph_template_matching_no_ocr",
        "font_bundle": "ui/font/originpack.asset",
        "font_bundle_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
        "font_sha256": {str(key): hashlib.sha256(value).hexdigest() for key, value in fonts.items()},
        "prefab_sha256": {rel: hashlib.sha256((assets / rel).read_bytes()).hexdigest() for rel in
                           ("ui/prison/manage/prisonedit.asset", "ui/common/prompt/prompt.asset")},
        "controls": controls,
        "limitations": "Generated from exact native fonts, not captured UI states; no matching or live test performed.",
    }
    path = PLAN / "data/meta/product_workshop_templates_pc.json"
    path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"catalog": str(path), "templates": sum(len(c["variants"]) for c in controls.values()),
                      "status": metadata["status"]}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-data", required=True, type=Path)
    parser.add_argument("--pythonlibs", required=True, type=Path)
    args = parser.parse_args()
    build(args.game_data, args.pythonlibs)
