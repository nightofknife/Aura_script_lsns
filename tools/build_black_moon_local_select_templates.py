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
PLAN_ROOT = ROOT / "plans/resonance_pc"
SOURCE = ROOT / "plans/resonance_pc/templates/commerce/shop/black_moon/native"
OUTPUT = ROOT / "plans/resonance_pc/templates/commerce/shop/black_moon/calibrated"
PAPER_ITEMS = {"advertising_ticket", "order_request", "renegotiation_request",
               "task_commission_letter", "purchase_order_book"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bank_path(reference: str) -> Path:
    path = (OUTPUT / reference).resolve()
    if Path(reference).is_absolute() or not path.is_relative_to(OUTPUT.resolve()):
        raise ValueError(f"Selection resource outside bank: {reference}")
    return path


def cleanup_previous_resources(previous, current):
    """A bank manifest owns its declared files; unrelated neighbours stay untouched."""
    for reference in set(previous.get("files", {})) - set(current["files"]):
        path = bank_path(reference)
        if path.is_file():
            path.unlink()


def write_identity_mask(path: Path) -> Image.Image:
    """Write the shared quantity/decoration exclusions once for this bank."""
    mask = Image.new("L", (96, 96), 255)
    draw = ImageDraw.Draw(mask)
    draw.rectangle((0, 0, 95, 23), fill=0)
    draw.rectangle((65, 67, 95, 95), fill=0)
    draw.rectangle((0, 92, 95, 95), fill=0)
    mask.save(path)
    return mask


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
    previous_path = OUTPUT / "manifest.json"
    previous = json.loads(previous_path.read_text(encoding="utf-8")) if previous_path.is_file() else {}
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "items").mkdir(exist_ok=True)
    (OUTPUT / "controls").mkdir(exist_ok=True)
    (OUTPUT / "masks").mkdir(exist_ok=True)
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
    mask_file = None
    for row in source["cards"]["items"]:
        if row["state"] != "normal":
            continue
        source_path = (PLAN_ROOT / row["file"]).resolve()
        if Path(row["file"]).is_absolute() or not source_path.is_relative_to((PLAN_ROOT / "templates").resolve()):
            raise ValueError(f"Native resource outside plan templates: {row['file']}")
        icon = Image.open(source_path).convert("RGB").resize((96, 96), Image.Resampling.BILINEAR)
        file = f"items/{row['item_id']}.png"
        icon.save(OUTPUT / file)
        if mask_file is None:
            mask_file = "masks/item_identity_96x96.png"
            mask = write_identity_mask(OUTPUT / mask_file)
        manifest["items"].append({"item_id": row["item_id"], "name": row["name"],
                                  "category": row["category"], "quality": row["quality"],
                                  "file": file, "mask_file": mask_file,
                                  "threshold": .92 if row["item_id"] in PAPER_ITEMS else .86,
                                  "source": row["source"]})

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
        file = f"controls/{name}.png"
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
    # The independently supplied SOLD OUT crop is not reconstructed from a
    # normal/selected frame. Preserve it when regenerating the icon bank.
    if "sold_out_boundary" in previous:
        manifest["sold_out_boundary"] = previous["sold_out_boundary"]
    entries = (manifest["items"] + list(manifest["geometry"].values())
               + [manifest[key] for key in ("selected", "confirm_purchase", "total_zero", "sold_out_boundary")
                  if key in manifest])
    files = {row[key] for row in entries for key in ("file", "mask_file") if row.get(key)}
    manifest["files"] = {filename: digest(bank_path(filename)) for filename in sorted(files)}
    (OUTPUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    cleanup_previous_resources(previous, manifest)
    print(json.dumps({"items": len(manifest["items"]), "output": str(OUTPUT)}))


if __name__ == "__main__":
    main()
