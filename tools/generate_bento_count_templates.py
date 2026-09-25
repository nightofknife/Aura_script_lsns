"""Render the 0..12 fatigue-page bento badge glyphs from the game's UI font.

Example (from the repository root):
  python tools/generate_bento_count_templates.py path/to/HarmonyOS_Sans_Bold.ttf

The game asset specifies HarmonyOS_Sans_Bold, size 23 on a 1920x1080
canvas. At Aura's 1280x720 reference resolution that is about 15 pixels.
The font file is supplied locally and is not included in the repository.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


EXPECTED_FONT_SHA256 = "7f973862c42353c9cc372dc2ae891d12c9ea5fe2a01b449adaf1eade9b469b47"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("font", type=Path)
    args = parser.parse_args()
    digest = hashlib.sha256(args.font.read_bytes()).hexdigest()
    if digest != EXPECTED_FONT_SHA256:
        parser.error("font is not the HarmonyOS_Sans_Bold asset used for these templates")
    output = Path(__file__).resolve().parents[1] / "plans/resonance_pc/templates/player_recovery/bento_count_digits"
    output.mkdir(parents=True, exist_ok=True)
    font = ImageFont.truetype(str(args.font), 15)
    for count in range(13):
        image = Image.new("L", (24, 22), 0)
        draw = ImageDraw.Draw(image)
        draw.text((12, 11), str(count), font=font, anchor="mm", fill=255)
        image = image.point(lambda pixel: 255 if pixel >= 110 else 0)
        image.save(output / f"{count}.png")


if __name__ == "__main__":
    main()
