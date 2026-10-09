"""Locate native card frames in real screenshots; no fixed grid coordinates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from build_trade_goods_investment_templates import crop
from review_trade_goods_investment_templates import match, prepare


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "plans/resonance_pc/templates/trade_goods_investment"
FIXTURES = ROOT / "tests/fixtures/trade_goods_investment"


def read_pixels(filename):
    return np.array(Image.open(TEMPLATES / filename))


def match_masked(source, template, mask):
    result = cv2.matchTemplate(source, template, cv2.TM_CCOEFF_NORMED, mask=mask)
    return np.nan_to_num(result, nan=-1., posinf=-1., neginf=-1.)


def find_headers(source, spec):
    vx, vy, vw, vh = spec["viewport"]
    region = cv2.GaussianBlur(crop(source, spec["viewport"]), (3, 3), 0)
    candidates = []
    for variant, filename in enumerate(spec["header_variants"]):
        response = match_masked(region, read_pixels(filename), read_pixels(spec["header_mask"]))
        local_maxima = response == cv2.dilate(response, np.ones((9, 9), dtype=np.uint8))
        ys, xs = np.where(local_maxima & (response >= spec["header_threshold"]))
        for x, y in zip(xs, ys):
            candidates.append({"x": int(vx + x - spec["header_box"][0]),
                               "y": int(vy + y - spec["header_box"][1]),
                               "header_score": float(response[y, x]), "variant": variant})
    unique = []
    for item in sorted(candidates, key=lambda row: row["header_score"], reverse=True):
        if not any(abs(item["x"] - old["x"]) < 15 and abs(item["y"] - old["y"]) < 15 for old in unique):
            unique.append(item)
    return sorted(unique, key=lambda row: (row["y"] // 20, row["x"]))


def detect(source, manifest):
    spec = manifest["cards"]
    assets = {entry["name"]: entry for entry in manifest["templates"]}
    card_width, card_height = spec["size"]
    vx, vy, vw, vh = spec["viewport"]
    success_meta = assets["success"]
    success = match(prepare(crop(source, success_meta["roi"]), success_meta["preprocess"]), read_pixels(success_meta["file"]))["score"] >= success_meta["threshold"]
    result = []
    for item in find_headers(source, spec):
        width, height = card_width, card_height
        x, y = item["x"], item["y"]
        if x < vx - 2 or x + width > vx + vw + 2:
            continue
        partial = y < vy or y + height > vy + vh
        occluded = success and y < 393 and y + height > 327
        frame_score = None
        if not partial and not occluded:
            # Allow a few raster-alignment pixels around a header-derived rectangle.
            search = cv2.GaussianBlur(crop(source, [x - 3, y - 3, width + 6, height + 6]), (3, 3), 0)
            rankings = []
            for variant_index, meta in enumerate(spec["frame_variants"]):
                response = match_masked(search, read_pixels(meta["file"]), read_pixels(meta["mask"]))
                _, score, _, point = cv2.minMaxLoc(response)
                rankings.append((score, point, variant_index))
            frame_score, point, variant_index = max(rankings)
            if frame_score < spec["frame_threshold"]:
                continue
            x, y = x - 3 + point[0], y - 3 + point[1]
            width, height = spec["frame_variants"][variant_index]["size"]
        selected_meta = assets["selected_corner"]
        selected = match(prepare(crop(source, [x - 3, y + 3, 28, 32]), selected_meta["preprocess"]), read_pixels(selected_meta["file"]))["score"] >= selected_meta["threshold"]
        locked = None
        lock_score = None
        if not partial and not occluded:
            lock_meta = assets["lock"]
            lock_score = match(prepare(crop(source, [x + 37, y + 38, 42, 50]), lock_meta["preprocess"]), read_pixels(lock_meta["file"]))["score"]
            locked = lock_score >= lock_meta["threshold"]
        result.append({**item, "rect": [int(x), int(y), width, height], "frame_score": frame_score,
                       "partial": partial, "occluded": occluded, "selected": selected,
                       "locked": locked, "lock_score": lock_score,
                       "actionable": not (partial or occluded) and not locked})
    result.sort(key=lambda row: (round(row["rect"][1] / 20), row["rect"][0]))
    return result


def annotate(source, cards, sid, output):
    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 15)
    small = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 12)
    image = Image.new("RGB", (1280, 800), "#172126")
    image.paste(Image.fromarray(source), (0, 80))
    draw = ImageDraw.Draw(image)
    complete = sum(not row["partial"] for row in cards)
    partial = sum(row["partial"] for row in cards)
    draw.text((15, 7), f"{sid}: \u901a\u7528\u539f\u751f\u5361\u7247\u6a21\u677f\u5b9a\u4f4d | \u5b8c\u6574 {complete} | \u534a\u9732 {partial}", font=font, fill="white")
    draw.text((15, 34), "\u7eff=\u5df2\u89e3\u9501  \u9ec4=\u5f53\u524d\u9009\u4e2d  \u7ea2=\u672a\u89e3\u9501  \u6a59=\u4e0d\u5b8c\u6574  \u7070=\u88ab\u63d0\u793a\u6761\u906e\u6321 | \u4e0d\u4f7f\u7528\u56fa\u5b9a\u683c\u5b50\u5750\u6807", font=font, fill="#c3cbd1")
    for index, item in enumerate(cards, 1):
        x, y, width, height = item["rect"]
        if item["partial"]:
            color, state = "#ff9d42", "\u534a\u9732"
        elif item["occluded"]:
            color, state = "#abb9c5", "\u906e\u6321"
        elif item["selected"]:
            color, state = "#ffe05a", "\u9009\u4e2d"
        elif item["locked"]:
            color, state = "#ff6565", "\u9501\u5b9a"
        else:
            color, state = "#44e293", "\u89e3\u9501"
        bottom = min(y + height, 589) if item["partial"] else y + height
        draw.rectangle((x, y + 80, x + width, bottom + 80), outline=color, width=2)
        text = f"{index} {state} H={item['header_score']:.2f}"
        box = draw.textbbox((0, 0), text, font=small)
        draw.rectangle((x, y + 83, x + box[2] + 5, y + 101), fill="#172126")
        draw.text((x + 2, y + 83), text, font=small, fill=color)
    image.save(output / f"{sid}_cards.png")
    image.crop((35, 200, 455, 710)).resize((840, 1020)).save(output / f"{sid}_cards_detail.png")


def evaluate(sid, cards):
    # Independently visually labeled bounds; used only after automatic detection.
    expected_bounds = [[61, 143, 115, 129], [188, 143, 115, 129], [315, 143, 115, 129],
                       [61, 275, 115, 129], [188, 275, 115, 129], [315, 275, 115, 129],
                       [61, 407, 115, 129], [188, 407, 115, 129], [315, 407, 115, 129]]
    checks = []
    def check(label, expected, actual):
        checks.append({"label": label, "expected": expected, "actual": actual, "passed": expected == actual})
    if sid == "shop":
        check("non_investment_has_no_cards", 0, len(cards))
        return checks
    complete = [row for row in cards if not row["partial"]]
    check("complete_card_count", 9, len(complete))
    check("partial_card_count", 1, sum(row["partial"] for row in cards))
    check("selected_card", [2] if sid == "shell" else [1], [i + 1 for i, row in enumerate(cards) if row["selected"]])
    locked = [2, 3, 7, 8, 9] if sid == "success" else list(range(3 if sid in ("shell", "double_digit") else 2, 10))
    check("locked_cards", locked, [i + 1 for i, row in enumerate(cards) if row["locked"]])
    check("occluded_cards", [4, 5, 6] if sid == "success" else [], [i + 1 for i, row in enumerate(cards) if row["occluded"]])
    for index, (detected, expected) in enumerate(zip(complete, expected_bounds), 1):
        x, y, width, height = detected["rect"]
        ex, ey, ew, eh = expected
        intersection = max(0, min(x + width, ex + ew) - max(x, ex)) * max(0, min(y + height, ey + eh) - max(y, ey))
        iou = intersection / (width * height + ew * eh - intersection)
        check(f"card_{index}_bounds_iou_ge_0.94", True, iou >= .94)
        checks[-1]["iou"] = iou
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".pytest_tmp/trade_goods_investment_cards")
    parser.add_argument("--sample", choices=("initial", "preview_10", "success", "shell", "double_digit", "shop"), action="append")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((TEMPLATES / "manifest.json").read_text(encoding="utf-8"))
    results = []
    for sid in args.sample or ("initial", "shell", "double_digit", "success", "shop"):
        source = np.array(Image.open(FIXTURES / f"{sid}.png").convert("RGB"))
        cards = detect(source, manifest)
        annotate(source, cards, sid, args.output)
        results.append({"sample": sid, "cards": cards, "checks": evaluate(sid, cards), "complete": sum(not row["partial"] for row in cards),
                        "partial": sum(row["partial"] for row in cards), "selected": [i + 1 for i, row in enumerate(cards) if row["selected"]],
                        "locked": [i + 1 for i, row in enumerate(cards) if row["locked"]]})
    checks = [check for row in results for check in row["checks"]]
    report = {"source": "native_game_card_frame_and_selection_layer", "fixed_grid_coordinates": False,
              "scope": "user-supplied real still screenshots", "results": results,
              "total_checks": len(checks), "passed_checks": sum(check["passed"] for check in checks),
              "failed_checks": [check for check in checks if not check["passed"]]}
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"samples": [{k: v for k, v in row.items() if k not in ("cards", "checks")} for row in results],
                      "passed": report["passed_checks"], "total": report["total_checks"], "failed": report["failed_checks"]}))
    if report["failed_checks"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
