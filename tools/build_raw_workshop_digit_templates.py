"""Extract the game's actual UI font and render individual glyph candidates.

No system-font fallback, OCR engine, game input, or whole-fraction templates.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "plans/resonance_pc"
FONT_ID = 6136794397608061395
SELECTION_FONT_ID = -3758921232364580136


def load_native_font(game_data: Path):
    sys.path.insert(0, str(ROOT / ".pytest_tmp/cuberogue_internal/unitypy"))
    sys.path.insert(0, "D:/python_project/Aura_script_lsns_devtools/src")
    from aura_resonance_devtools.unity_bundle import load_bundle
    bundle = game_data / "Patch/Asset/ui/font/originpack.asset"
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        env = load_bundle(bundle, game_data / "il2cpp_data/Metadata/global-metadata.dat")
    for obj in env.objects:
        if obj.type.name == "Font" and obj.path_id == FONT_ID:
            raw = bytes(obj.read().m_FontData)
            if not raw:
                raise ValueError("Native font has no embedded bytes")
            return raw, bundle
    raise ValueError("The exact font bound to the workshop counter was not found")


def render_glyph(raw, character, size, downscale=1):
    font = ImageFont.truetype(io.BytesIO(raw), size)
    box = font.getbbox(character)
    image = Image.new("L", (box[2]-box[0]+8, box[3]-box[1]+8))
    ImageDraw.Draw(image).text((4-box[0], 4-box[1]), character, font=font, fill=255)
    if downscale != 1:
        image = image.resize((round(image.width*downscale), round(image.height*downscale)), Image.Resampling.LANCZOS)
    return image


def build(game_data, preview_dir, font_export=None, selection_font_export=None):
    preview_dir = preview_dir.resolve()
    if not preview_dir.is_relative_to(ROOT) or preview_dir == ROOT:
        raise ValueError("Preview must stay in a dedicated repository directory")
    # Both prefab-bound families form one complete runtime bank. Validate and
    # render every input before touching the current bank or preview artifacts.
    if selection_font_export is None:
        raise ValueError("Selection font export is required to rebuild the complete workshop digit bank")
    if selection_font_export.stem != str(SELECTION_FONT_ID):
        raise ValueError("Selection font must match its native prefab binding")
    selection_raw = selection_font_export.read_bytes()
    if not selection_raw:
        raise ValueError("Selection font has no embedded bytes")
    if font_export is not None:
        if font_export.stem != str(FONT_ID):
            raise ValueError("Export must match the exact prefab-bound native Font ID")
        raw = font_export.read_bytes()
        provenance = {"font_export": str(font_export.resolve()),
                      "source_note": "Previously extracted native game font; not freshly extracted in this run"}
    else:
        raw, bundle = load_native_font(game_data)
        provenance = {"font_bundle": str(bundle), "font_bundle_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest()}
    if not raw:
        raise ValueError("Counter font has no embedded bytes")
    output = PLAN / "templates/workshop/production/digits"
    variants = [("native26_scaled", 26, 2/3), ("direct17", 17, 1),
                ("direct18", 18, 1), ("direct20", 20, 1)]
    families = [("list", raw, variants),
                ("selection", selection_raw, [("native32_scaled", 32, 2/3),
                                               ("direct21", 21, 1), ("direct22", 22, 1)])]
    provenance["selection_font_export"] = str(selection_font_export.resolve())
    provenance["selection_font_sha256"] = hashlib.sha256(selection_raw).hexdigest()
    rendered = {}
    assets = []
    for style, native_raw, rows in families:
        for variant, size, scale in rows:
            for character in "0123456789/":
                name = f"{style}_{variant}_{'slash' if character == '/' else character}.png"
                image = render_glyph(native_raw, character, size, scale)
                path = output / name
                rendered[name] = image
                assets.append({"character": character, "style": style, "variant": variant,
                               "image": path.relative_to(PLAN).as_posix(),
                               "size": list(image.size), "font_size": size, "scale": scale})
    metadata = {"schema_version": 1, "method": "Individual glyphs from exact native Font bytes",
                "font_path_id": FONT_ID, "native_ui_font_size": 26,
                "font_sha256": hashlib.sha256(raw).hexdigest(),
                **provenance,
                "alphabet": "0123456789/", "assets": assets,
                "note": "Candidates; synthetic glyph rendering is not live screenshot evidence"}
    catalog = PLAN / "data/meta/raw_workshop_digits_pc.json"
    preview_rows = [(style, variant) for style, _, rows in families for variant, _, _ in rows]
    canvas = Image.new("RGB", (990, 56+92*len(preview_rows)), "#152232")
    draw = ImageDraw.Draw(canvas)
    draw.text((20, 14), "NATIVE GAME FONT - 0..9 AND SLASH", fill="white")
    for row, (style, variant) in enumerate(preview_rows):
        draw.text((20, 50+row*92), style+" / "+variant, fill="#aacbdf")
        for column, character in enumerate("0123456789/"):
            item = next(a for a in assets if a["style"] == style and a["variant"] == variant and a["character"] == character)
            glyph = rendered[Path(item["image"]).name].convert("RGB")
            glyph = glyph.resize((glyph.width*3, glyph.height*3), Image.Resampling.NEAREST)
            canvas.paste(glyph, (170+column*72, 38+row*92))
    preview = preview_dir / "digit_templates.png"
    # Retire only previously cataloged outputs within this producer's own bank.
    # A changed input must not retain an obsolete variant alongside the new bank.
    old_files = set()
    if catalog.exists():
        previous = json.loads(catalog.read_text(encoding="utf-8"))
        for entry in previous["assets"]:
            old = (PLAN / entry["image"]).resolve()
            if old.parent != output.resolve() or old.suffix != ".png":
                raise ValueError("Previous digit catalog contains an output outside the owned bank")
            old_files.add(old)
    preview_dir.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    for name, image in rendered.items():
        image.save(output / name)
    for old in old_files - {(output / name).resolve() for name in rendered}:
        old.unlink(missing_ok=True)
    (preview_dir / "native_counter_font.ttf").write_bytes(raw)
    (preview_dir / "native_selection_font.ttf").write_bytes(selection_raw)
    canvas.save(preview)
    catalog.write_text(json.dumps(metadata, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    return {"glyph_assets": len(assets), "font_path_id": FONT_ID,
            "font_sha256": metadata["font_sha256"], "catalog": str(catalog), "preview": str(preview)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-data", type=Path, required=True)
    parser.add_argument("--preview-dir", type=Path, required=True)
    parser.add_argument("--font-export", type=Path, help="An existing export of the exact native prefab-bound Font ID")
    parser.add_argument("--selection-font-export", type=Path, required=True, help="Required exact selection-footer native font export; a partial bank is rejected")
    args = parser.parse_args()
    print(json.dumps(build(args.game_data, args.preview_dir, args.font_export, args.selection_font_export), ensure_ascii=False, indent=2))
