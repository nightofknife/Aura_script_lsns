"""Render player-status digits from the game's SourceHanSansCN-Bold font.

The source font is an extracted game asset and is intentionally not committed.
Run from the repository root with its path as the sole argument. The output
is a normalized binary glyph bank for the 1280x720 PC profile panel.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "plans/resonance_pc/templates/player_data/status_digits"
FONT_SHA256 = "97e5eff6dd208ccb814726458c8c7ab4b59327c62b9ee8df3440e7e835209ab9"
FONT_NAME = "SourceHanSansCN-Bold"
FONT_SIZE = 16
PIXEL_CUTOFF = 120
TEMPLATE_SIZE = (12, 16)
CHARACTERS = "0123456789/"


def render_glyph(font: ImageFont.FreeTypeFont, character: str) -> np.ndarray:
    canvas = Image.new("L", (32, 32), 0)
    ImageDraw.Draw(canvas).text((16, 16), character, font=font, anchor="mm", fill=255)
    raw = np.where(np.asarray(canvas) >= PIXEL_CUTOFF, 255, 0).astype(np.uint8)
    points = np.argwhere(raw > 0)
    if not len(points):
        raise ValueError(f"Empty profile-status glyph: {character!r}")
    top, left = points.min(axis=0)
    bottom, right = points.max(axis=0) + 1
    glyph = raw[top:bottom, left:right]
    width, height = TEMPLATE_SIZE
    if glyph.shape[1] > width or glyph.shape[0] > height:
        raise ValueError(f"Oversized profile-status glyph: {character!r}")
    result = np.zeros((height, width), dtype=np.uint8)
    y = (height - glyph.shape[0]) // 2
    x = (width - glyph.shape[1]) // 2
    result[y:y + glyph.shape[0], x:x + glyph.shape[1]] = glyph
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("font", type=Path, help="Extracted SourceHanSansCN-Bold game font")
    args = parser.parse_args()
    font_hash = hashlib.sha256(args.font.read_bytes()).hexdigest()
    if font_hash != FONT_SHA256:
        parser.error("font does not match the extracted game SourceHanSansCN-Bold asset")

    OUTPUT.mkdir(parents=True, exist_ok=True)
    font = ImageFont.truetype(str(args.font), FONT_SIZE)
    files = {}
    for character in CHARACTERS:
        filename = "slash.png" if character == "/" else f"{character}.png"
        Image.fromarray(render_glyph(font, character), mode="L").save(OUTPUT / filename)
        files[character] = filename

    manifest = {
        "schema_version": 1,
        "reference_client": [1280, 720],
        "source_font": FONT_NAME,
        "font_sha256": font_hash,
        "rendered_font_size": FONT_SIZE,
        "pixel_cutoff": PIXEL_CUTOFF,
        "template_size": list(TEMPLATE_SIZE),
        "matching": "binary centered glyph; classify all 11 characters, not first threshold hit",
        "characters": files,
    }
    (OUTPUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Generated {len(files)} profile-status glyphs at {OUTPUT}")


if __name__ == "__main__":
    main()
