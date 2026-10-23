"""Build experimental quantity glyphs and a bounded offline screenshot report.

No game actions or task integration. Run from the repository root. Supplied
capture labels are calibration evidence, not a claim about all inventories.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "plans/resonance_pc"
SOURCES = PLAN / "development/template_sources/warehouse_transfer/screenshots"
OUTPUT = PLAN / "templates/workshop/warehouse/digits"
CATALOG = PLAN / "data/meta/prison_warehouse_quantity_pc.json"
DEFAULT_SCOPE = ROOT / ".pytest_tmp/prison_warehouse_calibration_20261023/quantity_alt"


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sample_definitions():
    samples = []
    first = [[2, 1, 4, 2, 3], [6, 2, 3, 1, 2], [2, 5, 2, 1, 1], [1, 1, 4, 7, 1], [1, 1, 1, 1, 4]]
    for frame in ("warehouse_before", "warehouse_connected"):
        for row in range(5):
            for col in range(5):
                samples.append({"frame": frame, "id": f"train_{row}_{col}", "bbox": [28 + round(col * 96.5), 151 + round(row * 96.5), 84, 84], "expected": str(first[row][col]), "state": "dim_train_diagnostic" if frame == "warehouse_before" else "active"})
    samples.extend([
        {"frame": "warehouse_before", "id": "raw", "bbox": [586, 99, 84, 84], "expected": "20", "state": "active"},
        {"frame": "warehouse_connected", "id": "product", "bbox": [586, 431, 84, 84], "expected": "20", "state": "active"},
    ])
    after = [[2, 5, 2, 1, 1], [1, 1, 4, 7, 1], [1, 1, 1, 1, 4], [20, 1, 1, 1, 1]]
    for row in range(4):
        for col in range(5):
            samples.append({"frame": "warehouse_after", "id": f"train_{row}_{col}", "bbox": [28 + round(col * 96.5), 188 + round(row * 96.5), 84, 84], "expected": str(after[row][col]), "state": "active"})
    return samples


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", type=Path, default=DEFAULT_SCOPE)
    parser.add_argument("--font", type=Path, default=ROOT / ".pytest_tmp/prison_warehouse_calibration_20261023/quantity/-4884400286118422667.ttf")
    args = parser.parse_args()
    if Path.cwd().resolve() != ROOT:
        parser.error("Run from the repository root")
    scope = args.scope.resolve()
    if scope == ROOT / ".pytest_tmp" or not scope.is_relative_to(ROOT / ".pytest_tmp"):
        parser.error("--scope must remain in a dedicated directory under this repository's .pytest_tmp")
    scope.mkdir(parents=True, exist_ok=True)
    for key in ("TEMP", "TMP", "TMPDIR"):
        os.environ[key] = str(scope)
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont

    sys.path.insert(0, str(PLAN / "src/actions"))
    from _prison_warehouse_quantity import ScreenshotQuantityReader, locate_white_bar, quantity_response

    frames, sources = {}, []
    for name in ("warehouse_before", "warehouse_connected", "warehouse_after"):
        path = SOURCES / f"{name}.png"
        with Image.open(path) as original:
            if original.size != (1282, 752):
                parser.error(f"Unexpected capture dimensions for {name}: {original.size}")
            frames[name] = np.asarray(original.convert("RGB").crop((1, 31, 1281, 751)))
        sources.append({"id": name, "path": path.relative_to(PLAN).as_posix(), "sha256": sha256(path), "client_crop_xyxy": [1, 31, 1281, 751]})
    samples = sample_definitions()
    fixture = SOURCES.parent / "quantity_samples.json"
    fixture.write_text(json.dumps({"kind": "hand_transcribed_supplied_capture_quantities", "samples": samples}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for sample in samples:
        x, y, width, height = sample["bbox"]
        sample["card"] = frames[sample["frame"]][y:y + height, x:x + width].copy()
    seeds = [("0", "product", 70, 73, 83, "ones"), ("1", "train_0_1", 70, 70, 83, "ones"), ("2", "train_0_0", 70, 70, 83, "ones"), ("2", "product", 61, 61, 73, "tens_phase"), ("3", "train_0_4", 70, 70, 83, "ones"), ("4", "train_0_2", 70, 70, 83, "ones"), ("5", "train_2_1", 70, 70, 83, "ones"), ("6", "train_1_0", 70, 70, 83, "ones"), ("7", "train_3_3", 70, 70, 83, "ones")]
    OUTPUT.mkdir(parents=True, exist_ok=True)
    templates = []
    for digit, seed_id, left, white_left, white_right, variant in seeds:
        source = next(sample for sample in samples if sample["frame"] == "warehouse_connected" and sample["id"] == seed_id)
        card = source["card"]
        gray, response = quantity_response(card)
        bar = locate_white_bar(card)
        top, width, height = bar - 13, 13, 17
        white = ((gray > 225) & ((gray - cv2.erode(gray, np.ones((3, 3), np.uint8))) > 35)).astype(np.uint8)
        white[:bar - 12] = 0
        white[bar:] = 0
        white[:, :white_left] = 0
        white[:, white_right:] = 0
        support = cv2.dilate(white, np.ones((3, 3), np.uint8))[top:top + height, left:left + width]
        name = f"digit_{digit}_{variant}"
        patch_path, card_path = OUTPUT / f"{name}.patch.png", OUTPUT / f"{name}.card.png"
        mask_path, response_path = OUTPUT / f"{name}.mask.png", OUTPUT / f"{name}.response.npy"
        Image.fromarray(card[top:top + height, left:left + width]).save(patch_path)
        Image.fromarray(card).save(card_path)
        Image.fromarray(support * 255).save(mask_path)
        np.save(response_path, response[top:top + height, left:left + width].astype(np.float32), allow_pickle=False)
        templates.append({
            "id": name, "digit": digit, "variant": variant, "status": "supplied_screenshot_calibrated",
            "patch": patch_path.relative_to(PLAN).as_posix(), "source_card": card_path.relative_to(PLAN).as_posix(),
            "mask": mask_path.relative_to(PLAN).as_posix(), "response": response_path.relative_to(PLAN).as_posix(),
            "sha256": {"patch": sha256(patch_path), "source_card": sha256(card_path), "mask": sha256(mask_path), "response": sha256(response_path)},
            "source_capture": source["frame"], "source_sample": seed_id, "source_card_bbox": source["bbox"],
            "source_patch_bbox_in_card": [left, top, width, height], "white_bar_y": bar,
            "white_seed_x_range": [white_left, white_right],
        })
    pending = []
    if args.font.is_file():
        font = ImageFont.truetype(str(args.font), 13)
        for digit in ("8", "9"):
            image = Image.new("L", (24, 24))
            ImageDraw.Draw(image).text((4, 2), digit, font=font, fill=255)
            path = OUTPUT / f"digit_{digit}.native_unvalidated.png"
            image.save(path)
            pending.append({"digit": digit, "image": path.relative_to(PLAN).as_posix(), "sha256": sha256(path), "status": "native_font_candidate_not_screenshot_calibrated", "used_by_reader": False, "font_path_id": -4884400286118422667, "font_sha256": sha256(args.font), "font_size": 13})
    else:
        pending = [{"digit": digit, "status": "font_source_unavailable_candidate_not_reproduced", "used_by_reader": False} for digit in ("8", "9")]
    catalog = {
        "schema_version": 1, "experimental": True, "offline_only": True, "auto_click_ready": False,
        "reference_client": [1280, 720], "card_size": [84, 84], "input_order": "RGB",
        "sources": sources, "sample_fixture": {"path": fixture.relative_to(PLAN).as_posix(), "sha256": sha256(fixture)}, "templates": templates, "pending_native_candidates": pending,
        "reader": {"max_digits": 4, "digit_advance": 9, "minimum_score": .62, "digit_minimum_score": {"0": .90, "1": .62, "2": .86, "3": .80, "4": .78, "5": .90, "6": .90, "7": .90}, "minimum_margin": .15, "absence_score_ceiling": .45, "unexplained_white_pixel_limit": 3},
        "method": "Per-glyph high-pass response minus common row response; native-position support masks; right-aligned 9px advances; no connected-component split and no whole-number-20 template.",
        "limits": ["Calibrated glyphs are 0..7; 8/9 remain unresolved and may resemble a known glyph.", "Only 20 is a supplied multi-digit value; arbitrary 1-4 digit strings are a decoding mechanism, not an observed accuracy claim.", "The 0 and a 2 phase variant share the supplied product-20 seed; two other 20 observations are same commodity across captures.", "Per-digit acceptance thresholds were fit on this supplied capture set; this is calibration, not held-out accuracy.", "Strict white-bar confirmation intentionally rejects dimmed train cards.", "Synthetic white-bar and damaged-glyph negatives are bounded diagnostics, not exhaustive unknown-symbol validation.", "No task, capture, click or automatic loading integration exists."],
    }
    CATALOG.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    reader = ScreenshotQuantityReader(CATALOG)
    seed_ids = {seed[1] for seed in seeds}
    results = []
    for sample in samples:
        value = reader.read_card(sample["card"])
        results.append({**{key: sample[key] for key in ("frame", "id", "bbox", "expected", "state")}, "seed": sample["frame"] == "warehouse_connected" and sample["id"] in seed_ids, "result": value, "correct": value["value"] is not None and str(value["value"]) == sample["expected"]})
    base = next(sample["card"] for sample in samples if sample["frame"] == "warehouse_connected" and sample["id"] == "product")
    bar = locate_white_bar(base)
    negatives = []
    plain = np.full((84, 84, 3), 155, np.uint8)
    plain[78:82] = 255
    negatives.append(("white_bar_only", plain))
    gradient = np.tile(np.linspace(110, 210, 84, dtype=np.uint8)[:, None, None], (1, 84, 3))
    gradient[78:82] = 255
    negatives.append(("white_bar_gradient", gradient))
    for name, left, right in (("twenty_erase_both", 62, 83), ("twenty_drop_leading_digit", 62, 73), ("twenty_drop_trailing_digit", 73, 83), ("twenty_cut_leading_left_half", 64, 68), ("twenty_cut_trailing_right_half", 78, 83)):
        damage = np.zeros((84, 84), np.uint8)
        damage[bar - 12:bar + 3, left:right] = 255
        modified = cv2.inpaint(base, damage, 3, cv2.INPAINT_TELEA)
        negatives.append((name, modified))
    negative_results = []
    for name, card in negatives:
        path = scope / f"negative_{name}.png"
        Image.fromarray(card).resize((336, 336)).save(path)
        value = reader.read_card(card)
        negative_results.append({"id": name, "expected": "unknown", "result": value, "correct": value["value"] is None})
    summary = {
        "supplied_samples": len(results), "recognized_correct": sum(row["correct"] for row in results),
        "unknown": sum(row["result"]["value"] is None for row in results),
        "wrong_accepted": sum(row["result"]["value"] is not None and not row["correct"] for row in results),
        "seed_samples": sum(row["seed"] for row in results),
        "nonseed_samples": sum(not row["seed"] for row in results),
        "synthetic_negative_count": len(negative_results), "synthetic_negative_rejected": sum(row["correct"] for row in negative_results),
    }
    for state in ("active", "dim_train_diagnostic"):
        subset = [row for row in results if row["state"] == state]
        summary[state] = {"count": len(subset), "recognized_correct": sum(row["correct"] for row in subset), "unknown": sum(row["result"]["value"] is None for row in subset), "wrong_accepted": sum(row["result"]["value"] is not None and not row["correct"] for row in subset)}
    report = {"summary": summary, "results": results, "synthetic_negatives": negative_results, "limits": catalog["limits"]}
    (scope / "quantity_calibration_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    canvas = Image.new("RGB", (1100, 210 * 3), (28, 34, 42))
    draw = ImageDraw.Draw(canvas)
    for index, spec in enumerate(templates):
        x, y = (index % 3) * 365 + 12, (index // 3) * 210 + 10
        draw.text((x, y), spec["id"], fill="white")
        with Image.open(PLAN / spec["patch"]) as patch:
            canvas.paste(patch.resize((78, 102)), (x, y + 23))
        with Image.open(PLAN / spec["mask"]) as mask:
            canvas.paste(mask.resize((78, 102)).convert("RGB"), (x + 92, y + 23))
        draw.text((x, y + 135), f"{spec['source_sample']} {spec['source_patch_bbox_in_card']}", fill="white")
    canvas.save(scope / "quantity_digit_atlas.png")
    contact = Image.new("RGB", (1200, 13 * 112), (28, 34, 42))
    draw = ImageDraw.Draw(contact)
    for index, (sample, row) in enumerate(zip(samples, results)):
        x, y = index % 6 * 200, index // 6 * 112
        reading = row["result"]["value"]
        draw.text((x, y), f"{sample['frame'].replace('warehouse_', '')}:{sample['id']}", fill="white")
        draw.text((x, y + 12), f"expected={sample['expected']} read={reading}", fill=(155, 230, 175) if row["correct"] else (255, 205, 90))
        patch = sample["card"][58:84, 35:84]
        contact.paste(Image.fromarray(patch).resize((196, 78)), (x, y + 27))
    contact.save(scope / "quantity_samples_contact.png")
    twenty = Image.new("RGB", (1000, 570), (28, 34, 42))
    draw = ImageDraw.Draw(twenty)
    for index, (sample, row) in enumerate((pair for pair in zip(samples, results) if pair[0]["expected"] == "20")):
        y = index * 190
        draw.text((10, y + 5), f"{sample['frame']}:{sample['id']} expected 20 read {row['result']['value']} seed={row['seed']}", fill="white")
        twenty.paste(Image.fromarray(sample["card"][58:84, 35:84]).resize((294, 156)), (10, y + 25))
        for position in row["result"]["positions"][:2]:
            chosen = position["ranked"][0]
            spec = next(item for item in templates if item["id"] == chosen["template"])
            x = 340 + position["from_right"] * 290
            with Image.open(PLAN / spec["patch"]) as patch:
                twenty.paste(patch.resize((78, 102)), (x, y + 40))
            with Image.open(PLAN / spec["mask"]) as mask:
                twenty.paste(mask.resize((78, 102)).convert("RGB"), (x + 90, y + 40))
            draw.text((x, y + 150), f"{chosen['digit']} score={chosen['score']:.3f} gap={position['margin']:.3f}", fill="white")
    twenty.save(scope / "quantity_twenty_detail.png")
    lines = ["# Experimental offline quantity calibration", "", "No capture, click, game or task integration. auto_click_ready=false.", "", "```json", json.dumps(summary, indent=2), "```", "", "The supplied labels include 8 seed card samples and 64 nonseed samples. Per-digit thresholds were fitted to this set; these numbers are not held-out accuracy.", "", "| Capture | Sample | State | Expected | Read | Reasons |", "|---|---|---|---|---|---|"]
    lines.extend(f"| {row['frame']} | {row['id']} | {row['state']} | {row['expected']} | {row['result']['value']} | {', '.join(row['result']['reasons'])} |" for row in results)
    lines.extend(["", "| Synthetic negative | Read | Rejected | Reasons |", "|---|---|---|---|"])
    lines.extend(f"| {row['id']} | {row['result']['value']} | {row['correct']} | {', '.join(row['result']['reasons'])} |" for row in negative_results)
    lines.extend(["", "Limits:", "", *[f"- {limit}" for limit in catalog["limits"]], ""])
    (scope / "quantity_calibration_report.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"catalog": str(CATALOG), "report": str(scope / "quantity_calibration_report.json"), **summary}, ensure_ascii=False))


if __name__ == "__main__":
    main()
