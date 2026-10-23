"""Crop warehouse controls and report bounded offline matches on supplied captures.

This builder reads screenshots only. It never starts the game or sends input.
Run it from the repository root. All scratch captures and reports stay in scope.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "plans/resonance_pc"
OUTPUT = PLAN / "templates/workshop/warehouse/controls"
CATALOG = PLAN / "data/meta/prison_warehouse_transfer_templates_pc.json"
DEFAULT_SCOPE = ROOT / ".pytest_tmp/prison_warehouse_calibration_20261023/controls"
CLIENT_CROP = (1, 31, 1281, 751)
SOURCE_DIRECTORY = PLAN / "development/template_sources/warehouse_transfer/screenshots"
SOURCE_NAMES = ("overview", "warehouse_before", "warehouse_connected", "warehouse_after")
CAPTURES = (
    "c4766d9d-602d-4b63-8e40-ae50dbabf3ab",
    "701f5a52-8d47-42b8-b583-b5a381dd1267",
    "f060df83-a7b3-4143-93af-25d4ec5bb072",
    "4696c768-ee0e-4616-8535-735e04f32b93",
)
FRAME_STATES = (
    "prison_overview",
    "material_product_connected_product_empty",
    "train_product_connected_product_populated",
    "train_product_connected_product_empty_train_scrolled",
)
# name, source capture (one-based), XYWH, expected-positive supplied captures.
# These rectangles exclude goods, counts, capacity digits and maximum-mode controls.
DEFINITIONS = (
    ("overview_warehouse_shortcut", 1, (42, 596, 68, 68), (1,)),
    ("overview_event_list", 1, (1086, 119, 194, 43), (1,)),
    ("train_title_active", 3, (36, 91, 181, 38), (3, 4)),
    ("train_title_inactive", 2, (36, 91, 181, 38), (2,)),
    ("product_title_active", 3, (1118, 542, 94, 28), (2, 3, 4)),
    ("material_title_active", 2, (1118, 210, 94, 28), (2,)),
    ("material_title_inactive", 3, (1118, 210, 94, 28), (3, 4)),
    ("tp_connection_selected", 3, (520, 466, 40, 159), (3, 4)),
    ("tp_connection_unselected", 2, (520, 466, 40, 159), (2,)),
    ("product_empty", 2, (775, 512, 75, 70), (2, 4)),
)
THRESHOLD = 0.90
COLOR_MAE_LIMIT = 18.0
TOLERANCE = 6


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def box(rect):
    x, y, width, height = rect
    return x, y, x + width, y + height


def search_region(rect):
    x, y, width, height = rect
    left, top = max(0, x - TOLERANCE), max(0, y - TOLERANCE)
    right, bottom = min(1280, x + width + TOLERANCE), min(720, y + height + TOLERANCE)
    return [left, top, right - left, bottom - top]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, action="append", help="Four original 1282x752 captures, in supplied order")
    parser.add_argument("--scope", type=Path, default=DEFAULT_SCOPE)
    args = parser.parse_args()
    if Path.cwd().resolve() != ROOT:
        parser.error("Run this builder from the repository root")
    scope = args.scope.resolve()
    if scope == ROOT / ".pytest_tmp" or not scope.is_relative_to(ROOT / ".pytest_tmp"):
        parser.error("--scope must be a dedicated directory under this repository's .pytest_tmp")
    scope.mkdir(parents=True, exist_ok=True)
    for key in ("TEMP", "TMP", "TMPDIR"):
        os.environ[key] = str(scope)
    sys.dont_write_bytecode = True

    import cv2
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont

    sources = args.source or [SOURCE_DIRECTORY / f"{name}.png" for name in SOURCE_NAMES]
    if len(sources) != 4:
        parser.error("Exactly four --source captures are required")
    clients, source_rows = [], []
    for index, path in enumerate(sources, 1):
        path = path.resolve()
        with Image.open(path) as opened:
            if opened.size != (1282, 752):
                parser.error(f"Capture {index} has size {opened.size}; expected 1282x752")
            original = opened.convert("RGB")
        staged = scope / f"capture_{index}_original.png"
        if path.resolve() != staged.resolve():
            shutil.copyfile(path, staged)
        client = original.crop(CLIENT_CROP)
        client_path = scope / f"capture_{index}_client.png"
        client.save(client_path)
        clients.append(client)
        source_rows.append({
            "id": f"capture_{index}", "source_image": path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.name,
            "original_name": f"codex-clipboard-{CAPTURES[index - 1]}.png" if args.source is None else path.name,
            "original_sha256": digest(staged), "original_size": [1282, 752],
            "client_crop_xyxy": list(CLIENT_CROP), "client_size": [1280, 720],
            "client_sha256": digest(client_path), "state": FRAME_STATES[index - 1],
            "staged_original": staged.relative_to(ROOT).as_posix(),
            "staged_client": client_path.relative_to(ROOT).as_posix(),
        })

    OUTPUT.mkdir(parents=True, exist_ok=True)
    templates, report_rows = {}, []
    bgr_frames = [cv2.cvtColor(np.asarray(client), cv2.COLOR_RGB2BGR) for client in clients]
    atlas = Image.new("RGB", (940, 1090), (25, 30, 39))
    draw = ImageDraw.Draw(atlas)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/consola.ttf", 15)
    except OSError:
        font = ImageFont.load_default()
    annotated = [client.copy() for client in clients]

    for ordinal, (name, source_index, rect, positives) in enumerate(DEFINITIONS):
        image = clients[source_index - 1].crop(box(rect))
        target = OUTPUT / f"{name}.png"
        image.save(target)
        roi = search_region(rect)
        templates[name] = {
            "image": target.relative_to(PLAN).as_posix(), "bbox": list(rect),
            "search_region": roi, "preprocess": "none", "use_grayscale": False,
            "method": "TM_SQDIFF_NORMED", "score": "1-normalized_sqdiff",
            "threshold": THRESHOLD, "color_mae_limit": COLOR_MAE_LIMIT,
            "source_capture": f"capture_{source_index}", "sha256": digest(target),
            "source_bbox_xywh": list(rect), "kind": "supplied_screenshot_crop",
            "status": "offline_supplied_capture_calibration_no_live_execution",
        }
        if name == "overview_warehouse_shortcut":
            templates[name]["click"] = [76, 630]
        elif name == "overview_event_list":
            templates[name]["click"] = [1190, 140]
        elif name.startswith("tp_connection_"):
            templates[name]["click"] = [540, 546]
            templates[name]["note"] = "Fixed vertical TP button only; no goods or inventory digits. Require RGB and color MAE gate together."
        if name.endswith("_inactive"):
            templates[name]["note"] = "Dimmed title state; brightness alone is not a page identity proof."
        needle = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
        for index, frame in enumerate(bgr_frames, 1):
            rx, ry, rw, rh = roi
            region = frame[ry:ry + rh, rx:rx + rw]
            scores = cv2.matchTemplate(region, needle, cv2.TM_SQDIFF_NORMED)
            minimum, _, location, _ = cv2.minMaxLoc(scores)
            x, y = rx + location[0], ry + location[1]
            candidate = frame[y:y + image.height, x:x + image.width]
            mae = float(np.abs(candidate.astype(np.float32) - needle.astype(np.float32)).mean())
            score = 1.0 - float(minimum)
            detected = score >= THRESHOLD and mae <= COLOR_MAE_LIMIT
            expected = index in positives
            report_rows.append({
                "template": name, "target_capture": f"capture_{index}",
                "evidence": "same_source_self_match" if index == source_index else "cross_screenshot",
                "expected_present": expected, "detected": detected,
                "expectation_met": detected == expected, "score": round(score, 6),
                "color_mae": round(mae, 6), "match_bbox_xywh": [x, y, image.width, image.height],
            })
        column, row = ordinal % 2, ordinal // 2
        ax, ay = 20 + column * 470, 20 + row * 210
        draw.text((ax, ay), name, font=font, fill=(235, 240, 248))
        draw.text((ax, ay + 19), f"capture_{source_index}  {rect}", font=font, fill=(170, 184, 202))
        atlas.paste(image, (ax, ay + 45))
        ad = ImageDraw.Draw(annotated[source_index - 1])
        ad.rectangle(box(rect), outline=(255, 190, 32), width=2)
        ad.text((rect[0], max(0, rect[1] - 15)), name, font=font, fill=(255, 190, 32), stroke_width=1, stroke_fill=(0, 0, 0))

    # Auxiliary evidence only: a fixed left-frame strip contains no goods pixels.
    # It must not replace the complete colored TP button match.
    frame_probe = [12, 200, 7, 100]
    probes = []
    for index, client in enumerate(clients, 1):
        crop = np.asarray(client.crop(box(frame_probe)), dtype=np.float32)
        probes.append({"capture": f"capture_{index}", "mean_rgb": [round(float(value), 3) for value in crop.mean(axis=(0, 1))]})
    # Read the existing shared profile rather than copying its thresholds or pixels.
    sys.path.insert(0, str(PLAN / "src/actions"))
    from _navigation_templates import GLOBAL_BACK

    shared_path = PLAN / GLOBAL_BACK.path
    shared_needle = cv2.imdecode(np.fromfile(shared_path, np.uint8), cv2.IMREAD_GRAYSCALE)
    shared_rows = []
    for index, frame in enumerate(bgr_frames, 1):
        sx, sy, sw, sh = GLOBAL_BACK.roi
        shared_region = cv2.cvtColor(frame[sy:sy + sh, sx:sx + sw], cv2.COLOR_BGR2GRAY)
        scores = cv2.matchTemplate(shared_region, shared_needle, GLOBAL_BACK.match_method)
        _, score, _, location = cv2.minMaxLoc(scores)
        shared_rows.append({
            "capture": f"capture_{index}", "score": round(score, 6),
            "detected": score >= GLOBAL_BACK.threshold,
            "bbox_xywh": [sx + location[0], sy + location[1], shared_needle.shape[1], shared_needle.shape[0]],
        })
    shared_back = {
        "profile": "GLOBAL_BACK", "image": GLOBAL_BACK.path, "sha256": digest(shared_path),
        "threshold": GLOBAL_BACK.threshold, "roi": list(GLOBAL_BACK.roi),
        "method": "TM_CCOEFF_NORMED", "use_grayscale": True, "preprocess": "none",
        "results": shared_rows,
        "limit": "Present on both overview and warehouse pages; the back control alone does not prove the source page.",
    }
    catalog = {
        "schema_version": 1, "reference_client": [1280, 720],
        "kind": "supplied_screenshot_crops", "live_calibrated": False,
        "calibration": "Offline comparison on four user-supplied captures only; no live clicks or subsequent frames.",
        "color_contract": "PNG is RGB. OpenCV images are BGR. Preserve color and apply both normalized SQDIFF score and color MAE limit.",
        "shared_templates": {"back": "GLOBAL_BACK"},
        "shared_template_calibration": {"back": shared_back},
        "excluded": ["maximum_mode_detection", "inventory_digit_recognition", "goods_icon_catalog", "transfer_actions"],
        "sources": source_rows, "templates": templates,
        "auxiliary_frame_probe": {
            "bbox": frame_probe, "role": "Reference evidence for bright/dim train frame only; no runtime threshold or standalone state detector.",
            "observations": probes,
        },
        "limits": [
            "All matches use bounded position searches with six-pixel tolerance at 1280x720.",
            "Source-image self matches establish crop provenance only, not generalization.",
            "Cross-screenshot positives cover supplied states only; animation, scaling, other UI versions and obstruction remain unmeasured.",
            "Product EMPTY was supplied at one fixed location; material EMPTY is deliberately outside its search region.",
            "Warehouse titles are supporting page markers; TP state requires the fixed connection button state.",
        ],
    }
    CATALOG.parent.mkdir(parents=True, exist_ok=True)
    CATALOG.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    atlas_path = scope / "controls_atlas.png"
    atlas.save(atlas_path)
    for index, image in enumerate(annotated, 1):
        image.save(scope / f"capture_{index}_annotated.png")
    self_rows = [row for row in report_rows if row["evidence"] == "same_source_self_match"]
    cross_rows = [row for row in report_rows if row["evidence"] == "cross_screenshot"]
    report = {
        "method": "RGB 1-TM_SQDIFF_NORMED plus RGB/BGR-equivalent absolute color MAE gate",
        "threshold": THRESHOLD, "color_mae_limit": COLOR_MAE_LIMIT,
        "source_self_match": {"count": len(self_rows), "expectations_met": sum(row["expectation_met"] for row in self_rows), "meaning": "Crop provenance only"},
        "cross_screenshot": {"count": len(cross_rows), "expectations_met": sum(row["expectation_met"] for row in cross_rows), "positive_count": sum(row["expected_present"] for row in cross_rows), "negative_count": sum(not row["expected_present"] for row in cross_rows)},
        "shared_back": shared_back,
        "results": report_rows, "limits": catalog["limits"],
    }
    report_path = scope / "controls_matching_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# Warehouse control offline calibration", "", "Four supplied captures; no live input. Same-source rows are provenance checks only.", "", "| Template | Capture | Evidence | Expected | Detected | Score | Color MAE |", "|---|---|---|---|---|---:|---:|"]
    lines.extend(f"| {row['template']} | {row['target_capture']} | {row['evidence']} | {row['expected_present']} | {row['detected']} | {row['score']:.6f} | {row['color_mae']:.3f} |" for row in report_rows)
    lines.extend(["", "Boundaries:", "", *[f"- {limit}" for limit in catalog["limits"]], ""])
    (scope / "controls_matching_report.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"catalog": str(CATALOG), "atlas": str(atlas_path), "report": str(report_path), "templates": len(templates), "self_matches": report["source_self_match"], "cross_screenshot": report["cross_screenshot"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
