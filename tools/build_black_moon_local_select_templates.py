"""Calibrate native-derived Black Moon icon templates for the 1280x720 client.

The native bank is read-only input. Control crops come from explicitly supplied
client frames; no account text, prices, quantities, or merchandise labels are used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "plans/resonance_pc/templates/black_moon_local_shop"
OUTPUT = ROOT / "plans/resonance_pc/templates/black_moon_local_select"
PAPER_ITEMS = {"advertising_ticket", "order_request", "renegotiation_request",
               "task_commission_letter", "purchase_order_book"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--normal-frame", type=Path, required=True,
                        help="RGB 1280x720 client frame with an unselected card at 600,210")
    parser.add_argument("--selected-frame", type=Path, required=True,
                        help="RGB 1280x720 client frame with a selected card at 600,145")
    args = parser.parse_args()
    frames = {"normal": Image.open(args.normal_frame).convert("RGB"),
              "selected": Image.open(args.selected_frame).convert("RGB")}
    if any(frame.size != (1280, 720) for frame in frames.values()):
        raise ValueError("Supply exact 1280x720 client frames without window chrome")
    source = json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "items").mkdir(exist_ok=True)
    manifest = {
        "schema_version": 1, "reference_client": [1280, 720],
        "method": "1-TM_SQDIFF_NORMED", "viewport": [594, 143, 684, 500],
        "card_columns": [600, 938], "card_size": [327, 120],
        "icon_search_relative": [25, 6, 119, 109], "icon_size": [96, 96],
        "geometry": {}, "items": [],
        "source": {"native_bank_manifest_sha256": digest(SOURCE / "manifest.json"),
                   "calibration": "Native-derived icon bank resized from 120x119 to observed 96x96; exclude top24px, lower-right quantity x65..95/y67..95, and final4px border; retain lower-left quality background",
                   "normal_client_sha256": digest(args.normal_frame),
                   "selected_client_sha256": digest(args.selected_frame)},
    }
    for row in source["cards"]["items"]:
        if row["state"] != "normal":
            continue
        icon = Image.open(SOURCE / row["file"]).convert("RGB").resize((96, 96), Image.Resampling.BILINEAR)
        mask = Image.new("L", (96, 96), 255)
        draw = ImageDraw.Draw(mask)
        draw.rectangle((0, 0, 95, 23), fill=0)
        draw.rectangle((65, 67, 95, 95), fill=0)
        draw.rectangle((0, 92, 95, 95), fill=0)
        file = f"items/{row['item_id']}.png"
        mask_file = "icon.mask.png"
        icon.save(OUTPUT / file)
        mask.save(OUTPUT / mask_file)
        manifest["items"].append({"item_id": row["item_id"], "name": row["name"],
                                  "category": row["category"], "quality": row["quality"],
                                  "file": file, "mask_file": mask_file,
                                  "threshold": .92 if row["item_id"] in PAPER_ITEMS else .86,
                                  "source": row["source"]})

    for obsolete in (OUTPUT / "items").glob("*.mask.png"):
        obsolete.unlink()

    shared_mask = np.asarray(mask, dtype=bool)
    signatures = np.asarray([np.asarray(Image.open(OUTPUT / row["file"]).convert("RGB"))[shared_mask].ravel()
                             for row in manifest["items"]], dtype=np.float64)
    energy = np.sum(signatures * signatures, axis=1)
    distance = np.maximum(0, energy[:, None] + energy[None, :] - 2 * signatures @ signatures.T)
    similarity = 1 - distance / np.sqrt(energy[:, None] * energy[None, :])
    np.fill_diagonal(similarity, -1)
    for index, row in enumerate(manifest["items"]):
        row["confuser_ids"] = [manifest["items"][other]["item_id"]
                               for other in np.flatnonzero(similarity[index] >= .80)]
    pairs = [(float(similarity[left, right]), left, right)
             for left in range(len(manifest["items"]))
             for right in range(left + 1, len(manifest["items"]))
             if similarity[left, right] >= .80]
    pairs.sort(reverse=True)
    manifest["confuser_audit"] = {
        "method": "1-TM_SQDIFF_NORMED at equal icon origin using shared effective mask",
        "candidate_threshold": .80,
        "meaning": "Confusers include non-purchase and cross-quality items; evaluate them only after a purchase target hits, and require target strictly highest. Ties are unknown.",
        "pair_count": len(pairs),
        "pairs_above_086": sum(score >= .86 for score, _, _ in pairs),
        "maximum_group_size": max(len(row["confuser_ids"]) for row in manifest["items"]),
        "top_pairs": [{"item_ids": [manifest["items"][left]["item_id"], manifest["items"][right]["item_id"]],
                       "similarity": round(score, 6)} for score, left, right in pairs[:30]],
    }

    def crop(name: str, frame_name: str, rect: list[int], threshold=.90):
        x, y, w, h = rect
        file = f"{name}.png"
        frames[frame_name].crop((x, y, x + w, y + h)).save(OUTPUT / file)
        return {"file": file, "threshold": threshold,
                "source": {"type": "user_client_crop", "frame": frame_name,
                           "client_rect": rect}}

    manifest["geometry"]["normal_corner"] = crop("normal_corner", "normal", [600, 210, 23, 23])
    manifest["geometry"]["selected_corner"] = crop("selected_corner", "selected", [600, 145, 23, 23])
    manifest["selected"] = crop("selected", "selected", [821, 145, 33, 29])
    manifest["selected"]["roi_relative"] = [219, 0, 37, 32]
    manifest["confirm_purchase"] = crop("confirm_purchase", "normal", [1140, 650, 120, 40])
    manifest["confirm_purchase"]["roi"] = [1136, 646, 128, 48]
    manifest["total_zero"] = crop("total_zero", "normal", [1000, 652, 47, 37])
    manifest["total_zero"]["roi"] = [1000, 652, 47, 37]
    manifest["files"] = {path.relative_to(OUTPUT).as_posix(): digest(path)
                         for path in sorted(OUTPUT.rglob("*.png"))}
    (OUTPUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"items": len(manifest["items"]), "output": str(OUTPUT)}))


if __name__ == "__main__":
    main()
