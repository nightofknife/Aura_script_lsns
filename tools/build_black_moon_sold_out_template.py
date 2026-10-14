"""Extract the SOLD OUT plate from an explicitly supplied client screenshot.

This is resource generation only: no template matching or game input is run.
The source screenshot is not copied into the distributable template bank.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
BANK = ROOT / "plans/resonance_pc/templates/black_moon_local_select"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frame", type=Path, required=True)
    parser.add_argument("--client-box", type=int, nargs=4, required=True, metavar=("X", "Y", "W", "H"))
    parser.add_argument("--badge-box", type=int, nargs=4, required=True, metavar=("X", "Y", "W", "H"))
    parser.add_argument("--card-origin", type=int, nargs=2, required=True)
    args = parser.parse_args()
    cx, cy, cw, ch = args.client_box
    bx, by, bw, bh = args.badge_box
    with Image.open(args.frame) as source:
        if (cw, ch) != (1280, 720) or min(cx, cy, bx, by) < 0 or min(bw, bh) <= 0:
            raise ValueError("Use a 1280x720 client and a positive contained badge rectangle")
        if cx + cw > source.width or cy + ch > source.height or bx + bw > cw or by + bh > ch:
            raise ValueError("Source/badge rectangle is outside the supplied frame")
        plate = source.crop((cx + bx, cy + by, cx + bx + bw, cy + by + bh)).convert("L")
    path = BANK / "sold_out_badge.png"
    plate.save(path)
    manifest_path = BANK / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sold_out_boundary"] = {
        "file": path.name, "threshold": 0.9, "preprocess": "gray",
        "method": "1-TM_SQDIFF_NORMED",
        "card_relative_roi": [bx - args.card_origin[0], by - args.card_origin[1], bw, bh],
        "row_pitch": 196 / 1.5,
        "source": {"type": "user_client_crop", "source_sha256": hashlib.sha256(args.frame.read_bytes()).hexdigest(),
                   "client_box": args.client_box, "client_badge_box": args.badge_box,
                   "client_card_origin": args.card_origin},
    }
    manifest["files"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Generated {path.relative_to(ROOT).as_posix()}; no matching or live validation performed")


if __name__ == "__main__":
    main()
