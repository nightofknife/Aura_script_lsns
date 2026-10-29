"""Reproduce warehouse controls from supplied captures; no game input or OCR.

Page identity uses grayscale title structure. Target-connection localization
uses grayscale button structure; its inner color crops classify the state.
Run from the repository root; reports and OS scratch files remain in .pytest_tmp.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "plans/resonance_pc"
OUTPUT = PLAN / "templates/workshop/warehouse/controls"
CATALOG = PLAN / "data/meta/prison_warehouse_transfer_templates_pc.json"
SOURCES = PLAN / "development/template_sources/warehouse_transfer/screenshots"
DEFAULT_SCOPE = ROOT / ".pytest_tmp/warehouse_state_separation_20261024/controls"
CLIENT_CROP = (1, 31, 1281, 751)
STABLE_CROP = (26, 56, 1306, 776)
SOURCE_NAMES = ("overview", "warehouse_before", "warehouse_connected", "warehouse_after", "warehouse_stable")
FRAME_STATES = ("prison_overview", "target_connection_unselected",
                "train_product_connected_product_populated", "train_product_connected_product_empty",
                "stable_warehouse_target_connection_unselected")
ATTACHMENTS = (
    "codex-clipboard-c4766d9d-602d-4b63-8e40-ae50dbabf3ab.png",
    "codex-clipboard-701f5a52-8d47-42b8-b583-b5a381dd1267.png",
    "codex-clipboard-f060df83-a7b3-4143-93af-25d4ec5bb072.png",
    "codex-clipboard-4696c768-ee0e-4616-8535-735e04f32b93.png",
    "codex-clipboard-2b368679-76a1-4752-ae58-567fdb4cfe56.png",
)
# name, source capture (one-based), XYWH, expected positive supplied captures.
# Legacy color titles are retained as diagnostics, not as authorization gates.
DEFINITIONS = (
    ("overview_warehouse_shortcut", 1, (42, 596, 68, 68), (1,)),
    ("overview_event_list", 1, (1086, 119, 194, 43), (1,)),
    ("train_title_active", 3, (36, 91, 181, 38), (3, 4, 5)),
    ("train_title_inactive", 2, (36, 91, 181, 38), (2,)),
    ("product_title_active", 3, (1118, 542, 94, 28), (2, 3, 4, 5)),
    ("material_title_active", 2, (1118, 210, 94, 28), (2, 5)),
    ("material_title_inactive", 3, (1118, 210, 94, 28), (3, 4)),
    ("tp_connection_selected", 3, (524, 478, 32, 135), (3, 4)),
    ("tp_connection_unselected", 5, (524, 478, 32, 135), (2, 5)),
    ("product_empty", 2, (775, 512, 75, 70), (2, 4)),
    ("train_title_identity", 5, (108, 91, 105, 27), (2, 3, 4, 5)),
    ("material_title_identity", 5, (1118, 210, 94, 28), (2, 3, 4, 5)),
    ("product_title_identity", 5, (1118, 542, 94, 28), (2, 3, 4, 5)),
    ("tp_connection_anchor", 5, (520, 466, 40, 159), (2, 3, 4, 5)),
)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def box(rect):
    x, y, width, height = rect
    return x, y, x+width, y+height


def search_region(rect):
    x, y, width, height = rect
    return [max(0, x-6), max(0, y-6), min(1280, x+width+6)-max(0, x-6),
            min(720, y+height+6)-max(0, y-6)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, action="append", help="Four legacy 1282x752 originals, in order")
    parser.add_argument("--stable-source", type=Path, default=SOURCES / "warehouse_stable.png")
    parser.add_argument("--stable-client-crop", type=int, nargs=4, default=STABLE_CROP, metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"))
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
    sys.path.insert(0, str(ROOT))
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw

    paths = args.source or [SOURCES / f"{name}.png" for name in SOURCE_NAMES[:4]]
    if len(paths) != 4:
        parser.error("Exactly four legacy --source captures are required")
    clients, source_rows = [], []
    for index, path in enumerate([*paths, args.stable_source], 1):
        path = path.resolve()
        with Image.open(path) as opened:
            original = opened.convert("RGB")
        if index <= 4 and original.size != (1282, 752):
            parser.error(f"Legacy capture {index} must be 1282x752")
        crop = CLIENT_CROP if index <= 4 else tuple(args.stable_client_crop)
        left, top, right, bottom = crop
        if (left < 0 or top < 0 or right > original.width or bottom > original.height
                or right-left != 1280 or bottom-top != 720):
            parser.error(f"Invalid explicit 1280x720 client crop for capture {index}: {crop}")
        staged = scope / f"capture_{index}_original.png"
        original.save(staged)
        client = original.crop(crop)
        client_path = scope / f"capture_{index}_client.png"
        client.save(client_path)
        clients.append(client)
        source_rows.append({
            "id": f"capture_{index}", "source_image": path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.name,
            "original_name": ATTACHMENTS[index-1], "original_sha256": digest(path),
            "original_size": list(original.size), "client_crop_xyxy": list(crop), "client_size": [1280, 720],
            "client_sha256": digest(client_path), "state": FRAME_STATES[index-1],
            "staged_original": staged.relative_to(ROOT).as_posix(), "staged_client": client_path.relative_to(ROOT).as_posix(),
        })

    OUTPUT.mkdir(parents=True, exist_ok=True)

    def asset(name, index, rect):
        path = OUTPUT / f"{name}.png"
        clients[index-1].crop(box(rect)).save(path)
        return {"image": path.relative_to(PLAN).as_posix(), "sha256": digest(path),
                "source_capture": f"capture_{index}", "source_bbox_xywh": list(rect)}

    templates = {}
    for name, index, rect, positives in DEFINITIONS:
        structure = name.endswith("_identity") or name == "tp_connection_anchor"
        pixel_name = "tp_connection_anchor_unselected" if name == "tp_connection_anchor" else name
        spec = {**asset(pixel_name, index, rect), "bbox": list(rect), "search_region": search_region(rect),
                "preprocess": "none", "use_grayscale": structure,
                "method": "TM_CCOEFF_NORMED" if structure else "TM_SQDIFF_NORMED",
                "threshold": .92 if structure else .90, "color_mae_limit": None if structure else 18.0,
                "kind": "supplied_screenshot_crop", "status": "offline_supplied_capture_calibration_no_live_execution"}
        if name in {"tp_connection_selected", "tp_connection_unselected"}:
            spec.update(anchor_offset_xy=[4, 12], click=[540, 546],
                        note="Inner color-state crop; search exactly relative to the observed full button anchor.")
        if name == "tp_connection_unselected":
            spec["variants"] = [asset("tp_connection_unselected_legacy", 2, rect)]
        elif name == "tp_connection_anchor":
            spec["variants"] = [asset("tp_connection_anchor_selected", 3, rect),
                                asset("tp_connection_anchor_legacy", 2, rect)]
            spec["note"] = "Grayscale button structure locates both supported states; it does not authorize a connection state."
        elif name.endswith(("_active", "_inactive")):
            spec["note"] = "Legacy color diagnostic only; page and target connection no longer depend on panel brightness."
        templates[name] = spec

    catalog = {"schema_version": 2, "reference_client": [1280, 720], "kind": "supplied_screenshot_crops",
               "live_calibrated": False,
               "calibration": "Five supplied captures, including the user's stable warehouse state; offline replay only.",
               "color_contract": "Grayscale CCOEFF for page/button structure; RGB/BGR-equivalent SQDIFF and MAE for inner connection state.",
               "shared_templates": {"back": "GLOBAL_BACK"}, "sources": source_rows, "templates": templates,
               "excluded": ["maximum_mode_detection", "inventory_digit_recognition", "goods_icon_catalog", "transfer_actions"],
               "limits": [
                   "All searches are bounded at the 1280x720 client size; full-window captures require explicit cropping, never scaling.",
                   "Source-image self matches are provenance checks, not independent accuracy evidence.",
                   "Warehouse page identity and target connection are independent. Unselected does not identify another connected pair.",
                   "Only an exclusive inner color-state match on a recognized button authorizes selected/unselected; all other cases stay unknown.",
                   "Legacy title color templates are diagnostic only. No new live input, successful withdrawal or server persistence was verified."]}
    CATALOG.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Replay the actual production matcher, including anchor-relative state ROIs.
    from plans.resonance_pc.src.actions._prison_warehouse_ui import PrisonWarehouseUi
    ui = PrisonWarehouseUi()
    expected = [("overview", "unknown"), ("warehouse", "not_train_product"),
                ("warehouse", "train_product"), ("warehouse", "train_product"),
                ("warehouse", "not_train_product")]
    rows = []
    for index, (client, (page, connection)) in enumerate(zip(clients, expected), 1):
        view = ui.probe(cv2.cvtColor(np.asarray(client), cv2.COLOR_RGB2BGR), read_list=False)
        rows.append({"capture": f"capture_{index}", "expected_page": page, "expected_connection": connection,
                     "page": view["page"], "connection": view["connection"], "tp_connection": view["tp_connection"],
                     "expectation_met": view["page"] == page and view["connection"] == connection,
                     "controls": view["controls"]})
    report = {"sources": source_rows, "results": rows, "expectations_met": sum(row["expectation_met"] for row in rows),
              "live_game_verified": False, "limits": catalog["limits"]}
    report_path = scope / "controls_matching_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    atlas = Image.new("RGB", (940, 210*((len(templates)+1)//2)), (25, 30, 39))
    draw = ImageDraw.Draw(atlas)
    for ordinal, (name, spec) in enumerate(templates.items()):
        x, y = 20+(ordinal % 2)*470, 20+(ordinal//2)*210
        draw.text((x, y), name, fill=(235, 240, 248))
        with Image.open(PLAN / spec["image"]) as pixels:
            atlas.paste(pixels, (x, y+25))
    atlas.save(scope / "controls_atlas.png")
    print(json.dumps({"catalog": str(CATALOG), "report": str(report_path), "templates": len(templates),
                      "replay_expectations_met": report["expectations_met"], "supplied_captures": len(rows)}, ensure_ascii=False))
    if not all(row["expectation_met"] for row in rows):
        raise SystemExit("Supplied-capture replay mismatch; inspect controls_matching_report.json")


if __name__ == "__main__":
    main()
