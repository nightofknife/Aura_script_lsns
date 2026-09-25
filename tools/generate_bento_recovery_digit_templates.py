"""Build digit-only bento recovery templates from the game's original font.

Run from the repository root with the extracted HarmonyOS_Sans_Bold font as
the argument. Number-only screenshot samples under tests/fixtures are bundled
as additional glyph variants; neither card icons nor minus signs are included.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "plans/resonance_pc/templates/bento_consumption/recovery_digits"
SAMPLES = ROOT / "tests/fixtures/bento_recovery_digits"
FONT_SHA256 = "7f973862c42353c9cc372dc2ae891d12c9ea5fe2a01b449adaf1eade9b469b47"
STYLES = {"work": {"font_size": 15, "cutoff": 140},
          "love": {"font_size": 17, "cutoff": 80}}
TEMPLATE_SIZE = (12, 16)


def normalize(raw: np.ndarray) -> np.ndarray:
    positions = np.argwhere(raw > 0)
    if not len(positions):
        raise ValueError("empty digit glyph")
    top, left = positions.min(axis=0)
    bottom, right = positions.max(axis=0) + 1
    glyph = raw[top:bottom, left:right]
    width, height = TEMPLATE_SIZE
    if glyph.shape[1] > width or glyph.shape[0] > height:
        raise ValueError("digit glyph exceeds template canvas")
    output = np.zeros((height, width), dtype=np.uint8)
    y = (height - glyph.shape[0]) // 2
    x = (width - glyph.shape[1]) // 2
    output[y:y+glyph.shape[0], x:x+glyph.shape[1]] = glyph
    return output


def generated(font: ImageFont.FreeTypeFont, cutoff: int, digit: int) -> np.ndarray:
    image = Image.new("L", (24, 26), 0)
    ImageDraw.Draw(image).text((12, 13), str(digit), font=font, anchor="mm", fill=255)
    return normalize(np.where(np.asarray(image) >= cutoff, 255, 0).astype(np.uint8))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("font", type=Path)
    args = parser.parse_args()
    actual_hash = hashlib.sha256(args.font.read_bytes()).hexdigest()
    if actual_hash != FONT_SHA256:
        parser.error("font is not the game's HarmonyOS_Sans_Bold asset")

    manifest = {"font_sha256": actual_hash, "template_size": list(TEMPLATE_SIZE), "styles": {}}
    for style, options in STYLES.items():
        destination = OUTPUT / style
        destination.mkdir(parents=True, exist_ok=True)
        font = ImageFont.truetype(str(args.font), options["font_size"])
        variants = {digit: [] for digit in range(10)}
        for digit in range(10):
            path = destination / f"{digit}_font.png"
            Image.fromarray(generated(font, options["cutoff"], digit)).save(path)
            variants[digit].append(path.name)

        seen = {digit: set() for digit in range(10)}
        sources = sorted(SAMPLES.glob(f"{style}_*.png"))
        if style == "work":
            sources += sorted(SAMPLES.glob("fixture_work_*.png"))
        else:
            sources += sorted(SAMPLES.glob("fixture_love_*.png"))
        for source in sources:
            digits = source.stem.rsplit("_", 1)[-1]
            if len(digits) != 2 or not digits.isdigit():
                raise ValueError(f"sample name must end in two digits: {source.name}")
            image = np.asarray(Image.open(source).convert("L"))
            if image.shape != (16, 22):
                raise ValueError(f"sample must be 22x16: {source.name}")
            for half, digit_text in enumerate(digits):
                digit = int(digit_text)
                glyph = normalize(image[:, half*11:(half+1)*11])
                digest = hashlib.sha256(glyph.tobytes()).hexdigest()
                if digest in seen[digit]:
                    continue
                seen[digit].add(digest)
                name = f"{digit}_sample_{len(seen[digit])}.png"
                Image.fromarray(glyph).save(destination / name)
                variants[digit].append(name)
        manifest["styles"][style] = {**options, "variants": {str(k): value for k, value in variants.items()}}

    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Generated digit-only templates at {OUTPUT}")


if __name__ == "__main__":
    main()
