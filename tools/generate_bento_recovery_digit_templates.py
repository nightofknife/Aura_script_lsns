"""Build digit-only bento recovery templates from the game's original font.

Run from the repository root with the extracted HarmonyOS_Sans_Bold font as
the argument. Number-only screenshot samples under tests/fixtures are bundled
as additional glyph variants; neither card icons nor minus signs are included.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "plans/resonance_pc/templates/recovery/bento/recovery_digits"
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


def build(font_path: Path, *, output: Path = OUTPUT, samples: Path = SAMPLES) -> dict:
    """Write only this bank's owned files, keeping work/love styles separate."""
    actual_hash = hashlib.sha256(font_path.read_bytes()).hexdigest()
    if actual_hash != FONT_SHA256:
        raise ValueError("font is not the game's HarmonyOS_Sans_Bold asset")

    manifest = {"font_sha256": actual_hash, "template_size": list(TEMPLATE_SIZE), "styles": {}}
    for style, options in STYLES.items():
        destination = output / style
        destination.mkdir(parents=True, exist_ok=True)
        font = ImageFont.truetype(str(font_path), options["font_size"])
        variants = {digit: [] for digit in range(10)}
        seen = {digit: set() for digit in range(10)}
        sample_counts = {digit: 0 for digit in range(10)}
        for digit in range(10):
            path = destination / f"{digit}_font.png"
            glyph = generated(font, options["cutoff"], digit)
            seen[digit].add(hashlib.sha256(glyph.tobytes()).hexdigest())
            Image.fromarray(glyph).save(path)
            variants[digit].append(path.name)

        sources = sorted(samples.glob(f"{style}_*.png"))
        if style == "work":
            sources += sorted(samples.glob("fixture_work_*.png"))
        else:
            sources += sorted(samples.glob("fixture_love_*.png"))
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
                sample_counts[digit] += 1
                name = f"{digit}_sample_{sample_counts[digit]}.png"
                Image.fromarray(glyph).save(destination / name)
                variants[digit].append(name)
        manifest["styles"][style] = {**options, "variants": {str(k): value for k, value in variants.items()}}
        owned = {name for names in variants.values() for name in names}
        for path in destination.iterdir():
            if (path.is_file() and re.fullmatch(r"[0-9]_(?:font|sample_[0-9]+)\.png", path.name)
                    and path.name not in owned):
                path.unlink()

    output.mkdir(parents=True, exist_ok=True)
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("font", type=Path)
    args = parser.parse_args()
    try:
        build(args.font)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"Generated digit-only templates at {OUTPUT}")


if __name__ == "__main__":
    main()
